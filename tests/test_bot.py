import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
from quiz_questions import QUIZ_QUESTIONS
from spells import QUIZ_RULES, SPELLS
from combat import choose_forced_spell, roll_offensive_effect, FORCEABLE_OFFENSIVE_SPELLS


def user(uid):
    return SimpleNamespace(id=uid, display_name=f"Wizard {uid}")


def context(uid=1, channel=10):
    return SimpleNamespace(author=user(uid), channel=SimpleNamespace(id=channel), send=AsyncMock())


class GameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(database, "DATABASE_FILE", str(Path(self.directory.name) / "players.db"))
        self.db_patch.start()
        database.init_database()
        for state in (bot.players, bot.duel_requests, bot.active_duels,
                      bot.pending_attacks, bot.status_effects, bot.active_casts,
                      bot.offensive_cooldowns, bot.active_learning_trials,
                      bot.teaching_requests, bot.duel_sessions):
            state.clear()

    def tearDown(self):
        for session_id in list(bot.duel_inactivity_timers):
            bot.stop_duel_inactivity_timer(session_id)
        self.db_patch.stop()
        self.directory.cleanup()

    def ready_player(self, uid):
        player = bot.get_player(user(uid))
        player.update(house="gryffindor", ready=True, hp=100)
        player["stats"] = bot.HOUSE_STATS["gryffindor"].copy()
        return player

    def session(self):
        self.ready_player(1)
        self.ready_player(2)
        bot.active_duels.update({1: 2, 2: 1})
        session = {"id": "original", "channel_id": 10}
        bot.duel_sessions.update({1: session, 2: session})

    def test_quiz_bank_supports_every_difficulty(self):
        for q in QUIZ_QUESTIONS:
            self.assertEqual(len(q["answers"]), 4)
            self.assertIn(q["correct"], q["answers"])
        for difficulty, rules in QUIZ_RULES.items():
            questions = bot.select_quiz_questions(difficulty, rules["questions"], ["spells"])
            self.assertEqual(len(questions), rules["questions"])
            self.assertEqual(len({q["question"] for q in questions}), len(questions))

    def test_profile_roundtrip_and_read_without_write(self):
        player = self.ready_player(1)
        bot.persist_player(player)
        loaded = database.load_player(1)
        self.assertEqual(loaded["learned_spells"], player["learned_spells"])
        with patch.object(bot, "persist_player") as save:
            bot.get_player(user(1))
            save.assert_not_called()

    async def test_expired_challenge_cannot_start(self):
        bot.duel_requests[1] = {"challenger_id": 2, "expires": 0, "channel_id": 10}
        await bot.accept.callback(context())
        self.assertFalse(bot.active_duels)
        self.assertNotIn(1, bot.duel_requests)

    async def test_accept_rechecks_availability(self):
        self.session()
        bot.duel_requests[3] = {"challenger_id": 1, "expires": float("inf"), "channel_id": 10}
        await bot.accept.callback(context(3))
        self.assertEqual(bot.active_duels[1], 2)
        self.assertNotIn(3, bot.active_duels)

    async def test_old_bleed_cannot_damage_new_duel(self):
        self.session()
        bot.duel_sessions[2] = {"id": "new", "channel_id": 10}
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.apply_sectumsempra_bleed(context(), user(2), user(1), "original")
        self.assertEqual(bot.players[2]["hp"], 100)

    async def test_old_attack_cannot_damage_new_duel(self):
        self.session()
        await bot.apply_attack(context(), user(2), {"session_id": "old"})
        self.assertEqual(bot.players[2]["hp"], 100)

    async def test_all_new_offensive_effects_persist_damage(self):
        for spell in bot.MODELED_SPELLS:
            if spell in {"endoloris", "impero"}:
                continue
            self.session()
            ctx = context()
            attack = {"session_id": "original", "spell": spell, "attacker_id": 1, "damage": 10, "duration": 3}
            with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot, "spawn_combat_task") as task:
                task.side_effect = lambda session_id, coroutine: coroutine.close()
                await bot.apply_attack(ctx, user(2), attack)
            self.assertEqual(database.load_player(2)["hp"], 90, spell)
            self.assertEqual(bot.players[1]["spell_hits"][spell], 1, spell)

    async def test_duel_ending_awards_once_and_cleans_state(self):
        self.session()
        bot.players[2]["hp"] = 0
        with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))):
            self.assertTrue(await bot.check_duel_end(context(), user(2)))
            self.assertFalse(await bot.check_duel_end(context(), user(2)))
        self.assertEqual(database.load_player(1)["duel_wins"], 1)
        self.assertFalse(bot.duel_sessions)
        self.assertFalse(bot.active_duels)

    def test_restart_discards_sessions_and_trials(self):
        self.session()
        bot.active_learning_trials[3] = {"spell": "incendio", "score": 2}
        bot.offensive_cooldowns[1] = 9999999999
        bot.active_casts.add(1)
        database.save_runtime({"active_duels": {1: 2, 2: 1}})
        self.assertEqual(bot.restore_runtime(), [])
        for state in (bot.active_duels, bot.duel_sessions, bot.active_learning_trials,
                      bot.offensive_cooldowns, bot.active_casts):
            self.assertFalse(state)
        self.assertIsNone(database.load_runtime())

    async def test_accept_heals_and_saves_both_players(self):
        self.ready_player(1)["hp"] = 2
        self.ready_player(2)["hp"] = 3
        bot.duel_requests[1] = {"challenger_id": 2, "expires": float("inf"), "channel_id": 10}
        with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))):
            await bot.accept.callback(context())
        self.assertEqual(database.load_player(1)["hp"], 100)
        self.assertEqual(database.load_player(2)["hp"], 100)
        self.assertIs(bot.duel_sessions[1], bot.duel_sessions[2])

    async def test_launch_after_duel_end_does_not_create_attack(self):
        ctx = context()
        await bot.launch_attack(ctx, user(2), "confringo", 10, 10)
        self.assertFalse(bot.pending_attacks)

    def test_retired_spells_are_pruned_from_saved_profiles(self):
        player = self.ready_player(1)
        retired = {"levioso", "arrestomomentum"}
        player["learned_spells"].update(retired)
        for field in ("spell_levels", "spell_xp", "spell_hits"):
            player[field].update({spell: 8 for spell in retired})
        player["talent_points"] = 7
        bot.persist_player(player)
        bot.players.clear()
        migrated = bot.get_player(user(1))
        saved = database.load_player(1)
        for profile in (migrated, saved):
            self.assertFalse(profile["learned_spells"] & retired)
            for field in ("spell_levels", "spell_xp", "spell_hits"):
                self.assertFalse(set(profile[field]) & retired)
            self.assertEqual(profile["talent_points"], 7)
        for spell in retired:
            self.assertNotIn(spell, SPELLS)
            self.assertNotIn(spell, bot.SPELL_SPEEDS)
            self.assertIsNone(bot.bot.get_command(spell))

    def test_retired_learning_sessions_are_not_restored(self):
        bot.active_learning_trials[1] = {"spell": "levioso", "score": 0}
        bot.persist_runtime()
        bot.active_learning_trials.clear()
        bot.teaching_requests.clear()
        bot.restore_runtime()
        self.assertFalse(bot.active_learning_trials)
        self.assertFalse(bot.teaching_requests)

    def test_petrificus_requirements_and_ravenclaw_bonus(self):
        player = self.ready_player(1)
        player.update(level=4, duel_wins=3)
        player["spell_levels"]["stupefy"] = 2
        player["combat_stats"]["successful_control_spells"] = 5
        self.assertEqual(SPELLS["petrificustotalus"]["requirements"], {
            "level": 4, "spell_levels": {"stupefy": 2},
            "combat_stats": {"successful_control_spells": 5}, "duel_wins": 3,
        })
        self.assertTrue(bot.can_learn_spell(player, "petrificustotalus"))
        player["combat_stats"]["successful_control_spells"] = 4
        self.assertFalse(bot.can_learn_spell(player, "petrificustotalus"))
        player["house"] = "ravenclaw"
        self.assertTrue(bot.can_learn_spell(player, "petrificustotalus"))
        for spell in ("endoloris", "impero"):
            self.assertEqual(SPELLS[spell]["difficulty"], 4)
            self.assertIsNotNone(bot.bot.get_command(spell))
            rule = QUIZ_RULES[SPELLS[spell]["difficulty"]]
            self.assertEqual(len(bot.select_quiz_questions(4, rule["questions"], SPELLS[spell]["categories"])), 10)

    def test_forced_spell_selection_excludes_killing_and_command_curses(self):
        player = self.ready_player(2)
        player["learned_spells"].update({"avadakedavra", "impero", "endoloris"})
        player["spell_levels"].update(avadakedavra=99, impero=99, endoloris=4, confringo=3)
        self.assertEqual(choose_forced_spell(player), "endoloris")
        player["spell_levels"]["confringo"] = 4
        with patch("combat.random.choice", return_value="confringo") as choice:
            self.assertEqual(choose_forced_spell(player), "confringo")
            self.assertEqual(set(choice.call_args.args[0]), {"confringo", "endoloris"})
        player["learned_spells"] = {"avadakedavra", "protego", "impero"}
        self.assertIsNone(choose_forced_spell(player))

    def test_shared_offensive_formulas_preserve_existing_scaling(self):
        cases = {
            "confringo": (24, 36, 32, 0, 6),
            "expelliarmus": (8, 12, 12, 4, 6),
            "stupefy": (10, 15, 14, 2, 6),
            "sectumsempra": (40, 54, 48, 0, 15),
        }
        for spell, (low, high, damage, duration, cooldown) in cases.items():
            with patch("combat.random.randint", return_value=low) as roll:
                effect = roll_offensive_effect(spell, 3)
            roll.assert_called_once_with(low, high)
            self.assertEqual(effect, {"damage": damage, "duration": duration, "base_cooldown": cooldown})
        self.assertEqual(roll_offensive_effect("endoloris", 20)["damage"], 0)
        self.assertEqual(bot.SPELL_SPEEDS["endoloris"], 7)

    async def test_endoloris_hit_deals_no_immediate_damage(self):
        self.session()
        attack = {"session_id": "original", "spell": "endoloris", "attacker_id": 1, "damage": 0}
        with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot, "spawn_combat_task") as task:
            task.side_effect = lambda session_id, coroutine: coroutine.close()
            await bot.apply_attack(context(), user(2), attack)
            task.assert_called_once()
        self.assertEqual(database.load_player(2)["hp"], 100)
        self.assertEqual(bot.players[1]["spell_hits"]["endoloris"], 1)

    async def test_endoloris_ticks_exactly_at_three_six_and_nine_seconds(self):
        self.session()
        clock = [0]
        tick_times = []
        ctx = context()

        async def sleep(delay):
            clock[0] += delay

        async def send(message):
            tick_times.append(clock[0])
            self.assertEqual(database.load_player(2)["hp"], 100 - 14 * len(tick_times))
            clock[0] += 0.25  # Message latency must not move subsequent deadlines.

        ctx.send.side_effect = send
        with patch.object(bot.time, "monotonic", side_effect=lambda: clock[0]), patch.object(bot.asyncio, "sleep", side_effect=sleep):
            await bot.apply_endoloris(ctx, user(2), "original", hit_time=0)
        self.assertEqual(tick_times, [3, 6, 9])
        self.assertEqual(bot.players[2]["hp"], 58)

    async def test_each_endoloris_tick_can_end_duel_and_stops_later_ticks(self):
        for hp, ticks in ((14, 1), (28, 2), (42, 3)):
            self.session()
            bot.players[2]["hp"] = hp
            ctx = context()
            with patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep, patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))):
                await bot.apply_endoloris(ctx, user(2), "original")
            self.assertEqual(sleep.await_count, ticks)
            self.assertFalse(bot.active_duels)
            self.assertEqual(database.load_player(2)["hp"], 0)
            self.assertEqual(database.load_player(1)["duel_wins"], ticks)

    async def test_endoloris_cannot_leak_into_another_duel(self):
        self.session()
        bot.duel_sessions[2] = {"id": "new", "channel_id": 10}
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.apply_endoloris(context(), user(2), "original")
        self.assertEqual(bot.players[2]["hp"], 100)

    async def test_impero_creates_victims_normal_pending_attack(self):
        self.session()
        victim = bot.players[2]
        victim["spell_levels"]["confringo"] = 5
        victim["stats"]["speed"] = 22
        ctx = context()
        observed = []

        async def react(delay):
            self.assertEqual(delay, 10)
            observed.append(dict(bot.pending_attacks[2]))
            self.assertEqual(victim["hp"], 100)  # Impero itself does no damage.
            bot.pending_attacks.pop(2)

        with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot.asyncio, "sleep", side_effect=react), patch.object(bot, "calculate_spell_power", return_value=42) as power, patch.object(bot, "calculate_spell_accuracy", return_value=51) as accuracy, patch("combat.random.randint", return_value=20):
            await bot.apply_attack(ctx, user(2), {"session_id": "original", "spell": "impero", "attacker_id": 1})
        power.assert_called_once_with(victim, "confringo")
        accuracy.assert_called_once_with(victim, "confringo")
        self.assertEqual(observed[0]["attacker_id"], 2)
        self.assertEqual(observed[0]["spell"], "confringo")
        self.assertEqual(observed[0]["damage"], 36)
        self.assertEqual(observed[0]["power"], 42)
        self.assertEqual(observed[0]["accuracy"], 51)
        self.assertEqual(observed[0]["cooldown"], bot.calculate_cooldown(victim, 6))
        self.assertTrue(observed[0]["forced"])
        self.assertEqual(observed[0]["session_id"], "original")

    async def test_victim_can_use_each_normal_defense_against_forced_spell(self):
        for defense in (bot.protego, bot.expelliarmus, bot.dodge):
            self.session()
            ctx = context(2)

            async def react(delay):
                self.assertEqual(delay, 10)
                await defense.callback(ctx)

            with patch.object(bot.asyncio, "sleep", side_effect=react), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))), patch.object(bot, "calculate_protego_power", return_value=99), patch.object(bot, "calculate_spell_power", return_value=99), patch.object(bot, "calculate_dodge_power", return_value=99):
                await bot.queue_attack(context(), user(2), user(2), "confringo", 1, 1,
                                       damage=20, forced=True, session_id="original")
            self.assertEqual(bot.players[2]["hp"], 100, defense.name)
            self.assertNotIn(2, bot.pending_attacks)
            self.assertNotIn(2, bot.active_casts)

    async def test_forced_self_attack_preserves_stupefy_and_disarm_effects(self):
        for spell, status, duration in (("stupefy", "stunned_until", 2), ("expelliarmus", "unarmed_until", 4)):
            self.session()
            payload = roll_offensive_effect(spell, 3)
            with patch.object(bot.asyncio, "sleep", new=AsyncMock()), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))):
                await bot.queue_attack(context(), user(2), user(2), spell, 20, 20,
                                       forced=True, session_id="original", **payload)
            self.assertEqual(bot.players[2]["hp"], 100 - payload["damage"])
            self.assertGreater(bot.status_effects[2][status], bot.time.time())
            self.assertLessEqual(bot.status_effects[2][status], bot.time.time() + duration)

    async def test_forced_sectumsempra_retains_bleed_and_backlash(self):
        self.session()
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))), patch.object(bot, "spawn_combat_task") as task:
            task.side_effect = lambda session_id, coroutine: coroutine.close()
            await bot.queue_attack(context(), user(2), user(2), "sectumsempra", 20, 20,
                                   damage=30, base_cooldown=15, forced=True, session_id="original")
            task.assert_called_once()
        self.assertEqual(bot.players[2]["hp"], 70)
        with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))), patch.object(bot.random, "randint", return_value=5):
            await bot.apply_sectumsempra_backlash(context(2), {"spell": "sectumsempra", "power": 20, "attacker_id": 2}, 30)
        self.assertEqual(bot.players[2]["hp"], 65)

    async def test_forced_cast_does_not_release_another_unresolved_cast(self):
        self.session()
        bot.active_casts.add(2)
        bot.pending_attacks[1] = {"attacker_id": 2}
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(2))):
            await bot.queue_attack(context(), user(2), user(2), "confringo", 20, 20,
                                   damage=15, forced=True, session_id="original")
        self.assertIn(2, bot.active_casts)
        self.assertIn(1, bot.pending_attacks)

    async def test_blocked_curses_never_apply_their_effect(self):
        for spell in ("endoloris", "impero"):
            self.session()
            bot.pending_attacks[2] = {"session_id": "original", "attacker_id": 1,
                                      "spell": spell, "power": 1, "cooldown": 15}
            with patch.object(bot, "calculate_protego_power", return_value=99), patch.object(bot, "apply_attack", new=AsyncMock()) as apply, patch.object(bot, "spawn_combat_task") as task:
                await bot.protego.callback(context(2))
                apply.assert_not_awaited()
                task.assert_not_called()
            self.assertNotIn(2, bot.pending_attacks)
            self.assertEqual(bot.players[2]["hp"], 100)

    async def test_impero_payloads_use_victims_stats_for_every_eligible_spell(self):
        for selected in sorted(FORCEABLE_OFFENSIVE_SPELLS):
            self.session()
            victim = bot.players[2]
            victim["learned_spells"].add(selected)
            for spell in victim["spell_levels"]:
                victim["spell_levels"][spell] = 1
            victim["spell_levels"][selected] = 4
            victim["stats"].update(magic_power=31, speed=23)
            victim["level"] = 3
            bot.status_effects.clear()
            payload = []

            async def react(delay):
                self.assertEqual(delay, 10)
                payload.append(bot.pending_attacks.pop(2))

            with patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot.asyncio, "sleep", side_effect=react), patch("combat.random.randint", side_effect=lambda low, high: 6 if (low, high) == (1, 6) else low):
                expected = roll_offensive_effect(selected, 4)
                await bot.apply_attack(context(), user(2), {"session_id": "original", "spell": "impero", "attacker_id": 1})
            self.assertEqual(payload[0]["spell"], selected)
            self.assertEqual(payload[0]["power"], 1 + 31 + 3 * 2 + 4 * 5)
            self.assertEqual(payload[0]["accuracy"], 1 + 23 + bot.SPELL_SPEEDS[selected] + 4 * 3)
            self.assertEqual(payload[0]["damage"], expected["damage"])
            self.assertEqual(payload[0]["duration"], expected["duration"])
            self.assertEqual(payload[0]["cooldown"], bot.calculate_cooldown(victim, expected["base_cooldown"]))

    async def test_fatal_forced_self_damage_awards_opponent(self):
        self.session()
        bot.players[2]["hp"] = 10
        async def fetch(uid):
            return user(uid)
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()), patch.object(bot.bot, "fetch_user", side_effect=fetch):
            await bot.queue_attack(context(), user(2), user(2), "confringo", 20, 20,
                                   damage=15, forced=True, session_id="original")
        self.assertEqual(database.load_player(1)["duel_wins"], 1)
        self.assertEqual(database.load_player(2)["duel_losses"], 1)
        self.assertFalse(bot.active_duels)

    def test_endoloris_rename_preserves_saved_progression_and_learning(self):
        player = self.ready_player(1)
        player["learned_spells"].add("doloris")
        for field, value in (("spell_levels", 3), ("spell_xp", 250), ("spell_hits", 7)):
            player[field]["doloris"] = value
        bot.persist_player(player)
        bot.players.clear()
        renamed = bot.get_player(user(1))
        self.assertIn("endoloris", renamed["learned_spells"])
        self.assertNotIn("doloris", renamed["learned_spells"])
        self.assertEqual(renamed["spell_levels"]["endoloris"], 3)
        self.assertEqual(renamed["spell_xp"]["endoloris"], 250)
        self.assertEqual(renamed["spell_hits"]["endoloris"], 7)
        bot.active_learning_trials[2] = {"spell": "doloris", "display_name": "Doloris", "score": 2}
        bot.persist_runtime()
        bot.active_learning_trials.clear()
        bot.teaching_requests.clear()
        bot.restore_runtime()
        self.assertFalse(bot.active_learning_trials)
        self.assertIsNone(bot.bot.get_command("doloris"))
        self.assertIsNotNone(bot.bot.get_command("endoloris"))

    def test_impero_rename_preserves_saved_progression_and_learning(self):
        player = self.ready_player(1)
        player["learned_spells"].add("imperio")
        for field, value in (("spell_levels", 3), ("spell_xp", 250), ("spell_hits", 7)):
            player[field]["imperio"] = value
        bot.persist_player(player)
        bot.players.clear()
        renamed = bot.get_player(user(1))
        self.assertIn("impero", renamed["learned_spells"])
        self.assertNotIn("imperio", renamed["learned_spells"])
        self.assertEqual(renamed["spell_levels"]["impero"], 3)
        self.assertEqual(renamed["spell_xp"]["impero"], 250)
        self.assertEqual(renamed["spell_hits"]["impero"], 7)
        bot.active_learning_trials[2] = {"spell": "imperio", "display_name": "Imperio", "score": 2}
        bot.persist_runtime()
        bot.active_learning_trials.clear()
        bot.teaching_requests.clear()
        bot.restore_runtime()
        self.assertFalse(bot.active_learning_trials)
        self.assertIsNone(bot.bot.get_command("imperio"))
        self.assertIsNotNone(bot.bot.get_command("impero"))
        self.assertEqual(SPELLS["impero"]["display_name"], "Impero")
        self.assertEqual(SPELLS["endoloris"]["display_name"], "Endoloris")

    async def test_curse_d6_thresholds_for_all_six_faces(self):
        for spell, minimum in (("endoloris", 3), ("impero", 4)):
            for face in range(1, 7):
                self.session()
                bot.offensive_cooldowns.clear()
                bot.active_casts.clear()
                bot.pending_attacks.clear()
                bot.players[1]["learned_spells"].add(spell)
                bot.players[1]["spell_levels"][spell] = 1
                xp_before = bot.players[1]["xp"]
                ctx = context()

                async def react(delay):
                    self.assertEqual(delay, 10)
                    self.assertEqual(bot.pending_attacks[2]["spell"], spell)
                    attack = bot.pending_attacks.pop(2)
                    bot.finish_attack(attack)

                with patch.object(bot.random, "randint", return_value=face) as roll, patch.object(bot.asyncio, "sleep", side_effect=react) as sleep, patch.object(bot, "apply_attack", new=AsyncMock()) as apply:
                    await bot.launch_attack(ctx, user(2), spell, 20, 20, base_cooldown=15)
                roll.assert_called_once_with(1, 6)
                self.assertEqual(sleep.await_count, int(face >= minimum))
                apply.assert_not_awaited()
                self.assertFalse(bot.pending_attacks)
                self.assertNotIn(1, bot.active_casts)
                self.assertEqual(bot.players[1]["xp"], xp_before)
                self.assertTrue(bot.is_on_cooldown(1))
                if face < minimum:
                    ctx.send.assert_awaited_once_with("❌ **Spell missed.**")
                    self.assertIsNone(database.load_runtime())

    async def test_curse_roll_is_not_consumed_when_cast_is_unavailable(self):
        for reason in ("stunned", "recovering", "casting", "occupied"):
            self.session()
            bot.status_effects.clear()
            bot.offensive_cooldowns.clear()
            bot.active_casts.clear()
            bot.pending_attacks.clear()
            if reason == "stunned":
                bot.status_effects[1] = {"stunned_until": bot.time.time() + 100}
            elif reason == "recovering":
                bot.start_cooldown(1, 100)
            elif reason == "casting":
                bot.active_casts.add(1)
            else:
                bot.pending_attacks[2] = {"attacker_id": 1}
            with patch.object(bot.random, "randint") as roll:
                await bot.launch_attack(context(), user(2), "impero", 20, 20)
            roll.assert_not_called()

    async def test_forced_endoloris_uses_the_same_d6_roll(self):
        self.session()
        with patch.object(bot.random, "randint", return_value=2) as roll, patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            await bot.queue_attack(context(), user(2), user(2), "endoloris", 20, 20,
                                   base_cooldown=15, forced=True, session_id="original")
        roll.assert_called_once_with(1, 6)
        sleep.assert_not_awaited()
        self.assertFalse(bot.pending_attacks)
        self.assertTrue(bot.is_on_cooldown(2))

    async def test_successful_protego_takes_partial_heavy_spell_damage(self):
        for spell, damage in (("bombarda", 43), ("sectumsempra", 35)):
            self.session()
            bot.pending_attacks[2] = {
                "session_id": "original", "attacker_id": 1, "spell": spell,
                "power": 30, "damage": damage, "cooldown": 15,
            }
            hits_before = bot.players[1]["spell_hits"][spell]
            blocks_before = bot.players[2]["combat_stats"]["successful_protegos"]
            with patch.object(bot, "calculate_protego_power", return_value=31), patch.object(bot, "apply_attack", new=AsyncMock()) as apply, patch.object(bot, "spawn_combat_task") as task:
                await bot.protego.callback(context(2))
            apply.assert_not_awaited()
            task.assert_not_called()  # A blocked Sectumsempra must not cause bleeding.
            self.assertEqual(database.load_player(2)["hp"], 100 - damage // 4)
            self.assertEqual(bot.players[2]["combat_stats"]["successful_protegos"], blocks_before + 1)
            self.assertEqual(bot.players[1]["spell_hits"][spell], hits_before)
            self.assertNotIn(2, bot.pending_attacks)

    async def test_partial_protego_damage_can_end_the_duel(self):
        self.session()
        bot.players[2]["hp"] = 5
        bot.pending_attacks[2] = {
            "session_id": "original", "attacker_id": 1, "spell": "bombarda",
            "power": 30, "damage": 40, "cooldown": 15,
        }
        with patch.object(bot, "calculate_protego_power", return_value=31), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))):
            await bot.protego.callback(context(2))
        self.assertEqual(database.load_player(2)["hp"], 0)
        self.assertEqual(database.load_player(1)["duel_wins"], 1)
        self.assertFalse(bot.active_duels)

    async def test_sectumsempra_block_preserves_backlash_after_partial_damage(self):
        self.session()
        bot.pending_attacks[2] = {
            "session_id": "original", "attacker_id": 1, "spell": "sectumsempra",
            "power": 30, "damage": 40, "cooldown": 15,
        }
        with patch.object(bot, "calculate_protego_power", return_value=40), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot.random, "randint", return_value=5):
            await bot.protego.callback(context(2))
        self.assertEqual(database.load_player(2)["hp"], 90)
        self.assertEqual(database.load_player(1)["hp"], 95)

    async def test_heavy_spells_can_still_be_fully_dodged_or_countered(self):
        for spell in ("bombarda", "sectumsempra"):
            for defense in (bot.dodge, bot.expelliarmus):
                self.session()
                bot.status_effects.clear()
                bot.pending_attacks[2] = {
                    "session_id": "original", "attacker_id": 1, "spell": spell,
                    "power": 30, "accuracy": 30, "damage": 40, "cooldown": 15,
                }
                with patch.object(bot, "calculate_dodge_power", return_value=31), patch.object(bot, "calculate_spell_power", return_value=31), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))), patch.object(bot, "apply_attack", new=AsyncMock()) as apply:
                    await defense.callback(context(2))
                apply.assert_not_awaited()
                self.assertEqual(bot.players[2]["hp"], 100)

    async def test_glacius_slow_lasts_ten_seconds_and_incendio_is_faster(self):
        self.session()
        self.assertGreater(bot.SPELL_SPEEDS["incendio"], bot.SPELL_SPEEDS["diffindo"])
        self.assertEqual(bot.SPELL_SPEEDS["incendio"], 9)
        effect = roll_offensive_effect("glacius", 1)
        self.assertEqual(effect["duration"], 10)
        attack = {"session_id": "original", "attacker_id": 1, "spell": "glacius", **effect}
        with patch.object(bot.time, "time", return_value=100), patch.object(bot.bot, "fetch_user", new=AsyncMock(return_value=user(1))):
            await bot.apply_attack(context(), user(2), attack)
        self.assertEqual(bot.status_effects[2]["slowed_until"], 110)
        speed = bot.players[2]["stats"]["speed"]
        with patch.object(bot.time, "time", return_value=109.9):
            self.assertEqual(bot.effective_speed(bot.players[2]), speed - 5)
        with patch.object(bot.time, "time", return_value=110):
            self.assertEqual(bot.effective_speed(bot.players[2]), speed)

    async def test_wrong_channel_rejected(self):
        self.session()
        self.assertFalse(await bot.check_duel_channel(context(channel=99)))


if __name__ == "__main__":
    unittest.main()
