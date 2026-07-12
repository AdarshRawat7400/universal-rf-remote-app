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


class FakeWLEDClient:
    def __init__(self):
        self.calls = []
        self.fail_probe_ip = None
        self.closed = False

    def probe(self, ip_address, timeout=None, response_timeout=None):
        self.calls.append(("probe", ip_address))
        if ip_address == self.fail_probe_ip:
            raise RuntimeError("not a WLED controller")
        return {"ip": ip_address, "name": "Desk WLED", "version": "0.15.3"}

    def get_state(self, ip_address):
        self.calls.append(("state", ip_address))
        return {"on": True, "bri": 128, "seg": [{"fx": 7}]}

    def get_effects(self, ip_address):
        self.calls.append(("effects", ip_address))
        return [(0, "Solid"), (42, "Aurora")]

    def toggle_power(self, ip_address):
        self.calls.append(("toggle", ip_address))
        return {"on": False}

    def set_power(self, ip_address, enabled):
        self.calls.append(("power", ip_address, enabled))
        return {"on": bool(enabled)}

    def set_color(self, ip_address, red, green, blue):
        self.calls.append(("color", ip_address, red, green, blue))
        return {"success": True}

    def set_effect(self, ip_address, effect_id):
        self.calls.append(("effect", ip_address, effect_id))
        return {"success": True}

    def set_brightness(self, ip_address, brightness):
        self.calls.append(("brightness", ip_address, brightness))
        return {"success": True}

    def close(self):
        self.closed = True


class FakeWLEDScanner:
    def __init__(self, client, wlan, saved_ip=None):
        self.client = client
        self.wlan = wlan
        self.saved_ip = saved_ip
        self.results = []
        self.done = False
        self.error = None
        self.scanned = 0
        self.total = 4
        self.closed = False

    def start(self):
        return self

    def step(self):
        self.scanned = self.total
        self.results = [
            {"ip": "192.168.1.44", "name": "Desk WLED", "version": "0.15.3"}
        ]
        self.done = True
        return True

    def close(self):
        self.closed = True


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

    def configure_wled_fakes(self):
        client = FakeWLEDClient()
        self.app.wled_module = types.SimpleNamespace(
            WLEDClient=lambda timeout=1.2: client,
            WLEDScanner=FakeWLEDScanner,
        )
        self.app.wled_client = client
        self.wlan.connected = True
        self.wlan.ip = "192.168.1.20"
        self.wlan.ssid = "Home"
        return client

    def test_wled_category_has_discovery_and_full_controls(self):
        self.app.open_category("wled")
        actions = [row[1] for row in self.app.category_rows()]

        for action in (
            "wled_scan",
            "wled_power",
            "wled_colors",
            "wled_rgb",
            "wled_effects",
            "wled_brightness",
        ):
            self.assertIn(action, actions)

    def test_wled_scan_selection_is_reverified_before_atomic_save(self):
        client = self.configure_wled_fakes()
        self.app.open_category("wled")

        self.app.begin_wled_scan()
        self.assertEqual(self.app.WLED_SCANNING, self.app.state)
        self.app.step_wled_scan()
        self.assertEqual(self.app.WLED_DEVICES, self.app.state)
        self.app.choose_wled_device()
        self.assertEqual("save_wled_device", self.app.confirm_action)
        self.app.run_confirmed_action()
        self.assertEqual(self.app.WLED_REQUEST, self.app.state)
        self.app.perform_wled_request()

        self.assertIn(("probe", "192.168.1.44"), client.calls)
        self.assertEqual("192.168.1.44", self.read_values()["WLED_IP"])
        self.assertEqual(self.app.CATEGORY, self.app.state)

    def test_failed_wled_verification_preserves_saved_ip(self):
        client = self.configure_wled_fakes()
        self.app.save_updates({"WLED_IP": "192.168.1.40"})
        client.fail_probe_ip = "192.168.1.99"
        self.app.pending_wled_device = {
            "ip": "192.168.1.99",
            "name": "Imposter",
        }

        self.app.begin_wled_request("save_device", ("192.168.1.99",))
        self.app.perform_wled_request()

        self.assertEqual("192.168.1.40", self.read_values()["WLED_IP"])
        self.assertEqual(self.app.DETAILS, self.app.state)

    def test_wled_color_and_dynamic_effect_id_are_sent_exactly(self):
        client = self.configure_wled_fakes()
        self.app.save_updates({"WLED_IP": "192.168.1.44"})

        self.app.begin_wled_request(
            "color", ("192.168.1.44", 12, 34, 56), "Color"
        )
        self.app.perform_wled_request()
        self.app.wled_effects = [(42, "Aurora")]
        self.app.state = self.app.WLED_EFFECTS
        self.app.cursor = 0
        self.io.pressed = {self.io.BUTTON_B}
        self.app.handle_input()
        self.io.pressed = set()
        self.app.perform_wled_request()

        self.assertIn(("color", "192.168.1.44", 12, 34, 56), client.calls)
        self.assertIn(("effect", "192.168.1.44", 42), client.calls)

    def test_wled_request_cancel_is_handled_before_network_dispatch(self):
        client = self.configure_wled_fakes()
        self.app.save_updates({"WLED_IP": "192.168.1.44"})
        self.app.begin_wled_request("power", ("192.168.1.44",))
        self.app.wled_request_due = 0
        self.io.pressed = {self.io.BUTTON_A}

        self.app.update()

        self.assertEqual(self.app.CATEGORY, self.app.state)
        self.assertNotIn(("toggle", "192.168.1.44"), client.calls)

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
        wled_client = FakeWLEDClient()
        wled_scanner = FakeWLEDScanner(wled_client, self.wlan)
        self.app.wled_client = wled_client
        self.app.wled_scanner = wled_scanner
        self.app.wled_effects = [(42, "Aurora")]

        self.app.on_exit()

        self.assertEqual("", editor.value)
        self.assertIsNone(self.app.editor)
        self.assertIsNone(self.app.pending_password)
        self.assertGreaterEqual(self.wlan.disconnect_calls, 2)
        self.assertTrue(wled_client.closed)
        self.assertTrue(wled_scanner.closed)
        self.assertEqual([], self.app.wled_effects)


if __name__ == "__main__":
    unittest.main()
