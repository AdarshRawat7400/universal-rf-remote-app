import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "badge_settings"))

from badge_settings_model import (
    MAX_SCAN_RECORDS,
    SettingsValidationError,
    TextEditor,
    normalize_scan_results,
    security_is_enterprise,
    security_is_open,
    security_label,
    validate_github_token,
    validate_github_username,
    validate_ipv4,
    validate_ir_companion_url,
    validate_setting,
    validate_ssid,
    validate_weather_location,
    validate_wifi_password,
)


def scan(ssid, rssi=-50, security=3, channel=6, hidden=False):
    return (ssid, b"\x00\x01\x02\x03\x04\x05", channel, rssi, security, hidden)


class ScanNormalizationTests(unittest.TestCase):
    def test_native_rows_are_compact_sorted_and_deduplicated(self):
        rows = [
            scan(b"Office", -80, 3, 11),
            scan(b"Guest", -20, 0, 1),
            scan(b"Office", -35, 3, 6),
            scan(b"Office", -40, 0, 1),
        ]
        result = normalize_scan_results(rows)
        self.assertEqual(["Guest", "Office", "Office"], [row["ssid"] for row in result])
        self.assertEqual(-35, result[1]["rssi"])
        self.assertEqual(
            {"ssid", "rssi", "security", "hidden", "channel"}, set(result[0])
        )

    def test_dictionary_rows_and_deterministic_tie_break(self):
        rows = [
            {"ssid": "Same", "rssi": -40, "security": "wpa2", "hidden": 0, "channel": 11},
            {"ssid": "Same", "rssi": -40, "security": 3, "hidden": False, "channel": 1},
        ]
        self.assertEqual(1, normalize_scan_results(rows)[0]["channel"])

    def test_discards_hidden_blank_malformed_control_and_non_utf8_names(self):
        rows = [
            scan(b"", hidden=True),
            scan(b"bad\nname"),
            scan(b"\xff\xfe"),
            scan(b"x" * 33),
            (b"short",),
            scan(b"Good"),
        ]
        self.assertEqual(["Good"], [row["ssid"] for row in normalize_scan_results(rows)])

    def test_rejects_out_of_range_fields_and_bounds_output(self):
        rows = [scan(("N%d" % index).encode(), -index - 1) for index in range(25)]
        rows.extend((scan(b"bad", 1), scan(b"bad2", -20, 999), scan(b"bad3", -20, 3, 0)))
        self.assertEqual(4, len(normalize_scan_results(rows, 4)))
        for maximum in (0, 33, True):
            with self.assertRaises(ValueError):
                normalize_scan_results(rows, maximum)

    def test_input_inspection_is_bounded(self):
        observed = []

        def records():
            for index in range(1000):
                observed.append(index)
                yield scan(b"Same")

        normalize_scan_results(records())
        self.assertEqual(MAX_SCAN_RECORDS, len(observed))

    def test_security_helpers_do_not_echo_unknown_values(self):
        self.assertEqual("Open", security_label(0))
        self.assertEqual("WPA3", security_label("wpa3"))
        self.assertEqual("Secured", security_label("attacker-controlled"))
        self.assertTrue(security_is_open("none"))
        self.assertTrue(security_is_enterprise(5))
        self.assertFalse(security_is_open(True))

    def test_vendor_security_values_match_official_badge_mapping(self):
        results = normalize_scan_results(
            [
                (b"WPA2", b"a", 1, -40, 4194304, 0),
                (b"WPA2 mixed", b"b", 2, -41, 4194308, 0),
                (b"WPA3", b"c", 3, -42, 4194310, 0),
            ]
        )

        self.assertEqual([3, 3, 6], [item["security"] for item in results])


class ValidatorTests(unittest.TestCase):
    def test_ssid_uses_utf8_byte_limit(self):
        self.assertEqual("é" * 16, validate_ssid("é" * 16))
        with self.assertRaises(SettingsValidationError):
            validate_ssid("é" * 17)
        with self.assertRaises(SettingsValidationError):
            validate_ssid("bad\nssid")

    def test_wifi_password_accepts_passphrase_hex_and_open(self):
        self.assertEqual("", validate_wifi_password(""))
        self.assertEqual("12345678", validate_wifi_password("12345678"))
        self.assertEqual("a" * 64, validate_wifi_password("a" * 64))
        for invalid in ("short", "g" * 64, "password\n"):
            with self.assertRaises(SettingsValidationError):
                validate_wifi_password(invalid)

    def test_github_username_and_token(self):
        self.assertEqual("octo-cat", validate_github_username("octo-cat"))
        for invalid in ("-octo", "octo-", "octo--cat", "octo_cat"):
            with self.assertRaises(SettingsValidationError):
                validate_github_username(invalid)
        self.assertEqual("github_pat_" + "a" * 30, validate_github_token("github_pat_" + "a" * 30))
        with self.assertRaises(SettingsValidationError):
            validate_github_token("secret with whitespace")

    def test_weather_optional_and_bounded(self):
        self.assertIsNone(validate_weather_location(None))
        self.assertEqual("Bengaluru, IN", validate_weather_location("Bengaluru, IN"))
        with self.assertRaises(SettingsValidationError):
            validate_weather_location("x" * 65)

    def test_ipv4_rejects_non_device_addresses(self):
        self.assertEqual("192.168.1.50", validate_ipv4("192.168.1.50"))
        for invalid in (
            "192.168.01.2",
            "256.1.1.1",
            "0.0.0.0",
            "224.0.0.1",
            "255.255.255.255",
            "http://1.2.3.4",
            "1.2.3.4:80",
        ):
            with self.assertRaises(SettingsValidationError):
                validate_ipv4(invalid)

    def test_companion_url_is_http_bounded_and_normalized(self):
        self.assertEqual(
            "http://192.168.1.50:8765",
            validate_ir_companion_url("http://192.168.1.50:8765///"),
        )
        self.assertEqual("https://badge.local/base", validate_ir_companion_url("https://badge.local/base/"))
        for invalid in (
            "javascript:alert(1)",
            "http://",
            "http://user:pass@host",
            "http://host:99999",
            "http://host/path?q=x",
            "http://host/a b",
        ):
            with self.assertRaises(SettingsValidationError):
                validate_ir_companion_url(invalid)

    def test_dispatcher_accepts_none_only_for_optional_categories(self):
        for name in ("WEATHER_LOCATION", "WLED_IP", "IR_COMPANION_URL"):
            self.assertIsNone(validate_setting(name, None))
        for name in ("WIFI_SSID", "WIFI_PASSWORD", "GITHUB_USERNAME", "GITHUB_TOKEN"):
            with self.assertRaises(SettingsValidationError):
                validate_setting(name, None)
        with self.assertRaises(SettingsValidationError):
            validate_setting("UNKNOWN", "value")


class TextEditorTests(unittest.TestCase):
    @staticmethod
    def select_action(editor, action):
        while editor.selected_group != "actions":
            editor.move_group(1)
        while editor.selected_item != action:
            editor.move_item(1)

    def test_inserts_at_text_cursor_and_moves_left_right(self):
        editor = TextEditor("ac", max_bytes=8)
        self.select_action(editor, editor.ACTION_LEFT)
        editor.activate()
        self.assertEqual(1, editor.cursor)
        editor.move_group(1)  # wrap to lowercase
        editor.move_item(1)   # b
        self.assertEqual("changed", editor.activate())
        self.assertEqual("abc", editor.value)
        self.assertEqual(2, editor.cursor)

    def test_backspace_and_delete_are_utf8_character_safe(self):
        editor = TextEditor("aéb", max_bytes=8)
        self.select_action(editor, editor.ACTION_LEFT)
        editor.activate()
        self.select_action(editor, editor.ACTION_BACKSPACE)
        editor.activate()
        self.assertEqual("ab", editor.value)
        self.select_action(editor, editor.ACTION_DELETE)
        editor.activate()
        self.assertEqual("a", editor.value)

    def test_byte_limit_is_strict(self):
        editor = TextEditor("é", max_bytes=2)
        self.assertEqual("limit", editor.activate())
        self.assertEqual("é", editor.value)

    def test_mask_show_hide_cancel_and_repr_do_not_reveal_secret(self):
        secret = "hunter22"
        editor = TextEditor(secret, max_bytes=32, masked=True)
        self.assertEqual("*" * len(secret), editor.display_value())
        self.assertNotIn(secret, repr(editor))
        self.select_action(editor, editor.ACTION_SHOW_HIDE)
        editor.activate()
        self.assertEqual(secret, editor.display_value())
        self.select_action(editor, editor.ACTION_CANCEL)
        self.assertEqual("cancel", editor.activate())
        self.assertEqual("", editor.value)
        self.assertEqual(0, editor.cursor)

    def test_byte_length_does_not_require_plaintext_decode(self):
        editor = TextEditor("p\u00e4ss", max_bytes=32, masked=True)
        self.assertEqual(len("p\u00e4ss".encode("utf-8")), editor.byte_length)

    def test_clear_space_done_and_group_cursors(self):
        editor = TextEditor("x", max_bytes=8)
        self.select_action(editor, editor.ACTION_SPACE)
        self.assertEqual("changed", editor.activate())
        self.assertEqual("x ", editor.value)
        self.select_action(editor, editor.ACTION_DONE)
        self.assertEqual("done", editor.activate())
        self.select_action(editor, editor.ACTION_CLEAR)
        editor.activate()
        self.assertEqual("", editor.value)


if __name__ == "__main__":
    unittest.main()
