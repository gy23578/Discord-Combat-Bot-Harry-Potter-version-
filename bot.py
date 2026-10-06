import asyncio
import os
import random
import time
from database import init_database, save_player, load_player

import discord
from discord.ext import commands
from dotenv import load_dotenv

from quiz_questions import QUIZ_QUESTIONS
from spells import QUIZ_RULES, SPELLS, STARTING_SPELLS


# =========================================================
# CONFIGURATION
# =========================================================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("BOT_OWNER_ID"))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
init_database()


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
    "incendio": 6,
    "levioso": 7,
    "diffindo": 8,
    "depulso": 7,
    "glacius": 6,
    "petrificustotalus": 5,
    "bombarda": 4,
    "arrestomomentum": 6,
    "sectumsempra": 6,
    "avadakedavra": 8,
}

# =========================================================
# GAME DATA
# NOTE: duel/combat state is memory-only. Player progression is persisted in SQLite.
# =========================================================

players = {}
duel_requests = {}
active_duels = {}
pending_attacks = {}
status_effects = {}
active_casts = set()
offensive_cooldowns = {}
active_learning_trials = {}
teacher_help = {}


# =========================================================
# HELPERS
# =========================================================



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

    save_player(
        player["user_id"],
        player
    )



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

    players[user.id]["name"] = (
        user.display_name
    )

    player = players[user.id]

    # Lightweight migration for older saved profiles.
    player.setdefault("profile_started", False)
    player.setdefault("house", None)
    player.setdefault("ready", False)
    player.setdefault("talent_points", 0)
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


def gain_player_xp(player, amount):
    old_level = player["level"]
    player["xp"] = max(0, player["xp"] + amount)
    update_player_level(player)
    new_level = player["level"]

    if new_level > old_level:
        for reached_level in range(old_level + 1, new_level + 1):
            player["talent_points"] += talent_points_for_level(reached_level)

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
    speed = player["stats"]["speed"]
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
    player_speed = player["stats"]["speed"]
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


def finish_attack(attack):
    attacker_id = attack["attacker_id"]
    active_casts.discard(attacker_id)
    start_cooldown(attacker_id, attack["cooldown"])


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


def evaluate_spell_requirements(player, spell_key):
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

    for stat_name, base_required_value in requirements.get("combat_stats", {}).items():
        required_value = ravenclaw_requirement(player, "combat_stat", base_required_value)
        current = player["combat_stats"].get(stat_name, 0)
        display = COMBAT_STAT_DISPLAY_NAMES.get(stat_name, stat_name)
        results.append((
            current >= required_value,
            f"{display}: {required_value} ({current}/{required_value})"
        ))

    for required_spell, base_required_hits in requirements.get("spell_hits", {}).items():
        required_hits = ravenclaw_requirement(player, "spell_hits", base_required_hits)
        current = player["spell_hits"].get(required_spell, 0)
        display = SPELLS[required_spell]["display_name"]
        results.append((
            current >= required_hits,
            f"Land {display} {required_hits} times ({current}/{required_hits})"
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

async def check_duel_end(ctx, loser):
    loser_player = get_player(loser)

    if loser_player["hp"] > 0:
        return False

    loser_player["hp"] = 0

    winner_id = active_duels.get(loser.id)

    if winner_id is None:
        return False

    winner = await bot.fetch_user(winner_id)
    winner_player = get_player(winner)

    # Duel statistics
    winner_player["duel_wins"] += 1
    loser_player["duel_losses"] += 1

    for participant in (winner_player, loser_player):
        participant["duels_completed"] += 1
        participant["duels_since_avada"] += 1

    # XP rewards
    winner_leveled_up = gain_player_xp(
        winner_player,
        50
    )

    loser_leveled_up = gain_player_xp(
        loser_player,
        20
    )

    # Save both players
    persist_player(winner_player)
    persist_player(loser_player)

    await ctx.send(
        f"🏆 **DUEL OVER!**\n"
        f"✨ {winner.display_name} defeats {loser.display_name}!\n"
        f"❤️ {loser.display_name}: **0/{loser_player['max_hp']} HP**\n"
        f"⭐ {winner.display_name} earns **50 XP**.\n"
        f"⭐ {loser.display_name} earns **20 XP**."
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

    # Clean Duel state
    for user_id in (loser.id, winner_id):
        active_duels.pop(user_id, None)
        pending_attacks.pop(user_id, None)
        active_casts.discard(user_id)
        status_effects.pop(user_id, None)
        offensive_cooldowns.pop(user_id, None)

    return True


async def apply_sectumsempra_bleed(ctx, defender_user, attacker_user):
    for delay in (3, 3):
        await asyncio.sleep(delay)

        if defender_user.id not in active_duels:
            return

        defender = get_player(defender_user)
        defender["hp"] = max(0, defender["hp"] - 5)

        await ctx.send(
            f"🩸 **Sectumsempra bleeding deals 5 damage to {defender_user.display_name}.**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        if await check_duel_end(ctx, defender_user):
            return


async def apply_sectumsempra_backlash(ctx, attack, defense_power):
    if attack["spell"] != "sectumsempra":
        return

    if defense_power < attack["power"] + 10:
        return

    attacker_id = attack["attacker_id"]
    if attacker_id not in active_duels:
        return

    attacker_user = await bot.fetch_user(attacker_id)
    attacker = get_player(attacker_user)
    backlash = random.randint(5, 10)
    attacker["hp"] = max(0, attacker["hp"] - backlash)

    await ctx.send(
        f"🩸 **Sectumsempra backfires!**\n"
        f"{attacker_user.display_name} takes **{backlash} backlash damage**.\n"
        f"❤️ HP: **{attacker['hp']}/{attacker['max_hp']}**"
    )

    await check_duel_end(ctx, attacker_user)


async def apply_attack(ctx, defender_user, attack):
    defender = get_player(defender_user)
    spell = attack["spell"]
    attacker_id = attack["attacker_id"]
    attacker_user = await bot.fetch_user(attacker_id)
    attacker = get_player(attacker_user)

    if spell == "confringo":
        damage = attack["damage"]
        defender["hp"] = max(0, defender["hp"] - damage)

        attacker["combat_stats"]["successful_attacks"] += 1
        attacker["spell_hits"]["confringo"] += 1
        persist_player(attacker)

        await ctx.send(
            f"🔥 **Confringo hits {defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        gain_player_xp(attacker, 15)
        gain_spell_xp(attacker, "confringo", 15)
        await check_duel_end(ctx, defender_user)

    elif spell == "expelliarmus":

        damage = attack["damage"]
        duration = attack["duration"]

        # Deal small damage
        defender["hp"] = max(
            0,
            defender["hp"] - damage
        )

        attacker["combat_stats"]["successful_attacks"] += 1
        attacker["spell_hits"]["expelliarmus"] += 1

        gain_player_xp(
            attacker,
            10
        )

        gain_spell_xp(
            attacker,
            "expelliarmus",
            15
        )

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
                defender_user
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

        attacker["combat_stats"]["successful_attacks"] += 1
        attacker["combat_stats"]["successful_control_spells"] += 1
        attacker["spell_hits"]["stupefy"] += 1

        gain_player_xp(attacker, 10)
        gain_spell_xp(attacker, "stupefy", 15)

        if defender["hp"] <= 0:
            await ctx.send(
                f"🔴 **Stupefy hits {defender_user.display_name}!**\n"
                f"💥 Damage: **{damage}**\n"
                f"❤️ HP: **0/{defender['max_hp']}**"
            )
            await check_duel_end(ctx, defender_user)
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

        attacker["combat_stats"]["successful_attacks"] += 1
        attacker["spell_hits"]["sectumsempra"] += 1

        await ctx.send(
            f"🩸 **Sectumsempra hits {defender_user.display_name}!**\n"
            f"💥 Damage: **{damage}**\n"
            f"❤️ HP: **{defender['hp']}/{defender['max_hp']}**"
        )

        gain_player_xp(attacker, 25)
        gain_spell_xp(attacker, "sectumsempra", 20)

        if not await check_duel_end(ctx, defender_user):
            asyncio.create_task(
                apply_sectumsempra_bleed(ctx, defender_user, attacker_user)
            )

    elif spell == "avadakedavra":
        defender["hp"] = 0
        attacker["combat_stats"]["successful_attacks"] += 1
        attacker["spell_hits"]["avadakedavra"] += 1

        await ctx.send(
            f"💀 **AVADA KEDAVRA hits {defender_user.display_name}.**"
        )

        gain_spell_xp(attacker, "avadakedavra", 25)
        await check_duel_end(ctx, defender_user)


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

    attacker_player = get_player(ctx.author)
    cooldown = calculate_cooldown(attacker_player, base_cooldown)

    active_casts.add(ctx.author.id)
    attack_id = time.time_ns()

    pending_attacks[opponent.id] = {
        "id": attack_id,
        "attacker_id": ctx.author.id,
        "spell": spell_name,
        "power": power,
        "accuracy": accuracy,
        "damage": damage,
        "duration": duration,
        "cooldown": cooldown,
    }

    await ctx.send(
        f"🪄 **{ctx.author.display_name} casts {SPELLS[spell_name]['display_name'].upper()} at {opponent.display_name}!**\n"
        f"⏳ {opponent.display_name} has **10 seconds** to react.\n"
        f"Reactions: `!protego`, `!expelliarmus`, or `!dodge`."
    )

    await asyncio.sleep(10)

    if opponent.id not in pending_attacks:
        return

    current_attack = pending_attacks[opponent.id]
    if current_attack["id"] != attack_id:
        return

    del pending_attacks[opponent.id]
    finish_attack(current_attack)

    await ctx.send(
        f"⌛ {opponent.display_name} did not react in time!"
    )

    await apply_attack(ctx, opponent, current_attack)


# =========================================================
# BOT EVENTS
# =========================================================

@bot.event
async def on_ready():
    print(f"Connected as {bot.user}")


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

    teacher_key = (ctx.author.id, spell_key)
    teacher_used = False
    if teacher_key in teacher_help:
        required_score = max(1, required_score - 1)
        teacher_used = True
        teacher_help.pop(teacher_key, None)

    try:
        questions = select_quiz_questions(
            difficulty,
            quiz_rule["questions"],
            spell.get("categories", ["general"]),
        )
    except ValueError:
        await ctx.send("⚠️ The quiz question bank does not contain enough questions yet.")
        return

    active_learning_trials[ctx.author.id] = {
        "spell": spell_key,
        "display_name": spell["display_name"],
        "questions": questions,
        "current_question": 0,
        "score": 0,
        "required_score": required_score,
        "teacher_used": teacher_used,
    }

    ravenclaw_text = (
        "\n🦅 Ravenclaw bonus active: required score reduced by 1."
        if ravenclaw_bonus
        else ""
    )
    teacher_text = (
        "\n👨‍🏫 Teacher bonus active: required score reduced by 1."
        if teacher_used
        else ""
    )

    await ctx.send(
        f"📘 **{spell['display_name']} — Learning Trial**\n"
        f"Difficulty: **{stars(difficulty)}**\n"
        f"Questions: **{len(questions)}**\n"
        f"Required Score: **{required_score}/{len(questions)}**"
        f"{ravenclaw_text}"
        f"{teacher_text}"
    )

    await send_learning_question(ctx, active_learning_trials[ctx.author.id])


@bot.command()
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


@bot.command()
async def teach(ctx, student: discord.Member, *, spell_name: str):
    teacher = get_player(ctx.author)
    student_player = get_player(student)
    spell_key = normalize_spell_name(spell_name)

    if student.id == ctx.author.id:
        await ctx.send("You cannot teach yourself.")
        return

    if spell_key not in SPELLS:
        await ctx.send("❌ Unknown spell.")
        return

    if spell_key not in teacher["learned_spells"]:
        await ctx.send("You cannot teach a spell you have not learned.")
        return

    if teacher["spell_levels"].get(spell_key, 0) < 3:
        await ctx.send(
            f"📚 You need **{SPELLS[spell_key]['display_name']} Spell Level 3** to teach it."
        )
        return

    if spell_key in student_player["learned_spells"]:
        await ctx.send(f"{student.display_name} already knows that spell.")
        return

    teacher_help[(student.id, spell_key)] = ctx.author.id

    await ctx.send(
        f"👨‍🏫 **{ctx.author.display_name} is helping {student.display_name} learn {SPELLS[spell_key]['display_name']}!**\n"
        f"Their next Knowledge Trial for this spell requires **1 fewer correct answer**."
    )


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

    duel_requests[target.id] = ctx.author.id
    await ctx.send(
        f"⚔️ **{ctx.author.display_name} challenges {target.display_name} to a Duel!**\n"
        f"{target.display_name}, use `!accept` or `!decline`."
    )


@bot.command()
async def decline(ctx):
    if ctx.author.id not in duel_requests:
        await ctx.send("You have no Duel request.")
        return

    challenger_id = duel_requests.pop(ctx.author.id)
    challenger = await bot.fetch_user(challenger_id)

    await ctx.send(
        f"❌ {ctx.author.display_name} declined the Duel against {challenger.display_name}."
    )


@bot.command()
async def accept(ctx):
    if ctx.author.id not in duel_requests:
        await ctx.send("You have no Duel request.")
        return

    challenger_id = duel_requests.pop(ctx.author.id)
    active_duels[ctx.author.id] = challenger_id
    active_duels[challenger_id] = ctx.author.id

    challenger = await bot.fetch_user(challenger_id)
    player1 = get_player(challenger)
    player2 = get_player(ctx.author)

    player1["hp"] = player1["max_hp"]
    player2["hp"] = player2["max_hp"]

    for user_id in (challenger_id, ctx.author.id):
        pending_attacks.pop(user_id, None)
        active_casts.discard(user_id)
        status_effects.pop(user_id, None)
        offensive_cooldowns.pop(user_id, None)

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
    damage = random.randint(15, 30) + (spell_level - 1) * 3
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
    damage = random.randint(30, 45)
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

    # The attempt is consumed immediately, hit or miss.
    player["duels_since_avada"] = 0
    persist_player(player)

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
    attack = pending_attacks[ctx.author.id]
    defense_power = calculate_protego_power(defender)

    del pending_attacks[ctx.author.id]
    finish_attack(attack)

    if defense_power >= attack["power"]:
        defender["combat_stats"]["successful_protegos"] += 1
        gain_player_xp(defender, 5)
        gain_spell_xp(defender, "protego", 10)

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
    attack = pending_attacks[ctx.author.id]
    dodge_power = calculate_dodge_power(defender)
    attack_accuracy = attack["accuracy"]

    del pending_attacks[ctx.author.id]
    finish_attack(attack)

    if dodge_power >= attack_accuracy:
        defender["combat_stats"]["successful_dodges"] += 1
        gain_player_xp(defender, 5)

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


@bot.command()
async def expelliarmus(ctx):
    if ctx.author.id not in active_duels:
        await ctx.send("You are not currently in a Duel.")
        return

    player = get_player(ctx.author)

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
        attacker_id = incoming_attack["attacker_id"]
        attacker = await bot.fetch_user(attacker_id)

        del pending_attacks[ctx.author.id]
        finish_attack(incoming_attack)

        if expelliarmus_power >= incoming_attack["power"]:
            status_effects.setdefault(
                attacker_id,
                {}
            )["unarmed_until"] = time.time() + 4
            player["combat_stats"]["successful_counters"] += 1

            await ctx.send(
                f"⚡ **{ctx.author.display_name} counters with EXPELLIARMUS!**\n"
                f"💥 {SPELLS[incoming_attack['spell']]['display_name'].upper()} is interrupted!\n"
                f"🪄 {attacker.display_name} is unarmed for **4 seconds**!\n"

            )

            gain_player_xp(player, 10)
            gain_spell_xp(player, "expelliarmus", 15)
            await apply_sectumsempra_backlash(ctx, incoming_attack, expelliarmus_power)
        else:
            await ctx.send(
                f"⚡ **{ctx.author.display_name} counters with EXPELLIARMUS!**\n"
                f"❌ The counter fails.\n"

            )
            await apply_attack(ctx, ctx.author, incoming_attack)

        return

    opponent = await bot.fetch_user(active_duels[ctx.author.id])

    accuracy = calculate_spell_accuracy(
        player,
        "expelliarmus"
    )

    spell_level = player["spell_levels"]["expelliarmus"]

    damage = (
            random.randint(3, 7)
            + (spell_level - 1)
    )

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
# NOT-YET-MODELED COMBAT SPELLS
# They can be learned now. Their exact combat effects will be
# added once we lock their numbers/mechanics together.
# =========================================================

async def not_yet_modeled_spell(ctx, spell_key):
    player = get_player(ctx.author)
    spell = SPELLS[spell_key]

    if spell_key not in player["learned_spells"]:
        await ctx.send(f"🔒 You have not learned **{spell['display_name']}** yet.")
        return

    await ctx.send(
        f"📘 **{spell['display_name']} is learned, but its combat effect has not been modeled yet.**"
    )


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
    damage = random.randint(4, 8) + (spell_level - 1)
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
    await not_yet_modeled_spell(ctx, "incendio")


@bot.command()
async def levioso(ctx):
    await not_yet_modeled_spell(ctx, "levioso")


@bot.command()
async def diffindo(ctx):
    await not_yet_modeled_spell(ctx, "diffindo")


@bot.command()
async def depulso(ctx):
    await not_yet_modeled_spell(ctx, "depulso")


@bot.command()
async def glacius(ctx):
    await not_yet_modeled_spell(ctx, "glacius")


@bot.command(name="petrificustotalus")
async def petrificus_totalus(ctx):
    await not_yet_modeled_spell(ctx, "petrificustotalus")


@bot.command()
async def bombarda(ctx):
    await not_yet_modeled_spell(ctx, "bombarda")


@bot.command(name="arrestomomentum")
async def arresto_momentum(ctx):
    await not_yet_modeled_spell(ctx, "arrestomomentum")

# =========================================================
# OWNER / ADMIN COMMANDS
# =========================================================

def owner_only(ctx):
    return is_owner(ctx.author)


@bot.command()
async def setstat(ctx, target: discord.Member, stat_name: str, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def addspell(ctx, target: discord.Member, *, spell_name: str):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def removespell(ctx, target: discord.Member, *, spell_name: str):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def setlevel(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    if value < 1:
        await ctx.send("❌ Level must be at least 1.")
        return

    player = get_player(target)
    player["level"] = value
    player["xp"] = (value - 1) * 100
    persist_player(player)
    await ctx.send(f"✅ {target.display_name} is now Level **{value}**.")


@bot.command()
async def setxp(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["xp"] = max(0, value)
    update_player_level(player)
    persist_player(player)

    await ctx.send(
        f"✅ {target.display_name}'s XP was set to **{player['xp']}** "
        f"(Level {player['level']})."
    )


@bot.command()
async def addxp(ctx, target: discord.Member, amount: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def setspelllevel(ctx, target: discord.Member, spell_name: str, level: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def setspellxp(ctx, target: discord.Member, spell_name: str, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def setpoints(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["talent_points"] = max(0, value)
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name} now has **{player['talent_points']} Talent Points**."
    )


@bot.command()
async def addpoints(ctx, target: discord.Member, amount: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["talent_points"] = max(0, player["talent_points"] + amount)
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name} now has **{player['talent_points']} Talent Points**."
    )


@bot.command()
async def sethouse(ctx, target: discord.Member, house_name: str):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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
async def sethp(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["hp"] = max(0, min(value, player["max_hp"]))
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name}'s HP is now **{player['hp']}/{player['max_hp']}**."
    )


@bot.command()
async def setmaxhp(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["max_hp"] = max(1, value)
    player["hp"] = min(player["hp"], player["max_hp"])
    persist_player(player)
    await ctx.send(
        f"✅ {target.display_name}'s Max HP is now **{player['max_hp']}**."
    )


@bot.command()
async def setwins(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["duel_wins"] = max(0, value)
    persist_player(player)
    await ctx.send(f"✅ Wins set to **{player['duel_wins']}**.")


@bot.command()
async def setlosses(ctx, target: discord.Member, value: int):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

    player = get_player(target)
    player["duel_losses"] = max(0, value)
    persist_player(player)
    await ctx.send(f"✅ Losses set to **{player['duel_losses']}**.")


@bot.command()
async def adminprofile(ctx, target: discord.Member):
    if not owner_only(ctx):
        await ctx.send("❌ You are not allowed to use this command.")
        return

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

bot.run(TOKEN)