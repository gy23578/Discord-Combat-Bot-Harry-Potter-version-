"""Discord-independent tuning for the newly implemented combat spells."""
import random

CURSE_MIN_ROLLS = {"endoloris": 3, "impero": 4}
PROTEGO_DAMAGE_PERCENT = {"bombarda": 25, "sectumsempra": 25}

# Damage range, damage per additional spell level, status duration, base recovery.
MODELED_SPELLS = {
    "incendio": (18, 26, 3, 0, 6),
    "diffindo": (26, 36, 4, 0, 6),
    "depulso": (15, 22, 3, 2, 6),
    "glacius": (12, 18, 3, 10, 6),
    "petrificustotalus": (6, 10, 2, 4, 12),
    "bombarda": (38, 52, 4, 0, 15),
    "endoloris": (0, 0, 0, 10, 15),
    "impero": (0, 0, 0, 0, 15),
}


def roll_spell_effect(spell, level):
    low, high, scaling, duration, cooldown = MODELED_SPELLS[spell]
    return {
        "damage": random.randint(low, high) + max(0, level - 1) * scaling,
        "duration": duration,
        "base_cooldown": cooldown,
    }


# Impero is a non-damaging command curse, not a selectable offensive payload.
# An explicit allowlist also keeps Avada Kedavra out regardless of spell level.
FORCEABLE_OFFENSIVE_SPELLS = frozenset({
    "confringo", "expelliarmus", "stupefy", "sectumsempra",
    "incendio", "diffindo", "depulso", "glacius", "petrificustotalus",
    "bombarda", "endoloris",
})


def choose_forced_spell(player):
    candidates = sorted(player["learned_spells"] & FORCEABLE_OFFENSIVE_SPELLS)
    if not candidates:
        return None
    highest = max(player["spell_levels"].get(spell, 0) for spell in candidates)
    return random.choice([spell for spell in candidates
                          if player["spell_levels"].get(spell, 0) == highest])


def roll_offensive_effect(spell, level):
    """Use the same damage and effects for voluntary and compelled casts."""
    if spell == "confringo":
        damage, duration, cooldown = random.randint(24, 36) + max(0, level - 1) * 4, 0, 6
    elif spell == "expelliarmus":
        damage, duration, cooldown = random.randint(8, 12) + max(0, level - 1) * 2, 4, 6
    elif spell == "stupefy":
        damage, duration, cooldown = random.randint(10, 15) + max(0, level - 1) * 2, 2, 6
    elif spell == "sectumsempra":
        damage, duration, cooldown = random.randint(40, 54) + max(0, level - 1) * 4, 0, 15
    else:
        return roll_spell_effect(spell, level)
    return {"damage": damage, "duration": duration, "base_cooldown": cooldown}
