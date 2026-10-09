"""Ranked normal-duel results and local/global leaderboard isolation."""
import copy
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
import test_bot as game
import test_training as training_game


def context(uid=1, guild_id=100):
    ctx = game.context(uid)
    ctx.guild = SimpleNamespace(id=guild_id, get_member=lambda uid: None)
    return ctx


class RankingTests(unittest.IsolatedAsyncioTestCase):
    setUp = game.GameTests.setUp
    tearDown = game.GameTests.tearDown
    ready_player = game.GameTests.ready_player
    session = game.GameTests.session

    def points(self, guild, uid):
        rows, personal = database.player_leaderboard(uid, guild)
        return personal[1] if personal else 0

    def seed(self, guild, uid, points):
        with database.connection() as db:
            db.execute("INSERT INTO local_player_points VALUES (?, ?, ?)", (guild, uid, points))

    async def finish(self, guild=100, session_id="original"):
        self.session()
        session = bot.duel_sessions[1]
        session.update(id=session_id, guild_id=guild, mode="normal")
        for player in bot.players.values():
            bot.persist_player(player)
        bot.players[2]["hp"] = 0
        ctx = context(guild_id=guild)
        self.assertTrue(await bot.check_duel_end(ctx, game.user(2), session_id))
        return ctx

    async def test_winner_loser_and_house_awards_with_local_only_message(self):
        self.seed(100, 1, 120)
        self.seed(100, 2, 80)
        ctx = await self.finish()
        self.assertEqual(self.points(100, 1), 130)
        self.assertEqual(self.points(100, 2), 77)
        self.assertIn(("gryffindor", 10), database.house_leaderboard(100))
        message = ctx.send.await_args_list[0].args[0]
        self.assertIn("120 → 130 (+10)", message)
        self.assertIn("80 → 77 (-3)", message)
        self.assertIn("Gryffindor +10 House Points", message)
        self.assertNotIn("GLOBAL", message)
        self.assertEqual(database.load_player(1)["xp"], 50)
        self.assertEqual(database.load_player(2)["xp"], 20)

    async def test_zero_and_two_point_losses_floor_at_zero(self):
        for before in (0, 2):
            self.seed(100 + before, 2, before)
            await self.finish(100 + before, f"floor-{before}")
            self.assertEqual(self.points(100 + before, 2), 0)

    async def test_servers_and_houses_are_independent_global_is_sum(self):
        await self.finish(100, "server-a")
        await self.finish(200, "server-b")
        self.assertEqual(self.points(100, 1), 10)
        self.assertEqual(self.points(200, 1), 10)
        _, personal = database.player_leaderboard(1)
        self.assertEqual(personal, (1, 20))
        self.assertIn(("gryffindor", 10), database.house_leaderboard(100))
        self.assertIn(("gryffindor", 10), database.house_leaderboard(200))
        self.assertTrue(all(points == 0 for house, points in database.house_leaderboard(300)))

    async def test_completion_and_persistent_receipt_prevent_duplicate_rewards(self):
        await self.finish()
        snapshot = copy.deepcopy(database.load_player(1))
        self.assertFalse(await bot.check_duel_end(context(), game.user(2), "original"))
        winner = database.load_player(1)
        loser = database.load_player(2)
        winner["xp"] += 50
        self.assertIsNone(database.save_ranked_duel_result("original", 100, winner, loser))
        self.assertEqual(winner, snapshot)
        self.assertEqual(database.load_player(1), snapshot)
        self.assertEqual(self.points(100, 1), 10)
        self.assertIn(("gryffindor", 10), database.house_leaderboard(100))
        database.init_database()
        self.assertIsNone(database.save_ranked_duel_result("original", 100, winner, loser))
        self.assertEqual(self.points(100, 1), 10)

    async def test_ranking_failure_rolls_back_profiles_points_house_and_receipt(self):
        self.session()
        bot.duel_sessions[1].update(guild_id=100, mode="normal")
        for player in bot.players.values():
            bot.persist_player(player)
        with database.connection() as db:
            db.execute("CREATE TRIGGER reject_house BEFORE INSERT ON local_house_points BEGIN SELECT RAISE(ABORT, 'test'); END")
        bot.players[2]["hp"] = 0
        with self.assertLogs("database", level="ERROR"), self.assertRaises(sqlite3.IntegrityError):
            await bot.check_duel_end(context(), game.user(2), "original")
        self.assertFalse(bot.active_duels)
        self.assertEqual(database.load_player(1)["xp"], 0)
        self.assertEqual(database.load_player(1)["duel_wins"], 0)
        with database.connection() as db:
            for table in ("local_player_points", "local_house_points", "ranked_duel_results"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    async def test_inactivity_and_cancellation_do_not_affect_rankings(self):
        self.session()
        session = bot.duel_sessions[1]
        session.update(guild_id=100, mode="normal", last_activity=0)
        with patch.object(bot, "duel_clock", return_value=300):
            self.assertTrue(await bot.expire_inactive_duel(session, context()))
        self.session()
        bot.duel_sessions[1].update(guild_id=100, mode="normal")
        await bot.abort_session(bot.duel_sessions[1], "cancelled")
        self.assertEqual(database.player_leaderboard(1), ([], None))
        self.assertTrue(all(points == 0 for house, points in database.house_leaderboard(100)))

    async def test_no_surviving_winner_does_not_award_ranking_points(self):
        self.session()
        bot.duel_sessions[1].update(guild_id=100, mode="normal")
        for player in bot.players.values():
            bot.persist_player(player)
            player["hp"] = 0
        await bot.check_duel_end(context(), game.user(2), "original")
        self.assertEqual(database.player_leaderboard(1), ([], None))
        self.assertTrue(all(points == 0 for house, points in database.house_leaderboard(100)))

    async def test_global_sum_decreases_when_local_points_are_lost(self):
        self.seed(100, 2, 2)
        self.seed(200, 2, 27)
        self.assertEqual(database.player_leaderboard(2)[1][1], 29)
        await self.finish(100, "loss-affects-sum")
        self.assertEqual(database.player_leaderboard(2)[1][1], 27)
        self.assertEqual(self.points(200, 2), 27)

    async def test_local_global_commands_and_personal_rank_use_sql_isolation(self):
        for uid in range(1, 13):
            self.ready_player(uid)
            self.seed(100, uid, 100 - uid)
        self.seed(200, 12, 500)
        ctx = context(12)
        with patch.object(bot.bot, "get_user", return_value=None):
            await bot.leaderboard.callback(ctx)
            local = ctx.send.await_args.args[0]
            self.assertIn("Your Rank: #12 — 88 pts", local)
            self.assertNotIn("Wizard 12 — 588 pts", local)
            self.assertEqual(sum(line.startswith("#") for line in local.splitlines()), 10)
            await bot.globalleaderboard.callback(ctx)
            global_board = ctx.send.await_args.args[0]
            self.assertIn("#1 Wizard 12 — 588 pts", global_board)
            await bot.houseleaderboard.callback(ctx)
            houses = ctx.send.await_args.args[0]
            for house in bot.HOUSE_NAMES.values():
                self.assertIn(f"{house} — 0 pts", houses)

    async def test_ties_are_deterministic_and_unresolved_names_are_safe(self):
        self.seed(100, 2, 10)
        self.seed(100, 1, 10)
        rows, personal = database.player_leaderboard(2, 100)
        self.assertEqual([row["user_id"] for row in rows], [1, 2])
        self.assertEqual(personal, (2, 10))
        ctx = context(3)
        with patch.object(bot.bot, "get_user", return_value=None):
            await bot.leaderboard.callback(ctx)
        self.assertIn("Player 1", ctx.send.await_args.args[0])
        self.assertIn("Unranked — 0 pts", ctx.send.await_args.args[0])

    async def test_schema_upgrade_and_restart_leave_existing_profiles_unchanged(self):
        player = self.ready_player(1)
        bot.persist_player(player)
        snapshot = copy.deepcopy(database.load_player(1))
        self.seed(100, 1, 23)
        database.init_database()
        bot.restore_runtime()
        bot.players.clear()
        self.assertEqual(database.load_player(1), snapshot)
        self.assertEqual(self.points(100, 1), 23)
        self.assertEqual(database.player_leaderboard(1)[1], (1, 23))

    async def test_current_stored_house_and_no_losing_house_penalty(self):
        self.session()
        bot.players[1]["house"] = "ravenclaw"
        bot.players[2]["house"] = "slytherin"
        for player in bot.players.values():
            bot.persist_player(player)
        bot.duel_sessions[1].update(guild_id=100, mode="normal")
        bot.players[2]["hp"] = 0
        await bot.check_duel_end(context(), game.user(2), "original")
        self.assertEqual(dict(database.house_leaderboard(100)), {"ravenclaw": 10, "gryffindor": 0, "slytherin": 0, "hufflepuff": 0})

    async def test_local_commands_guild_only_and_global_command_registered(self):
        import discord.ext.commands as commands
        ctx = context()
        ctx.guild = None
        for name in ("leaderboard", "houseleaderboard"):
            command = bot.bot.get_command(name)
            with self.assertRaises(commands.NoPrivateMessage):
                await command.checks[0](ctx)
        self.assertIsNotNone(bot.bot.get_command("globalleaderboard"))
        with patch.object(bot.bot, "get_user", return_value=None):
            await bot.globalleaderboard.callback(ctx)
        self.assertIn("GLOBAL LEADERBOARD", ctx.send.await_args.args[0])


class TrainingRankingTests(unittest.IsolatedAsyncioTestCase):
    setUp = training_game.TrainingTests.setUp
    tearDown = training_game.TrainingTests.tearDown
    ready_player = training_game.TrainingTests.ready_player
    fetch = training_game.TrainingTests.fetch
    start = training_game.TrainingTests.start

    async def test_training_zero_hp_and_completed_objectives_never_award_points(self):
        session = await self.start()
        session["guild_id"] = 100
        self.student["hp"] = 0
        await bot.check_duel_end(context(2), game.user(2), session["id"])
        session = await self.start()
        session["guild_id"] = 100
        for objective in session["objectives"].values():
            objective["teacher"] = objective["target"]
            objective["student"] = 1
        await bot.end_training(context(2), session, "Objectives completed", completed=True)
        self.assertEqual(database.player_leaderboard(1), ([], None))
        self.assertTrue(all(points == 0 for house, points in database.house_leaderboard(100)))
