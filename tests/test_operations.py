"""Private-beta reliability regression checks; no live Discord or production DB."""
import asyncio
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
from operations import ProcessLock, SecretFormatter
import test_bot as game
from test_bot import context, user


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.db_patch = patch.object(database, "DATABASE_FILE", str(self.path / "players.db"))
        self.backup_patch = patch.object(database, "BACKUP_DIR", self.path / "backups")
        self.db_patch.start()
        self.backup_patch.start()
        database.init_database()

    def tearDown(self):
        self.backup_patch.stop()
        self.db_patch.stop()
        self.directory.cleanup()

    def test_startup_configuration_fails_clearly_without_connecting(self):
        for token in (None, "", "   "):
            with self.assertRaisesRegex(SystemExit, "DISCORD_TOKEN"):
                bot.validate_configuration(token, 1)
        for owner in (None, "invalid", 0, -1):
            with self.assertRaisesRegex(SystemExit, "BOT_OWNER_ID"):
                bot.validate_configuration("test-token", owner)
        bot.validate_configuration("test-token", 1)

    def test_failed_save_restores_in_memory_profile(self):
        player = {"user_id": 1, "xp": 100, "learned_spells": set()}
        database.save_player(1, player)
        with database.connection() as db:
            db.execute("CREATE TRIGGER reject_write BEFORE UPDATE ON players BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        player["xp"] = 500
        with self.assertLogs("database", level="ERROR"), self.assertRaises(sqlite3.IntegrityError):
            database.save_player(1, player)
        self.assertEqual(player["xp"], 100)


    def test_older_record_missing_learned_spells_is_loadable(self):
        with database.connection() as db:
            db.execute("INSERT INTO players VALUES (1, ?)", ('{"xp":120}',))
        player = database.load_player(1)
        self.assertEqual(player["xp"], 120)
        self.assertEqual(player["learned_spells"], set())
        self.assertEqual(player["user_id"], 1)

    def test_invalid_json_is_not_replaced(self):
        with database.connection() as db:
            db.execute("INSERT INTO players VALUES (1, 'broken')")
        with self.assertLogs("database", level="ERROR"), self.assertRaises(ValueError):
            database.load_player(1)
        with database.connection() as db:
            self.assertEqual(db.execute("SELECT data FROM players").fetchone()[0], "broken")

    def test_online_backup_contains_latest_committed_profiles_and_retains_ten(self):
        database.save_player(1, {"user_id": 1, "xp": 100, "learned_spells": {"confringo"}})
        for _ in range(12):
            snapshot = database.backup_database()
        self.assertEqual(len(list((self.path / "backups").glob("*.sqlite3"))), 10)
        self.assertFalse(list((self.path / "backups").glob("*.tmp")))
        with sqlite3.connect(str(snapshot)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIn('"xp": 100', db.execute("SELECT data FROM players").fetchone()[0])
        self.assertEqual(database.database_health(), (True, 1))

    def test_multi_profile_write_rolls_back_on_second_insert_failure(self):
        database.save_player(1, {"user_id": 1, "xp": 100})
        database.save_player(2, {"user_id": 2, "xp": 100})
        with database.connection() as db:
            db.execute("CREATE TRIGGER reject_second BEFORE UPDATE ON players WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertLogs("database", level="ERROR"), self.assertRaises(sqlite3.IntegrityError):
            database.save_players([{"user_id": 1, "xp": 150}, {"user_id": 2, "xp": 120}])
        self.assertEqual(database.load_player(1)["xp"], 100)
        self.assertEqual(database.load_player(2)["xp"], 100)

    def test_token_redacted_in_message_and_traceback(self):
        formatter = SecretFormatter("SECRET-TOKEN")
        try:
            raise ValueError("SECRET-TOKEN")
        except ValueError as error:
            record = logging.LogRecord("test", logging.ERROR, "", 1, "token=%s", ("SECRET-TOKEN",), (type(error), error, error.__traceback__))
        result = formatter.format(record)
        self.assertNotIn("SECRET-TOKEN", result)
        self.assertIn("[REDACTED]", result)

    def test_process_lock_rejects_second_instance_and_releases(self):
        path = self.path / "bot.lock"
        with ProcessLock(path):
            with self.assertRaises(SystemExit):
                with ProcessLock(path):
                    pass
        with ProcessLock(path):
            pass


# Share fixture helpers without re-running the entire inherited gameplay suite.
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = game.GameTests.setUp
    tearDown = game.GameTests.tearDown
    ready_player = game.GameTests.ready_player
    session = game.GameTests.session

    async def test_profile_house_and_stat_training_persist_before_send_failure(self):
        ctx = context()
        await bot.profile.callback(ctx)
        await bot.house.callback(ctx, "gryffindor")
        await bot.train.callback(ctx, "speed")
        await bot.train.callback(ctx, "agility")
        ctx.send.side_effect = RuntimeError("simulated send failure")
        with self.assertRaises(RuntimeError):
            await bot.train.callback(ctx, "endurance")
        saved = database.load_player(1)
        self.assertEqual(saved["talent_points"], 0)
        self.assertTrue(saved["ready"])
        self.assertEqual(saved["house"], "gryffindor")
        self.assertEqual(saved["stats"]["endurance"], 19)

    async def test_failed_trial_discards_memory_only_questions(self):
        import discord.ext.commands as commands
        bot.active_learning_trials[1] = {"spell": "incendio", "score": 1}
        ctx = context()
        ctx.command = SimpleNamespace(name="answer")
        with self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("quiz failure")))
        self.assertNotIn(1, bot.active_learning_trials)

    async def test_failed_result_save_cleans_state_without_partial_rewards(self):
        self.session()
        before = {uid: database.load_player(uid)["xp"] for uid in (1, 2)}
        bot.players[2]["hp"] = 0
        with database.connection() as db:
            db.execute("CREATE TRIGGER reject_result BEFORE UPDATE ON players WHEN NEW.user_id=2 BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertLogs("database", level="ERROR"), self.assertRaises(sqlite3.IntegrityError):
            await bot.check_duel_end(context(), user(2), "original")
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.duel_sessions)
        for uid in (1, 2):
            self.assertEqual(database.load_player(uid)["xp"], before[uid])
            self.assertEqual(database.load_player(uid)["duels_completed"], 0)

    async def test_result_send_failure_does_not_strand_duel_or_duplicate_rewards(self):
        self.session()
        bot.players[2]["hp"] = 0
        ctx = context()
        ctx.send.side_effect = RuntimeError("simulated send failure")
        with self.assertRaises(RuntimeError):
            await bot.check_duel_end(ctx, user(2), "original")
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.duel_sessions)
        self.assertEqual(database.load_player(1)["duel_wins"], 1)
        self.assertEqual(database.load_player(2)["duel_losses"], 1)
        await bot.check_duel_end(context(), user(2), "original")
        self.assertEqual(database.load_player(1)["duel_wins"], 1)

    async def test_cancelling_one_duel_leaves_other_duel_task_alive(self):
        self.session()
        other = {"id": "other", "mode": "normal", "channel_id": 20}
        for uid, opponent in ((3, 4), (4, 3)):
            self.ready_player(uid)
            bot.active_duels[uid] = opponent
            bot.duel_sessions[uid] = other
        event = asyncio.Event()
        first = bot.spawn_combat_task("original", event.wait())
        second = bot.spawn_combat_task("other", event.wait())
        await bot.abort_session(bot.duel_sessions[1], "test")
        await asyncio.gather(first, return_exceptions=True)
        self.assertTrue(first.cancelled())
        self.assertFalse(second.done())
        self.assertEqual(bot.active_duels, {3: 4, 4: 3})
        bot.cancel_combat_tasks("other")
        await asyncio.gather(second, return_exceptions=True)

    async def test_old_backlash_cannot_affect_new_session_after_fetch(self):
        self.session()
        attack = {"spell": "sectumsempra", "power": 1, "attacker_id": 1, "session_id": "original"}
        async def fetch(uid):
            bot.duel_sessions[1] = {"id": "replacement", "channel_id": 10}
            return user(uid)
        with patch.object(bot.bot, "fetch_user", side_effect=fetch):
            await bot.apply_sectumsempra_backlash(context(), attack, 50)
        self.assertEqual(bot.players[1]["hp"], 100)

    async def test_owner_diagnostics_and_backup_registered_and_authorized(self):
        self.session()
        for name in ("botstatus", "backupdb"):
            command = bot.bot.get_command(name)
            self.assertIsNotNone(command)
            with patch.object(bot, "OWNER_ID", 99):
                self.assertFalse(await command.checks[0](context(1)))
                self.assertTrue(await command.checks[0](context(99)))
        ctx = context(99)
        await bot.botstatus.callback(ctx)
        result = ctx.send.await_args.args[0]
        self.assertIn("Normal / training duels: 1/0", result)
        self.assertIn("Health: **OK**", result)
        ctx.command = SimpleNamespace(name="botstatus")
        ctx.channel.id = 999
        self.assertTrue(await bot.check_duel_channel(ctx))

    async def test_argument_error_is_short_and_internal_error_is_generic(self):
        import discord.ext.commands as commands
        ctx = context()
        ctx.command = SimpleNamespace(name="setxp")
        await bot.on_command_error(ctx, commands.MemberNotFound("sensitive-input"))
        self.assertIn("Player not found", ctx.send.await_args.args[0])
        with self.assertLogs(bot.logger, level="ERROR"):
            await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("private/internal/path")))
        self.assertNotIn("private/internal/path", ctx.send.await_args.args[0])

    async def test_stale_normal_request_changed_during_fetch_is_not_accepted(self):
        self.ready_player(1)
        self.ready_player(2)
        bot.duel_requests[1] = {"challenger_id": 2, "expires": float("inf"), "channel_id": 10}
        async def fetch(uid):
            bot.duel_requests.pop(1)
            return user(uid)
        with patch.object(bot.bot, "fetch_user", side_effect=fetch):
            await bot.accept.callback(context())
        self.assertFalse(bot.active_duels)

    async def test_async_failure_is_logged_and_cleans_its_session(self):
        self.session()
        async def fail():
            raise RuntimeError("test effect error")
        with self.assertLogs(bot.logger, level="ERROR"):
            task = bot.spawn_combat_task("original", fail())
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            await asyncio.gather(*list(bot.maintenance_tasks), return_exceptions=True)
        self.assertFalse(bot.active_duels)
        self.assertFalse(bot.combat_tasks)
