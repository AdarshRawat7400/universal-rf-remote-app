import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from presets import SAMSUNG_TV_COMMANDS, STANDARD_BUTTON_LABELS, samsung_tv_profile
from ir.codec import decode_samsung32, encode_samsung32
from storage import validate_command


class PresetTests(unittest.TestCase):
    def test_samsung_tv_profile_contains_expected_controls(self):
        profile = samsung_tv_profile()
        self.assertEqual({name for name, _ in SAMSUNG_TV_COMMANDS}, set(profile))
        expected = dict(SAMSUNG_TV_COMMANDS)
        for name, command in profile.items():
            canonical = validate_command(command)
            self.assertEqual("samsung32", canonical["format"])
            self.assertEqual(0x07, canonical["address"])
            self.assertEqual(expected[name], canonical["command"])
            self.assertNotIn("pulses", canonical)
            pulses = encode_samsung32(canonical["address"], canonical["command"])
            self.assertEqual(34, len(pulses))
            self.assertEqual(expected[name], decode_samsung32(pulses)["command"])

    def test_standard_learning_template_matches_the_full_samsung_layout(self):
        self.assertEqual(
            tuple(name for name, _command in SAMSUNG_TV_COMMANDS),
            STANDARD_BUTTON_LABELS,
        )
        self.assertGreaterEqual(len(STANDARD_BUTTON_LABELS), 30)

    def test_samsung_profile_can_generate_a_memory_bounded_subset(self):
        subset = samsung_tv_profile(("Power", "Mute"))
        self.assertEqual({"Power", "Mute"}, set(subset))


if __name__ == "__main__":
    unittest.main()
