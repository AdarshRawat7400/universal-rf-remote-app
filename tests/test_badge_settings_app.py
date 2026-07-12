import importlib
import os
import sys
import tempfile
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
SETTINGS_DIR = os.path.join(ROOT, "badge_settings")
if SETTINGS_DIR not in sys.path:
    sys.path.insert(0, SETTINGS_DIR)

import badge_settings_secrets


class FakeScreen:
    def __init__(self):
        self.font = None
        self.brush = None
        self.text_calls = []

    def clear(self):
        return None

    def draw(self, shape):
        return None

    def text(self, value, x, y):
        self.text_calls.append(str(value))

    def measure_text(self, value):
        return len(str(value)) * 6, 9


class FakePixelFont:
    @staticmethod
    def load(path):
        return path


class FakeBrushes:
    @staticmethod
    def color(*values):
        return values


class FakeShapes:
    @staticmethod
    def rectangle(*values):
        return values


class FakeIO:
    BUTTON_A = "A"
    BUTTON_B = "B"
    BUTTON_C = "C"
    BUTTON_UP = "UP"
    BUTTON_DOWN = "DOWN"

    def __init__(self):
        self.ticks = 0
        self.ticks_delta = 16
        self.pressed = set()
        self.held = set()
        self.released = set()
        self.changed = set()


class FakeWLAN:
    def __init__(self):
        self.active_value = False
        self.connected = False
        self.ip = "0.0.0.0"
        self.ssid = ""
        self.status_value = 1
        self.scan_results = []
        self.connect_calls = []
        self.disconnect_calls = 0

    def active(self, value=None):
        if value is not None:
            self.active_value = bool(value)
        return self.active_value

    def scan(self):
        return list(self.scan_results)

    def connect(self, *args):
        self.connect_calls.append(args)
        self.ssid = args[0]

    def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False
        self.ip = "0.0.0.0"

    def isconnected(self):
        return self.connected

    def status(self):
        return self.status_value

    def ifconfig(self):
        return (self.ip, "255.255.255.0", "10.0.0.1", "10.0.0.1")

    def config(self, name):
        if name == "ssid":
            return self.ssid
        raise ValueError(name)


class BadgeSettingsAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.secrets_path = os.path.join(self.temporary_directory.name, "secrets.py")
        self.write_secrets(
            'WIFI_SSID = "Old WiFi"\n'
            'WIFI_PASSWORD = "old-password"\n'
            'GITHUB_USERNAME = "octocat"\n'
            'GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz"\n'
            'WEATHER_LOCATION = None\n'
            'WLED_IP = None\n'
            'IR_COMPANION_URL = None\n'
            'UNRELATED = "preserve-me"\n'
        )
        self.screen = FakeScreen()
        self.io = FakeIO()
        self.wlan = FakeWLAN()
        self.badgeware_module = types.ModuleType("badgeware")
        self.badgeware_module.PixelFont = FakePixelFont
        self.badgeware_module.brushes = FakeBrushes
        self.badgeware_module.io = self.io
        self.badgeware_module.screen = self.screen
        self.badgeware_module.shapes = FakeShapes
        self.network_module = types.ModuleType("network")
        self.network_module.STA_IF = 0
        self.network_module.STAT_CONNECTING = 1
        self.network_module.STAT_WRONG_PASSWORD = -3
        self.network_module.STAT_NO_AP_FOUND = -2
        self.network_module.STAT_CONNECT_FAIL = -1
        self.network_module.STAT_GOT_IP = 3
        self.network_module.WLAN = lambda interface_id: self.wlan
        self.previous_badgeware = sys.modules.get("badgeware")
        self.previous_network = sys.modules.get("network")
        sys.modules["badgeware"] = self.badgeware_module
        sys.modules["network"] = self.network_module

        real_store = badge_settings_secrets.SecretsStore
        badge_settings_secrets.SecretsStore = lambda: real_store(self.secrets_path)
        try:
            sys.modules.pop("badge_settings_app", None)
            self.module = importlib.import_module("badge_settings_app")
        finally:
            badge_settings_secrets.SecretsStore = real_store
        self.app = self.module.app

    def tearDown(self):
        try:
            self.app.on_exit()
        except Exception:
            pass
        sys.modules.pop("badge_settings_app", None)
        if self.previous_badgeware is None:
            sys.modules.pop("badgeware", None)
        else:
            sys.modules["badgeware"] = self.previous_badgeware
        if self.previous_network is None:
            sys.modules.pop("network", None)
        else:
            sys.modules["network"] = self.previous_network
        self.temporary_directory.cleanup()

    def write_secrets(self, text):
        with open(self.secrets_path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read_values(self):
        return badge_settings_secrets.SecretsStore(self.secrets_path).read_values()

    def test_root_has_five_categories_and_retains_no_secret_values(self):
        self.assertEqual(5, len(self.app.root_rows()))
        self.assertTrue(self.app.token_configured)
        self.assertTrue(self.app.saved_wifi_has_password)
        self.assertIsNone(self.app.values["GITHUB_TOKEN"])
        self.assertIsNone(self.app.values["WIFI_PASSWORD"])
        rendered = repr(self.app.root_rows())
        self.assertNotIn("ghp_", rendered)
        self.assertNotIn("old-password", rendered)

    def test_secured_scan_opens_masked_password_editor(self):
        self.wlan.scan_results = [(b"Home", b"bssid", 6, -42, 3, 0)]
        self.app.open_category("wifi")
        self.app.begin_scan()
        self.app.scan_due = 0
        self.app.update()
        self.assertEqual(self.app.NETWORKS, self.app.state)

        self.app.choose_network()

        self.assertEqual(self.app.EDITOR, self.app.state)
        self.assertEqual("wifi_password", self.app.editor_kind)
        self.assertTrue(self.app.editor.masked)

    def test_hidden_ssid_advances_to_password_editor(self):
        self.app.category = "wifi"
        self.app.begin_editor("hidden_ssid", None, "Hidden SSID", "HiddenNet", 32)

        self.app.finish_editor()

        self.assertEqual("HiddenNet", self.app.pending_network["ssid"])
        self.assertTrue(self.app.pending_network["hidden"])
        self.assertEqual(self.app.EDITOR, self.app.state)
        self.assertEqual("wifi_password", self.app.editor_kind)
        self.assertTrue(self.app.editor.masked)

    def test_new_wifi_is_saved_only_after_successful_dhcp(self):
        self.app.pending_network = {
            "ssid": "New WiFi",
            "security": 3,
            "hidden": False,
            "rssi": -40,
            "channel": 6,
        }
        self.app.pending_password = "new-password"
        self.app.start_pending_connection(save=True)
        self.assertEqual("Old WiFi", self.read_values()["WIFI_SSID"])

        self.wlan.connected = True
        self.wlan.ip = "10.0.0.42"
        self.app.refresh_values = lambda: self.fail(
            "successful commit must not re-read secrets"
        )
        self.app.poll_connection()

        values = self.read_values()
        self.assertEqual("New WiFi", values["WIFI_SSID"])
        self.assertEqual("new-password", values["WIFI_PASSWORD"])
        self.assertEqual("ghp_abcdefghijklmnopqrstuvwxyz", values["GITHUB_TOKEN"])
        self.assertEqual(self.app.RESULT, self.app.state)
        self.assertTrue(self.app.result_success)

    def test_general_save_uses_committed_result_without_file_reread(self):
        self.app.refresh_values = lambda: self.fail(
            "successful commit must not re-read secrets"
        )

        saved = self.app.save_updates({"WEATHER_LOCATION": "Bengaluru"})

        self.assertTrue(saved)
        self.assertEqual("Bengaluru", self.app.values["WEATHER_LOCATION"])
        self.assertEqual("Bengaluru", self.read_values()["WEATHER_LOCATION"])

    def test_failed_wifi_does_not_replace_saved_credentials(self):
        self.app.pending_network = {
            "ssid": "Wrong WiFi",
            "security": 3,
            "hidden": False,
            "rssi": -40,
            "channel": 6,
        }
        self.app.pending_password = "wrong-password"
        self.app.start_pending_connection(save=True)
        self.wlan.status_value = self.network_module.STAT_WRONG_PASSWORD

        self.app.poll_connection()

        values = self.read_values()
        self.assertEqual("Old WiFi", values["WIFI_SSID"])
        self.assertEqual("old-password", values["WIFI_PASSWORD"])
        self.assertFalse(self.app.result_success)

    def test_secret_editor_never_draws_plaintext(self):
        token = "ghp_abcdefghijklmnopqrstuvwxyz"
        self.app.open_category("github")
        self.app.begin_editor("setting", "GITHUB_TOKEN", "GitHub token", token, 255, True)

        self.app.draw()

        self.assertNotIn(token, self.screen.text_calls)
        self.assertTrue(any("*" in value for value in self.screen.text_calls))

    def test_masked_editor_draw_never_reads_plaintext_value(self):
        text_editor = self.module.TextEditor

        class NoPlaintextValueEditor(text_editor):
            @property
            def value(self):
                raise AssertionError("masked drawing decoded plaintext")

        self.app.editor = NoPlaintextValueEditor("top-secret", 64, True)
        self.app.editor_title = "Secret"
        self.app.state = self.app.EDITOR

        self.app.draw()

        self.assertTrue(any("*" in value for value in self.screen.text_calls))

    def test_blank_weather_editor_saves_automatic_none(self):
        self.app.open_category("weather")
        self.app.begin_editor(
            "setting", "WEATHER_LOCATION", "Weather city", "", 64
        )

        self.app.finish_editor()

        self.assertIsNone(self.read_values()["WEATHER_LOCATION"])

    def test_home_cleanup_wipes_editor_and_cancels_attempt(self):
        self.app.open_category("github")
        self.app.begin_editor(
            "setting",
            "GITHUB_TOKEN",
            "GitHub token",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            255,
            True,
        )
        editor = self.app.editor
        self.app.pending_network = {"ssid": "Home"}
        self.app.pending_password = "top-secret"
        self.app.ensure_wifi().start_connect("Home", "top-secret", 0)

        self.app.on_exit()

        self.assertEqual("", editor.value)
        self.assertIsNone(self.app.editor)
        self.assertIsNone(self.app.pending_password)
        self.assertGreaterEqual(self.wlan.disconnect_calls, 2)


if __name__ == "__main__":
    unittest.main()
