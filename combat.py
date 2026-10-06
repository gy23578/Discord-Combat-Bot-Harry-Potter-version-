"""Discord-independent spell tuning and compelled spell selection."""

import random


MODELED_SPELLS = frozenset({
    "incendio", "diffindo", "depulso", "glacius", "petrificustotalus",
    "bombarda", "endoloris", "impero",
})
CURSE_MIN_ROLLS = {"endoloris": 3, "impero": 4}
PROTEGO_DAMAGE_PERCENT = {"bombarda": 25, "sectumsempra": 25}

# Minimum damage, maximum damage, bonus per level, duration, base recovery.
_OFFENSIVE_EFFECTS = {
    "confringo": (15, 30, 3, 0, 6),
    "expelliarmus": (3, 7, 1, 4, 6),
    "stupefy": (4, 8, 1, 2, 6),
    "sectumsempra": (30, 45, 0, 0, 15),
    "incendio": (10, 18, 2, 0, 6),
    "diffindo": (18, 28, 3, 0, 6),
    "depulso": (8, 14, 2, 2, 6),
    "glacius": (6, 12, 2, 10, 6),
    "petrificustotalus": (0, 3, 1, 4, 12),
    "bombarda": (30, 42, 3, 0, 15),
    "endoloris": (0, 0, 0, 10, 15),
    "impero": (0, 0, 0, 0, 15),
}


def roll_offensive_effect(spell_name, spell_level):
    """Return keyword arguments accepted by the bot's attack helpers."""
    low, high, bonus, duration, cooldown = _OFFENSIVE_EFFECTS[spell_name]
    damage = random.randint(low, high) + max(0, spell_level - 1) * bonus
    return {"damage": damage, "duration": duration, "base_cooldown": cooldown}


def choose_forced_spell(player):
    """Choose the highest-level learned payload, breaking ties randomly."""
    eligible = sorted(
        spell for spell in player["learned_spells"]
        if spell in _OFFENSIVE_EFFECTS and spell != "impero"
    )
    if not eligible:
        return None
    levels = player["spell_levels"]
    highest_level = max(levels.get(spell, 0) for spell in eligible)
    return random.choice([
        spell for spell in eligible if levels.get(spell, 0) == highest_level
    ])
