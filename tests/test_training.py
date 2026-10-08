"""Cooperative teaching flow and normal combat compatibility."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
from spells import SPELLS
from test_bot import context, user


class TrainingTests(unittest.IsolatedAsyncioTestCase):
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
        self.fetch_patch = patch.object(bot.bot, "fetch_user", side_effect=self.fetch)
        self.fetch_patch.start()
        self.teacher = self.ready_player(1)
        self.student = self.ready_player(2)
        self.teacher["learned_spells"].add("diffindo")
        self.teacher["spell_levels"]["diffindo"] = 3
        self.student["spell_levels"]["confringo"] = 3

    async def fetch(self, uid):
        return user(uid)

    def ready_player(self, uid):
        player = bot.get_player(user(uid))
        player.update(ready=True, profile_started=True, house="gryffindor", level=5, xp=400,
                      duel_wins=5, hp=100)
        player["stats"] = bot.HOUSE_STATS["gryffindor"].copy()
        bot.persist_player(player)
        return player

    def tearDown(self):
        for session_id in list(bot.duel_inactivity_timers):
            bot.stop_duel_inactivity_timer(session_id)
        self.fetch_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    async def start(self, spell="diffindo", direction="teach"):
        self.teacher["learned_spells"].add(spell)
        self.teacher["spell_levels"][spell] = 3
        if direction == "teach":
            await bot.teach.callback(context(1), user(2), spell_name=spell)
            await bot.accept.callback(context(2))
        else:
            await bot.askhelp.callback(context(2), user(1), spell_name=spell)
            await bot.accept.callback(context(1))
        self.assertIsNotNone(bot.training_session(1))
        return bot.training_session(1)

    async def hit(self, caster, spell="confringo", damage=1, duration=2):
        opponent = bot.active_duels[caster]
        session_id = bot.duel_sessions[caster]["id"]
        await bot.apply_attack(context(caster), user(opponent), {
            "session_id": session_id, "attacker_id": caster, "spell": spell,
            "power": 1, "accuracy": 1, "damage": damage, "duration": duration,
        })

    def pending(self, defender, spell="confringo", damage=1):
        attack = {"session_id": bot.duel_sessions[defender]["id"],
                  "attacker_id": bot.active_duels[defender], "spell": spell,
                  "power": 1, "accuracy": 1, "damage": damage,
                  "duration": 2, "cooldown": 6}
        bot.pending_attacks[defender] = attack
        return attack

    async def test_teacher_offer_preserves_roles(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        request = bot.teaching_requests[2]
        self.assertEqual((request["teacher_id"], request["student_id"], request["initiator_id"]), (1, 2, 1))
        await bot.accept.callback(context(2))
        session = bot.training_session(1)
        self.assertEqual((session["teacher_id"], session["student_id"]), (1, 2))
        self.assertIs(session, bot.training_session(2))
        self.assertFalse(bot.teaching_requests)
        self.assertEqual(session["mode"], "training")

    async def test_student_request_preserves_roles(self):
        await bot.askhelp.callback(context(2), user(1), spell_name="diffindo")
        request = bot.teaching_requests[1]
        self.assertEqual((request["teacher_id"], request["student_id"], request["initiator_id"]), (1, 2, 2))
        await bot.accept.callback(context(1))
        session = bot.training_session(2)
        self.assertEqual((session["teacher_id"], session["student_id"]), (1, 2))

    async def test_each_direction_can_decline_cleanly(self):
        for direction in ("teach", "askhelp"):
            if direction == "teach":
                await bot.teach.callback(context(1), user(2), spell_name="diffindo")
                receiver = 2
            else:
                await bot.askhelp.callback(context(2), user(1), spell_name="diffindo")
                receiver = 1
            await bot.decline.callback(context(receiver))
            self.assertFalse(bot.teaching_requests)
            self.assertFalse(bot.active_duels)

    async def test_normal_duel_accept_and_decline_remain_normal(self):
        await bot.duel.callback(context(1), user(2))
        self.assertIn(2, bot.duel_requests)
        await bot.decline.callback(context(2))
        self.assertFalse(bot.duel_requests)
        await bot.duel.callback(context(1), user(2))
        await bot.accept.callback(context(2))
        self.assertEqual(bot.duel_sessions[1]["mode"], "normal")
        self.assertIsNone(bot.training_session(1))
        self.assertEqual(bot.active_duels, {1: 2, 2: 1})

    async def test_normal_duel_still_awards_hits_xp_and_results(self):
        await bot.duel.callback(context(1), user(2))
        await bot.accept.callback(context(2))
        await self.hit(1, damage=200)
        self.assertEqual(self.teacher["combat_stats"]["successful_attacks"], 1)
        self.assertEqual(self.teacher["spell_hits"]["confringo"], 1)
        self.assertEqual(self.teacher["xp"], 465)
        self.assertEqual(self.student["xp"], 420)
        self.assertEqual(self.teacher["duel_wins"], 6)
        self.assertEqual(self.student["duel_losses"], 1)
        self.assertEqual(self.teacher["duels_completed"], 1)
        self.assertEqual(self.teacher["duels_since_avada"], 6)

    async def test_ambiguous_legacy_inbox_requires_explicit_choice(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        bot.duel_requests[2] = {"challenger_id": 3, "channel_id": 10, "expires": bot.time.time() + 100}
        ctx = context(2)
        await bot.accept.callback(ctx)
        self.assertFalse(bot.active_duels)
        self.assertIn(2, bot.teaching_requests)
        self.assertIn(2, bot.duel_requests)
        await bot.decline.callback(ctx, "training")
        self.assertNotIn(2, bot.teaching_requests)
        self.assertIn(2, bot.duel_requests)

    async def test_overlapping_and_normal_requests_are_rejected(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        await bot.askhelp.callback(context(2), user(1), spell_name="diffindo")
        self.assertEqual(len(bot.teaching_requests), 1)
        original = dict(bot.teaching_requests[2])
        await bot.duel.callback(context(1), user(2))
        self.assertFalse(bot.duel_requests)
        self.assertEqual(bot.teaching_requests[2], original)
        await bot.canceltraining.callback(context(1))
        await bot.duel.callback(context(1), user(2))
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        self.assertFalse(bot.teaching_requests)
        self.assertIn(2, bot.duel_requests)

    async def test_request_validation_rejects_bad_teacher_student_and_spell(self):
        self.assertIsNotNone(bot.training_validation(user(1), user(1), "diffindo"))
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "unknown"))
        self.teacher["learned_spells"].remove("diffindo")
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "diffindo"))
        self.teacher["learned_spells"].add("diffindo")
        self.teacher["spell_levels"]["diffindo"] = 2
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "diffindo"))
        self.teacher["spell_levels"]["diffindo"] = 3
        self.student["learned_spells"].add("diffindo")
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "diffindo"))
        self.student["learned_spells"].remove("diffindo")
        self.student["ready"] = False
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "diffindo"))
        self.student["ready"] = True
        self.assertIsNone(bot.training_validation(user(1), user(2), "diffindo"))
        bot.active_duels[1] = 3
        self.assertIsNotNone(bot.training_validation(user(1), user(2), "diffindo"))

    async def test_accept_revalidates_knowledge_level_availability_and_prerequisites(self):
        for mutate in (
            lambda: self.teacher["learned_spells"].remove("diffindo"),
            lambda: self.teacher["spell_levels"].update(diffindo=2),
            lambda: self.student.update(level=1),
            lambda: bot.active_duels.update({1: 3}),
            lambda: self.student["learned_spells"].add("diffindo"),
        ):
            self.teacher["learned_spells"].add("diffindo")
            self.teacher["spell_levels"]["diffindo"] = 3
            self.student["learned_spells"].discard("diffindo")
            self.student["level"] = 5
            bot.active_duels.clear()
            await bot.teach.callback(context(1), user(2), spell_name="diffindo")
            self.assertIn(2, bot.teaching_requests)
            mutate()
            await bot.accept.callback(context(2))
            self.assertIsNone(bot.training_session(2))
            self.assertNotIn(2, bot.teaching_requests)

    async def test_wrong_channel_and_expired_teaching_request_do_not_start(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        await bot.accept.callback(context(2, channel=99))
        self.assertFalse(bot.active_duels)
        self.assertIn(2, bot.teaching_requests)
        bot.teaching_requests[2]["expires"] = 0
        await bot.accept.callback(context(2))
        self.assertFalse(bot.teaching_requests)
        self.assertFalse(bot.active_duels)

    async def test_teacher_alone_cannot_complete_and_student_unlocks_completion(self):
        session = await self.start()
        before_teacher = copy.deepcopy(self.teacher)
        before_student = copy.deepcopy(self.student)
        for _ in range(10):
            await self.hit(1)
        obj = session["objectives"]["combat_stats:successful_attacks"]
        self.assertEqual((obj["teacher"], obj["student"]), (10, 0))
        self.assertFalse(bot.objective_complete(obj))
        self.assertIsNotNone(bot.training_session(1))
        self.assertIn("Student must contribute", bot.training_progress_message(session))
        self.assertFalse(bot.can_learn_spell(self.student, "diffindo"))
        await self.hit(2)
        self.assertFalse(bot.active_duels)
        self.assertTrue(self.student["practical_training"]["diffindo"]["completed"])
        self.assertNotIn("diffindo", self.student["learned_spells"])
        self.assertTrue(bot.can_learn_spell(self.student, "diffindo"))
        for player, before in ((self.teacher, before_teacher), (self.student, before_student)):
            for field in ("combat_stats", "spell_hits", "spell_xp", "spell_levels", "xp", "level",
                          "talent_points", "duel_wins", "duel_losses", "duels_completed", "duels_since_avada", "hp"):
                self.assertEqual(player[field], before[field], field)
        ctx = context(2)
        await bot.learn.callback(ctx, spell_name="diffindo")
        self.assertEqual(bot.active_learning_trials[2]["required_score"], 5)
        self.assertEqual(len(bot.active_learning_trials[2]["questions"]), 7)
        self.assertNotIn("diffindo", self.student["learned_spells"])
        while 2 in bot.active_learning_trials:
            trial = bot.active_learning_trials[2]
            choice = trial["questions"][trial["current_question"]]["correct_letter"]
            await bot.answer.callback(ctx, choice)
        self.assertIn("diffindo", self.student["learned_spells"])

    async def test_each_objective_needs_student_contribution_and_completed_categories_persist(self):
        synthetic = dict(SPELLS["diffindo"], requirements={
            "level": 3, "spell_levels": {"confringo": 3},
            "spell_hits": {"confringo": 2},
            "combat_stats": {"successful_attacks": 2, "successful_protegos": 2},
        })
        with patch.dict(SPELLS, {"diffindo": synthetic}):
            session = await self.start()
            for _ in range(2):
                await self.hit(1)
                self.pending(1)
                with patch.object(bot, "calculate_protego_power", return_value=99):
                    await bot.protego.callback(context(1))
            self.assertTrue(all(not bot.objective_complete(obj) for obj in session["objectives"].values()))
            await self.hit(2)
            self.assertTrue(bot.objective_complete(session["objectives"]["spell_hits:confringo"]))
            self.assertTrue(bot.objective_complete(session["objectives"]["combat_stats:successful_attacks"]))
            self.assertFalse(bot.objective_complete(session["objectives"]["combat_stats:successful_protegos"]))
            await bot.canceltraining.callback(context(1))
            self.assertFalse(bot.active_duels)
            saved = database.load_player(2)["practical_training"]["diffindo"]
            self.assertEqual(set(saved["objectives"]), {"spell_hits:confringo", "combat_stats:successful_attacks"})
            self.assertFalse(saved.get("completed", False))
            session = await self.start()
            self.assertEqual(set(session["objectives"]), {"combat_stats:successful_protegos"})
            for uid in (1, 2):
                self.pending(uid)
                with patch.object(bot, "calculate_protego_power", return_value=99):
                    await bot.protego.callback(context(uid))
            self.assertFalse(bot.active_duels)
            self.assertTrue(bot.can_learn_spell(self.student, "diffindo"))

    async def test_actual_dodge_counter_and_control_actions_contribute_without_permanent_credit(self):
        synthetic = dict(SPELLS["diffindo"], requirements={"combat_stats": {
            "successful_dodges": 2, "successful_counters": 2, "successful_control_spells": 2,
        }})
        with patch.dict(SPELLS, {"diffindo": synthetic}):
            session = await self.start()
            for actor in (1, 2):
                bot.status_effects.clear()
                self.pending(actor)
                with patch.object(bot, "calculate_dodge_power", return_value=99):
                    await bot.dodge.callback(context(actor))
                self.pending(actor)
                with patch.object(bot, "calculate_spell_power", return_value=99):
                    await bot.expelliarmus.callback(context(actor))
                bot.status_effects.clear()
                await self.hit(actor, "stupefy")
            self.assertFalse(bot.active_duels)
            self.assertTrue(all(bot.objective_complete(obj) for obj in session["objectives"].values()))
            for player in (self.teacher, self.student):
                self.assertTrue(all(value == 0 for value in player["combat_stats"].values()))
                self.assertEqual(player["xp"], 400)

    async def test_unrelated_players_cannot_contribute(self):
        session = await self.start()
        outsider = self.ready_player(3)
        bot.record_combat_success(outsider, "combat_stats", "successful_attacks")
        objective = session["objectives"]["combat_stats:successful_attacks"]
        self.assertEqual((objective["teacher"], objective["student"]), (0, 0))
        self.assertEqual(outsider["combat_stats"]["successful_attacks"], 1)

    async def test_zero_hp_ends_training_without_results_and_restores_hp(self):
        self.teacher["hp"] = 42
        self.student["hp"] = 37
        before = copy.deepcopy(self.teacher), copy.deepcopy(self.student)
        await self.start()
        await self.hit(1, damage=200)
        self.assertFalse(bot.active_duels)
        self.assertEqual(self.teacher["hp"], 42)
        self.assertEqual(self.student["hp"], 37)
        self.assertEqual(self.teacher["practical_training"], {})
        for player, original in zip((self.teacher, self.student), before):
            for field in ("xp", "spell_xp", "spell_levels", "combat_stats", "spell_hits", "duel_wins",
                          "duel_losses", "duels_completed", "duels_since_avada"):
                self.assertEqual(player[field], original[field], field)

    async def test_cancel_cleans_all_training_state_and_restores_hp(self):
        self.teacher["hp"] = 42
        self.student["hp"] = 37
        await self.start()
        self.pending(2)
        bot.active_casts.add(1)
        bot.start_cooldown(1, 100)
        bot.status_effects[2] = {"stunned_until": bot.time.time() + 10}
        await bot.canceltraining.callback(context(2))
        for state in (bot.active_duels, bot.duel_sessions, bot.pending_attacks,
                      bot.active_casts, bot.offensive_cooldowns, bot.status_effects, bot.teaching_requests):
            self.assertFalse(state)
        self.assertEqual((self.teacher["hp"], self.student["hp"]), (42, 37))

    async def test_restart_preserves_certification_but_not_live_training_or_transient_hp(self):
        self.student["combat_stats"]["successful_attacks"] = 9
        self.student["hp"] = 37
        bot.persist_player(self.student)
        await self.start()
        await self.hit(2)
        self.assertFalse(bot.active_duels)
        bot.players.clear()
        self.student = bot.get_player(user(2))
        self.assertTrue(bot.can_learn_spell(self.student, "diffindo"))
        self.assertEqual(self.student["hp"], 37)
        # Another training session is temporary and does not replace certification.
        self.teacher = bot.get_player(user(1))
        self.teacher["learned_spells"].add("glacius")
        self.teacher["spell_levels"]["glacius"] = 3
        await self.start("glacius")
        await self.hit(1, "stupefy", damage=5)
        bot.persist_runtime()
        runtime = database.load_runtime()
        self.assertIsNone(runtime)
        self.assertEqual(database.load_player(2)["hp"], 37)
        for state in (bot.active_duels, bot.duel_sessions, bot.pending_attacks, bot.players):
            state.clear()
        bot.restore_runtime()
        self.assertFalse(bot.active_duels)
        self.assertTrue(bot.can_learn_spell(bot.get_player(user(2)), "diffindo"))

    async def test_ravenclaw_targets_and_quiz_bonus_remain_without_teacher_discount(self):
        self.student.update(house="ravenclaw", level=3)
        self.student["combat_stats"]["successful_control_spells"] = 3
        session = await self.start("glacius")
        self.assertEqual(session["objectives"]["combat_stats:successful_control_spells"]["target"], 1)
        await self.hit(2, "stupefy")
        self.assertFalse(bot.active_duels)
        await bot.learn.callback(context(2), spell_name="glacius")
        self.assertEqual(bot.active_learning_trials[2]["required_score"], 4)  # 5 - Ravenclaw's 1 only.

    async def test_training_never_satisfies_noncombat_prerequisites(self):
        self.student["practical_training"]["diffindo"] = {"completed": True, "objectives": {}}
        self.student["level"] = 1
        self.assertFalse(bot.can_learn_spell(self.student, "diffindo"))
        await bot.learn.callback(context(2), spell_name="diffindo")
        self.assertNotIn(2, bot.active_learning_trials)
        self.student["level"] = 5
        self.student["spell_levels"]["confringo"] = 2
        self.assertFalse(bot.can_learn_spell(self.student, "diffindo"))
        self.student["spell_levels"]["confringo"] = 3
        self.student["stats"]["magic_power"] = 1
        self.assertFalse(bot.can_learn_spell(self.student, "diffindo"))
        self.student["practical_training"]["depulso"] = {"completed": True, "objectives": {}}
        self.student["spell_levels"]["expelliarmus"] = 2
        self.student["duel_wins"] = 0
        self.assertFalse(bot.can_learn_spell(self.student, "depulso"))

    async def test_spells_without_practical_requirements_do_not_start_empty_training(self):
        for spell in ("bombarda", "avadakedavra"):
            self.teacher["learned_spells"].add(spell)
            self.teacher["spell_levels"][spell] = 3
            await bot.teach.callback(context(1), user(2), spell_name=spell)
            self.assertFalse(bot.teaching_requests)
            self.assertFalse(bot.active_duels)

    async def test_member_departure_and_channel_deletion_end_training(self):
        session = await self.start()
        session["guild_id"] = 99
        member = SimpleNamespace(id=2, display_name="Student", guild=SimpleNamespace(id=99))
        channel = SimpleNamespace(send=AsyncMock())
        with patch.object(bot.bot, "get_channel", return_value=channel):
            await bot.on_member_remove(member)
        self.assertFalse(bot.active_duels)
        await self.start()
        await bot.on_guild_channel_delete(SimpleNamespace(id=10))
        self.assertFalse(bot.active_duels)

    async def test_training_does_not_consume_avada_recharge(self):
        await self.start()
        self.teacher["learned_spells"].add("avadakedavra")
        self.teacher["spell_levels"]["avadakedavra"] = 1
        with patch.object(bot.random, "randint", return_value=1):
            await bot.avadakedavra.callback(context(1))
        self.assertEqual(self.teacher["duels_since_avada"], 5)
        self.assertIsNotNone(bot.training_session(1))

    async def test_cancellation_during_hit_announcement_does_not_award_combat_xp(self):
        await self.start()
        ctx = context(1)

        async def cancel_during_send(message):
            await bot.canceltraining.callback(context(2))

        ctx.send.side_effect = cancel_during_send
        await bot.apply_attack(ctx, user(2), {
            "session_id": bot.duel_sessions[1]["id"], "attacker_id": 1,
            "spell": "confringo", "damage": 1, "power": 1, "accuracy": 1,
        })
        self.assertEqual(self.teacher["xp"], 400)
        self.assertEqual(self.teacher["spell_xp"]["confringo"], 0)
        self.assertFalse(bot.active_duels)

    async def test_pending_attacks_and_delayed_damage_are_invalid_after_cancel(self):
        session = await self.start()
        old_id = session["id"]
        old_attack = self.pending(2, spell="stupefy", damage=10)
        await bot.canceltraining.callback(context(2))
        await bot.duel.callback(context(1), user(2))
        await bot.accept.callback(context(2))
        hp = self.student["hp"]
        with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
            await bot.apply_attack(context(1), user(2), old_attack)
            await bot.apply_endoloris(context(1), user(2), old_id)
            await bot.apply_sectumsempra_bleed(context(1), user(2), user(1), old_id)
        self.assertEqual(self.student["hp"], hp)
        self.assertNotIn(2, bot.status_effects)
        self.assertEqual(self.teacher["combat_stats"]["successful_attacks"], 0)

    async def test_unavailable_member_at_accept_cancels_request(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        guild = SimpleNamespace(id=99, fetch_member=AsyncMock(side_effect=bot.discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Member left")))
        ctx = context(2)
        ctx.guild = guild
        await bot.accept.callback(ctx)
        self.assertFalse(bot.teaching_requests)
        self.assertFalse(bot.active_duels)

    async def test_background_damage_ends_training_without_normal_rewards(self):
        for effect in ("burn", "bleed", "curse"):
            session = await self.start()
            self.student["hp"] = 1
            with patch.object(bot.asyncio, "sleep", new=AsyncMock()):
                if effect == "burn":
                    await bot.apply_burn(context(), user(2), session["id"])
                elif effect == "bleed":
                    await bot.apply_sectumsempra_bleed(context(), user(2), user(1), session["id"])
                else:
                    await bot.apply_endoloris(context(), user(2), session["id"])
            self.assertFalse(bot.active_duels)
            self.assertEqual(self.student["hp"], 100)
            self.assertEqual(self.teacher["duel_wins"], 5)
            self.assertEqual(self.teacher["duels_completed"], 0)
            self.assertEqual(self.student["duel_losses"], 0)
            self.assertEqual(self.student["xp"], 400)

    async def test_spellbook_and_spell_details_recognize_saved_practical_certificate(self):
        self.student["practical_training"]["diffindo"] = {"completed": True, "objectives": {}}
        self.assertEqual(bot.spell_status(self.student, "diffindo", 2), "📘 READY TO LEARN")
        ctx = context(2)
        await bot.spell_info.callback(ctx, spell_name="diffindo")
        message = ctx.send.await_args.args[0]
        self.assertIn("practical training completed", message)
        self.assertIn("5/7 required", message)
        await bot.spellbook.callback(ctx)
        self.assertIn("Diffindo", ctx.send.await_args.args[0])

    async def test_admin_xp_and_talent_progression_work_during_training(self):
        await self.start()
        with patch.object(bot, "OWNER_ID", 1):
            await bot.addxp.callback(context(1), user(2), 100)
        self.assertEqual(self.student["xp"], 500)
        self.assertEqual(self.student["level"], 6)
        self.assertEqual(self.student["talent_points"], 3)

    async def test_deleted_channel_and_removed_guild_clear_pending_requests(self):
        await bot.teach.callback(context(1), user(2), spell_name="diffindo")
        await bot.on_guild_channel_delete(SimpleNamespace(id=10))
        self.assertFalse(bot.teaching_requests)
        ctx = context(1)
        ctx.guild = SimpleNamespace(id=99)
        await bot.teach.callback(ctx, user(2), spell_name="diffindo")
        await bot.on_guild_remove(SimpleNamespace(id=99))
        self.assertFalse(bot.teaching_requests)

    async def test_training_start_saves_original_hp_and_cancel_is_allowed_elsewhere(self):
        self.teacher["hp"] = 42
        self.student["hp"] = 37
        await self.start()
        self.assertEqual(database.load_player(1)["hp"], 42)
        self.assertEqual(database.load_player(2)["hp"], 37)
        self.assertEqual(self.teacher["hp"], self.teacher["max_hp"])
        ctx = context(2, channel=99)
        ctx.command = bot.bot.get_command("canceltraining")
        self.assertTrue(await bot.check_duel_channel(ctx))
        await bot.canceltraining.callback(ctx)
        self.assertEqual(self.student["hp"], 37)
        self.assertFalse(bot.active_duels)

    async def test_every_expected_command_registers(self):
        for name in ("teach", "askhelp", "accept", "decline", "training", "canceltraining",
                     "learn", "answer", "protego", "dodge", "expelliarmus"):
            self.assertIsNotNone(bot.bot.get_command(name), name)
