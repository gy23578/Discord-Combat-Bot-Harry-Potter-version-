"""Deadline races, XP penalties, activity eligibility, and shared duel timers."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import bot
import database
from test_bot import context, user


class InactivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DATABASE_FILE", str(Path(self.directory.name) / "players.db"))
        self.db_patch.start()
        database.init_database()
        for session_id in list(bot.duel_inactivity_timers):
            bot.stop_duel_inactivity_timer(session_id)
        for state in (bot.players, bot.duel_requests, bot.active_duels, bot.duel_sessions,
                      bot.pending_attacks, bot.active_casts, bot.status_effects,
                      bot.offensive_cooldowns, bot.teaching_requests, bot.active_learning_trials):
            state.clear()
        self.clock = [1000.0]
        self.clock_patch = patch.object(bot.time, "time", side_effect=lambda: self.clock[0])
        self.clock_patch.start()
        self.duel_clock_patch = patch.object(bot, "duel_clock", side_effect=lambda: self.clock[0])
        self.duel_clock_patch.start()
        self.fetch_patch = patch.object(bot.bot, "fetch_user", side_effect=self.fetch)
        self.fetch_patch.start()
        self.channel = context()
        self.channel_patch = patch.object(bot.bot, "get_channel", return_value=self.channel)
        self.channel_patch.start()

    async def fetch(self, uid):
        return user(uid)

    def tearDown(self):
        for session_id in list(bot.duel_inactivity_timers):
            bot.stop_duel_inactivity_timer(session_id)
        self.channel_patch.stop()
        self.fetch_patch.stop()
        self.clock_patch.stop()
        self.duel_clock_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def player(self, uid):
        player = bot.get_player(user(uid))
        player.update(ready=True, house="gryffindor", xp=400, level=5, talent_points=7,
                      hp=100, max_hp=106)
        player["stats"] = bot.HOUSE_STATS["gryffindor"].copy()
        bot.persist_player(player)
        return player

    async def duel(self, a=1, b=2):
        self.player(a)
        self.player(b)
        await bot.duel.callback(context(a), user(b))
        await bot.accept.callback(context(b))
        session = bot.duel_sessions[a]
        self.assertIs(session, bot.duel_sessions[b])
        return session

    def pending(self, defender, spell="confringo"):
        attack = {"session_id": bot.duel_sessions[defender]["id"],
                  "attacker_id": bot.active_duels[defender], "spell": spell,
                  "power": 1, "accuracy": 1, "damage": 20, "cooldown": 6, "duration": 2}
        bot.pending_attacks[defender] = attack
        return attack

    async def test_timeout_at_exact_five_minutes_penalizes_and_cleans_without_results(self):
        session = await self.duel()
        bot.players[1].update(xp=105, level=2, hp=12)
        bot.players[2].update(xp=5, level=1, hp=20)
        self.pending(1)
        self.pending(2)
        bot.active_casts.update({1, 2})
        bot.status_effects.update({1: {"stunned_until": 9999, "unarmed_until": 9999},
                                   2: {"slowed_until": 9999, "speed_penalty": 5}})
        bot.offensive_cooldowns.update({1: 9999, 2: 9999})
        before = copy.deepcopy(bot.players)
        self.clock[0] = 1299.999
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.clock[0] = 1300
        self.assertTrue(await bot.expire_inactive_duel(session, self.channel))
        for uid, xp, level in ((1, 95, 1), (2, 0, 1)):
            saved = database.load_player(uid)
            self.assertEqual(saved["xp"], xp)
            self.assertEqual(saved["level"], level)
            self.assertEqual(saved["talent_points"], 7)
            self.assertEqual(saved["hp"], saved["max_hp"])
            for field in ("duel_wins", "duel_losses", "duels_completed", "duels_since_avada",
                          "spell_xp", "spell_levels", "spell_hits", "combat_stats"):
                self.assertEqual(saved[field], before[uid][field], field)
        for state in (bot.active_duels, bot.duel_sessions, bot.pending_attacks, bot.active_casts,
                      bot.offensive_cooldowns, bot.status_effects, bot.duel_inactivity_timers):
            self.assertFalse(state)
        self.channel.send.assert_awaited_once()
        message = self.channel.send.await_args.args[0]
        self.assertIn("DUEL ENDED DUE TO INACTIVITY", message)
        self.assertIn("Wizard 1: **-10 XP**", message)
        self.assertIn("Wizard 2: **-10 XP**", message)
        self.assertIsNone(database.load_runtime())
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.assertEqual(database.load_player(1)["xp"], 95)

    async def test_either_player_refreshes_one_shared_timer_and_timestamp(self):
        session = await self.duel()
        old_handle = bot.duel_inactivity_timers[session["id"]]
        self.clock[0] = 1100
        self.assertTrue(bot.note_duel_activity(1, session["id"], 10))
        self.assertTrue(old_handle.cancelled())
        first_handle = bot.duel_inactivity_timers[session["id"]]
        self.clock[0] = 1200
        self.assertTrue(bot.note_duel_activity(2, session["id"], 10))
        self.assertTrue(first_handle.cancelled())
        self.assertEqual(session["last_activity"], 1200)
        self.assertEqual(len(bot.duel_inactivity_timers), 1)
        self.assertIsNone(database.load_runtime())
        self.clock[0] = 1300
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.clock[0] = 1500
        self.assertTrue(await bot.expire_inactive_duel(session, self.channel))

    async def test_offensive_cast_and_failed_curse_roll_count_as_activity(self):
        session = await self.duel()
        self.clock[0] = 1100

        async def react(delay):
            attack = bot.pending_attacks.pop(2)
            bot.finish_attack(attack)

        with patch.object(bot.asyncio, "sleep", side_effect=react):
            await bot.confringo.callback(context(1))
        self.assertEqual(session["last_activity"], 1100)
        bot.offensive_cooldowns.clear()
        bot.players[1]["learned_spells"].add("endoloris")
        bot.players[1]["spell_levels"]["endoloris"] = 1
        self.clock[0] = 1200
        with patch.object(bot.random, "randint", return_value=1):
            await bot.endoloris.callback(context(1))
        self.assertEqual(session["last_activity"], 1200)
        self.assertNotIn(2, bot.pending_attacks)

    async def test_avada_miss_counts_as_activity(self):
        session = await self.duel()
        bot.players[1]["learned_spells"].add("avadakedavra")
        bot.players[1]["spell_levels"]["avadakedavra"] = 1
        self.clock[0] = 1100
        with patch.object(bot.random, "randint", return_value=1):
            await bot.avadakedavra.callback(context(1))
        self.assertEqual(session["last_activity"], 1100)

    async def test_valid_defensive_reactions_count_even_when_roll_fails(self):
        for command in (bot.protego, bot.dodge, bot.expelliarmus):
            for success in (True, False):
                bot.active_duels.clear()
                bot.duel_sessions.clear()
                bot.pending_attacks.clear()
                bot.status_effects.clear()
                bot.offensive_cooldowns.clear()
                self.clock[0] = 1000
                session = await self.duel()
                self.pending(2)
                self.clock[0] = 1100
                with patch.object(bot, "calculate_protego_power", return_value=99 if success else 0), patch.object(bot, "calculate_dodge_power", return_value=99 if success else 0), patch.object(bot, "calculate_spell_power", return_value=99 if success else 0):
                    await command.callback(context(2))
                self.assertEqual(session["last_activity"], 1100, command.name)

    async def test_unrelated_commands_invalid_actions_and_outsiders_do_not_refresh(self):
        session = await self.duel()
        self.clock[0] = 1100
        await bot.profile.callback(context(1))
        await bot.test.callback(context(1))
        await bot.protego.callback(context(1))
        await bot.dodge.callback(context(1))
        await bot.impero.callback(context(1))  # Not learned.
        self.assertFalse(bot.note_duel_activity(99, session["id"], 10))
        self.assertFalse(bot.note_duel_activity(1, "another-session", 10))
        self.assertFalse(bot.note_duel_activity(1, session["id"], 99))
        bot.start_cooldown(1, 100)
        await bot.confringo.callback(context(1))
        self.pending(2)
        bot.status_effects[2] = {"stunned_until": 1200}
        await bot.protego.callback(context(2))
        self.assertEqual(session["last_activity"], 1000)

    async def test_delayed_damage_and_forced_casts_do_not_refresh(self):
        session = await self.duel()
        self.clock[0] = 1100
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.apply_burn(context(1), user(2), session["id"])
        self.assertEqual(session["last_activity"], 1000)
        bot.players[2]["learned_spells"].add("confringo")
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.queue_attack(context(1), user(2), user(2), "confringo", 1, 1,
                                   damage=1, forced=True, session_id=session["id"])
        self.assertEqual(session["last_activity"], 1000)

    async def test_activity_just_before_deadline_wins_and_stale_callback_cannot_expire(self):
        session = await self.duel()
        self.clock[0] = 1299.999
        self.assertTrue(bot.note_duel_activity(1, session["id"], 10))
        self.clock[0] = 1300
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.assertEqual(bot.players[1]["xp"], 400)
        self.assertIn(1, bot.active_duels)

    async def test_action_at_deadline_is_rejected_and_timeout_penalizes_once(self):
        session = await self.duel()
        self.clock[0] = 1300
        ctx = context(1)
        await bot.launch_attack(ctx, user(2), "confringo", 1, 1, damage=20)
        self.assertNotIn(2, bot.pending_attacks)
        self.assertEqual(session["last_activity"], 1000)
        self.assertTrue(await bot.expire_inactive_duel(session, self.channel))
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.assertFalse(bot.note_duel_activity(1, session["id"], 10))
        self.assertEqual(bot.players[1]["xp"], 390)

    async def test_normal_duel_end_stops_timer_and_never_gets_penalty_later(self):
        session = await self.duel()
        bot.players[2]["hp"] = 0
        self.assertTrue(await bot.check_duel_end(context(1), user(2), session["id"]))
        self.assertNotIn(session["id"], bot.duel_inactivity_timers)
        self.clock[0] = 1400
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.assertEqual(bot.players[1]["xp"], 450)
        self.assertEqual(bot.players[2]["xp"], 420)

    async def test_multiple_duels_keep_independent_deadlines(self):
        first = await self.duel(1, 2)
        second = await self.duel(3, 4)
        self.assertEqual(len(bot.duel_inactivity_timers), 2)
        self.clock[0] = 1200
        self.assertTrue(bot.note_duel_activity(3, second["id"], 10))
        self.clock[0] = 1300
        self.assertTrue(await bot.expire_inactive_duel(first, self.channel))
        self.assertFalse(await bot.expire_inactive_duel(second, self.channel))
        self.assertEqual(bot.active_duels, {3: 4, 4: 3})
        self.assertEqual(bot.players[3]["xp"], 400)
        self.assertEqual(len(bot.duel_inactivity_timers), 1)

    async def test_expired_duel_invalidates_old_attacks_bleed_burn_and_curse(self):
        session = await self.duel()
        attack = self.pending(2)
        self.clock[0] = 1300
        await bot.expire_inactive_duel(session, self.channel)
        self.clock[0] = 1400
        await self.duel()
        hp = bot.players[2]["hp"]
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.apply_attack(context(1), user(2), attack)
            await bot.apply_burn(context(1), user(2), session["id"])
            await bot.apply_sectumsempra_bleed(context(1), user(2), user(1), session["id"])
            await bot.apply_endoloris(context(1), user(2), session["id"])
        self.assertEqual(bot.players[2]["hp"], hp)
        self.assertEqual(bot.players[1]["combat_stats"]["successful_attacks"], 0)

    async def test_old_announcement_cannot_award_xp_after_expiration(self):
        session = await self.duel()
        ctx = context(1)

        async def expire_during_send(message):
            self.clock[0] = 1300
            await bot.expire_inactive_duel(session, self.channel)

        ctx.send.side_effect = expire_during_send
        await bot.apply_attack(ctx, user(2), {
            "session_id": session["id"], "attacker_id": 1, "spell": "confringo", "damage": 20,
        })
        self.assertEqual(bot.players[1]["xp"], 390)
        self.assertEqual(bot.players[1]["spell_xp"]["confringo"], 0)
        self.assertEqual(bot.players[2]["hp"], bot.players[2]["max_hp"])

    async def test_training_has_no_timer_or_inactivity_penalty(self):
        self.player(1)
        self.player(2)
        bot.players[1]["learned_spells"].add("glacius")
        bot.players[1]["spell_levels"]["glacius"] = 3
        await bot.teach.callback(context(1), user(2), spell_name="glacius")
        await bot.accept.callback(context(2))
        session = bot.training_session(1)
        self.assertIsNotNone(session)
        self.assertFalse(bot.duel_inactivity_timers)
        self.clock[0] = 10000
        self.assertFalse(await bot.expire_inactive_duel(session, self.channel))
        self.assertTrue(bot.note_duel_activity(1, session["id"], 10))
        self.assertFalse(bot.duel_inactivity_timers)
        self.assertIsNotNone(bot.training_session(2))
        self.assertEqual(bot.players[1]["xp"], 400)

    async def test_scheduled_callback_runs_once_and_cleans_its_handle(self):
        session = await self.duel()
        self.clock[0] = 1300
        bot.schedule_duel_inactivity(session)  # Zero-delay deadline, no real five-minute wait.
        for _ in range(10):
            await asyncio.sleep(0)
            if not bot.active_duels:
                break
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.duel_inactivity_timers)
        self.channel.send.assert_awaited_once()

    async def test_restart_discards_overdue_duel_without_penalty(self):
        await self.duel()
        bot.players[1].update(xp=105, level=2)
        bot.persist_player(bot.players[1])
        database.save_runtime({"active_duels": {1: 2, 2: 1}})
        bot.runtime_restored = False
        self.clock[0] = 1300
        await bot.on_ready()
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.duel_inactivity_timers)
        self.assertEqual(database.load_player(1)["xp"], 105)
        await bot.on_ready()
        self.assertEqual(database.load_player(1)["xp"], 105)
        self.channel.send.assert_not_awaited()
        bot.runtime_restored = False

    async def test_gateway_reconnect_preserves_live_timer(self):
        session = await self.duel()
        bot.runtime_restored = True
        handle = bot.duel_inactivity_timers[session["id"]]
        await bot.on_ready()
        self.assertIs(bot.duel_inactivity_timers[session["id"]], handle)
        self.assertEqual(bot.duel_sessions[1]["id"], session["id"])
        bot.runtime_restored = False

    async def test_competing_timeout_callbacks_only_penalize_once(self):
        session = await self.duel()
        self.clock[0] = 1300
        results = await asyncio.gather(
            bot.expire_inactive_duel(session, self.channel),
            bot.expire_inactive_duel(session, self.channel),
        )
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(bot.players[1]["xp"], 390)
        self.assertEqual(bot.players[2]["xp"], 390)
        self.channel.send.assert_awaited_once()

    async def test_legacy_snapshot_is_expired(self):
        await self.duel()
        database.save_runtime({"active_duels": {1: 2, 2: 1}, "status_effects": {1: {"stunned_until": 9999999999}}})
        bot.runtime_restored = False
        await bot.on_ready()
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.status_effects)
        self.assertFalse(bot.duel_inactivity_timers)
        self.assertIsNone(database.load_runtime())
        self.assertEqual(bot.players[1]["xp"], 400)
        bot.runtime_restored = False
