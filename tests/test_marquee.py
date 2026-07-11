import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from marquee import marquee_window


class MarqueeTests(unittest.TestCase):
    def test_short_text_does_not_move(self):
        for now in (0, 1000, 5000):
            self.assertEqual(
                "Power", marquee_window("Power", 10, len, now)
            )

    def test_long_text_pauses_then_wraps_without_ellipsis(self):
        self.assertEqual(
            "Long ", marquee_window("Long device name", 5, len, 0)
        )
        self.assertEqual(
            "Long ", marquee_window("Long device name", 5, len, 4 * 180)
        )
        self.assertEqual(
            "ong d", marquee_window("Long device name", 5, len, 6 * 180)
        )

    def test_variable_width_measurement_never_overflows(self):
        def width(value):
            return sum(2 if character == "W" else 1 for character in value)

        for now in range(0, 9000, 137):
            visible = marquee_window("WWW narrow words", 8, width, now)
            self.assertLessEqual(width(visible), 8)


if __name__ == "__main__":
    unittest.main()
