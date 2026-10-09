import asyncio
import os
import random
import time
import logging
import uuid
import signal
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import database
from operations import configure_logging, ProcessLock
from combat import MODELED_SPELLS, CURSE_MIN_ROLLS, PROTEGO_DAMAGE_PERCENT, roll_offensive_effect, choose_forced_spell
from database import init_database, save_player, load_player

import discord
from discord.ext import commands
from dotenv import load_dotenv

from quiz_questions import QUIZ_QUESTIONS
from spells import QUIZ_RULES, SPELLS, STARTING_SPELLS


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv(Path(__file__).resolve().with_name(".env"))
TOKEN = os.getenv("DISCORD_TOKEN")
try:
    OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
except ValueError:
    OWNER_ID = 0

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, allowed_mentions=discord.AllowedMentions.none())
STARTED_AT = time.monotonic()
combat_tasks = {}
maintenance_tasks = set()


# =========================================================
# HOUSE BASE STATS
# =========================================================

HOUSE_STATS = {
    "gryffindor": {
        "endurance": 18,
        "magic_power": 18,
        "speed": 16,
        "agility": 16,
    },
    "slytherin": {
        "endurance": 14,
        "magic_power": 19,
        "speed": 19,
        "agility": 16,
    },
    "ravenclaw": {
        "endurance": 13,
        "magic_power": 24,
        "speed": 15,
        "agility": 16,
    },
    "hufflepuff": {
        "endurance": 24,
        "magic_power": 15,
        "speed": 14,
        "agility": 15,
    },
}

HOUSE_NAMES = {
    "gryffindor": "Gryffindor",
    "slytherin": "Slytherin",
    "ravenclaw": "Ravenclaw",
    "hufflepuff": "Hufflepuff",
}

STAT_DISPLAY_NAMES = {
    "endurance": "Endurance",
    "magic_power": "Magic Power",
    "speed": "Speed",
    "agility": "Agility",
}

COMBAT_STAT_DISPLAY_NAMES = {
    "successful_attacks": "Successful Attacks",
    "successful_dodges": "Successful Dodges",
    "successful_protegos": "Successful Protegos",
    "successful_counters": "Successful Counters",
    "successful_control_spells": "Successful Control Spells",
}



# =========================================================
# SPELL SPEED
# Higher value = harder to Dodge.
# Player Speed and Spell Speed are both used.
# =========================================================

SPELL_SPEEDS = {
    "confringo": 6,
    "protego": 0,
    "expelliarmus": 9,
    "stupefy": 8,
    "incendio": 9,
    "diffindo": 8,
    "depulso": 7,
    "glacius": 6,
    "petrificustotalus": 5,
    "bombarda": 4,
    "endoloris": 7,
    "impero": 7,
    "sectumsempra": 6,
    "avadakedavra": 8,
}

# =========================================================
# GAME DATA
# Permanent progression uses SQLite; all live sessions are temporary.
# =========================================================

players = {}
duel_requests = {}
active_duels = {}
pending_attacks = {}
status_effects = {}
active_casts = set()
offensive_cooldowns = {}
active_learning_trials = {}
teaching_requests = {}
duel_sessions = {}
DUEL_REQUEST_TTL = 120
logger = logging.getLogger(__name__)
runtime_restored = False
SPELL_RENAMES = {"doloris": "endoloris", "imperio": "impero"}


# =========================================================
# HELPERS
# =========================================================



def persist_runtime():
    # Live combat and quiz state are intentionally memory-only.
    pass


def restore_runtime():
    """Discard legacy live snapshots; never resume combat or penalize downtime."""
    database.clear_runtime()
    for handle in duel_inactivity_timers.values():
        handle.cancel()
    duel_inactivity_timers.clear()
    for session_id in list(combat_tasks):
        cancel_combat_tasks(session_id)
    for state in (duel_requests, active_duels, duel_sessions, pending_attacks,
                  status_effects, active_casts, offensive_cooldowns,
                  active_learning_trials, teaching_requests):
        state.clear()
    logger.info("Temporary sessions expired; permanent progression retained")
    return []


def cancel_combat_tasks(session_id):
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    for task in combat_tasks.pop(session_id, set()):
        if task is not current:
            task.cancel()
    stop_duel_inactivity_timer(session_id)


def spawn_combat_task(session_id, coroutine):
    if not any(s["id"] == session_id and not s.get("ending") for s in duel_sessions.values()):
        coroutine.close()
        return None
    task = asyncio.create_task(coroutine)
    combat_tasks.setdefault(session_id, set()).add(task)
    def completed(done):
        tasks = combat_tasks.get(session_id)
        if tasks is not None:
            tasks.discard(done)
            if not tasks:
                combat_tasks.pop(session_id, None)
        if not done.cancelled() and done.exception() is not None:
            error = done.exception()
            logger.error("Combat task failed in session %s", session_id,
                         exc_info=(type(error), error, error.__traceback__))
            session = next((s for s in duel_sessions.values() if s["id"] == session_id), None)
            if session is not None:
                recovery = asyncio.create_task(abort_session(session, "An internal error interrupted combat"))
                maintenance_tasks.add(recovery)
                recovery.add_done_callback(maintenance_done)
    task.add_done_callback(completed)
    return task


def maintenance_done(task):
    maintenance_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        error = task.exception()
        logger.error("Maintenance task failed", exc_info=(type(error), error, error.__traceback__))


async def abort_session(session, reason):
    if session.get("ending"):
        return
    if session.get("mode") == "training":
        await end_training(None, session, reason)
        return
    session["ending"] = True
    cancel_combat_tasks(session["id"])
    profiles = []
    participants = duel_participant_ids(session)
    for uid in participants:
        player = players.get(uid)
        if player is not None:
            player["hp"] = player["max_hp"]
            profiles.append(player)
        for state in (active_duels, duel_sessions, pending_attacks, status_effects, offensive_cooldowns):
            state.pop(uid, None)
        active_casts.discard(uid)
    clear_participant_requests(set(participants))
    database.save_players(profiles)
    logger.warning("Duel %s aborted without rewards: %s", session["id"], reason)


def is_owner(user):
    return user.id == OWNER_ID


def normalize_spell_name(name: str) -> str:
    return (
        name.lower()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
    )


def stars(difficulty: int) -> str:
    if difficulty <= 0:
        return "Starting Spell"
    return "★" * difficulty + "☆" * (5 - difficulty)

def persist_player(player):
    session = training_session(player["user_id"])
    if session is not None:
        # Training HP is temporary. A restart must retain the pre-training HP.
        player = dict(player, hp=session["original_hp"][player["user_id"]])
    save_player(player["user_id"], player)



def get_player(user):

    if user.id not in players:

        saved_player = load_player(
            user.id
        )

        if saved_player is not None:

            players[user.id] = (
                saved_player
            )

        else:

            players[user.id] = {

                "user_id": user.id,

                "name": user.display_name,

                "profile_started": False,
                "house": None,
                "ready": False,

                "stats": {
                    "endurance": 0,
                    "magic_power": 0,
                    "speed": 0,
                    "agility": 0,
                },

                "talent_points": 0,

                "hp": 100,
                "max_hp": 100,

                "level": 1,
                "xp": 0,

                "learned_spells": set(
                    STARTING_SPELLS
                ),

                "spell_levels": {
                    spell_key:
                        1
                        if spell_key in STARTING_SPELLS
                        else 0

                    for spell_key in SPELLS
                },

                "spell_xp": {
                    spell_key: 0
                    for spell_key in SPELLS
                },

                "duel_wins": 0,
                "duel_losses": 0,
                "duels_completed": 0,

                "duels_since_avada": 5,

                "combat_stats": {
                    "successful_attacks": 0,
                    "successful_dodges": 0,
                    "successful_protegos": 0,
                    "successful_counters": 0,
                    "successful_control_spells": 0,
                },

                "spell_hits": {
                    spell_key: 0
                    for spell_key in SPELLS
                },
            }

            save_player(
                user.id,
                players[user.id]
            )

    player = players[user.id]
    before_migration = repr(player)
    player["name"] = user.display_name

    # Lightweight migration for older saved profiles.
    player.setdefault("profile_started", False)
    player.setdefault("house", None)
    player.setdefault("ready", False)
    player.setdefault("talent_points", 0)
    player.setdefault("practical_training", {})
    player.setdefault("hp", 100)
    player.setdefault("max_hp", 100)
    player.setdefault("level", 1)
    player.setdefault("xp", 0)
    player.setdefault("duel_wins", 0)
    player.setdefault("duel_losses", 0)
    player.setdefault("duels_completed", 0)
    player.setdefault("duels_since_avada", 5)

    player.setdefault("stats", {})
    for stat_name in ("endurance", "magic_power", "speed", "agility"):
        player["stats"].setdefault(stat_name, 0)

    player.setdefault("learned_spells", set())
    if not isinstance(player["learned_spells"], set):
        player["learned_spells"] = set(player["learned_spells"])

    player.setdefault("spell_levels", {})
    player.setdefault("spell_xp", {})
    player.setdefault("spell_hits", {})

    # Preserve progression when loading profiles saved under former names.
    for old_name, new_name in SPELL_RENAMES.items():
        if old_name in player["learned_spells"]:
            player["learned_spells"].remove(old_name)
            player["learned_spells"].add(new_name)
        for field in ("spell_levels", "spell_xp", "spell_hits"):
            if old_name in player[field]:
                player[field][new_name] = max(
                    player[field].get(new_name, 0), player[field].pop(old_name)
                )

    # Prune retired spell data without touching unrelated player progression.
    player["learned_spells"].intersection_update(SPELLS)
    for field in ("spell_levels", "spell_xp", "spell_hits"):
        for spell_key in set(player[field]) - SPELLS.keys():
            player[field].pop(spell_key)

    for spell_key in SPELLS:
        player["spell_levels"].setdefault(
            spell_key,
            1 if spell_key in STARTING_SPELLS else 0
        )
        player["spell_xp"].setdefault(spell_key, 0)
        player["spell_hits"].setdefault(spell_key, 0)

    player.setdefault("combat_stats", {})
    for stat_name in COMBAT_STAT_DISPLAY_NAMES:
        player["combat_stats"].setdefault(stat_name, 0)

    for spell_name in STARTING_SPELLS:

        if spell_name not in player["learned_spells"]:
            player["learned_spells"].add(
                spell_name
            )

            player["spell_levels"][spell_name] = max(
                1,
                player["spell_levels"].get(
                    spell_name,
                    0
                )
            )

            player["spell_xp"].setdefault(
                spell_name,
                0
            )

    if repr(player) != before_migration:
        persist_player(player)
    return player






def knows_spell(player, spell_name):
    return spell_name in player["learned_spells"]


# =========================================================
# HP / LEVEL / XP
# =========================================================

def calculate_max_hp(player):
    endurance = player["stats"]["endurance"]
    return max(80, 100 + (endurance - 15) * 2)


def update_max_hp(player):
    player["max_hp"] = calculate_max_hp(player)
    if player["hp"] > player["max_hp"]:
        player["hp"] = player["max_hp"]


def update_player_level(player):
    player["level"] = 1 + player["xp"] // 100


def talent_points_for_level(level):
    return 3 if level >= 5 else 2


def gain_player_xp(player, amount, persist=True):
    old_level = player["level"]
    player["xp"] = max(0, player["xp"] + amount)
    update_player_level(player)
    new_level = player["level"]

    if new_level > old_level:
        for reached_level in range(old_level + 1, new_level + 1):
            player["talent_points"] += talent_points_for_level(reached_level)

    if persist:
        persist_player(player)
    return new_level > old_level


def gain_spell_xp(player, spell_name, amount):
    if spell_name not in player["spell_xp"]:
        return

    player["spell_xp"][spell_name] += amount
    new_level = 1 + player["spell_xp"][spell_name] // 100
    old_level = player["spell_levels"][spell_name]
    player["spell_levels"][spell_name] = max(old_level, new_level)
    persist_player(player)


# =========================================================
# UNARMED STATUS
# =========================================================

def is_unarmed(user_id):
    if user_id not in status_effects:
        return False
    return time.time() < status_effects[user_id].get("unarmed_until", 0)


def remaining_unarmed_time(user_id):
    if not is_unarmed(user_id):
        return 0
    return max(0, status_effects[user_id]["unarmed_until"] - time.time())




# =========================================================
# STUNNED STATUS
# =========================================================

def is_stunned(user_id):
    if user_id not in status_effects:
        return False
    return time.time() < status_effects[user_id].get("stunned_until", 0)


def remaining_stun_time(user_id):
    if not is_stunned(user_id):
        return 0
    return max(0, status_effects[user_id]["stunned_until"] - time.time())

# =========================================================
# COOLDOWN
# =========================================================

def is_on_cooldown(user_id):
    if user_id not in offensive_cooldowns:
        return False
    return time.time() < offensive_cooldowns[user_id]


def remaining_cooldown(user_id):
    if not is_on_cooldown(user_id):
        return 0
    return max(0, offensive_cooldowns[user_id] - time.time())


def start_cooldown(user_id, duration):
    offensive_cooldowns[user_id] = time.time() + duration


def calculate_cooldown(player, base_cooldown):
    speed = effective_speed(player)
    modifier = (speed - 15) * 0.12
    cooldown = base_cooldown - modifier

    if base_cooldown <= 6:
        return max(4.5, cooldown)
    return max(10, cooldown)


# =========================================================
# COMBAT CALCULATIONS
# =========================================================

def calculate_spell_power(player, spell_name):
    magic_power = player["stats"]["magic_power"]
    spell_level = player["spell_levels"][spell_name]
    return random.randint(1, 20) + magic_power + player["level"] * 2 + spell_level * 5


def calculate_protego_power(player):
    endurance = player["stats"]["endurance"]
    magic_power = player["stats"]["magic_power"]
    spell_level = player["spell_levels"]["protego"]
    defensive_stat = (endurance + magic_power) / 2

    return (
        random.randint(1, 20)
        + int(defensive_stat)
        + player["level"] * 2
        + spell_level * 5
    )


def calculate_spell_accuracy(player, spell_name):
    player_speed = effective_speed(player)
    spell_level = player["spell_levels"][spell_name]
    spell_speed = SPELL_SPEEDS.get(spell_name, 6)

    return (
        random.randint(1, 20)
        + player_speed
        + spell_speed
        + spell_level * 3
    )


def calculate_dodge_power(player):
    agility = player["stats"]["agility"]
    return random.randint(1, 20) + agility + player["level"] * 2


def effective_speed(player):
    effects = status_effects.get(player["user_id"], {})
    penalty = effects.get("speed_penalty", 0) if time.time() < effects.get("slowed_until", 0) else 0
    return max(0, player["stats"]["speed"] - penalty)


def session_is_current(user_id, session_id):
    session = duel_sessions.get(user_id, {})
    return session.get("id") == session_id and user_id in active_duels and not session.get("ending")


def in_duel_channel(ctx):
    session = duel_sessions.get(ctx.author.id)
    return session is None or session["channel_id"] == ctx.channel.id


@bot.check
async def check_duel_channel(ctx):
    if getattr(getattr(ctx, "command", None), "name", None) in {"canceltraining", "botstatus", "backupdb"}:
        return True
    if not in_duel_channel(ctx):
        await ctx.send("Continue your Duel in the channel where it was accepted.")
        return False
    return True


def finish_attack(attack):
    attacker_id = attack["attacker_id"]
    if not any(pending["attacker_id"] == attacker_id for pending in pending_attacks.values()):
        active_casts.discard(attacker_id)
    start_cooldown(attacker_id, attack["cooldown"])


# =========================================================
# NORMAL DUEL INACTIVITY
# One deadline and scheduled callback per shared duel session.
# =========================================================

DUEL_INACTIVITY_SECONDS = 5 * 60
DUEL_INACTIVITY_XP_PENALTY = 10
duel_inactivity_timers = {}


def duel_clock():
    return time.monotonic()


def stop_duel_inactivity_timer(session_id):
    handle = duel_inactivity_timers.pop(session_id, None)
    if handle is not None:
        handle.cancel()


def duel_participant_ids(session):
    return [uid for uid, current in duel_sessions.items()
            if current["id"] == session["id"] and uid in active_duels]


def schedule_duel_inactivity(session):
    stop_duel_inactivity_timer(session["id"])
    if session.get("mode") == "training" or session.get("ending") or len(duel_participant_ids(session)) != 2:
        return
    session.setdefault("last_activity", duel_clock())
    delay = max(0, session["last_activity"] + DUEL_INACTIVITY_SECONDS - duel_clock())
    handle = asyncio.get_running_loop().call_later(delay, fire_duel_inactivity, session)
    duel_inactivity_timers[session["id"]] = handle


def fire_duel_inactivity(session):
    # Old callbacks only recheck the deadline; they cannot penalize a refreshed duel.
    spawn_combat_task(session["id"], expire_inactive_duel(session))


def note_duel_activity(user_id, session_id=None, channel_id=None):
    """Call only after a player command has passed its combat validity checks."""
    session = duel_sessions.get(user_id)
    if session is None or user_id not in active_duels or session.get("ending"):
        return False
    if session_id is not None and session["id"] != session_id:
        return False
    if channel_id is not None and session["channel_id"] != channel_id:
        return False
    if session.get("mode") == "training":
        return True
    now = duel_clock()
    session.setdefault("last_activity", now)
    if now >= session["last_activity"] + DUEL_INACTIVITY_SECONDS:
        # The deadline has won the race. Reject the action and schedule cleanup now.
        schedule_duel_inactivity(session)
        return False
    session["last_activity"] = now
    schedule_duel_inactivity(session)
    persist_runtime()
    return True


async def expire_inactive_duel(session, channel=None):
    if session.get("mode") == "training" or session.get("ending"):
        return False
    participants = duel_participant_ids(session)
    if len(participants) != 2:
        return False
    session.setdefault("last_activity", duel_clock())
    if duel_clock() < session["last_activity"] + DUEL_INACTIVITY_SECONDS:
        schedule_duel_inactivity(session)
        return False

    # No awaits between the final deadline check, claiming the session, and cleanup.
    # A simultaneous action either refreshed the deadline already or sees an ended duel.
    session["ending"] = True
    cancel_combat_tasks(session["id"])
    names = []
    changed = []
    for uid in participants:
        player = players.get(uid)
        if player is None:
            player = load_player(uid)
            if player is not None:
                players[uid] = player
        names.append(player["name"] if player is not None else f"Player {uid}")
        active_duels.pop(uid, None)
        duel_sessions.pop(uid, None)
        pending_attacks.pop(uid, None)
        active_casts.discard(uid)
        offensive_cooldowns.pop(uid, None)
        status_effects.pop(uid, None)
        if player is not None:
            player["xp"] = max(0, player["xp"] - DUEL_INACTIVITY_XP_PENALTY)
            update_player_level(player)
            player["hp"] = player["max_hp"]
            changed.append(player)
        else:
            logger.error("No saved profile for timed-out duelist %s", uid)
    database.save_players(changed)
    logger.info("Duel %s ended due to inactivity", session["id"])
    persist_runtime()
    message = (
        "⏳ **DUEL ENDED DUE TO INACTIVITY**\n\n"
        f"**{names[0]}** and **{names[1]}** didn't finish the duel.\n\n"
        f"⭐ {names[0]}: **-10 XP**\n⭐ {names[1]}: **-10 XP**"
    )
    try:
        if channel is None:
            channel = bot.get_channel(session["channel_id"])
            if channel is None:
                channel = await bot.fetch_channel(session["channel_id"])
        await channel.send(message)
    except discord.HTTPException:
        logger.exception("Could not announce inactivity timeout; duel was cleaned up")
    return True


# =========================================================
# SPELLBOOK REQUIREMENTS
# =========================================================

def ravenclaw_requirement(player, kind, required):
    if player.get("house") != "ravenclaw":
        return required

    if kind == "min_stat":
        return max(1, required - 1)

    if kind == "spell_level" and required >= 3:
        return required - 1

    if kind == "duel_wins" and required >= 3:
        return required - 1

    if kind in {"combat_stat", "spell_hits"} and required >= 5:
        reduction = max(1, round(required * 0.10))
        return max(1, required - reduction)

    return required


def evaluate_spell_requirements(player, spell_key, include_practical=True):
    spell = SPELLS[spell_key]
    requirements = spell.get("requirements", {})
    results = []

    if "level" in requirements:
        required = requirements["level"]
        current = player["level"]
        results.append((current >= required, f"Level {required} ({current}/{required})"))

    for required_spell, base_required_level in requirements.get("spell_levels", {}).items():
        required_level = ravenclaw_requirement(player, "spell_level", base_required_level)
        current = player["spell_levels"].get(required_spell, 0)
        display = SPELLS[required_spell]["display_name"]
        results.append((
            current >= required_level,
            f"{display} Spell Level {required_level} ({current}/{required_level})"
        ))

    for stat_name, base_required_value in requirements.get("min_stats", {}).items():
        required_value = ravenclaw_requirement(player, "min_stat", base_required_value)
        current = player["stats"].get(stat_name, 0)
        display = STAT_DISPLAY_NAMES.get(stat_name, stat_name)
        results.append((
            current >= required_value,
            f"{display} {required_value} ({current}/{required_value})"
        ))

    if "duel_wins" in requirements:
        required = ravenclaw_requirement(player, "duel_wins", requirements["duel_wins"])
        current = player["duel_wins"]
        results.append((current >= required, f"Win {required} Duels ({current}/{required})"))

    if include_practical:
        for stat_name, base_required_value in requirements.get("combat_stats", {}).items():
            required_value = ravenclaw_requirement(player, "combat_stat", base_required_value)
            current = player["combat_stats"].get(stat_name, 0)
            display = COMBAT_STAT_DISPLAY_NAMES.get(stat_name, stat_name)
            results.append((
                current >= required_value or practical_requirement_met(player, spell_key, f"combat_stats:{stat_name}"),
                f"{display}: {required_value} " + ("(practical training completed)" if practical_requirement_met(player, spell_key, f"combat_stats:{stat_name}") else f"({current}/{required_value})")
            ))

        for required_spell, base_required_hits in requirements.get("spell_hits", {}).items():
            required_hits = ravenclaw_requirement(player, "spell_hits", base_required_hits)
            current = player["spell_hits"].get(required_spell, 0)
            display = SPELLS[required_spell]["display_name"]
            results.append((
                current >= required_hits or practical_requirement_met(player, spell_key, f"spell_hits:{required_spell}"),
                f"Land {display} {required_hits} times " + ("(practical training completed)" if practical_requirement_met(player, spell_key, f"spell_hits:{required_spell}") else f"({current}/{required_hits})")
            ))

    for required_spell in requirements.get("required_spells", []):
        display = SPELLS[required_spell]["display_name"]
        results.append((required_spell in player["learned_spells"], f"Learn {display}"))

    if requirements.get("all_other_spells"):
        other_unlockable_spells = {
            key
            for key, value in SPELLS.items()
            if key != spell_key and not value.get("starting", False)
        }
        missing = sorted(other_unlockable_spells - player["learned_spells"])
        if missing:
            results.append((False, f"Learn every other Spell ({len(missing)} remaining)"))
        else:
            results.append((True, "Learn every other Spell"))

    return results


def can_learn_spell(player, spell_key):
    return all(met for met, _ in evaluate_spell_requirements(player, spell_key))


def spell_status(player, spell_key, user_id):
    if spell_key in player["learned_spells"]:
        return "✅ LEARNED"
    if user_id in active_learning_trials and active_learning_trials[user_id]["spell"] == spell_key:
        return "🧠 LEARNING"
    if can_learn_spell(player, spell_key):
        return "📘 READY TO LEARN"
    return "🔒 LOCKED"


# =========================================================
# QUIZ ENGINE
# =========================================================

def select_quiz_questions(difficulty, count, categories):
    min_difficulty = max(1, difficulty - 1)
    eligible = [
        q for q in QUIZ_QUESTIONS
        if min_difficulty <= q["difficulty"] <= difficulty
    ]

    preferred = [q for q in eligible if q["category"] in categories]
    selected = []

    preferred_target = min(len(preferred), max(1, round(count * 0.65)))
    if preferred_target:
        selected.extend(random.sample(preferred, preferred_target))

    selected_ids = {id(q) for q in selected}
    remaining = [q for q in eligible if id(q) not in selected_ids]

    needed = count - len(selected)
    if len(remaining) < needed:
        raise ValueError("Not enough quiz questions in the question bank.")

    selected.extend(random.sample(remaining, needed))
    random.shuffle(selected)

    prepared = []
    letters = ["A", "B", "C", "D"]

    for question in selected:
        answers = question["answers"].copy()
        random.shuffle(answers)
        answer_map = dict(zip(letters, answers))
        correct_letter = next(letter for letter, answer in answer_map.items() if answer == question["correct"])

        prepared.append({
            "question": question["question"],
            "answers": answer_map,
            "correct_letter": correct_letter,
        })

    return prepared


async def send_learning_question(ctx, trial):
    index = trial["current_question"]
    question = trial["questions"][index]

    answers_text = "\n".join(
        f"**{letter}.** {answer}"
        for letter, answer in question["answers"].items()
    )

    await ctx.send(
        f"📚 **{trial['display_name']} — Knowledge Trial**\n"
        f"Question **{index + 1}/{len(trial['questions'])}**\n\n"
        f"**{question['question']}**\n\n"
        f"{answers_text}\n\n"
        f"Answer with `!answer A`, `!answer B`, `!answer C`, or `!answer D`."
    )


# =========================================================
# DUEL END / DAMAGE HELPERS
# =========================================================

async def check_duel_end(ctx, loser, session_id=None):
    if session_id is not None and not session_is_current(loser.id, session_id):
        return False
    loser_player = get_player(loser)
    session = training_session(loser.id)
    if session is not None:
        completed = all(objective_complete(obj) for obj in session["objectives"].values())
        if completed or loser_player["hp"] <= 0:
            await end_training(ctx, session, "A participant reached 0 HP", completed=completed)
            return True
        return False

    if loser_player["hp"] > 0:
        return False

    loser_player["hp"] = 0

    session = duel_sessions.get(loser.id)
    if session is None or session.get("ending"):
        return False
    session["ending"] = True
    stop_duel_inactivity_timer(session["id"])

    winner_id = active_duels.get(loser.id)

    if winner_id is None:
        return False

    winner_player = players[winner_id]
    winner = SimpleNamespace(id=winner_id, display_name=winner_player["name"])

    # Duel statistics
    winner_player["duel_wins"] += 1
    loser_player["duel_losses"] += 1

    for participant in (winner_player, loser_player):
        participant["duels_completed"] += 1
        participant["duels_since_avada"] += 1

    # XP rewards
    winner_leveled_up = gain_player_xp(
        winner_player,
        50, persist=False
    )

    loser_leveled_up = gain_player_xp(
        loser_player,
        20, persist=False
    )

    ranking_eligible = (
        session.get("mode", "normal") == "normal"
        and winner_player["hp"] > 0
        and active_duels.get(winner_id) == loser.id
        and duel_sessions.get(winner_id, {}).get("id") == session["id"]
    )
    # Claim and clear state even if persistence or Discord is unavailable.
    cancel_combat_tasks(session["id"])
    for user_id in (loser.id, winner_id):
        for state in (duel_sessions, active_duels, pending_attacks, status_effects, offensive_cooldowns):
            state.pop(user_id, None)
        active_casts.discard(user_id)
    clear_participant_requests({loser.id, winner_id})
    guild_id = session.get("guild_id") or getattr(getattr(ctx, "guild", None), "id", None)
    ranking_text = ""
    if guild_id is not None and ranking_eligible:
        ranking = database.save_ranked_duel_result(session["id"], guild_id, winner_player, loser_player)
        if ranking is None:
            return False
        winner_before, winner_after = ranking["players"][winner_id]
        loser_before, loser_after = ranking["players"][loser.id]
        ranking_text = (
            f"\n\n**SERVER POINTS**\n"
            f"{winner.display_name}: {winner_before} → {winner_after} (+10)\n"
            f"{loser.display_name}: {loser_before} → {loser_after} ({loser_after - loser_before:+d})"
        )
        if ranking["house"] is not None:
            ranking_text += f"\n🏠 **{HOUSE_NAMES[ranking['house']]} +10 House Points**"
    else:
        # Missing guilds or states without a legitimate surviving winner earn no ranking points.
        database.save_players([winner_player, loser_player])
    logger.info("Normal duel %s completed", session["id"])

    await ctx.send(
        f"🏆 **DUEL OVER!**\n"
        f"✨ {winner.display_name} defeats {loser.display_name}!\n"
        f"❤️ {loser.display_name}: **0/{loser_player['max_hp']} HP**\n"
        f"⭐ {winner.display_name} earns **50 XP**.\n"
        f"⭐ {loser.display_name} earns **20 XP**."
        + ranking_text
    )

    if winner_leveled_up:
        await ctx.send(
            f"🌟 **{winner.display_name} reached "
            f"Level {winner_player['level']}!**\n"
            f"🎯 Talent Points were awarded for the new Level."
        )

    if loser_leveled_up:
        await ctx.send(
            f"🌟 **{loser.display_name} reached "
            f"Level {loser_player['level']}!**\n"
            f"🎯 Talent Points were awarded for the new Level."
        )

    persist_runtime()
    return True


async def apply_sectumsempra_bleed(ctx, defender_user, attacker_user, session_id):
    for delay in (3, 3):
        await asyncio.sleep(delay)

        if not session_is_current(defender_user.id, session_id):
            return

        defender = get_player(defender_user)
        defender["hp"] = max(0, defender["hp"] - 6)
        persist_player(defender)

        await ctx.send(
            f"🩸 **Sectumsempra bleeding deals 6 damage to {defender_user.display_name}.**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        if await check_duel_end(ctx, defender_user, session_id):
            return


async def apply_sectumsempra_backlash(ctx, attack, defense_power):
    if attack.get("session_id") is not None and not session_is_current(attack["attacker_id"], attack["session_id"]):
        return
    if attack["spell"] != "sectumsempra":
        return

    if defense_power < attack["power"] + 10:
        return

    attacker_id = attack["attacker_id"]
    if attacker_id not in active_duels:
        return

    attacker_user = await bot.fetch_user(attacker_id)
    if attack.get("session_id") is not None and not session_is_current(attacker_id, attack["session_id"]):
        return
    attacker = get_player(attacker_user)
    backlash = random.randint(5, 10)
    attacker["hp"] = max(0, attacker["hp"] - backlash)
    persist_player(attacker)

    await ctx.send(
        f"🩸 **Sectumsempra backfires!**\n"
        f"{attacker_user.display_name} takes **{backlash} backlash damage**.\n"
        f"❤️ HP: **{attacker['hp']}/{attacker['max_hp']}**"
    )

    await check_duel_end(ctx, attacker_user, attack.get("session_id"))


async def apply_attack(ctx, defender_user, attack):
    defender = get_player(defender_user)
    if not session_is_current(defender_user.id, attack["session_id"]):
        return
    spell = attack["spell"]
    attacker_id = attack["attacker_id"]
    attacker_user = await bot.fetch_user(attacker_id)
    if not session_is_current(defender_user.id, attack["session_id"]):
        return
    attacker = get_player(attacker_user)
    training = training_session(defender_user.id) is not None

    if spell == "confringo":
        damage = attack["damage"]
        defender["hp"] = max(0, defender["hp"] - damage)

        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "spell_hits", "confringo")
        database.save_players([attacker, defender])

        await ctx.send(
            f"🔥 **Confringo hits {defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        if not session_is_current(defender_user.id, attack["session_id"]):
            return
        gain_combat_player_xp(attacker, 15, training=training)
        gain_combat_spell_xp(attacker, "confringo", 15, training=training)
        await check_duel_end(ctx, defender_user, attack["session_id"])

    elif spell == "expelliarmus":

        damage = attack["damage"]
        duration = attack["duration"]

        # Deal small damage
        defender["hp"] = max(
            0,
            defender["hp"] - damage
        )

        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "spell_hits", "expelliarmus")

        persist_player(defender)

        gain_combat_player_xp(attacker, 10, training=training)

        gain_combat_spell_xp(attacker, "expelliarmus", 15, training=training)

        # If Expelliarmus finishes the opponent,
        # end the Duel immediately
        if defender["hp"] <= 0:
            await ctx.send(
                f"⚡ **Expelliarmus hits "
                f"{defender_user.display_name}!**\n"
                f"💥 Damage: **{damage}**\n"
                f"❤️ HP: **0/{defender['max_hp']}**"
            )

            await check_duel_end(
                ctx,
                defender_user, attack["session_id"]
            )

            return

        # Apply the disarm if the opponent survives
        status_effects.setdefault(
            defender_user.id,
            {}
        )["unarmed_until"] = time.time() + duration

        await ctx.send(
            f"⚡ **Expelliarmus hits "
            f"{defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"🪄 {defender_user.display_name} "
            f"is unarmed for **{duration} seconds**!\n"
            f"❤️ HP: "
            f"**{defender['hp']}/{defender['max_hp']}**"
        )

    elif spell == "stupefy":
        damage = attack["damage"]
        duration = attack["duration"]
        defender["hp"] = max(0, defender["hp"] - damage)

        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "combat_stats", "successful_control_spells")
        record_combat_success(attacker, "spell_hits", "stupefy")

        persist_player(defender)
        gain_combat_player_xp(attacker, 10, training=training)
        gain_combat_spell_xp(attacker, "stupefy", 15, training=training)

        if defender["hp"] <= 0:
            await ctx.send(
                f"🔴 **Stupefy hits {defender_user.display_name}!**\n"
                f"💥 Damage: **{damage}**\n"
                f"❤️ HP: **0/{defender['max_hp']}**"
            )
            await check_duel_end(ctx, defender_user, attack["session_id"])
            return

        status_effects.setdefault(defender_user.id, {})["stunned_until"] = (
            time.time() + duration
        )

        await ctx.send(
            f"🔴 **Stupefy hits {defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"💫 {defender_user.display_name} is stunned for **{duration} seconds**!\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

    elif spell == "sectumsempra":
        damage = attack["damage"]
        defender["hp"] = max(0, defender["hp"] - damage)

        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "spell_hits", "sectumsempra")

        persist_player(defender)
        await ctx.send(
            f"🩸 **Sectumsempra hits {defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        if not session_is_current(defender_user.id, attack["session_id"]):
            return
        gain_combat_player_xp(attacker, 25, training=training)
        gain_combat_spell_xp(attacker, "sectumsempra", 20, training=training)

        if not await check_duel_end(ctx, defender_user, attack["session_id"]):
            spawn_combat_task(attack["session_id"],
                apply_sectumsempra_bleed(ctx, defender_user, attacker_user, attack["session_id"])
            )

    elif spell == "avadakedavra":
        defender["hp"] = 0
        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "spell_hits", "avadakedavra")

        persist_player(defender)
        await ctx.send(
            f"💀 **AVADA KEDAVRA hits {defender_user.display_name}.**"
        )

        if not session_is_current(defender_user.id, attack["session_id"]):
            return
        gain_combat_spell_xp(attacker, "avadakedavra", 25, training=training)
        await check_duel_end(ctx, defender_user, attack["session_id"])

    elif spell in {"endoloris", "impero"}:
        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "combat_stats", "successful_control_spells")
        record_combat_success(attacker, "spell_hits", spell)
        gain_combat_player_xp(attacker, 15, training=training)
        gain_combat_spell_xp(attacker, spell, 15, training=training)
        if spell == "endoloris":
            hit_time = time.monotonic()
            await ctx.send(f"⚡ **Endoloris afflicts {defender_user.display_name}!** No immediate damage; 14 damage at 3s, 6s, and 9s.")
            spawn_combat_task(attack["session_id"], apply_endoloris(ctx, defender_user, attack["session_id"], hit_time))
        else:
            forced_spell = choose_forced_spell(defender)
            if forced_spell is None:
                await ctx.send("Impero finds no eligible offensive spell.")
                return
            effect = roll_offensive_effect(forced_spell, defender["spell_levels"][forced_spell])
            await queue_attack(ctx, defender_user, defender_user, forced_spell,
                               calculate_spell_power(defender, forced_spell),
                               calculate_spell_accuracy(defender, forced_spell),
                               forced=True, session_id=attack["session_id"], **effect)

    elif spell in MODELED_SPELLS:
        defender["hp"] = max(0, defender["hp"] - attack["damage"])
        record_combat_success(attacker, "combat_stats", "successful_attacks")
        record_combat_success(attacker, "spell_hits", spell)
        effects = status_effects.setdefault(defender_user.id, {})
        if spell in {"depulso", "petrificustotalus"}:
            effects["stunned_until"] = max(effects.get("stunned_until", 0), time.time() + attack["duration"])
            record_combat_success(attacker, "combat_stats", "successful_control_spells")
        if spell == "glacius":
            effects["slowed_until"] = time.time() + attack["duration"]
            effects["speed_penalty"] = 5
            record_combat_success(attacker, "combat_stats", "successful_control_spells")
        gain_combat_player_xp(attacker, 15, training=training)
        gain_combat_spell_xp(attacker, spell, 15, training=training)
        persist_player(defender)
        await ctx.send(f"{SPELLS[spell]['emoji']} **{SPELLS[spell]['display_name']} hits!** Damage: **{attack['damage']}**. HP: **{defender['hp']}/{defender['max_hp']}**")
        if not await check_duel_end(ctx, defender_user, attack["session_id"]) and spell == "incendio":
            spawn_combat_task(attack["session_id"], apply_burn(ctx, defender_user, attack["session_id"]))

    if not session_is_current(defender_user.id, attack["session_id"]):
        return
    persist_player(defender)

    await report_training_progress(ctx, defender_user.id, attack["session_id"])


async def apply_burn(ctx, defender_user, session_id):
    for _ in range(2):
        await asyncio.sleep(3)
        if not session_is_current(defender_user.id, session_id):
            return
        defender = get_player(defender_user)
        defender["hp"] = max(0, defender["hp"] - 4)
        persist_player(defender)
        await ctx.send(f"🔥 Burn deals **4 damage** to {defender_user.display_name}.")
        if await check_duel_end(ctx, defender_user, session_id):
            return


async def apply_endoloris(ctx, defender_user, session_id, hit_time=None):
    hit_time = time.monotonic() if hit_time is None else hit_time
    for offset in (3, 6, 9):
        await asyncio.sleep(max(0, hit_time + offset - time.monotonic()))
        if not session_is_current(defender_user.id, session_id):
            return
        defender = get_player(defender_user)
        defender["hp"] = max(0, defender["hp"] - 14)
        persist_player(defender)
        await ctx.send(f"⚡ **Endoloris deals 14 damage to {defender_user.display_name}.** HP: **{defender['hp']}/{defender['max_hp']}**")
        if await check_duel_end(ctx, defender_user, session_id):
            return


async def launch_attack(
    ctx,
    opponent,
    spell_name,
    power,
    accuracy,
    damage=0,
    duration=0,
    base_cooldown=6,
):
    if active_duels.get(ctx.author.id) != opponent.id or not in_duel_channel(ctx):
        await ctx.send("This Duel is no longer available here.")
        return
    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    if ctx.author.id in active_casts:
        await ctx.send("⏳ Your previous spell has not been resolved yet.")
        return

    if is_on_cooldown(ctx.author.id):
        remaining = remaining_cooldown(ctx.author.id)
        await ctx.send(
            f"⏳ You are recovering for another **{remaining:.1f} seconds**."
        )
        return

    if opponent.id in pending_attacks:
        await ctx.send(
            f"⚠️ {opponent.display_name} already has an Attack to react to."
        )
        return

    if getattr(ctx, "combat_session_id", duel_sessions.get(ctx.author.id, {}).get("id")) != duel_sessions.get(ctx.author.id, {}).get("id"):
        return
    await queue_attack(ctx, ctx.author, opponent, spell_name, power, accuracy,
                       damage=damage, duration=duration, base_cooldown=base_cooldown,
                       session_id=duel_sessions[ctx.author.id]["id"])


async def queue_attack(*args, **kwargs):
    session_id = kwargs.get("session_id")
    task = asyncio.current_task()
    combat_tasks.setdefault(session_id, set()).add(task)
    try:
        return await _queue_attack(*args, **kwargs)
    finally:
        tasks = combat_tasks.get(session_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                combat_tasks.pop(session_id, None)


async def _queue_attack(ctx, caster, opponent, spell_name, power, accuracy,
                       damage=0, duration=0, base_cooldown=6, forced=False,
                       session_id=None):
    """Share pending-attack creation/timing; forced casts bypass voluntary gates."""
    if not session_is_current(opponent.id, session_id) or opponent.id in pending_attacks:
        return
    attacker_player = get_player(caster)
    cooldown = calculate_cooldown(attacker_player, base_cooldown)
    if not forced and not note_duel_activity(caster.id, session_id, ctx.channel.id):
        await ctx.send("This Duel has ended or expired.")
        return
    if spell_name in CURSE_MIN_ROLLS and random.randint(1, 6) < CURSE_MIN_ROLLS[spell_name]:
        start_cooldown(caster.id, cooldown)
        persist_runtime()
        await ctx.send("❌ **Spell missed.**")
        return
    active_casts.add(caster.id)
    attack_id = time.time_ns()
    pending_attacks[opponent.id] = {
        "id": attack_id,
        "session_id": session_id,
        "attacker_id": caster.id,
        "spell": spell_name,
        "power": power,
        "accuracy": accuracy,
        "damage": damage,
        "duration": duration,
        "cooldown": cooldown,
        "forced": forced,
    }
    persist_runtime()
    verb = "is compelled by Impero to cast" if forced else "casts"
    await ctx.send(
        f"🪄 **{caster.display_name} {verb} {SPELLS[spell_name]['display_name'].upper()} at {opponent.display_name}!**\n"
        f"⏳ {opponent.display_name} has **10 seconds** to react.\n"
        f"Reactions: `!protego`, `!expelliarmus`, or `!dodge`."
    )

    await asyncio.sleep(10)

    if opponent.id not in pending_attacks:
        return

    current_attack = pending_attacks[opponent.id]
    if current_attack["id"] != attack_id or not session_is_current(opponent.id, current_attack["session_id"]):
        return

    del pending_attacks[opponent.id]
    finish_attack(current_attack)

    await ctx.send(
        f"⌛ {opponent.display_name} did not react in time!"
    )

    await apply_attack(ctx, opponent, current_attack)
    persist_runtime()


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def on_ready():
    global runtime_restored
    if not runtime_restored:
        restore_runtime()
        runtime_restored = True
    healthy, count = database.database_health()
    logger.info("Ready as %s | database=%s | saved players=%s | healthy=%s",
                bot.user, database.DATABASE_FILE, count, healthy)


# =========================================================
# BASIC COMMANDS
# =========================================================

@bot.command()
async def test(ctx):
    await ctx.send("The bot is working 🪄")


# =========================================================
# PROFILE / HOUSE / TRAIN
# =========================================================

@bot.command()
async def profile(ctx):
    player = get_player(ctx.author)

    if player["house"] is None:
        player["profile_started"] = True
        persist_player(player)
        await ctx.send(
            "🧙 **Create Your Wizard**\n\n"
            "Choose your House:\n\n"
            "🦁 `!house gryffindor`\n"
            "🐍 `!house slytherin`\n"
            "🦅 `!house ravenclaw`\n"
            "🦡 `!house hufflepuff`\n\n"
            "⚠️ Your House choice is permanent."
        )
        return

    stats = player["stats"]
    ready_text = "✅ Ready to Duel" if player["ready"] else "⚠️ Spend your remaining Talent Points"

    learned_lines = []
    for spell_key in sorted(player["learned_spells"], key=lambda k: SPELLS[k]["display_name"]):
        spell = SPELLS[spell_key]
        learned_lines.append(
            f"{spell['emoji']} {spell['display_name']} — Spell Level {player['spell_levels'][spell_key]}"
        )

    await ctx.send(
        f"🧙 **{player['name']}**\n\n"
        f"🏠 House: **{HOUSE_NAMES[player['house']]}**\n"
        f"⭐ Level: **{player['level']}**\n"
        f"✨ XP: **{player['xp']}**\n"
        f"❤️ HP: **{player['hp']}/{player['max_hp']}**\n\n"
        f"📊 **STATS**\n"
        f"🛡️ Endurance: **{stats['endurance']}**\n"
        f"✨ Magic Power: **{stats['magic_power']}**\n"
        f"⚡ Speed: **{stats['speed']}**\n"
        f"💨 Agility: **{stats['agility']}**\n\n"
        f"🎯 Talent Points: **{player['talent_points']}**\n\n"
        f"⚔️ **DUEL RECORD**\n"
        f"Wins: **{player['duel_wins']}** | Losses: **{player['duel_losses']}**\n\n"
        f"📚 **SPELLS LEARNED**\n"
        + "\n".join(learned_lines)
        + f"\n\n{ready_text}"
    )


@bot.command()
async def house(ctx, house_name: str):
    player = get_player(ctx.author)

    if not player["profile_started"]:
        await ctx.send("Use `!profile` first.")
        return

    if player["house"] is not None:
        await ctx.send("🏠 You have already chosen your House.")
        return

    house_name = house_name.lower()
    if house_name not in HOUSE_STATS:
        await ctx.send(
            "Unknown House. Choose `gryffindor`, `slytherin`, `ravenclaw`, or `hufflepuff`."
        )
        return

    player["house"] = house_name
    player["stats"] = HOUSE_STATS[house_name].copy()
    player["talent_points"] = 3
    update_max_hp(player)
    player["hp"] = player["max_hp"]
    persist_player(player)

    stats = player["stats"]
    await ctx.send(
        f"🏠 **You joined {HOUSE_NAMES[house_name]}!**\n\n"
        f"📊 Starting Stats:\n"
        f"🛡️ Endurance: **{stats['endurance']}**\n"
        f"✨ Magic Power: **{stats['magic_power']}**\n"
        f"⚡ Speed: **{stats['speed']}**\n"
        f"💨 Agility: **{stats['agility']}**\n\n"
        f"🎯 You have **3 Talent Points** to spend.\n\n"
        f"`!train endurance`\n"
        f"`!train magicpower`\n"
        f"`!train speed`\n"
        f"`!train agility`"
    )


@bot.command()
async def train(ctx, stat_name: str):
    player = get_player(ctx.author)

    if player["house"] is None:
        await ctx.send("Create your Profile first.")
        return

    if ctx.author.id in active_duels:
        await ctx.send("⚠️ You cannot train your stats during a Duel.")
        return

    if player["talent_points"] <= 0:
        await ctx.send("You have no Talent Points left.")
        return

    stat_name = stat_name.lower()
    if stat_name == "magicpower":
        stat_name = "magic_power"

    if stat_name not in STAT_DISPLAY_NAMES:
        await ctx.send(
            "Unknown stat. Choose `endurance`, `magicpower`, `speed`, or `agility`."
        )
        return

    player["stats"][stat_name] += 1
    player["talent_points"] -= 1

    if stat_name == "endurance":
        old_max_hp = player["max_hp"]
        update_max_hp(player)
        player["hp"] += player["max_hp"] - old_max_hp

    if player["talent_points"] == 0:
        player["ready"] = True
    persist_player(player)
    await ctx.send(
        f"⬆️ **{STAT_DISPLAY_NAMES[stat_name]} +1**\n"
        f"New value: **{player['stats'][stat_name]}**\n"
        f"🎯 Talent Points remaining: **{player['talent_points']}**"
    )

    if player["talent_points"] == 0:
        player["ready"] = True
        await ctx.send(
            "✅ **Your character is ready!**\n"
            "You can now participate in Duels."
        )

    persist_player(player)


# =========================================================
# SPELLBOOK / SPELL / LEARN / ANSWER / CANCELLEARN / TEACH
# =========================================================

@bot.command()
async def spellbook(ctx):
    player = get_player(ctx.author)

    if player["house"] is None:
        await ctx.send("Create your Profile first with `!profile`.")
        return

    learned = []
    ready = []
    locked = []

    for spell_key, spell in SPELLS.items():
        if spell.get("starting"):
            if spell_key in player["learned_spells"]:
                learned.append(
                    f"{spell['emoji']} **{spell['display_name']}** — Spell Level {player['spell_levels'][spell_key]}"
                )
            continue

        status = spell_status(player, spell_key, ctx.author.id)
        line = f"{spell['emoji']} **{spell['display_name']}** {stars(spell['difficulty'])}"

        if spell_key in player["learned_spells"]:
            learned.append(
                f"{spell['emoji']} **{spell['display_name']}** — Spell Level {player['spell_levels'][spell_key]}"
            )
        elif status == "📘 READY TO LEARN":
            ready.append(line)
        else:
            requirements = evaluate_spell_requirements(player, spell_key)
            completed = sum(1 for met, _ in requirements if met)
            total = len(requirements)
            progress = f" — {completed}/{total} requirements" if total else ""
            locked.append(line + progress)

    message = "📖 **SPELLBOOK**\n\n"
    message += "✅ **LEARNED**\n" + ("\n".join(learned) if learned else "None")
    message += "\n\n📘 **READY TO LEARN**\n" + ("\n".join(ready) if ready else "None")
    message += "\n\n🔒 **LOCKED / IN PROGRESS**\n" + ("\n".join(locked) if locked else "None")
    message += "\n\nUse `!spell <name>` for details or `!learn <name>` to start a Knowledge Trial."

    await ctx.send(message)


@bot.command(name="spell")
async def spell_info(ctx, *, spell_name: str):
    player = get_player(ctx.author)
    spell_key = normalize_spell_name(spell_name)

    if spell_key not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    spell = SPELLS[spell_key]
    status = spell_status(player, spell_key, ctx.author.id)

    lines = [
        f"{spell['emoji']} **{spell['display_name'].upper()}**",
        f"Difficulty: **{stars(spell['difficulty'])}**",
        f"Status: **{status}**",
        "",
        spell["description"],
    ]

    if spell.get("starting"):
        lines.append("\nThis is a Starting Spell.")
    else:
        lines.append("\n**Requirements:**")
        requirements = evaluate_spell_requirements(player, spell_key)
        for met, text in requirements:
            lines.append(f"{'✅' if met else '❌'} {text}")

        proof = player["practical_training"].get(spell_key, {})
        if proof.get("completed") or proof.get("objectives"):
            lines.append("📚 Completed cooperative objectives count toward practical requirements.")

        quiz_rule = QUIZ_RULES[spell["difficulty"]]
        quiz_required = quiz_rule["required_score"]
        if player.get("house") == "ravenclaw":
            quiz_required = max(1, quiz_required - 1)

        lines.append(
            f"\n🧠 Knowledge Trial: **{quiz_required}/{quiz_rule['questions']} required**"
        )
        if player.get("house") == "ravenclaw":
            lines.append("🦅 Ravenclaw learning bonus applied.")

    if spell_key == "avadakedavra" and spell_key in player["learned_spells"]:
        remaining = max(0, 5 - player["duels_since_avada"])
        if remaining == 0:
            lines.append("\n💀 Availability: **READY**")
        else:
            lines.append(f"\n💀 Availability: **{remaining} completed Duels remaining**")
        lines.append("Usage: **once every 5 completed Duels**")

    await ctx.send("\n".join(lines))


@bot.command()
async def learn(ctx, *, spell_name: str):
    player = get_player(ctx.author)
    spell_key = normalize_spell_name(spell_name)

    if player["house"] is None:
        await ctx.send("Create your Profile first with `!profile`.")
        return

    if ctx.author.id in active_duels:
        await ctx.send("⚠️ You cannot start a Learning Trial during a Duel.")
        return

    if ctx.author.id in active_learning_trials:
        trial = active_learning_trials[ctx.author.id]
        await ctx.send(
            f"🧠 You are already learning **{trial['display_name']}**. Finish it or use `!cancellearn`."
        )
        return

    if spell_key not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    spell = SPELLS[spell_key]

    if spell.get("starting"):
        await ctx.send(f"✅ You already know **{spell['display_name']}**.")
        return

    if spell_key in player["learned_spells"]:
        await ctx.send(f"✅ You have already learned **{spell['display_name']}**.")
        return

    requirements = evaluate_spell_requirements(player, spell_key)
    missing = [text for met, text in requirements if not met]

    if missing:
        await ctx.send(
            f"🔒 **{spell['display_name']} cannot be learned yet.**\n\n"
            "Missing requirements:\n"
            + "\n".join(f"❌ {text}" for text in missing)
        )
        return

    difficulty = spell["difficulty"]
    quiz_rule = QUIZ_RULES[difficulty]
    required_score = quiz_rule["required_score"]

    ravenclaw_bonus = player.get("house") == "ravenclaw"
    if ravenclaw_bonus:
        required_score = max(1, required_score - 1)

    try:
        questions = select_quiz_questions(
            difficulty,
            quiz_rule["questions"],
            spell.get("categories", ["general"]),
        )
    except ValueError:
        logger.exception("Knowledge Trial question selection failed for %s", spell_key)
        await ctx.send("⚠️ The quiz question bank does not contain enough questions yet.")
        return

    active_learning_trials[ctx.author.id] = {
        "spell": spell_key,
        "display_name": spell["display_name"],
        "questions": questions,
        "current_question": 0,
        "score": 0,
        "required_score": required_score,
    }

    ravenclaw_text = (
        "\n🦅 Ravenclaw bonus active: required score reduced by 1."
        if ravenclaw_bonus
        else ""
    )
    await ctx.send(
        f"📘 **{spell['display_name']} — Learning Trial**\n"
        f"Difficulty: **{stars(difficulty)}**\n"
        f"Questions: **{len(questions)}**\n"
        f"Required Score: **{required_score}/{len(questions)}**"
        f"{ravenclaw_text}"
    )

    await send_learning_question(ctx, active_learning_trials[ctx.author.id])


@bot.command()
@commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
async def answer(ctx, choice: str):
    if ctx.author.id not in active_learning_trials:
        await ctx.send("You do not have an active Learning Trial.")
        return

    choice = choice.upper().strip()
    if choice not in {"A", "B", "C", "D"}:
        await ctx.send("Answer with `!answer A`, `!answer B`, `!answer C`, or `!answer D`.")
        return

    trial = active_learning_trials[ctx.author.id]
    question = trial["questions"][trial["current_question"]]

    if choice == question["correct_letter"]:
        trial["score"] += 1
        result_text = "✅ Correct!"
    else:
        result_text = "❌ Incorrect."

    trial["current_question"] += 1

    if trial["current_question"] < len(trial["questions"]):
        await ctx.send(
            f"{result_text}\n"
            f"Current Score: **{trial['score']}/{trial['current_question']}**"
        )
        await send_learning_question(ctx, trial)
        return

    player = get_player(ctx.author)
    spell_key = trial["spell"]
    display_name = trial["display_name"]
    final_score = trial["score"]
    required_score = trial["required_score"]
    total = len(trial["questions"])

    del active_learning_trials[ctx.author.id]

    if final_score >= required_score:
        player["learned_spells"].add(spell_key)
        player["spell_levels"][spell_key] = 1
        player["spell_xp"][spell_key] = 0
        persist_player(player)

        await ctx.send(
            f"{result_text}\n\n"
            f"📚 **TRIAL COMPLETE**\n"
            f"Score: **{final_score}/{total}**\n"
            f"Required: **{required_score}/{total}**\n\n"
            f"✅ **SUCCESS!**\n"
            f"{SPELLS[spell_key]['emoji']} You have learned **{display_name}**!"
        )
    else:
        await ctx.send(
            f"{result_text}\n\n"
            f"📚 **TRIAL COMPLETE**\n"
            f"Score: **{final_score}/{total}**\n"
            f"Required: **{required_score}/{total}**\n\n"
            f"❌ **FAILED**\n"
            f"You did not learn **{display_name}**. You can try again."
        )


@bot.command()
async def cancellearn(ctx):
    if ctx.author.id not in active_learning_trials:
        await ctx.send("You do not have an active Learning Trial.")
        return

    trial = active_learning_trials.pop(ctx.author.id)
    await ctx.send(
        f"❌ Your **{trial['display_name']}** Learning Trial has been cancelled."
    )


# =========================================================
# COOPERATIVE TRAINING
# Sessions share the duel engine but never award normal combat progression.
# =========================================================

TEACHING_MIN_SPELL_LEVEL = 3


def training_session(user_id):
    session = duel_sessions.get(user_id)
    return session if session and session.get("mode") == "training" else None


def practical_requirement_met(player, spell_key, objective_key):
    proof = player.get("practical_training", {}).get(spell_key, {})
    return proof.get("completed", False) or objective_key in proof.get("objectives", {})


def build_training_objectives(student, spell_key):
    objectives = {}
    requirements = SPELLS[spell_key].get("requirements", {})
    for field, kind in (("spell_hits", "spell_hits"), ("combat_stats", "combat_stat")):
        for key, base_target in requirements.get(field, {}).items():
            objective_key = f"{field}:{key}"
            target = ravenclaw_requirement(student, kind, base_target)
            remaining = target - student[field].get(key, 0)
            if remaining <= 0 or practical_requirement_met(student, spell_key, objective_key):
                continue
            label = (f"{SPELLS[key]['emoji']} {SPELLS[key]['display_name']} Hits"
                     if field == "spell_hits" else COMBAT_STAT_DISPLAY_NAMES.get(key, key))
            objectives[objective_key] = {
                "label": label, "target": remaining, "teacher": 0, "student": 0,
            }
    return objectives


def objective_complete(objective):
    return (objective["student"] >= 1 and
            objective["teacher"] + objective["student"] >= objective["target"])


def record_combat_success(player, field, key):
    session = training_session(player["user_id"])
    if session is None:
        player[field][key] += 1
        return
    objective_key = f"{field}:{key}"
    objective = session["objectives"].get(objective_key)
    if objective is None or objective_complete(objective) or session.get("ending"):
        return
    role = "student" if player["user_id"] == session["student_id"] else "teacher"
    if player["user_id"] not in (session["student_id"], session["teacher_id"]):
        return
    objective[role] += 1
    session["progress_dirty"] = True
    if objective_complete(objective):
        student = players[session["student_id"]]
        proof = student["practical_training"].setdefault(session["spell"], {"objectives": {}})
        proof["objectives"][objective_key] = {
            "teacher_id": session["teacher_id"], "teacher": objective["teacher"],
            "student": objective["student"], "target": objective["target"],
        }
        persist_player(student)


def gain_combat_player_xp(player, amount, training=False):
    return False if training else gain_player_xp(player, amount)


def gain_combat_spell_xp(player, spell, amount, training=False):
    if not training:
        gain_spell_xp(player, spell, amount)


def training_progress_message(session):
    lines = [f"📚 **TRAINING SESSION — {SPELLS[session['spell']]['display_name']}**",
             f"Teacher: **{session['teacher_name']}**", f"Student: **{session['student_name']}**", "", "**Objectives:**"]
    for objective in session["objectives"].values():
        total = objective["teacher"] + objective["student"]
        marker = "✅" if objective_complete(objective) else "⏳"
        lines.extend([f"{marker} **{objective['label']}: {total}/{objective['target']}**",
                      f"Teacher: {objective['teacher']} | Student: {objective['student']}"])
        if objective["student"] == 0:
            lines.append("⚠️ Student must contribute")
    lines.append("Use `!training` to view progress or `!canceltraining` to leave.")
    return "\n".join(lines)


def clear_participant_requests(user_ids):
    for receiver, request in list(teaching_requests.items()):
        if user_ids.intersection({request["teacher_id"], request["student_id"]}):
            teaching_requests.pop(receiver)
    for receiver, request in list(duel_requests.items()):
        if receiver in user_ids or request["challenger_id"] in user_ids:
            duel_requests.pop(receiver)


async def end_training(ctx, session, reason, completed=False):
    if session.get("ending"):
        return
    session["ending"] = True
    cancel_combat_tasks(session["id"])
    logger.info("Training %s ended: %s; completed=%s", session["id"], reason, completed)
    student = players[session["student_id"]]
    if completed:
        proof = student["practical_training"].setdefault(session["spell"], {"objectives": {}})
        proof["completed"] = True
        proof["teacher_id"] = session["teacher_id"]
    changed = []
    for uid in (session["teacher_id"], session["student_id"]):
        # Only clean the session being ended, never a subsequent duel.
        if duel_sessions.get(uid, {}).get("id") != session["id"]:
            continue
        player = players[uid]
        player["hp"] = min(session["original_hp"][uid], player["max_hp"])
        duel_sessions.pop(uid, None)
        active_duels.pop(uid, None)
        pending_attacks.pop(uid, None)
        active_casts.discard(uid)
        status_effects.pop(uid, None)
        offensive_cooldowns.pop(uid, None)
        changed.append(player)
    clear_participant_requests({session["teacher_id"], session["student_id"]})
    database.save_players(changed)
    persist_runtime()
    if ctx is None:
        return
    if completed:
        message = (f"✅ **TRAINING COMPLETE**\n{session['student_name']} has completed practical training "
                   f"for **{SPELLS[session['spell']]['display_name']}** with {session['teacher_name']}.\n")
        if can_learn_spell(student, session["spell"]):
            message += f"The Knowledge Trial is now available: `!learn {session['spell']}`."
        else:
            message += "Practical training is saved. Remaining normal requirements must still be met before `!learn`."
    else:
        message = f"📚 **Training ended:** {reason}\nCompleted objectives are saved; incomplete counters reset."
    try:
        await ctx.send(message)
    except discord.HTTPException:
        logger.exception("Could not announce training end; session was cleaned up")


async def report_training_progress(ctx, user_id, session_id=None):
    session = training_session(user_id)
    if session is None or (session_id is not None and session["id"] != session_id):
        return
    if all(objective_complete(obj) for obj in session["objectives"].values()):
        await end_training(ctx, session, "Objectives completed", completed=True)
    elif session.pop("progress_dirty", False):
        await ctx.send(training_progress_message(session))


def expire_teaching_requests():
    for receiver, request in list(teaching_requests.items()):
        if time.time() >= request["expires"]:
            teaching_requests.pop(receiver)


def has_teaching_request(user_id):
    expire_teaching_requests()
    return any(user_id in (request["teacher_id"], request["student_id"])
               for request in teaching_requests.values())


def training_validation(teacher_user, student_user, spell_key):
    if teacher_user.id == student_user.id:
        return "You cannot train yourself."
    if spell_key not in SPELLS:
        return "❌ Unknown spell."
    teacher = get_player(teacher_user)
    student = get_player(student_user)
    if spell_key not in teacher["learned_spells"]:
        return "The teacher has not learned that spell."
    if teacher["spell_levels"].get(spell_key, 0) < TEACHING_MIN_SPELL_LEVEL:
        return f"The teacher needs **{SPELLS[spell_key]['display_name']} Spell Level {TEACHING_MIN_SPELL_LEVEL}**."
    if spell_key in student["learned_spells"]:
        return "The student already knows that spell."
    if getattr(teacher_user, "bot", False) or getattr(student_user, "bot", False):
        return "Both participants must be players."
    if not teacher["ready"] or not student["ready"]:
        return "Both players must finish their profiles first."
    if any(uid in active_duels for uid in (teacher_user.id, student_user.id)):
        return "One of you is already in a Duel or Training Session."
    if any(uid in active_learning_trials for uid in (teacher_user.id, student_user.id)):
        return "Finish your current Knowledge Trial before training."
    missing = [text for met, text in evaluate_spell_requirements(student, spell_key, include_practical=False) if not met]
    if missing:
        return "The student must meet the normal prerequisites first:\n" + "\n".join(missing)
    if not build_training_objectives(student, spell_key):
        return "The practical requirements are already satisfied, or this spell has no practical objectives. Use `!learn`."
    return None


async def create_teaching_request(ctx, teacher_user, student_user, spell_name, receiver):
    spell_key = normalize_spell_name(spell_name)
    error = training_validation(teacher_user, student_user, spell_key)
    if error:
        await ctx.send(error)
        return
    for uid in (teacher_user.id, student_user.id):
        if has_teaching_request(uid):
            await ctx.send("One of you already has a pending teaching request. Decline or cancel it first.")
            return
    for target, request in list(duel_requests.items()):
        if time.time() >= request["expires"]:
            duel_requests.pop(target)
    if any(target in (teacher_user.id, student_user.id) or request["challenger_id"] in (teacher_user.id, student_user.id)
           for target, request in duel_requests.items()):
        await ctx.send("Resolve pending normal Duel requests before requesting training.")
        return
    teaching_requests[receiver.id] = {
        "teacher_id": teacher_user.id, "student_id": student_user.id, "spell": spell_key,
        "initiator_id": ctx.author.id, "channel_id": ctx.channel.id,
        "guild_id": getattr(getattr(ctx, "guild", None), "id", None),
        "expires": time.time() + DUEL_REQUEST_TTL,
    }
    logger.info("Teaching request created: teacher=%s student=%s spell=%s", teacher_user.id, student_user.id, spell_key)
    await ctx.send(
        f"📚 **Training request — {SPELLS[spell_key]['display_name']}**\n"
        f"Teacher: **{teacher_user.display_name}**\nStudent: **{student_user.display_name}**\n"
        f"{receiver.display_name}, use `!accept` or `!decline` here within 2 minutes."
    )


async def request_kind(ctx, selected):
    if selected is not None and selected not in {"training", "duel"}:
        await ctx.send("Use `!accept training` / `!accept duel` (or the equivalent `!decline`).")
        return None
    if selected is None:
        if ctx.author.id in teaching_requests and ctx.author.id in duel_requests:
            await ctx.send("You have both requests. Choose `training` or `duel` after `!accept` or `!decline`.")
            return None
        selected = "training" if ctx.author.id in teaching_requests else "duel"
    return selected


async def training_member(ctx, user_id):
    guild = getattr(ctx, "guild", None)
    return await guild.fetch_member(user_id) if guild is not None else await bot.fetch_user(user_id)


async def accept_training(ctx):
    request = teaching_requests.get(ctx.author.id)
    if request is None:
        await ctx.send("You have no teaching request.")
        return
    if time.time() >= request["expires"]:
        teaching_requests.pop(ctx.author.id)
        await ctx.send("This teaching request has expired.")
        return
    if ctx.channel.id != request["channel_id"]:
        await ctx.send("Accept training in the channel where it was requested.")
        return
    try:
        teacher_user = await training_member(ctx, request["teacher_id"])
        student_user = await training_member(ctx, request["student_id"])
    except discord.HTTPException:
        teaching_requests.pop(ctx.author.id, None)
        await ctx.send("A participant is unavailable. The teaching request was cancelled.")
        return
    if teaching_requests.get(ctx.author.id) is not request:
        return
    error = training_validation(teacher_user, student_user, request["spell"])
    if time.time() >= request["expires"]:
        error = "This teaching request has expired."
    if error:
        teaching_requests.pop(ctx.author.id, None)
        await ctx.send(error)
        return
    teacher = get_player(teacher_user)
    student = get_player(student_user)
    session = {
        "id": uuid.uuid4().hex, "mode": "training", "channel_id": ctx.channel.id,
        "guild_id": request["guild_id"], "teacher_id": teacher_user.id, "student_id": student_user.id,
        "teacher_name": teacher_user.display_name, "student_name": student_user.display_name,
        "spell": request["spell"], "objectives": build_training_objectives(student, request["spell"]),
        "original_hp": {teacher_user.id: teacher["hp"], student_user.id: student["hp"]},
    }
    clear_participant_requests({teacher_user.id, student_user.id})
    for player, opponent in ((teacher, student), (student, teacher)):
        uid = player["user_id"]
        duel_sessions[uid] = session
        active_duels[uid] = opponent["user_id"]
        pending_attacks.pop(uid, None)
        active_casts.discard(uid)
        status_effects.pop(uid, None)
        offensive_cooldowns.pop(uid, None)
        player["hp"] = player["max_hp"]
        persist_player(player)
    persist_runtime()
    logger.info("Training %s accepted: teacher=%s student=%s spell=%s", session["id"], teacher_user.id, student_user.id, session["spell"])
    await ctx.send(training_progress_message(session))


@bot.command()
async def teach(ctx, student: discord.Member, *, spell_name: str):
    await create_teaching_request(ctx, ctx.author, student, spell_name, receiver=student)


@bot.command()
async def askhelp(ctx, teacher: discord.Member, *, spell_name: str):
    await create_teaching_request(ctx, teacher, ctx.author, spell_name, receiver=teacher)


@bot.command()
async def training(ctx):
    session = training_session(ctx.author.id)
    if session is None:
        await ctx.send("You have no active Training Session.")
    else:
        await ctx.send(training_progress_message(session))


@bot.command()
async def canceltraining(ctx):
    session = training_session(ctx.author.id)
    if session is not None:
        await end_training(ctx, session, f"{ctx.author.display_name} left the session")
    elif has_teaching_request(ctx.author.id):
        clear_participant_requests({ctx.author.id})
        await ctx.send("Pending teaching request cancelled.")
    else:
        await ctx.send("You have no Training Session or teaching request to cancel.")


@bot.event
async def on_member_remove(member):
    session = duel_sessions.get(member.id)
    if session and session.get("guild_id") == member.guild.id:
        await abort_session(session, "A participant left the server")
    for receiver, request in list(teaching_requests.items()):
        if request["guild_id"] == member.guild.id and member.id in (request["teacher_id"], request["student_id"]):
            teaching_requests.pop(receiver)


@bot.event
async def on_guild_channel_delete(channel):
    for session in list(duel_sessions.values()):
        if session["channel_id"] == channel.id:
            await abort_session(session, "Combat channel was deleted")
    for receiver, request in list(teaching_requests.items()):
        if request["channel_id"] == channel.id:
            teaching_requests.pop(receiver)
    for receiver, request in list(duel_requests.items()):
        if request["channel_id"] == channel.id:
            duel_requests.pop(receiver)


@bot.event
async def on_guild_remove(guild):
    for session in list(duel_sessions.values()):
        if session.get("guild_id") == guild.id:
            await abort_session(session, "The bot left the server")
    for receiver, request in list(teaching_requests.items()):
        if request["guild_id"] == guild.id:
            teaching_requests.pop(receiver)


# =========================================================
# DUEL COMMANDS
# =========================================================

@bot.command()
async def duel(ctx, target: discord.Member):
    challenger = get_player(ctx.author)
    target_player = get_player(target)

    if not challenger["ready"]:
        await ctx.send("⚠️ Finish creating your character with `!profile` first.")
        return

    if not target_player["ready"]:
        await ctx.send(f"⚠️ {target.display_name} has not finished their Profile.")
        return

    if target.id == ctx.author.id:
        await ctx.send("You cannot Duel yourself.")
        return

    if ctx.author.id in active_duels or target.id in active_duels:
        await ctx.send("One of you is already in a Duel.")
        return

    if has_teaching_request(ctx.author.id) or has_teaching_request(target.id):
        await ctx.send("Resolve your pending teaching request before starting a normal Duel.")
        return

    if getattr(target, "bot", False):
        await ctx.send("Challenge another player.")
        return
    if any(uid in active_learning_trials for uid in (ctx.author.id, target.id)):
        await ctx.send("Finish Knowledge Trials before dueling.")
        return
    for uid, request in list(duel_requests.items()):
        if time.time() >= request["expires"]:
            duel_requests.pop(uid)
    if any(uid in (ctx.author.id, target.id) or request["challenger_id"] in (ctx.author.id, target.id)
           for uid, request in duel_requests.items()):
        await ctx.send("Resolve existing Duel requests first.")
        return
    duel_requests[target.id] = {"challenger_id": ctx.author.id, "expires": time.time() + DUEL_REQUEST_TTL, "channel_id": ctx.channel.id}
    await ctx.send(
        f"⚔️ **{ctx.author.display_name} challenges {target.display_name} to a Duel!**\n"
        f"{target.display_name}, use `!accept` or `!decline`."
    )


@bot.command()
async def decline(ctx, request_type: str = None):
    kind = await request_kind(ctx, request_type)
    if kind is None:
        return
    if kind == "training":
        request = teaching_requests.get(ctx.author.id)
        if request is None:
            await ctx.send("You have no teaching request.")
            return
        if ctx.channel.id != request["channel_id"]:
            await ctx.send("Decline teaching in the channel where it was requested.")
            return
        teaching_requests.pop(ctx.author.id)
        logger.info("Teaching request declined by %s", ctx.author.id)
        await ctx.send(f"📚 Teaching request for **{SPELLS[request['spell']]['display_name']}** declined.")
        return
    if ctx.author.id not in duel_requests:
        await ctx.send("You have no Duel request.")
        return

    challenger_id = duel_requests.pop(ctx.author.id)["challenger_id"]
    challenger = await bot.fetch_user(challenger_id)

    await ctx.send(
        f"❌ {ctx.author.display_name} declined the Duel against {challenger.display_name}."
    )


@bot.command()
async def accept(ctx, request_type: str = None):
    kind = await request_kind(ctx, request_type)
    if kind is None:
        return
    if kind == "training":
        await accept_training(ctx)
        return
    if ctx.author.id not in duel_requests:
        await ctx.send("You have no Duel request.")
        return

    request = duel_requests[ctx.author.id]
    challenger_id = request["challenger_id"]
    if time.time() >= request["expires"]:
        duel_requests.pop(ctx.author.id)
        await ctx.send("This Duel request has expired. Ask for a new challenge.")
        return
    if ctx.channel.id != request["channel_id"]:
        await ctx.send("Accept the challenge in the channel where it was sent.")
        return
    if ctx.author.id in active_duels or challenger_id in active_duels:
        duel_requests.pop(ctx.author.id)
        await ctx.send("One of you is already in a Duel.")
        return
    challenger = await bot.fetch_user(challenger_id)
    player1 = get_player(challenger)
    player2 = get_player(ctx.author)
    if not player1["ready"] or not player2["ready"]:
        await ctx.send("Both players must finish their profiles first.")
        return
    if duel_requests.get(ctx.author.id) is not request or time.time() >= request["expires"]:
        await ctx.send("This request has changed or expired. Request a new Duel.")
        return
    if any(uid in active_learning_trials or has_teaching_request(uid) for uid in (ctx.author.id, challenger_id)):
        await ctx.send("Finish pending learning or teaching first.")
        return
    # Reserve the session without awaiting between validation and mutation.
    if ctx.author.id in active_duels or challenger_id in active_duels:
        await ctx.send("One of you is already in a Duel.")
        return
    duel_requests.pop(ctx.author.id)
    session = {"id": uuid.uuid4().hex, "mode": "normal", "channel_id": ctx.channel.id,
               "last_activity": duel_clock(), "guild_id": getattr(getattr(ctx, "guild", None), "id", None)}
    duel_sessions[ctx.author.id] = session
    duel_sessions[challenger_id] = session
    active_duels[ctx.author.id] = challenger_id
    active_duels[challenger_id] = ctx.author.id

    clear_participant_requests({ctx.author.id, challenger_id})

    player1["hp"] = player1["max_hp"]
    player2["hp"] = player2["max_hp"]
    database.save_players([player1, player2])

    for user_id in (challenger_id, ctx.author.id):
        pending_attacks.pop(user_id, None)
        active_casts.discard(user_id)
        status_effects.pop(user_id, None)
        offensive_cooldowns.pop(user_id, None)

    schedule_duel_inactivity(session)
    logger.info("Normal duel %s started: %s vs %s", session["id"], challenger_id, ctx.author.id)
    persist_runtime()

    await ctx.send(
        f"⚔️ **Duel accepted!**\n\n"
        f"{challenger.display_name} ({HOUSE_NAMES[player1['house']]})\n"
        f"❤️ {player1['max_hp']} HP\n\n"
        f"VS\n\n"
        f"{ctx.author.display_name} ({HOUSE_NAMES[player2['house']]})\n"
        f"❤️ {player2['max_hp']} HP\n\n"
        f"🪄 **Let the Duel begin!**"
    )


# =========================================================
# COMBAT SPELLS
# =========================================================

@bot.command()
async def confringo(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)

    if is_unarmed(ctx.author.id):
        await ctx.send(
            f"🪄 You are unarmed for another **{remaining_unarmed_time(ctx.author.id):.1f} seconds**."
        )
        return

    if ctx.author.id in pending_attacks:
        await ctx.send("⚠️ You must react to the incoming Attack first.")
        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])
    spell_level = player["spell_levels"]["confringo"]
    damage = roll_offensive_effect("confringo", spell_level)["damage"]
    power = calculate_spell_power(player, "confringo")
    accuracy = calculate_spell_accuracy(player, "confringo")

    await launch_attack(
        ctx,
        opponent,
        "confringo",
        power,
        accuracy,
        damage=damage,
        base_cooldown=6,
    )


@bot.command()
async def sectumsempra(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)
    if not knows_spell(player, "sectumsempra"):
        await ctx.send("🔒 You have not learned **Sectumsempra** yet.")
        return

    if is_unarmed(ctx.author.id):
        await ctx.send(
            f"🪄 You are unarmed for another **{remaining_unarmed_time(ctx.author.id):.1f} seconds**."
        )
        return

    if ctx.author.id in pending_attacks:
        await ctx.send("⚠️ You must react to the incoming Attack first.")
        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])
    damage = roll_offensive_effect("sectumsempra", player["spell_levels"]["sectumsempra"])["damage"]
    power = calculate_spell_power(player, "sectumsempra")
    accuracy = calculate_spell_accuracy(player, "sectumsempra")

    await launch_attack(
        ctx,
        opponent,
        "sectumsempra",
        power,
        accuracy,
        damage=damage,
        base_cooldown=15,
    )


@bot.command()
async def avadakedavra(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)

    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    if not knows_spell(player, "avadakedavra"):
        await ctx.send("🔒 You have not learned **Avada Kedavra** yet.")
        return

    if player["duels_since_avada"] < 5:
        remaining = 5 - player["duels_since_avada"]
        await ctx.send(
            f"💀 Avada Kedavra is unavailable. Complete **{remaining} more Duel{'s' if remaining != 1 else ''}** before using it again."
        )
        return

    if is_unarmed(ctx.author.id):
        await ctx.send(
            f"🪄 You are unarmed for another **{remaining_unarmed_time(ctx.author.id):.1f} seconds**."
        )
        return

    if ctx.author.id in pending_attacks:
        await ctx.send("⚠️ You must react to the incoming Attack first.")
        return

    if ctx.author.id in active_casts:
        await ctx.send("⏳ Your previous spell has not been resolved yet.")
        return

    if is_on_cooldown(ctx.author.id):
        await ctx.send(
            f"⏳ You are recovering for another **{remaining_cooldown(ctx.author.id):.1f} seconds**."
        )
        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])

    if opponent.id in pending_attacks:
        await ctx.send(
            f"⚠️ {opponent.display_name} already has an Attack to react to."
        )
        return

    if active_duels.get(ctx.author.id) != opponent.id:
        await ctx.send("This Duel has ended.")
        return

    if not note_duel_activity(ctx.author.id, session_id=getattr(ctx, "combat_session_id", None), channel_id=ctx.channel.id):
        await ctx.send("This Duel has ended or expired.")
        return

    # The attempt is consumed immediately, hit or miss.
    if training_session(ctx.author.id) is None:
        player["duels_since_avada"] = 0
        persist_player(player)

    persist_runtime()

    # Hidden 1d6 roll: 5-6 succeeds, 1-4 misses.
    if random.randint(1, 6) < 5:
        start_cooldown(ctx.author.id, 25)
        await ctx.send("❌ **Spell missed.**")
        return

    power = calculate_spell_power(player, "avadakedavra")
    accuracy = calculate_spell_accuracy(player, "avadakedavra")

    await launch_attack(
        ctx,
        opponent,
        "avadakedavra",
        power,
        accuracy,
        base_cooldown=25,
    )


@bot.command()
async def protego(ctx):
    if ctx.author.id not in pending_attacks:
        await ctx.send("🛡️ There is no Attack to block.")
        return

    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    defender = get_player(ctx.author)
    training = training_session(ctx.author.id) is not None
    attack = pending_attacks[ctx.author.id]
    defense_power = calculate_protego_power(defender)
    if not note_duel_activity(ctx.author.id, attack["session_id"], ctx.channel.id):
        await ctx.send("This Duel has ended or expired.")
        return

    del pending_attacks[ctx.author.id]
    finish_attack(attack)

    if defense_power >= attack["power"]:
        record_combat_success(defender, "combat_stats", "successful_protegos")
        gain_combat_player_xp(defender, 5, training=training)
        gain_combat_spell_xp(defender, "protego", 10, training=training)

        chip = attack.get("damage", 0) * PROTEGO_DAMAGE_PERCENT.get(attack["spell"], 0) // 100
        if chip:
            defender["hp"] = max(0, defender["hp"] - chip)
            persist_player(defender)
            await ctx.send(
                f"🛡️ **{ctx.author.display_name} casts PROTEGO!**\n"
                f"✨ Most of the Attack is blocked, but **{chip} damage** gets through!\n"
                f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
            )
            if await check_duel_end(ctx, ctx.author, attack["session_id"]):
                return
        else:
            await ctx.send(
                f"🛡️ **{ctx.author.display_name} casts PROTEGO!**\n"
                f"✨ The Attack is blocked!\n"
            )

        await apply_sectumsempra_backlash(ctx, attack, defense_power)
    else:
        await ctx.send(
            f"🛡️ **{ctx.author.display_name} casts PROTEGO!**\n"
            f"💥 Protego is broken!\n"

        )
        await apply_attack(ctx, ctx.author, attack)

    await report_training_progress(ctx, ctx.author.id, attack["session_id"])


@bot.command()
async def dodge(ctx):
    if ctx.author.id not in pending_attacks:
        await ctx.send("💨 There is no Attack to Dodge.")
        return

    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    defender = get_player(ctx.author)
    training = training_session(ctx.author.id) is not None
    attack = pending_attacks[ctx.author.id]
    dodge_power = calculate_dodge_power(defender)
    attack_accuracy = attack["accuracy"]
    if not note_duel_activity(ctx.author.id, attack["session_id"], ctx.channel.id):
        await ctx.send("This Duel has ended or expired.")
        return

    del pending_attacks[ctx.author.id]
    finish_attack(attack)

    if dodge_power >= attack_accuracy:
        record_combat_success(defender, "combat_stats", "successful_dodges")
        gain_combat_player_xp(defender, 5, training=training)

        await ctx.send(
            f"💨 **{ctx.author.display_name} Dodges!**\n"
            f"✨ {SPELLS[attack['spell']]['display_name'].upper()} misses!\n"

        )
    else:
        await ctx.send(
            f"💨 **{ctx.author.display_name} tries to Dodge!**\n"
            f"❌ The Dodge fails.\n"

        )
        await apply_attack(ctx, ctx.author, attack)

    await report_training_progress(ctx, ctx.author.id, attack["session_id"])


@bot.command()
async def expelliarmus(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)
    training = training_session(ctx.author.id) is not None

    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    if is_unarmed(ctx.author.id):
        await ctx.send(
            f"🪄 You are unarmed for another **{remaining_unarmed_time(ctx.author.id):.1f} seconds**."
        )
        return

    expelliarmus_power = calculate_spell_power(player, "expelliarmus")

    if ctx.author.id in pending_attacks:
        incoming_attack = pending_attacks[ctx.author.id]
        if not note_duel_activity(ctx.author.id, incoming_attack["session_id"], ctx.channel.id):
            await ctx.send("This Duel has ended or expired.")
            return
        attacker_id = incoming_attack["attacker_id"]
        attacker = await bot.fetch_user(attacker_id)
        if pending_attacks.get(ctx.author.id) is not incoming_attack or not session_is_current(ctx.author.id, incoming_attack["session_id"]):
            return

        del pending_attacks[ctx.author.id]
        finish_attack(incoming_attack)

        if expelliarmus_power >= incoming_attack["power"]:
            status_effects.setdefault(
                attacker_id,
                {}
            )["unarmed_until"] = time.time() + 4
            record_combat_success(player, "combat_stats", "successful_counters")

            await ctx.send(
                f"⚡ **{ctx.author.display_name} counters with EXPELLIARMUS!**\n"
                f"💥 {SPELLS[incoming_attack['spell']]['display_name'].upper()} is interrupted!\n"
                f"🪄 {attacker.display_name} is unarmed for **4 seconds**!\n"

            )

            if not session_is_current(ctx.author.id, incoming_attack["session_id"]):
                return
            gain_combat_player_xp(player, 10, training=training)
            gain_combat_spell_xp(player, "expelliarmus", 15, training=training)
            await apply_sectumsempra_backlash(ctx, incoming_attack, expelliarmus_power)
        else:
            await ctx.send(
                f"⚡ **{ctx.author.display_name} counters with EXPELLIARMUS!**\n"
                f"❌ The counter fails.\n"

            )
            await apply_attack(ctx, ctx.author, incoming_attack)

        await report_training_progress(ctx, ctx.author.id, incoming_attack["session_id"])
        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])

    accuracy = calculate_spell_accuracy(
        player,
        "expelliarmus"
    )

    spell_level = player["spell_levels"]["expelliarmus"]

    damage = roll_offensive_effect("expelliarmus", spell_level)["damage"]

    await launch_attack(
        ctx,
        opponent,
        "expelliarmus",
        expelliarmus_power,
        accuracy,
        damage=damage,
        duration=4,
        base_cooldown=6,
    )


# =========================================================
# ADDITIONAL COMBAT SPELLS
# Numeric tuning is maintained in combat.py.
# =========================================================

async def modeled_spell(ctx, spell_key):
    player = get_player(ctx.author)
    if spell_key not in player["learned_spells"]:
        await ctx.send(f"🔒 You have not learned **{SPELLS[spell_key]['display_name']}** yet.")
        return
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return
    if is_unarmed(ctx.author.id):
        await ctx.send("You are unarmed.")
        return
    if ctx.author.id in pending_attacks:
        await ctx.send("React to the incoming Attack first.")
        return
    opponent = await bot.fetch_user(active_duels[ctx.author.id])
    effect = roll_offensive_effect(spell_key, player["spell_levels"][spell_key])
    await launch_attack(ctx, opponent, spell_key,
                        calculate_spell_power(player, spell_key),
                        calculate_spell_accuracy(player, spell_key), **effect)


@bot.command()
async def stupefy(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)

    if not knows_spell(player, "stupefy"):
        await ctx.send("🔒 You have not learned **Stupefy** yet.")
        return

    if is_stunned(ctx.author.id):
        await ctx.send(
            f"💫 You are stunned for another **{remaining_stun_time(ctx.author.id):.1f} seconds**."
        )
        return

    if is_unarmed(ctx.author.id):
        await ctx.send(
            f"🪄 You are unarmed for another **{remaining_unarmed_time(ctx.author.id):.1f} seconds**."
        )
        return

    if ctx.author.id in pending_attacks:
        await ctx.send("⚠️ You must react to the incoming Attack first.")
        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])
    spell_level = player["spell_levels"]["stupefy"]
    damage = roll_offensive_effect("stupefy", spell_level)["damage"]
    power = calculate_spell_power(player, "stupefy")
    accuracy = calculate_spell_accuracy(player, "stupefy")

    await launch_attack(
        ctx,
        opponent,
        "stupefy",
        power,
        accuracy,
        damage=damage,
        duration=2,
        base_cooldown=6,
    )


@bot.command()
async def incendio(ctx):
    await modeled_spell(ctx, "incendio")


@bot.command()
async def diffindo(ctx):
    await modeled_spell(ctx, "diffindo")


@bot.command()
async def depulso(ctx):
    await modeled_spell(ctx, "depulso")


@bot.command()
async def glacius(ctx):
    await modeled_spell(ctx, "glacius")


@bot.command(name="petrificustotalus")
async def petrificus_totalus(ctx):
    await modeled_spell(ctx, "petrificustotalus")


@bot.command()
async def bombarda(ctx):
    await modeled_spell(ctx, "bombarda")



@bot.command()
async def endoloris(ctx):
    await modeled_spell(ctx, "endoloris")


@bot.command()
async def impero(ctx):
    await modeled_spell(ctx, "impero")


# =========================================================
# OWNER / ADMIN COMMANDS
# =========================================================

def owner_only(ctx):
    return is_owner(ctx.author)


async def owner_check(ctx):
    if owner_only(ctx):
        return True
    await ctx.send("❌ You are not allowed to use this command.")
    return False


@bot.command()
@commands.check(owner_check)
async def setstat(ctx, target: discord.Member, stat_name: str, value: int):
    player = get_player(target)
    stat_name = stat_name.lower().replace("magicpower", "magic_power")

    if stat_name not in STAT_DISPLAY_NAMES:
        await ctx.send("❌ Invalid stat.")
        return

    player["stats"][stat_name] = max(0, value)

    if stat_name == "endurance":
        update_max_hp(player)
        player["hp"] = min(player["hp"], player["max_hp"])

    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name}'s "
        f"{STAT_DISPLAY_NAMES[stat_name]} was set to **{player['stats'][stat_name]}**."
    )


@bot.command()
@commands.check(owner_check)
async def addspell(ctx, target: discord.Member, *, spell_name: str):
    spell_name = normalize_spell_name(spell_name)
    if spell_name not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    player = get_player(target)
    player["learned_spells"].add(spell_name)
    player["spell_levels"][spell_name] = max(1, player["spell_levels"].get(spell_name, 0))
    player["spell_xp"].setdefault(spell_name, 0)
    player["spell_hits"].setdefault(spell_name, 0)
    persist_player(player)

    await ctx.send(
        f"✅ {SPELLS[spell_name]['display_name']} was added to {target.display_name}."
    )


@bot.command()
@commands.check(owner_check)
async def removespell(ctx, target: discord.Member, *, spell_name: str):
    spell_name = normalize_spell_name(spell_name)
    if spell_name not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    if spell_name in STARTING_SPELLS:
        await ctx.send("❌ Starting Spells are automatically restored and cannot be removed.")
        return

    player = get_player(target)
    player["learned_spells"].discard(spell_name)
    player["spell_levels"][spell_name] = 0
    player["spell_xp"][spell_name] = 0
    player["spell_hits"][spell_name] = 0
    persist_player(player)

    await ctx.send(
        f"✅ {SPELLS[spell_name]['display_name']} was removed from {target.display_name}."
    )


@bot.command()
@commands.check(owner_check)
async def setlevel(ctx, target: discord.Member, value: int):
    if value < 1:
        await ctx.send("❌ Level must be at least 1.")
        return

    player = get_player(target)
    player["level"] = value
    player["xp"] = (value - 1) * 100
    persist_player(player)
    await ctx.send(f"✅ {target.display_name} is now Level **{value}**.")


@bot.command()
@commands.check(owner_check)
async def setxp(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["xp"] = max(0, value)
    update_player_level(player)
    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name}'s XP was set to **{player['xp']}** "
        f"(Level {player['level']})."
    )


@bot.command()
@commands.check(owner_check)
async def addxp(ctx, target: discord.Member, amount: int):
    player = get_player(target)
    old_level = player["level"]
    old_points = player["talent_points"]
    gain_player_xp(player, amount)
    gained_points = player["talent_points"] - old_points

    message = f"✅ Added **{amount} XP** to {target.display_name}."
    if player["level"] > old_level:
        message += (
            f"\n🌟 Level {old_level} → **{player['level']}**"
            f"\n🎯 +**{gained_points} Talent Points**"
        )
    await ctx.send(message)


@bot.command()
@commands.check(owner_check)
async def setspelllevel(ctx, target: discord.Member, spell_name: str, level: int):
    spell_name = normalize_spell_name(spell_name)
    if spell_name not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    if level < 1:
        await ctx.send("❌ Spell Level must be at least 1.")
        return

    player = get_player(target)
    player["learned_spells"].add(spell_name)
    player["spell_levels"][spell_name] = level
    player["spell_xp"][spell_name] = (level - 1) * 100
    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name}'s {SPELLS[spell_name]['display_name']} "
        f"is now Spell Level **{level}**."
    )


@bot.command()
@commands.check(owner_check)
async def setspellxp(ctx, target: discord.Member, spell_name: str, value: int):
    spell_name = normalize_spell_name(spell_name)
    if spell_name not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    player = get_player(target)
    player["learned_spells"].add(spell_name)
    player["spell_xp"][spell_name] = max(0, value)
    player["spell_levels"][spell_name] = max(1, 1 + player["spell_xp"][spell_name] // 100)
    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name}'s {SPELLS[spell_name]['display_name']} XP is now "
        f"**{player['spell_xp'][spell_name]}** "
        f"(Spell Level {player['spell_levels'][spell_name]})."
    )


@bot.command(name="setpoints", aliases=["settpoints"])
@commands.check(owner_check)
async def setpoints(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["talent_points"] = max(0, value)
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name} now has **{player['talent_points']} Talent Points**."
    )


@bot.command()
@commands.check(owner_check)
async def addpoints(ctx, target: discord.Member, amount: int):
    player = get_player(target)
    player["talent_points"] = max(0, player["talent_points"] + amount)
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name} now has **{player['talent_points']} Talent Points**."
    )


@bot.command()
@commands.check(owner_check)
async def sethouse(ctx, target: discord.Member, house_name: str):
    house_name = house_name.lower()
    if house_name not in HOUSE_STATS:
        await ctx.send("❌ Unknown House.")
        return

    player = get_player(target)
    player["house"] = house_name
    player["profile_started"] = True
    player["ready"] = True
    player["stats"] = HOUSE_STATS[house_name].copy()
    update_max_hp(player)
    player["hp"] = player["max_hp"]
    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name} was moved to **{HOUSE_NAMES[house_name]}**."
    )


@bot.command()
@commands.check(owner_check)
async def sethp(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["hp"] = max(0, min(value, player["max_hp"]))
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name}'s HP is now **{player['hp']}/{player['max_hp']}**."
    )


@bot.command()
@commands.check(owner_check)
async def setmaxhp(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["max_hp"] = max(1, value)
    player["hp"] = min(player["hp"], player["max_hp"])
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name}'s Max HP is now **{player['max_hp']}**."
    )


@bot.command()
@commands.check(owner_check)
async def setwins(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["duel_wins"] = max(0, value)
    persist_player(player)
    await ctx.send(f"✅ Wins set to **{player['duel_wins']}**.")


@bot.command()
@commands.check(owner_check)
async def setlosses(ctx, target: discord.Member, value: int):
    player = get_player(target)
    player["duel_losses"] = max(0, value)
    persist_player(player)
    await ctx.send(f"✅ Losses set to **{player['duel_losses']}**.")


@bot.command()
@commands.check(owner_check)
async def adminprofile(ctx, target: discord.Member):
    player = get_player(target)
    spells_text = ", ".join(
        f"{SPELLS[key]['display_name']} L{player['spell_levels'].get(key, 0)}"
        for key in sorted(player["learned_spells"], key=lambda k: SPELLS[k]["display_name"])
    )

    await ctx.send(
        f"🔐 **ADMIN PROFILE — {target.display_name}**\n"
        f"House: **{player['house']}**\n"
        f"Level: **{player['level']}** | XP: **{player['xp']}**\n"
        f"HP: **{player['hp']}/{player['max_hp']}**\n"
        f"Talent Points: **{player['talent_points']}**\n"
        f"Stats: `{player['stats']}`\n"
        f"Wins: **{player['duel_wins']}** | Losses: **{player['duel_losses']}** | "
        f"Completed: **{player['duels_completed']}**\n"
        f"Avada progress: **{player['duels_since_avada']}/5**\n"
        f"Combat Stats: `{player['combat_stats']}`\n"
        f"Spells: {spells_text}"
    )


# =========================================================
# START BOT
# =========================================================

def ranking_display_name(ctx, entry):
    guild = getattr(ctx, "guild", None)
    member = guild.get_member(entry["user_id"]) if guild else None
    cached = member or bot.get_user(entry["user_id"])
    name = getattr(cached, "display_name", None) or entry["name"] or f"Player {entry['user_id']}"
    name = str(name).replace("\n", " ").replace("\r", " ")[:60]
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


async def send_player_leaderboard(ctx, global_points=False):
    guild_id = None if global_points else ctx.guild.id
    entries, personal = database.player_leaderboard(ctx.author.id, guild_id)
    title = "🌍 **DUELLIUM GLOBAL LEADERBOARD**" if global_points else "🏆 **SERVER LEADERBOARD**"
    lines = [title]
    lines.extend(f"#{entry['rank']} {ranking_display_name(ctx, entry)} — {entry['points']} pts" for entry in entries)
    if not entries:
        lines.append("No ranked results yet.")
    if personal is None:
        lines.append("\nYour Global Rank: Unranked — 0 pts" if global_points else "\nYour Rank: Unranked — 0 pts")
    elif personal[0] > 10:
        label = "Your Global Rank" if global_points else "Your Rank"
        lines.append(f"\n{label}: #{personal[0]} — {personal[1]} pts")
    await ctx.send("\n".join(lines))


@bot.command()
@commands.guild_only()
async def leaderboard(ctx):
    await send_player_leaderboard(ctx)


@bot.command()
async def globalleaderboard(ctx):
    await send_player_leaderboard(ctx, global_points=True)


@bot.command()
@commands.guild_only()
async def houseleaderboard(ctx):
    rows = database.house_leaderboard(ctx.guild.id)
    lines = ["🏆 **HOUSE LEADERBOARD**"]
    for medal, (house, points) in zip(("🥇", "🥈", "🥉", "4."), rows):
        lines.append(f"{medal} {HOUSE_NAMES[house]} — {points} pts")
    await ctx.send("\n".join(lines))


@bot.command()
@commands.check(owner_check)
async def botstatus(ctx):
    try:
        healthy, saved = database.database_health()
        health = "OK" if healthy else "FAILED"
    except Exception:
        logger.exception("Database diagnostic failed")
        health, saved = "FAILED", "unknown"
    sessions = {s["id"]: s for s in duel_sessions.values()}
    normal = sum(s.get("mode") != "training" for s in sessions.values())
    expire_teaching_requests()
    await ctx.send(
        f"🔧 **Bot status**\nUptime: {int(time.monotonic() - STARTED_AT)} seconds\n"
        f"Players loaded/saved: {len(players)}/{saved}\n"
        f"Normal / training duels: {normal}/{len(sessions) - normal}\n"
        f"Teaching requests / trials: {len(teaching_requests)}/{len(active_learning_trials)}\n"
        f"Combat tasks / inactivity timers: {sum(len(t) for t in combat_tasks.values())}/{len(duel_inactivity_timers)}\n"
        f"Database: `{database.DATABASE_FILE}`\nHealth: **{health}**"
    )


@bot.command()
@commands.check(owner_check)
async def backupdb(ctx):
    await asyncio.to_thread(database.backup_database)
    await ctx.send("✅ Database backup created.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        message = f"Missing `{error.param.name}`. Use `!help {ctx.command}`."
    elif isinstance(error, commands.MemberNotFound):
        message = "Player not found. Mention a member of this server."
    elif isinstance(error, commands.NoPrivateMessage):
        message = "Use this command in a server channel."
    elif isinstance(error, (commands.MissingPermissions, commands.NotOwner)):
        message = "You do not have permission to use this command."
    elif isinstance(error, commands.BotMissingPermissions):
        message = "The bot is missing channel permissions. Ask the server owner to check them."
    elif isinstance(error, commands.CheckFailure):
        return  # Existing checks already explain their rejection.
    elif isinstance(error, commands.UserInputError):
        message = f"Invalid arguments. Use `!help {ctx.command}`; numbers must be whole numbers."
    elif isinstance(error, commands.MaxConcurrencyReached):
        message = "Your previous answer is still being processed."
    else:
        original = getattr(error, "original", error)
        logger.error("Command %s failed for user %s", ctx.command, ctx.author.id,
                     exc_info=(type(original), original, original.__traceback__))
        command_name = getattr(ctx.command, "name", None)
        if command_name in {"learn", "answer", "cancellearn"}:
            active_learning_trials.pop(ctx.author.id, None)
        if command_name in {"teach", "askhelp", "duel"}:
            clear_participant_requests({ctx.author.id})
        session = duel_sessions.get(ctx.author.id)
        if session is not None and not session.get("ending"):
            try:
                await abort_session(session, "An internal error interrupted combat")
            except Exception:
                logger.exception("Failed to persist aborted combat")
        if isinstance(original, sqlite3.Error):
            # Do not later persist an in-memory mutation whose database write failed.
            for uid in list(players):
                try:
                    saved = load_player(uid)
                    if saved is not None:
                        players[uid] = saved
                    else:
                        players.pop(uid, None)
                except Exception:
                    players.pop(uid, None)
            active_learning_trials.pop(ctx.author.id, None)
        message = "The command could not be completed. Please try again; details were recorded in the owner logs."
    try:
        await ctx.send(message)
    except discord.HTTPException:
        logger.warning("Could not send command error response")


@bot.before_invoke
async def capture_combat_session(ctx):
    ctx.combat_session_id = duel_sessions.get(ctx.author.id, {}).get("id")


@bot.check
async def guild_context(ctx):
    if ctx.guild is None and ctx.command.name not in {"botstatus", "backupdb", "help", "test"}:
        raise commands.NoPrivateMessage()
    return True


async def periodic_backups():
    while True:
        await asyncio.sleep(86400)
        try:
            await asyncio.to_thread(database.backup_database)
        except Exception:
            logger.exception("Scheduled database backup failed")


async def run_bot():
    global runtime_restored
    restore_runtime()
    runtime_restored = True
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with bot:
        backup_task = asyncio.create_task(periodic_backups())
        maintenance_tasks.add(backup_task)
        backup_task.add_done_callback(maintenance_done)
        runner = asyncio.create_task(bot.start(TOKEN))
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([runner, stopper], return_when=asyncio.FIRST_COMPLETED)
            if runner in done:
                await runner
        finally:
            logger.info("Shutting down; expiring live sessions without rewards or penalties")
            for session in list({s["id"]: s for s in duel_sessions.values()}.values()):
                try:
                    await abort_session(session, "Bot shutting down")
                except Exception:
                    logger.exception("Session shutdown persistence failed")
            tasks = [runner, stopper, *maintenance_tasks]
            for session_id in list(combat_tasks):
                tasks.extend(combat_tasks[session_id])
                cancel_combat_tasks(session_id)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await bot.close()


def validate_configuration(token, owner_id):
    if not token or not token.strip():
        raise SystemExit("DISCORD_TOKEN is missing. Configure .env beside bot.py.")
    if not isinstance(owner_id, int) or owner_id <= 0:
        raise SystemExit("BOT_OWNER_ID must be a positive numeric Discord user ID.")


if __name__ == "__main__":
    configure_logging(TOKEN)
    validate_configuration(TOKEN, OWNER_ID)
    with ProcessLock(Path(__file__).resolve().with_name(".bot.lock")):
        logger.info("Starting private beta bot")
        try:
            init_database()
            healthy, _ = database.database_health()
            if not healthy:
                raise SystemExit("Database integrity check failed; restore a verified backup.")
            database.backup_database()
            asyncio.run(run_bot())
        except Exception:
            logger.exception("Bot startup or lifecycle failed")
            raise SystemExit("Bot stopped after an operational error; check the private logs.")
