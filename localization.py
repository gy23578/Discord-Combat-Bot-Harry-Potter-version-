"""Guild language resolution and shared command/event presentation contexts."""
from functools import wraps
import database
from translations import CURRENT_LANGUAGE, DEFAULT_LANGUAGE, COMMAND_ALIASES, tr

guild_language_cache = {}
_cache_database = None


def clear_language_cache():
    global _cache_database
    guild_language_cache.clear()
    _cache_database = database.DATABASE_FILE


def language_for_guild(guild_id):
    global _cache_database
    if guild_id is None:
        return DEFAULT_LANGUAGE
    if _cache_database != database.DATABASE_FILE:
        clear_language_cache()
    if guild_id not in guild_language_cache:
        guild_language_cache[guild_id] = database.get_guild_language(guild_id)
    return guild_language_cache[guild_id]


def language_for_context(ctx):
    guild = getattr(ctx, 'guild', None)
    if guild is None:
        guild = getattr(getattr(ctx, 'channel', None), 'guild', None)
    return language_for_guild(getattr(guild, 'id', None))


def localized_context(callback):
    @wraps(callback)
    async def localized(ctx, *args, **kwargs):
        token = CURRENT_LANGUAGE.set(language_for_context(ctx))
        try:
            return await callback(ctx, *args, **kwargs)
        finally:
            CURRENT_LANGUAGE.reset(token)
    return localized


def localized_session(callback):
    @wraps(callback)
    async def localized(session, *args, **kwargs):
        token = CURRENT_LANGUAGE.set(language_for_guild(session.get('guild_id')))
        try:
            return await callback(session, *args, **kwargs)
        finally:
            CURRENT_LANGUAGE.reset(token)
    return localized


def install_command_localization(bot):
    for command in bot.walk_commands():
        if not any(getattr(check, "__name__", "") == "owner_check" for check in command.checks):
            command.callback = localized_context(command.callback)
    aliases = {}
    for language, mapping in COMMAND_ALIASES.items():
        for canonical, alias in mapping.items():
            command = bot.get_command(canonical)
            if command is None or bot.get_command(alias) is not None:
                raise ValueError(f'Invalid localized alias registration: {alias}')
            command.aliases.append(alias)
            bot.all_commands[alias] = command
            aliases[alias] = (language, canonical)

    @bot.check
    @localized_context
    async def localized_alias_check(ctx):
        alias = aliases.get(getattr(ctx, 'invoked_with', None))
        if alias is not None and CURRENT_LANGUAGE.get() != alias[0]:
            await ctx.send(tr('alias.unavailable', command=alias[1]))
            return False
        return True
    return localized_alias_check
