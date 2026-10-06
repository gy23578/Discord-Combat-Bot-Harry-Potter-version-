import unittest
from unittest.mock import patch

import combat


class CombatTests(unittest.TestCase):
    def test_documented_damage_and_recovery(self):
        cases = {
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
        for spell, (low, high, bonus, duration, cooldown) in cases.items():
            for level in (1, 3):
                for endpoint in (low, high):
                    with self.subTest(spell=spell, level=level, endpoint=endpoint):
                        with patch("combat.random.randint", return_value=endpoint) as roll:
                            effect = combat.roll_offensive_effect(spell, level)
                        roll.assert_called_once_with(low, high)
                        self.assertEqual(effect, {
                            "damage": endpoint + (level - 1) * bonus,
                            "duration": duration, "base_cooldown": cooldown,
                        })

    def test_forced_spell_selects_highest_eligible_level(self):
        player = {
            "learned_spells": {"protego", "impero", "avadakedavra", "confringo", "endoloris"},
            "spell_levels": {"protego": 99, "impero": 99, "avadakedavra": 99,
                             "confringo": 2, "endoloris": 3, "bombarda": 99},
        }
        self.assertEqual(combat.choose_forced_spell(player), "endoloris")

    def test_forced_spell_randomizes_ties(self):
        player = {"learned_spells": {"confringo", "stupefy"},
                  "spell_levels": {"confringo": 2, "stupefy": 2}}
        with patch("combat.random.choice", return_value="stupefy") as choice:
            self.assertEqual(combat.choose_forced_spell(player), "stupefy")
        self.assertEqual(set(choice.call_args.args[0]), {"confringo", "stupefy"})

    def test_forced_spell_without_eligible_payload(self):
        player = {"learned_spells": {"protego", "impero", "avadakedavra"},
                  "spell_levels": {}}
        self.assertIsNone(combat.choose_forced_spell(player))

    def test_curse_thresholds_and_shield_damage(self):
        self.assertEqual(combat.CURSE_MIN_ROLLS, {"endoloris": 3, "impero": 4})
        self.assertEqual(combat.PROTEGO_DAMAGE_PERCENT, {"bombarda": 25, "sectumsempra": 25})


if __name__ == "__main__":
    unittest.main()
