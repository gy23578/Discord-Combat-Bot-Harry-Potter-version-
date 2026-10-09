"""Owner-only guild maintenance; no live Discord calls or production DB writes."""
import hashlib
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import discord
from discord.ext import commands
import bot


def context(guild=None):
    return SimpleNamespace(author=SimpleNamespace(id=99), guild=guild,
                           channel=SimpleNamespace(id=123), send=AsyncMock())


def guild(uid=1, name="Hogwarts France", members=450):
    return SimpleNamespace(id=uid, name=name, member_count=members, leave=AsyncMock())


class OwnerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_owner_checks_and_dm_checks(self):
        ctx = context()
        for name in ('servers', 'leaveguild'):
            command = bot.bot.get_command(name)
            self.assertIsNotNone(command)
            self.assertIn(bot.owner_check, command.checks)
            with patch.object(bot, 'OWNER_ID', 99):
                self.assertTrue(await command.checks[0](ctx))
            with patch.object(bot, 'OWNER_ID', 100):
                self.assertFalse(await command.checks[0](ctx))
            ctx.command = command
            self.assertTrue(await bot.guild_context(ctx))
            with patch.object(bot, 'in_duel_channel', return_value=False):
                self.assertTrue(await bot.check_duel_channel(ctx))
        for name in ('duel', 'leaderboard', 'houseleaderboard', 'dodge'):
            ctx.command = bot.bot.get_command(name)
            with self.assertRaises(commands.NoPrivateMessage):
                await bot.guild_context(ctx)

    async def test_servers_dm_lists_names_ids_counts_and_empty_state(self):
        ctx = context()
        with patch.object(type(bot.bot), 'guilds', new_callable=PropertyMock, return_value=[guild(), guild(2, 'Wizarding World', None)]):
            await bot.servers.callback(ctx)
        message = ctx.send.await_args.args[0]
        for text in ('Hogwarts France', 'ID: 1', 'Members: 450', 'Wizarding World', 'ID: 2', 'Members: Unknown', 'Total servers: 2'):
            self.assertIn(text, message)
        ctx.send.reset_mock()
        with patch.object(type(bot.bot), 'guilds', new_callable=PropertyMock, return_value=[]):
            await bot.servers.callback(ctx)
        ctx.send.assert_awaited_once_with('Duellium is not currently in any servers.')

    async def test_long_server_list_is_complete_and_under_limit(self):
        ctx = context()
        guilds = [guild(uid, ('*' if uid % 2 else '🧙') * 100) for uid in range(1, 101)]
        with patch.object(type(bot.bot), 'guilds', new_callable=PropertyMock, return_value=guilds):
            await bot.servers.callback(ctx)
        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) < 2000 for message in messages))
        self.assertTrue(all(len(message.encode("utf-16-le")) // 2 < 2000 for message in messages))
        combined = '\n'.join(messages)
        for item in guilds:
            self.assertIn(f'ID: {item.id}\n', combined)
        self.assertIn('Total servers: 100', messages[-1])

    async def test_leave_dm_requires_no_owner_membership_and_confirms_after(self):
        ctx, target = context(), guild()
        target.leave.side_effect = lambda: self.assertEqual(ctx.send.await_count, 1)
        with patch.object(bot.bot, 'get_guild', return_value=target) as lookup, self.assertLogs(bot.logger, level='INFO') as logs:
            await bot.leaveguild.callback(ctx, 1)
        lookup.assert_called_once_with(1)
        target.leave.assert_awaited_once()
        self.assertEqual(ctx.send.await_count, 2)
        self.assertIn('✅ Duellium left Hogwarts France (1).', ctx.send.await_args.args[0])
        self.assertIn('Owner 99', '\n'.join(logs.output))
        self.assertIn('Hogwarts France', '\n'.join(logs.output))

    async def test_leave_same_guild_only_confirms_before_leaving(self):
        target = guild()
        ctx = context(target)
        with patch.object(bot.bot, 'get_guild', return_value=target):
            await bot.leaveguild.callback(ctx, 1)
        target.leave.assert_awaited_once()
        ctx.send.assert_awaited_once()
        self.assertIn('is leaving', ctx.send.await_args.args[0])

    async def test_leave_other_guild_confirms_success(self):
        ctx, target = context(guild(2)), guild()
        with patch.object(bot.bot, 'get_guild', return_value=target):
            await bot.leaveguild.callback(ctx, 1)
        self.assertEqual(ctx.send.await_count, 2)
        self.assertIn('✅ Duellium left', ctx.send.await_args.args[0])

    async def test_unknown_guild_and_http_failure_return_clean_errors(self):
        ctx = context()
        with patch.object(bot.bot, 'get_guild', return_value=None):
            await bot.leaveguild.callback(ctx, 123)
        ctx.send.assert_awaited_once_with('Server not found or Duellium is not currently in that server.')
        target = guild()
        target.leave.side_effect = discord.HTTPException(SimpleNamespace(status=403, reason='Forbidden'), 'private error')
        with patch.object(bot.bot, 'get_guild', return_value=target), self.assertLogs(bot.logger, level='ERROR'):
            await bot.leaveguild.callback(ctx, 1)
        self.assertEqual(ctx.send.await_args.args[0], 'Duellium could not leave that server. Please try again.')

    async def test_leave_does_not_write_or_delete_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'players.db'
            with patch.object(bot.database, 'DATABASE_FILE', str(path)):
                bot.database.init_database()
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                with patch.object(bot.bot, 'get_guild', return_value=guild()), patch.object(bot.database, 'connection', side_effect=AssertionError('No database access allowed')):
                    await bot.leaveguild.callback(context(), 1)
                self.assertTrue(path.exists())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_ignore_entries_and_dodge_registration(self):
        entries = Path('.gitignore').read_text().splitlines()
        for entry in ('players.db','players.db-wal','players.db-shm','backups/'):
            self.assertIn(entry, entries)
        self.assertEqual(bot.bot.get_command('dodge').aliases, ['esquive'])
        self.assertIs(bot.bot.get_command('esquive'), bot.bot.get_command('dodge'))
