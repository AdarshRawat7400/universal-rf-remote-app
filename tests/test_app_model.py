import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from app_model import AppModel


class AppModelTests(unittest.TestCase):
    def test_navigation_history_and_home(self):
        model = AppModel()
        model.open(model.DEVICES)
        model.open(model.REMOTE)
        self.assertEqual(model.DEVICES, model.back())
        self.assertEqual(model.HOME, model.go_home())
        self.assertEqual([], model.history)

    def test_cursor_scrolls_and_clamps_when_rows_change(self):
        model = AppModel(visible_rows=3)
        model.open(model.DEVICES)
        model.move(7, 5)
        self.assertEqual(5, model.cursor)
        self.assertEqual((3, 6), model.visible_range(7))
        self.assertEqual(1, model.clamp(2))
        self.assertEqual((0, 2), model.visible_range(2))

    def test_cursor_does_not_wrap_at_list_ends(self):
        model = AppModel()
        model.open(model.ADD_DEVICE)
        self.assertEqual(0, model.move(3, -1))
        self.assertEqual(2, model.move(3, 99))

    def test_discovery_multi_select_supports_one_many_and_all(self):
        results = [
            {"transport": "wifi", "address": "aa", "name": "One"},
            {"transport": "ble", "address": "bb", "name": "Two"},
            {"transport": "ble", "address": "cc", "name": "Three"},
        ]
        model = AppModel()
        self.assertTrue(model.toggle_result(results[0]))
        self.assertEqual([results[0]], model.selected_from(results))
        self.assertFalse(model.toggle_result(results[0]))
        self.assertEqual(3, model.select_all_results(results))
        self.assertEqual(results, model.selected_from(results))
        model.clear_result_selection()
        self.assertEqual([], model.selected_from(results))

    def test_discovery_keys_require_stable_identity(self):
        with self.assertRaises(ValueError):
            AppModel.result_key({"transport": "wifi"})


if __name__ == "__main__":
    unittest.main()
