"""Guild-local presentation with canonical global game data preserved."""
import ast
import copy
import json
import string
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
import localization
import translations
import test_bot as game
import test_training as training_game
from quiz_questions import QUIZ_QUESTIONS
from spells import SPELLS


def context(uid=1, guild_id=100, invoked=None, manage=False, admin=False):
    ctx = game.context(uid)
    ctx.guild = None if guild_id is None else SimpleNamespace(id=guild_id, get_member=lambda uid: None, fetch_member=AsyncMock(side_effect=game.user))
    ctx.author.guild_permissions = SimpleNamespace(manage_guild=manage, administrator=admin)
    ctx.invoked_with = invoked
    return ctx


class LocalizationTests(unittest.IsolatedAsyncioTestCase):
    setUp = game.GameTests.setUp
    tearDown = game.GameTests.tearDown
    ready_player = game.GameTests.ready_player
    session = game.GameTests.session

    async def test_defaults_persistence_restart_and_server_isolation(self):
        self.assertEqual(database.get_guild_language(100), 'en')
        ctx = context(manage=True)
        await bot.setlanguage.callback(ctx, 'fr')
        self.assertEqual(database.get_guild_language(100), 'fr')
        self.assertIn('utilise maintenant le français', ctx.send.await_args.args[0])
        localization.clear_language_cache()
        database.init_database()
        self.assertEqual(localization.language_for_context(ctx), 'fr')
        self.assertEqual(localization.language_for_context(context(guild_id=200)), 'en')
        self.assertEqual(localization.language_for_context(context(guild_id=None)), 'en')
        await bot.language.callback(ctx)
        self.assertEqual(ctx.send.await_args.args[0], '🌐 Langue du serveur : Français')

    async def test_change_language_never_changes_profiles_or_rankings_or_house_points(self):
        player = self.ready_player(1)
        bot.persist_player(player)
        snapshot = copy.deepcopy(database.load_player(1))
        with database.connection() as db:
            db.execute('INSERT INTO local_player_points VALUES (100, 1, 23)')
            db.execute("INSERT INTO local_house_points VALUES (100, 'gryffindor', 40)")
        await bot.setlanguage.callback(context(manage=True), 'français')
        self.assertEqual(database.load_player(1), snapshot)
        self.assertNotIn('language', player)
        self.assertEqual(database.player_leaderboard(1)[1], (1, 23))
        self.assertIn(('gryffindor', 40), database.house_leaderboard(100))
        # Same global profile, different presentation guild.
        self.assertIs(bot.get_player(context(guild_id=200).author), player)

    async def test_setting_validation_permissions_and_french_inputs(self):
        for value in ('en', 'english', 'anglais', 'fr', 'french', 'français', 'francais'):
            ctx = context(manage=True)
            await bot.setlanguage.callback(ctx, value)
            self.assertEqual(database.get_guild_language(100), translations.LANGUAGE_INPUTS[value])
        ctx = context()
        with patch.object(bot, 'OWNER_ID', 99):
            self.assertFalse(await bot.language_permission(ctx))
            self.assertTrue(await bot.language_permission(context(manage=True)))
            self.assertTrue(await bot.language_permission(context(admin=True)))
            self.assertTrue(await bot.language_permission(context(uid=99)))
        await bot.setlanguage.callback(ctx, 'invalid')
        self.assertEqual(database.get_guild_language(100), 'fr')
        with self.assertRaises(ValueError):
            database.set_guild_language(100, 'unsupported')

    async def test_profile_and_spellbook_are_localized(self):
        self.ready_player(1)
        for guild_id, language in ((100, 'en'), (200, 'fr')):
            database.set_guild_language(guild_id, language)
            ctx = context(guild_id=guild_id)
            await bot.profile.callback(ctx)
            message = ctx.send.await_args.args[0]
            if language == 'en':
                self.assertIn('House: **Gryffindor**', message)
                self.assertIn('Magic Power', message)
            else:
                self.assertIn('Maison : **Gryffondor**', message)
                self.assertIn('Puissance magique', message)
                self.assertIn('Points de talent', message)
                self.assertNotIn('Spell Level', message)
            await bot.spellbook.callback(ctx)
            self.assertIn('SPELLBOOK' if language == 'en' else 'LISTE DES SORTS', ctx.send.await_args.args[0])

    async def test_local_global_and_house_boards_are_localized_without_score_changes(self):
        self.ready_player(1)
        database.set_guild_language(100, 'fr')
        with database.connection() as db:
            db.execute('INSERT INTO local_player_points VALUES (100, 1, 23)')
        ctx = context()
        await bot.leaderboard.callback(ctx)
        self.assertIn('CLASSEMENT DU SERVEUR', ctx.send.await_args.args[0])
        await bot.globalleaderboard.callback(ctx)
        self.assertIn('CLASSEMENT GLOBAL DUELLIUM', ctx.send.await_args.args[0])
        await bot.houseleaderboard.callback(ctx)
        for name in ('Gryffondor', 'Serpentard', 'Serdaigle', 'Poufsouffle'):
            self.assertIn(name, ctx.send.await_args.args[0])
        self.assertEqual(database.player_leaderboard(1)[1], (1, 23))

    async def test_aliases_share_callbacks_and_are_language_restricted(self):
        database.set_guild_language(100, 'fr')
        for canonical, alias in translations.COMMAND_ALIASES['fr'].items():
            command = bot.bot.get_command(canonical)
            self.assertIs(bot.bot.get_command(alias), command)
            ctx = context(invoked=alias)
            ctx.command = command
            self.assertTrue(await bot.localized_alias_check(ctx))
            ctx.guild.id = 200
            self.assertFalse(await bot.localized_alias_check(ctx))
            ctx.invoked_with = canonical
            self.assertTrue(await bot.localized_alias_check(ctx))
        ctx = context(invoked='esquive')
        ctx.command = bot.bot.get_command('esquive')
        self.assertTrue(await bot.localized_alias_check(ctx))
        await ctx.command.callback(ctx)
        self.assertIn('aucune attaque à esquiver', ctx.send.await_args.args[0])
        english = context(guild_id=200, invoked='esquive')
        english.command = ctx.command
        executed = False
        if await bot.localized_alias_check(english):
            executed = True
            await english.command.callback(english)
        self.assertFalse(executed)
        for guild_id in (100, 200):
            ctx = context(guild_id=guild_id, invoked='dodge')
            ctx.command = bot.bot.get_command('dodge')
            self.assertTrue(await bot.localized_alias_check(ctx))
            await ctx.command.callback(ctx)

    async def test_french_house_and_stat_inputs_keep_canonical_keys(self):
        database.set_guild_language(100, 'fr')
        for uid, (french, english) in enumerate(translations.INPUT_ALIASES['fr']['house'].items(), 1):
            ctx = context(uid)
            await bot.profile.callback(ctx)
            await bot.house.callback(ctx, french)
            self.assertEqual(database.load_player(uid)['house'], english)
        player = bot.get_player(context(1).author)
        before = player['stats']['magic_power']
        await bot.train.callback(context(1), 'puissancemagique')
        self.assertEqual(player['stats']['magic_power'], before + 1)
        self.assertNotIn('puissancemagique', player['stats'])

    async def test_quiz_translation_keeps_question_selection_and_answer_identity(self):
        snapshot = copy.deepcopy(QUIZ_QUESTIONS)
        database.set_guild_language(100, 'fr')
        for original in QUIZ_QUESTIONS:
            questions = [{"question": original['question'], 'answers': dict(zip('ABCD', original['answers'])), 'correct_letter': 'ABCD'[original['answers'].index(original['correct'])]}]
            trial = {'display_name': 'Incendio', 'current_question': 0, 'questions': questions}
            before = copy.deepcopy(trial)
            ctx = context()
            await bot.send_learning_question(ctx, trial)
            self.assertEqual(trial, before)
            rendered = ctx.send.await_args.args[0]
            self.assertIn('Épreuve de connaissances', rendered)
            self.assertIn('!reponse A', rendered)
            self.assertNotIn(original['question'], rendered)
            token = translations.CURRENT_LANGUAGE.set('fr')
            try:
                correct = translations.quiz_display(original['correct'])
            finally:
                translations.CURRENT_LANGUAGE.reset(token)
            self.assertIn(f"**{questions[0]['correct_letter']}.** {correct}", rendered)
        self.assertEqual(QUIZ_QUESTIONS, snapshot)

    async def test_french_learning_success_preserves_scores_and_ravenclaw_bonus(self):
        player = self.ready_player(1)
        player['house'] = 'ravenclaw'
        player['level'] = 5
        player['spell_levels']['confringo'] = 3
        player['spell_hits']['confringo'] = 5
        database.set_guild_language(100, 'fr')
        ctx = context()
        await bot.learn.callback(ctx, spell_name='incendio')
        trial = bot.active_learning_trials[1]
        self.assertEqual(len(trial['questions']), 5)
        self.assertEqual(trial['required_score'], 3)
        for question in list(trial['questions']):
            await bot.answer.callback(ctx, question['correct_letter'])
        self.assertIn('RÉUSSITE', ctx.send.await_args.args[0])
        self.assertIn('incendio', player['learned_spells'])

    async def test_french_spell_requirements_descriptions_and_canonical_names(self):
        self.ready_player(1)
        database.set_guild_language(100, 'fr')
        for spell in SPELLS:
            ctx = context()
            await bot.spell_info.callback(ctx, spell_name=spell)
            message = ctx.send.await_args.args[0]
            self.assertIn(SPELLS[spell]['display_name'].upper(), message)
            self.assertIn('Difficulté', message)
            self.assertNotIn('Requirements', message)
            self.assertNotIn('Spell Level', message)
            self.assertNotIn(SPELLS[spell]['description'], message)

    async def test_french_duel_end_rewards_and_local_only_points(self):
        self.session()
        bot.duel_sessions[1].update(guild_id=100, mode='normal')
        database.set_guild_language(100, 'fr')
        for player in bot.players.values():
            bot.persist_player(player)
        bot.players[2]['hp'] = 0
        ctx = context()
        await bot.check_duel_end(ctx, game.user(2), 'original')
        message = ctx.send.await_args.args[0]
        self.assertIn('DUEL TERMINÉ', message)
        self.assertIn('POINTS DU SERVEUR', message)
        self.assertIn('Gryffondor +10 points de maison', message)
        self.assertNotIn('GLOBAL', message)
        self.assertEqual(database.player_leaderboard(1, 100)[1], (1, 10))
        self.assertEqual(database.load_player(1)['xp'], 50)

    async def test_inactivity_event_uses_its_session_guild_language(self):
        self.session()
        session = bot.duel_sessions[1]
        session.update(guild_id=100, mode='normal', last_activity=0)
        database.set_guild_language(100, 'fr')
        channel = context()
        with patch.object(bot, 'duel_clock', return_value=300):
            await bot.expire_inactive_duel(session, channel)
        self.assertIn('DUEL TERMINÉ POUR INACTIVITÉ', channel.send.await_args.args[0])
        self.assertEqual(database.player_leaderboard(1), ([], None))

    async def test_cache_reads_once_and_updates_immediately(self):
        localization.clear_language_cache()
        with patch.object(database, 'get_guild_language', wraps=database.get_guild_language) as query:
            for _ in range(10):
                self.assertEqual(localization.language_for_context(context()), 'en')
            self.assertEqual(query.call_count, 1)
            await bot.setlanguage.callback(context(manage=True), 'fr')
            self.assertEqual(localization.language_for_context(context()), 'fr')
            self.assertEqual(query.call_count, 1)

    async def test_localized_help_hides_owner_commands_and_dm_rules_stay(self):
        from discord.ext import commands
        database.set_guild_language(100, 'fr')
        ctx = context()
        await bot.localized_help.callback(ctx)
        text = ctx.send.await_args.args[0]
        self.assertIn('!esquive', text)
        self.assertIn('!classement', text)
        self.assertNotIn('!leaveguild', text)
        with patch.object(bot, 'OWNER_ID', 99):
            await bot.localized_help.callback(ctx, command_name='leaveguild')
        self.assertIn('Commande inconnue', ctx.send.await_args.args[0])
        for name in ('language', 'setlanguage', 'duel', 'dodge', 'leaderboard', 'houseleaderboard'):
            dm = context(guild_id=None)
            dm.command = bot.bot.get_command(name)
            with self.assertRaises(commands.NoPrivateMessage):
                await bot.guild_context(dm)
        for name in ('servers', 'leaveguild', 'help'):
            dm.command = bot.bot.get_command(name)
            self.assertTrue(await bot.guild_context(dm))

    async def test_parallel_guild_contexts_do_not_leak_languages(self):
        import asyncio
        database.set_guild_language(200, 'fr')
        @localization.localized_context
        async def render(ctx):
            await asyncio.sleep(0)
            return translations.tr('stat.speed')
        results = await asyncio.gather(render(context(guild_id=100)), render(context(guild_id=200)))
        self.assertEqual(results, ['Speed', 'Vitesse'])
        self.assertEqual(translations.CURRENT_LANGUAGE.get(), 'en')

    async def test_english_canonical_commands_work_for_french_profiles(self):
        database.set_guild_language(100, 'fr')
        ctx = context()
        for canonical in translations.COMMAND_ALIASES['fr']:
            ctx.command = bot.bot.get_command(canonical)
            ctx.invoked_with = canonical
            self.assertTrue(await bot.localized_alias_check(ctx))
        await bot.profile.callback(ctx)
        await bot.house.callback(ctx, 'gryffindor')
        self.assertEqual(database.load_player(1)['house'], 'gryffindor')
        await bot.train.callback(ctx, 'speed')
        self.assertIn('Vitesse', ctx.send.await_args.args[0])
        await bot.localized_help.callback(ctx, command_name='spell')
        self.assertIn('!sort', ctx.send.await_args.args[0])
        self.assertIn('nom_du_sort', ctx.send.await_args.args[0])

    async def test_guild_departure_keeps_language_setting(self):
        database.set_guild_language(100, 'fr')
        await bot.on_guild_remove(SimpleNamespace(id=100))
        self.assertEqual(database.get_guild_language(100), 'fr')

    def test_public_gameplay_has_no_literal_english_send_calls(self):
        source = Path('bot.py').read_text()
        tree = ast.parse(source)
        owner_commands = {command.callback.__name__ for command in bot.bot.walk_commands()
                          if bot.owner_check in command.checks}
        for function in tree.body:
            if not isinstance(function, ast.AsyncFunctionDef) or function.name in owner_commands:
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'send' and node.args:
                    self.assertNotIsInstance(node.args[0], (ast.Constant, ast.JoinedStr), function.name)

    async def test_localized_user_errors_are_generic(self):
        from discord.ext import commands
        database.set_guild_language(100, 'fr')
        ctx = context()
        ctx.command = bot.bot.get_command('duel')
        await bot.on_command_error(ctx, commands.MemberNotFound('private'))
        self.assertIn('Joueur introuvable', ctx.send.await_args.args[0])
        with self.assertLogs(bot.logger, level='ERROR'):
            await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError('private/path')))
        self.assertNotIn('private/path', ctx.send.await_args.args[0])
        self.assertIn('n’a pas pu être exécutée', ctx.send.await_args.args[0])

    def test_catalogs_cover_every_key_and_placeholder_identity(self):
        self.assertEqual(set(translations.TRANSLATIONS['en']), set(translations.TRANSLATIONS['fr']))
        fields = lambda text: {field for _, field, _, _ in string.Formatter().parse(text) if field is not None}
        for key in translations.TRANSLATIONS['en']:
            self.assertEqual(fields(translations.TRANSLATIONS['en'][key]), fields(translations.TRANSLATIONS['fr'][key]), key)
        with patch.dict(translations.TRANSLATIONS['fr'], {}, clear=True):
            self.assertEqual(translations.translate('fr', 'stat.speed'), 'Speed')
        self.assertEqual(translations.translate('unknown', 'stat.speed'), 'Speed')
        self.assertIn('{not_interpreted}', translations.translate('fr', 'training.hits', spell='{not_interpreted}'))


class FrenchTrainingTests(unittest.IsolatedAsyncioTestCase):
    setUp = training_game.TrainingTests.setUp
    tearDown = training_game.TrainingTests.tearDown
    ready_player = training_game.TrainingTests.ready_player
    fetch = training_game.TrainingTests.fetch

    async def test_french_training_roles_objectives_and_completion(self):
        database.set_guild_language(100, 'fr')
        ctx = context(1)
        await bot.teach.callback(ctx, game.user(2), spell_name='diffindo')
        self.assertIn('Demande d’entraînement', ctx.send.await_args.args[0])
        await bot.accept.callback(context(2))
        session = bot.duel_sessions[1]
        message = bot.training_progress_message(session)
        self.assertIn('Professeur', message)
        self.assertIn('Élève', message)
        self.assertIn('Attaques réussies', message)
        for objective in session['objectives'].values():
            objective.update(teacher=objective['target'], student=1)
        student = context(2)
        await bot.report_training_progress(student, 2, session['id'])
        self.assertIn('ENTRAÎNEMENT TERMINÉ', student.send.await_args.args[0])
        self.assertEqual(database.player_leaderboard(2), ([], None))
