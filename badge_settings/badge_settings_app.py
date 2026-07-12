"""Category-based settings editor for the GitHub Universe 2025 Badge."""

import gc

try:
    import time
except ImportError:
    time = None

import network
from badgeware import PixelFont, brushes, io, screen, shapes

from badge_settings_model import (
    TextEditor,
    security_is_enterprise,
    security_is_open,
    security_label,
    validate_setting,
    validate_ssid,
    validate_wifi_password,
)
from badge_settings_network import WiFiManager
from badge_settings_secrets import SecretsStore


BACKGROUND = brushes.color(13, 17, 23)
PANEL = brushes.color(28, 35, 45)
SELECTED = brushes.color(211, 250, 55)
TEXT = brushes.color(235, 245, 255)
MUTED = brushes.color(135, 148, 163)
SUCCESS = brushes.color(46, 160, 67)
WARNING = brushes.color(255, 191, 82)
ERROR = brushes.color(248, 81, 73)
INK = brushes.color(13, 17, 23)

ROOT_ITEMS = (
    ("Wi-Fi", "wifi"),
    ("GitHub", "github"),
    ("Weather", "weather"),
    ("WLED", "wled"),
    ("IR Companion", "ir"),
)

WLED_COLOR_PRESETS = (
    ("White", 255, 255, 255),
    ("Warm white", 255, 160, 80),
    ("Red", 255, 0, 0),
    ("Green", 0, 255, 0),
    ("Blue", 0, 0, 255),
    ("Yellow", 255, 255, 0),
    ("Cyan", 0, 255, 255),
    ("Magenta", 255, 0, 255),
    ("Orange", 255, 96, 0),
    ("Purple", 128, 0, 255),
    ("Pink", 255, 80, 160),
)


def _short_error(error):
    text = str(error) or error.__class__.__name__
    lowered = text.lower()
    if "password" in lowered or "token" in lowered:
        return "Sensitive value was rejected"
    return text[:72]


def _ticks_due(now_ms, deadline_ms):
    ticks_diff = None if time is None else getattr(time, "ticks_diff", None)
    if ticks_diff is not None:
        return ticks_diff(int(now_ms), int(deadline_ms)) >= 0
    return int(now_ms) >= int(deadline_ms)


def _ticks_add(now_ms, delta_ms):
    ticks_add = None if time is None else getattr(time, "ticks_add", None)
    if ticks_add is not None:
        return ticks_add(int(now_ms), int(delta_ms))
    return int(now_ms) + int(delta_ms)


class BadgeSettingsApp:
    ROOT = "root"
    CATEGORY = "category"
    DETAILS = "details"
    EDITOR = "editor"
    CONFIRM = "confirm"
    SCANNING = "scanning"
    NETWORKS = "networks"
    REVIEW = "review"
    CONNECTING = "connecting"
    RESULT = "result"
    WLED_SCANNING = "wled_scanning"
    WLED_DEVICES = "wled_devices"
    WLED_COLORS = "wled_colors"
    WLED_RGB = "wled_rgb"
    WLED_EFFECTS = "wled_effects"
    WLED_BRIGHTNESS = "wled_brightness"
    WLED_REQUEST = "wled_request"

    def __init__(self):
        screen.font = PixelFont.load("/system/assets/fonts/ark.ppf")
        self.store = SecretsStore()
        self.wifi = None
        self.values = {}
        self.token_configured = False
        self.saved_wifi_has_password = False
        self.state = self.ROOT
        self.category = None
        self.cursor = 0
        self.scroll = 0
        self.editor = None
        self.editor_kind = None
        self.editor_setting = None
        self.editor_title = ""
        self.editor_return_category = None
        self.networks = []
        self.scan_due = 0
        self.scan_error = None
        self.pending_network = None
        self.pending_password = None
        self.save_on_connect = False
        self.connection_result = None
        self.result_success = False
        self.result_save_failed = False
        self.details_title = ""
        self.details_lines = []
        self.confirm_title = ""
        self.confirm_lines = []
        self.confirm_action = None
        self.flash_text = ""
        self.flash_until = 0
        self.wled_module = None
        self.wled_client = None
        self.wled_scanner = None
        self.wled_devices = []
        self.pending_wled_device = None
        self.wled_effects = []
        self.wled_rgb = [255, 255, 255]
        self.wled_rgb_channel = 0
        self.wled_brightness = 128
        self.wled_request_kind = None
        self.wled_request_args = None
        self.wled_request_due = 0
        self.wled_request_title = "WLED"
        self.wled_request_message = "Working..."
        try:
            self.store.recover()
            self.refresh_values()
        except Exception as error:
            self.details_title = "Secrets error"
            self.details_lines = [_short_error(error), "USB edit may be required"]
            self.state = self.DETAILS

    def refresh_values(self):
        self.accept_values(self.store.read_values())

    def accept_values(self, loaded):
        """Adopt validated store output while dropping retained credentials."""

        self.token_configured = bool(loaded.get("GITHUB_TOKEN"))
        self.saved_wifi_has_password = bool(loaded.get("WIFI_PASSWORD"))
        # Do not retain passwords or tokens in the long-lived UI model.
        loaded["GITHUB_TOKEN"] = None
        loaded["WIFI_PASSWORD"] = None
        self.values = loaded

    def ensure_wifi(self):
        if self.wifi is None:
            self.wifi = WiFiManager(network, timeout_ms=20_000, max_results=16)
        return self.wifi

    def ensure_wled(self):
        if self.wled_module is None:
            import badge_settings_wled as wled_module

            self.wled_module = wled_module
        if self.wled_client is None:
            self.wled_client = self.wled_module.WLEDClient(timeout=1.2)
        return self.wled_client

    def require_wled_network(self, require_ip=True):
        try:
            current = self.ensure_wifi().current()
        except Exception as error:
            self.show_details("WLED", [_short_error(error), "Connect Wi-Fi first"])
            return None
        if not current.get("connected"):
            self.show_details("WLED", ["Badge is not on Wi-Fi", "Connect Wi-Fi first"])
            return None
        ip_address = self.values.get("WLED_IP")
        if require_ip and not ip_address:
            self.show_details("WLED", ["No controller selected", "Use Scan & select"])
            return None
        return ip_address or current.get("ip")

    def flash(self, text, duration_ms=2200):
        self.flash_text = str(text)[:42]
        self.flash_until = _ticks_add(io.ticks, duration_ms)

    def reset_list(self):
        self.cursor = 0
        self.scroll = 0

    def move(self, count):
        if io.BUTTON_UP in io.pressed and self.cursor > 0:
            self.cursor -= 1
        if io.BUTTON_DOWN in io.pressed and self.cursor + 1 < count:
            self.cursor += 1
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + 5:
            self.scroll = self.cursor - 4

    def root_rows(self):
        saved_wifi = self.values.get("WIFI_SSID") or "Not set"
        github_user = self.values.get("GITHUB_USERNAME") or "Not set"
        weather = self.values.get("WEATHER_LOCATION") or "Automatic"
        wled = self.values.get("WLED_IP") or "Not set"
        companion = "Configured" if self.values.get("IR_COMPANION_URL") else "Not set"
        details = (saved_wifi, github_user, weather, wled, companion)
        return [(ROOT_ITEMS[index][0], details[index]) for index in range(len(ROOT_ITEMS))]

    def category_rows(self):
        if self.category == "wifi":
            return (
                ("Status", "status"),
                ("Scan / change", "scan"),
                ("Connect saved", "connect_saved"),
                ("Disconnect", "disconnect"),
                ("Forget saved", "forget_wifi"),
            )
        if self.category == "github":
            username = self.values.get("GITHUB_USERNAME") or "Not set"
            token = "Set" if self.token_configured else "Not set"
            return (
                ("Username: " + username, "github_username"),
                ("Token: " + token, "github_token"),
                ("Clear token", "clear_token"),
                ("Clear GitHub", "clear_github"),
            )
        if self.category == "weather":
            location = self.values.get("WEATHER_LOCATION") or "Automatic"
            return (
                ("Location: " + str(location), "weather_status"),
                ("Edit location", "weather_location"),
                ("Use automatic", "weather_auto"),
            )
        if self.category == "wled":
            return (
                ("Controller: " + str(self.values.get("WLED_IP") or "Not set"), "wled_status"),
                ("Scan & select", "wled_scan"),
                ("Edit IP", "wled_ip"),
                ("Power toggle", "wled_power"),
                ("Color presets", "wled_colors"),
                ("Custom RGB", "wled_rgb"),
                ("Effects", "wled_effects"),
                ("Brightness", "wled_brightness"),
                ("Clear IP", "clear_wled"),
            )
        return (
            (
                "URL: " + str(self.values.get("IR_COMPANION_URL") or "Not set"),
                "ir_status",
            ),
            ("Edit URL", "ir_url"),
            ("Clear URL", "clear_ir"),
        )

    def open_category(self, category):
        self.category = category
        self.state = self.CATEGORY
        self.reset_list()

    def category_title(self):
        return {
            "wifi": "Wi-Fi",
            "github": "GitHub",
            "weather": "Weather",
            "wled": "WLED",
            "ir": "IR Companion",
        }.get(self.category, "Settings")

    def show_details(self, title, lines):
        self.details_title = title
        self.details_lines = [str(line) for line in lines]
        self.state = self.DETAILS

    def begin_editor(self, kind, setting, title, initial, max_bytes, masked=False):
        self.clear_editor()
        self.editor = TextEditor(initial or "", max_bytes=max_bytes, masked=masked)
        self.editor_kind = kind
        self.editor_setting = setting
        self.editor_title = title
        self.editor_return_category = self.category
        self.state = self.EDITOR

    def clear_editor(self):
        if self.editor is not None:
            try:
                self.editor.wipe()
            except Exception:
                pass
        self.editor = None
        self.editor_kind = None
        self.editor_setting = None
        gc.collect()

    def save_updates(self, updates, success_text="Saved; HOME applies"):
        try:
            loaded = self.store.update(updates)
        except Exception as error:
            self.show_details("Save failed", [_short_error(error), "Old settings preserved"])
            return False
        # update() prepares this result before committing, so adopting it does
        # not perform another file parse that could misreport a successful save.
        self.accept_values(loaded)
        self.flash(success_text, 3200)
        return True

    def finish_editor(self):
        value = self.editor.value
        kind = self.editor_kind
        setting = self.editor_setting
        try:
            if kind == "hidden_ssid":
                value = validate_ssid(value, False)
            elif kind == "wifi_password":
                security = None if self.pending_network is None else self.pending_network.get("security")
                value = validate_wifi_password(
                    value, allow_empty=security in (None, 0)
                )
            else:
                if setting == "WEATHER_LOCATION" and not value:
                    value = None
                value = validate_setting(setting, value)
        except Exception as error:
            self.flash(_short_error(error), 3800)
            return

        if kind == "hidden_ssid":
            self.pending_network = {
                "ssid": value,
                "rssi": 0,
                "security": None,
                "hidden": True,
                "channel": 0,
            }
            self.clear_editor()
            self.begin_editor("wifi_password", None, "Wi-Fi password", "", 64, True)
            return
        if kind == "wifi_password":
            self.pending_password = value
            self.clear_editor()
            self.state = self.REVIEW
            return

        self.clear_editor()
        if self.save_updates({setting: value}):
            self.open_category(self.editor_return_category or self.category)

    def editor_input(self):
        if self.editor is None:
            self.open_category(self.category or "wifi")
            return
        if io.BUTTON_UP in io.pressed:
            self.editor.move_group(-1)
        if io.BUTTON_DOWN in io.pressed:
            self.editor.move_group(1)
        if io.BUTTON_A in io.pressed:
            self.editor.move_item(-1)
        if io.BUTTON_C in io.pressed:
            self.editor.move_item(1)
        if io.BUTTON_B in io.pressed:
            if self.editor.selected_item == TextEditor.ACTION_SHOW_HIDE:
                self.flash("Secrets stay masked on screen")
                return
            action = self.editor.activate()
            if action == "done":
                self.finish_editor()
            elif action == "cancel":
                category = self.editor_return_category or self.category
                kind = self.editor_kind
                self.clear_editor()
                if kind in ("hidden_ssid", "wifi_password"):
                    self.state = self.NETWORKS
                else:
                    self.open_category(category)
            elif action == "limit":
                self.flash("Field is at its byte limit")

    def ask_confirm(self, title, lines, action):
        self.confirm_title = title
        self.confirm_lines = [str(line) for line in lines]
        self.confirm_action = action
        self.state = self.CONFIRM

    def run_confirmed_action(self):
        action = self.confirm_action
        if action == "disconnect":
            try:
                self.ensure_wifi().disconnect()
            except Exception as error:
                self.show_details("Disconnect failed", [_short_error(error)])
                return
            self.flash("Wi-Fi disconnected")
        elif action == "forget_wifi":
            try:
                self.ensure_wifi().disconnect()
            except Exception:
                pass
            if not self.save_updates({"WIFI_SSID": "", "WIFI_PASSWORD": ""}, "Saved Wi-Fi removed"):
                return
        elif action == "clear_token":
            if not self.save_updates({"GITHUB_TOKEN": ""}, "GitHub token removed"):
                return
        elif action == "clear_github":
            if not self.save_updates({"GITHUB_USERNAME": "", "GITHUB_TOKEN": ""}, "GitHub settings removed"):
                return
        elif action == "weather_auto":
            if not self.save_updates({"WEATHER_LOCATION": None}, "Weather uses automatic location"):
                return
        elif action == "clear_wled":
            if not self.save_updates({"WLED_IP": None}, "WLED address removed"):
                return
        elif action == "save_wled_device":
            if self.pending_wled_device is None:
                self.show_details("WLED", ["No controller selected"])
                return
            self.begin_wled_request(
                "save_device",
                (self.pending_wled_device.get("ip"),),
                "Verify WLED",
                "Checking controller",
            )
            return
        elif action == "clear_ir":
            if not self.save_updates({"IR_COMPANION_URL": None}, "Companion URL removed"):
                return
        elif action == "connect":
            self.start_pending_connection(save=True)
            return
        self.open_category(self.category)

    def wifi_status(self):
        try:
            current = self.ensure_wifi().current()
        except Exception as error:
            self.show_details("Wi-Fi status", [_short_error(error)])
            return
        saved = self.values.get("WIFI_SSID") or "Not set"
        self.show_details(
            "Wi-Fi status",
            [
                "Connected: " + (current["ssid"] or "No"),
                "IP: " + current["ip"],
                "Saved: " + saved,
                "2.4 GHz networks only",
            ],
        )

    def begin_scan(self):
        self.networks = []
        self.scan_error = None
        self.scan_due = _ticks_add(io.ticks, 120)
        self.state = self.SCANNING
        self.reset_list()
        gc.collect()

    def perform_scan(self):
        try:
            self.networks = self.ensure_wifi().scan()
        except Exception as error:
            self.networks = []
            self.scan_error = _short_error(error)
        self.state = self.NETWORKS
        self.reset_list()

    def network_count(self):
        return len(self.networks) + 1

    def selected_network_row(self):
        if self.cursor < len(self.networks):
            return self.networks[self.cursor]
        return None

    def choose_network(self):
        network_row = self.selected_network_row()
        if network_row is None:
            self.begin_editor("hidden_ssid", None, "Hidden SSID", "", 32, False)
            return
        security = network_row.get("security")
        if security_is_enterprise(security) or security not in (0, 2, 3, 4):
            self.show_details(
                "Unsupported network",
                [security_label(security), "is not supported safely", "Use personal WPA/WPA2"],
            )
            return
        self.pending_network = dict(network_row)
        if security_is_open(network_row.get("security")):
            self.pending_password = ""
            self.state = self.REVIEW
        else:
            self.begin_editor("wifi_password", None, "Wi-Fi password", "", 64, True)

    def connect_saved(self):
        try:
            sensitive = self.store.read_values()
            ssid = sensitive.get("WIFI_SSID") or ""
            password = sensitive.get("WIFI_PASSWORD") or ""
        except Exception as error:
            self.show_details("Saved Wi-Fi", [_short_error(error)])
            return
        if not ssid:
            password = None
            self.show_details("Saved Wi-Fi", ["No saved network", "Use Scan / change"])
            return
        self.pending_network = {"ssid": ssid, "security": None, "hidden": False, "rssi": 0, "channel": 0}
        self.pending_password = password
        self.start_pending_connection(save=False)
        password = None
        sensitive = None
        gc.collect()

    def start_pending_connection(self, save):
        if self.pending_network is None:
            self.show_details("Connect", ["No network selected"])
            return
        try:
            self.ensure_wifi().start_connect(
                self.pending_network["ssid"], self.pending_password or "", io.ticks
            )
        except Exception as error:
            self.show_details("Connection failed", [_short_error(error)])
            return
        self.save_on_connect = bool(save)
        self.connection_result = {"state": "connecting", "message": "Connecting", "ip": "0.0.0.0"}
        self.networks = []
        self.state = self.CONNECTING
        gc.collect()

    def poll_connection(self):
        if self.wifi is None:
            return
        result = self.wifi.poll(io.ticks)
        self.connection_result = result
        if result["state"] == "connecting":
            return
        if result["state"] == "connected":
            self.result_success = True
            self.result_save_failed = False
            if self.save_on_connect:
                try:
                    loaded = self.store.update(
                        {
                            "WIFI_SSID": self.pending_network["ssid"],
                            "WIFI_PASSWORD": self.pending_password or "",
                        }
                    )
                except Exception as error:
                    self.result_save_failed = True
                    result["message"] = "Connected, not saved: " + _short_error(error)
                else:
                    self.accept_values(loaded)
            if not self.result_save_failed:
                self.pending_password = None
            self.state = self.RESULT
            return
        self.result_success = False
        self.result_save_failed = False
        self.state = self.RESULT

    def retry_save(self):
        try:
            loaded = self.store.update(
                {
                    "WIFI_SSID": self.pending_network["ssid"],
                    "WIFI_PASSWORD": self.pending_password or "",
                }
            )
        except Exception as error:
            self.connection_result["message"] = "Save failed: " + _short_error(error)
            return
        self.accept_values(loaded)
        self.result_save_failed = False
        self.pending_password = None
        self.connection_result["message"] = "Connected and saved"

    def begin_wled_request(self, kind, args=(), title="WLED", message="Contacting controller"):
        self.wled_request_kind = kind
        self.wled_request_args = tuple(args)
        self.wled_request_title = title
        self.wled_request_message = message
        self.wled_request_due = _ticks_add(io.ticks, 100)
        self.state = self.WLED_REQUEST

    def clear_wled_request(self):
        self.wled_request_kind = None
        self.wled_request_args = None

    def begin_wled_scan(self):
        if self.require_wled_network(require_ip=False) is None:
            return
        self.stop_wled_scan(clear_results=True)
        try:
            wlan = self.ensure_wifi().ensure_interface()
            client = self.ensure_wled()
            self.wled_scanner = self.wled_module.WLEDScanner(
                client,
                wlan,
                saved_ip=self.values.get("WLED_IP"),
            )
            self.wled_scanner.start()
        except Exception as error:
            self.wled_scanner = None
            self.show_details("WLED scan failed", [_short_error(error)])
            return
        self.wled_devices = []
        self.scan_error = None
        self.state = self.WLED_SCANNING
        self.reset_list()
        gc.collect()

    def stop_wled_scan(self, clear_results=False):
        scanner = self.wled_scanner
        if scanner is not None:
            if not clear_results:
                try:
                    self.wled_devices = list(scanner.results)
                except Exception:
                    self.wled_devices = []
            try:
                scanner.close()
            except Exception:
                pass
        self.wled_scanner = None
        if clear_results:
            self.wled_devices = []
        gc.collect()

    def release_wled_runtime(self):
        self.stop_wled_scan(clear_results=True)
        if self.wled_client is not None:
            try:
                self.wled_client.close()
            except Exception:
                pass
        self.wled_client = None
        self.wled_effects = []
        self.pending_wled_device = None
        self.clear_wled_request()
        gc.collect()

    def step_wled_scan(self):
        scanner = self.wled_scanner
        if scanner is None:
            self.state = self.WLED_DEVICES
            self.reset_list()
            return
        try:
            scanner.step()
            self.wled_devices = scanner.results
        except Exception as error:
            self.scan_error = _short_error(error)
            self.stop_wled_scan(clear_results=False)
            self.state = self.WLED_DEVICES
            self.reset_list()
            return
        if scanner.done:
            self.scan_error = scanner.error
            self.stop_wled_scan(clear_results=False)
            self.state = self.WLED_DEVICES
            self.reset_list()

    def choose_wled_device(self):
        if self.cursor >= len(self.wled_devices):
            return
        self.pending_wled_device = dict(self.wled_devices[self.cursor])
        self.ask_confirm(
            "Save WLED controller?",
            [
                self.pending_wled_device.get("name") or "WLED",
                self.pending_wled_device.get("ip") or "Unknown IP",
            ],
            "save_wled_device",
        )

    def wled_state_object(self, result):
        if isinstance(result, dict) and isinstance(result.get("state"), dict):
            return result["state"]
        return result if isinstance(result, dict) else {}

    def perform_wled_request(self):
        kind = self.wled_request_kind
        args = self.wled_request_args or ()
        if kind is None:
            self.open_category("wled")
            return
        try:
            client = self.ensure_wled()
            if kind == "status":
                ip_address = args[0]
                info = client.probe(ip_address)
                state = self.wled_state_object(client.get_state(ip_address))
                power = "On" if state.get("on") else "Off"
                brightness = int(state.get("bri", 0))
                lines = [
                    (info.get("name") or "WLED") + "  " + power,
                    "IP: " + ip_address,
                    "Version: " + str(info.get("version") or "Unknown"),
                    "Brightness: %d%%" % ((brightness * 100) // 255),
                ]
                segments = state.get("seg") or []
                if segments and isinstance(segments[0], dict):
                    lines.append("Effect ID: " + str(segments[0].get("fx", 0)))
                self.clear_wled_request()
                self.show_details("WLED status", lines)
                return
            if kind == "save_device":
                device = self.pending_wled_device or {}
                ip_address = device.get("ip")
                info = client.probe(ip_address)
                if not self.save_updates({"WLED_IP": ip_address}, "WLED controller saved"):
                    self.clear_wled_request()
                    return
                self.pending_wled_device = None
                self.clear_wled_request()
                self.open_category("wled")
                self.flash("Saved " + str(info.get("name") or "WLED"), 3000)
                return
            if kind == "power":
                ip_address = args[0]
                toggle = getattr(client, "toggle_power", None)
                if toggle is not None:
                    result = toggle(ip_address)
                else:
                    current = self.wled_state_object(client.get_state(ip_address))
                    result = client.set_power(ip_address, not bool(current.get("on")))
                state = self.wled_state_object(result)
                self.clear_wled_request()
                self.open_category("wled")
                self.flash("WLED " + ("on" if state.get("on", True) else "off"))
                return
            if kind == "color":
                ip_address, red, green, blue = args
                client.set_color(ip_address, red, green, blue)
                self.clear_wled_request()
                self.state = self.WLED_COLORS
                self.flash("Color applied")
                return
            if kind == "rgb":
                ip_address, red, green, blue = args
                client.set_color(ip_address, red, green, blue)
                self.clear_wled_request()
                self.state = self.WLED_RGB
                self.flash("RGB color applied")
                return
            if kind == "load_effects":
                ip_address = args[0]
                effects = client.get_effects(ip_address)
                if not effects:
                    raise RuntimeError("controller returned no usable effects")
                self.wled_effects = effects
                self.clear_wled_request()
                self.state = self.WLED_EFFECTS
                self.reset_list()
                return
            if kind == "effect":
                ip_address, effect_id = args
                client.set_effect(ip_address, effect_id)
                self.clear_wled_request()
                self.state = self.WLED_EFFECTS
                self.flash("Effect applied")
                return
            if kind == "load_brightness":
                ip_address = args[0]
                state = self.wled_state_object(client.get_state(ip_address))
                brightness = state.get("bri", 128)
                if isinstance(brightness, int) and 1 <= brightness <= 255:
                    self.wled_brightness = brightness
                self.clear_wled_request()
                self.state = self.WLED_BRIGHTNESS
                return
            if kind == "brightness":
                ip_address, brightness = args
                client.set_brightness(ip_address, brightness)
                self.clear_wled_request()
                self.state = self.WLED_BRIGHTNESS
                self.flash("Brightness applied")
                return
            raise RuntimeError("unsupported WLED request")
        except Exception as error:
            self.clear_wled_request()
            self.show_details(
                "WLED request failed",
                [_short_error(error), "Check power, IP and Wi-Fi"],
            )

    def open_wled_control(self, action):
        ip_address = self.require_wled_network(require_ip=True)
        if ip_address is None:
            return
        if action == "wled_status":
            self.begin_wled_request("status", (ip_address,), "WLED status")
        elif action == "wled_power":
            self.begin_wled_request("power", (ip_address,), "WLED power", "Sending toggle")
        elif action == "wled_colors":
            self.state = self.WLED_COLORS
            self.reset_list()
        elif action == "wled_rgb":
            self.state = self.WLED_RGB
            self.wled_rgb_channel = 0
        elif action == "wled_effects":
            self.begin_wled_request("load_effects", (ip_address,), "WLED effects", "Loading effect list")
        elif action == "wled_brightness":
            self.begin_wled_request("load_brightness", (ip_address,), "WLED brightness", "Reading brightness")

    def open_action(self, action):
        if action == "status":
            self.wifi_status()
        elif action == "scan":
            self.begin_scan()
        elif action == "connect_saved":
            self.connect_saved()
        elif action == "disconnect":
            self.ask_confirm("Disconnect Wi-Fi?", ["Current session will end"], "disconnect")
        elif action == "forget_wifi":
            saved = self.values.get("WIFI_SSID") or "saved network"
            self.ask_confirm("Forget Wi-Fi?", [saved, "Password will be removed"], "forget_wifi")
        elif action == "github_username":
            self.begin_editor("setting", "GITHUB_USERNAME", "GitHub username", self.values.get("GITHUB_USERNAME") or "", 39)
        elif action == "github_token":
            self.begin_editor("setting", "GITHUB_TOKEN", "GitHub token", "", 255, True)
        elif action == "clear_token":
            self.ask_confirm("Clear GitHub token?", ["Public API still works", "with lower rate limits"], "clear_token")
        elif action == "clear_github":
            self.ask_confirm("Clear GitHub settings?", ["Username and token", "will be removed"], "clear_github")
        elif action == "weather_status":
            self.show_details("Weather location", [self.values.get("WEATHER_LOCATION") or "Automatic IP location"])
        elif action == "weather_location":
            initial = self.values.get("WEATHER_LOCATION")
            if not isinstance(initial, str):
                initial = ""
            self.begin_editor("setting", "WEATHER_LOCATION", "Weather city", initial, 64)
        elif action == "weather_auto":
            self.ask_confirm("Use automatic weather?", ["Location will use IP"], "weather_auto")
        elif action == "wled_status":
            self.open_wled_control(action)
        elif action == "wled_scan":
            self.begin_wled_scan()
        elif action == "wled_ip":
            self.begin_editor("setting", "WLED_IP", "WLED IPv4", self.values.get("WLED_IP") or "", 15)
        elif action in (
            "wled_power",
            "wled_colors",
            "wled_rgb",
            "wled_effects",
            "wled_brightness",
        ):
            self.open_wled_control(action)
        elif action == "clear_wled":
            self.ask_confirm("Clear WLED address?", ["WLED app becomes", "unconfigured"], "clear_wled")
        elif action == "ir_status":
            self.show_details("IR companion", [self.values.get("IR_COMPANION_URL") or "Not configured", "Trusted LAN only"])
        elif action == "ir_url":
            self.begin_editor("setting", "IR_COMPANION_URL", "Companion URL", self.values.get("IR_COMPANION_URL") or "http://", 160)
        elif action == "clear_ir":
            self.ask_confirm("Clear companion URL?", ["Backup/restore becomes", "unavailable"], "clear_ir")

    def handle_input(self):
        if self.state == self.ROOT:
            self.move(len(ROOT_ITEMS))
            if io.BUTTON_B in io.pressed:
                self.open_category(ROOT_ITEMS[self.cursor][1])
        elif self.state == self.CATEGORY:
            rows = self.category_rows()
            self.move(len(rows))
            if io.BUTTON_A in io.pressed:
                if self.category == "wled":
                    self.release_wled_runtime()
                self.state = self.ROOT
                self.reset_list()
            elif io.BUTTON_B in io.pressed:
                self.open_action(rows[self.cursor][1])
        elif self.state == self.DETAILS:
            if io.BUTTON_A in io.pressed or io.BUTTON_B in io.pressed:
                if self.category is None:
                    self.state = self.ROOT
                else:
                    self.open_category(self.category)
        elif self.state == self.EDITOR:
            self.editor_input()
        elif self.state == self.CONFIRM:
            if io.BUTTON_A in io.pressed:
                if self.confirm_action == "connect":
                    self.state = self.REVIEW
                elif self.confirm_action == "save_wled_device":
                    self.state = self.WLED_DEVICES
                else:
                    self.open_category(self.category)
            elif io.BUTTON_B in io.pressed:
                self.run_confirmed_action()
        elif self.state == self.SCANNING:
            if io.BUTTON_A in io.pressed:
                self.open_category("wifi")
        elif self.state == self.NETWORKS:
            self.move(self.network_count())
            if io.BUTTON_A in io.pressed:
                self.open_category("wifi")
            elif io.BUTTON_B in io.pressed:
                self.choose_network()
            elif io.BUTTON_C in io.pressed:
                self.begin_scan()
        elif self.state == self.REVIEW:
            if io.BUTTON_A in io.pressed:
                self.pending_password = None
                self.state = self.NETWORKS
            elif io.BUTTON_B in io.pressed:
                self.ask_confirm("Connect to Wi-Fi?", [self.pending_network["ssid"], security_label(self.pending_network.get("security"))], "connect")
        elif self.state == self.CONNECTING:
            if io.BUTTON_A in io.pressed:
                self.ensure_wifi().cancel_attempt()
                self.pending_password = None
                self.open_category("wifi")
        elif self.state == self.RESULT:
            if io.BUTTON_A in io.pressed:
                self.pending_password = None
                self.open_category("wifi")
            elif io.BUTTON_B in io.pressed:
                if self.result_save_failed:
                    self.retry_save()
                elif self.result_success:
                    self.pending_password = None
                    self.state = self.ROOT
                    self.reset_list()
                else:
                    self.start_pending_connection(save=self.save_on_connect)
            elif io.BUTTON_C in io.pressed and not self.result_success:
                self.begin_editor("wifi_password", None, "Wi-Fi password", "", 64, True)
        elif self.state == self.WLED_SCANNING:
            if io.BUTTON_A in io.pressed:
                self.stop_wled_scan(clear_results=False)
                self.open_category("wled")
            elif io.BUTTON_B in io.pressed and self.wled_devices:
                self.stop_wled_scan(clear_results=False)
                self.state = self.WLED_DEVICES
                self.reset_list()
        elif self.state == self.WLED_DEVICES:
            self.move(len(self.wled_devices))
            if io.BUTTON_A in io.pressed:
                self.open_category("wled")
            elif io.BUTTON_B in io.pressed:
                self.choose_wled_device()
            elif io.BUTTON_C in io.pressed:
                self.begin_wled_scan()
        elif self.state == self.WLED_COLORS:
            self.move(len(WLED_COLOR_PRESETS))
            if io.BUTTON_A in io.pressed:
                self.open_category("wled")
            elif io.BUTTON_B in io.pressed:
                ip_address = self.require_wled_network(require_ip=True)
                if ip_address is not None:
                    unused_name, red, green, blue = WLED_COLOR_PRESETS[self.cursor]
                    self.begin_wled_request(
                        "color",
                        (ip_address, red, green, blue),
                        "WLED color",
                        "Applying color",
                    )
        elif self.state == self.WLED_RGB:
            if io.BUTTON_A in io.pressed:
                self.open_category("wled")
            elif io.BUTTON_C in io.pressed:
                self.wled_rgb_channel = (self.wled_rgb_channel + 1) % 3
            elif io.BUTTON_UP in io.pressed:
                channel = self.wled_rgb_channel
                self.wled_rgb[channel] = min(255, self.wled_rgb[channel] + 15)
            elif io.BUTTON_DOWN in io.pressed:
                channel = self.wled_rgb_channel
                self.wled_rgb[channel] = max(0, self.wled_rgb[channel] - 15)
            elif io.BUTTON_B in io.pressed:
                ip_address = self.require_wled_network(require_ip=True)
                if ip_address is not None:
                    self.begin_wled_request(
                        "rgb",
                        (ip_address, self.wled_rgb[0], self.wled_rgb[1], self.wled_rgb[2]),
                        "Custom RGB",
                        "Applying RGB",
                    )
        elif self.state == self.WLED_EFFECTS:
            self.move(len(self.wled_effects))
            if io.BUTTON_A in io.pressed:
                self.wled_effects = []
                self.open_category("wled")
                gc.collect()
            elif io.BUTTON_B in io.pressed and self.wled_effects:
                ip_address = self.require_wled_network(require_ip=True)
                if ip_address is not None:
                    effect_id, unused_name = self.wled_effects[self.cursor]
                    self.begin_wled_request(
                        "effect",
                        (ip_address, effect_id),
                        "WLED effect",
                        "Applying effect",
                    )
            elif io.BUTTON_C in io.pressed:
                ip_address = self.require_wled_network(require_ip=True)
                if ip_address is not None:
                    self.begin_wled_request(
                        "load_effects",
                        (ip_address,),
                        "WLED effects",
                        "Refreshing effects",
                    )
        elif self.state == self.WLED_BRIGHTNESS:
            if io.BUTTON_A in io.pressed:
                self.open_category("wled")
            elif io.BUTTON_UP in io.pressed:
                self.wled_brightness = min(255, self.wled_brightness + 15)
            elif io.BUTTON_DOWN in io.pressed:
                self.wled_brightness = max(1, self.wled_brightness - 15)
            elif io.BUTTON_B in io.pressed:
                ip_address = self.require_wled_network(require_ip=True)
                if ip_address is not None:
                    self.begin_wled_request(
                        "brightness",
                        (ip_address, self.wled_brightness),
                        "WLED brightness",
                        "Applying brightness",
                    )
        elif self.state == self.WLED_REQUEST:
            if io.BUTTON_A in io.pressed:
                self.clear_wled_request()
                self.open_category("wled")

    def update(self):
        if self.state == self.SCANNING and _ticks_due(io.ticks, self.scan_due):
            self.perform_scan()
        if self.state == self.CONNECTING:
            self.poll_connection()
        if self.state == self.WLED_SCANNING:
            self.step_wled_scan()
        self.handle_input()
        # Input gets the first chance to cancel a queued synchronous request.
        if self.state == self.WLED_REQUEST and _ticks_due(io.ticks, self.wled_request_due):
            self.perform_wled_request()
        self.draw()

    def fit_text(self, value, maximum_width):
        value = str(value)
        width, _ = screen.measure_text(value)
        if width <= maximum_width:
            return value
        suffix = "..."
        low = 0
        high = len(value)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = value[:middle] + suffix
            width, _ = screen.measure_text(candidate)
            if width <= maximum_width:
                low = middle
            else:
                high = middle - 1
        return value[:low] + suffix

    def marquee_text(self, value, maximum_width):
        value = str(value)
        if screen.measure_text(value)[0] <= maximum_width:
            return value
        cycle = value + "   "
        offset = (io.ticks // 180) % len(cycle)
        result = ""
        for index in range(len(cycle) * 2):
            character = cycle[(offset + index) % len(cycle)]
            candidate = result + character
            if screen.measure_text(candidate)[0] > maximum_width:
                break
            result = candidate
        return result

    def center_text(self, value, y):
        width, _ = screen.measure_text(value)
        screen.text(value, 80 - width / 2, y)

    def draw_header(self, title):
        screen.brush = PANEL
        screen.draw(shapes.rectangle(0, 0, 160, 17))
        screen.brush = SELECTED
        screen.text(self.fit_text(title, 150), 5, 3)

    def draw_footer(self, text):
        screen.brush = PANEL
        screen.draw(shapes.rectangle(0, 101, 160, 19))
        screen.brush = MUTED
        self.center_text(self.fit_text(text, 154), 105)

    def draw_list(self, title, rows, footer):
        self.draw_header(title)
        visible = rows[self.scroll : self.scroll + 5]
        for offset, row in enumerate(visible):
            index = self.scroll + offset
            y = 19 + offset * 16
            selected = index == self.cursor
            if selected:
                screen.brush = SELECTED
                screen.draw(shapes.rectangle(3, y, 154, 14))
                screen.brush = INK
            else:
                screen.brush = TEXT
            label = row[0]
            detail = row[1] if len(row) > 1 else ""
            visible_label = (
                self.marquee_text(label, 91) if selected else self.fit_text(label, 91)
            )
            screen.text(visible_label, 7, y + 2)
            if detail and detail not in (
                "wifi", "github", "weather", "wled", "ir", "status", "scan",
                "connect_saved", "disconnect", "forget_wifi", "github_username",
                "github_token", "clear_token", "clear_github", "weather_status",
                "weather_location", "weather_auto", "wled_status", "wled_ip",
                "clear_wled", "ir_status", "ir_url", "clear_ir",
            ):
                screen.text(self.fit_text(detail, 52), 103, y + 2)
        if self.scroll > 0:
            screen.brush = MUTED
            screen.text("^", 151, 18)
        if self.scroll + 5 < len(rows):
            screen.brush = MUTED
            screen.text("v", 151, 91)
        self.draw_footer(footer)

    def draw_editor(self):
        self.draw_header(self.editor_title)
        editor = self.editor
        if editor is None:
            return
        value = editor.display_value().replace("\u2022", "*")
        before = value[: editor.cursor]
        start = max(0, len(before) - 18)
        viewport = value[start : start + 22]
        screen.brush = TEXT
        screen.text(self.fit_text(viewport or "<empty>", 150), 5, 23)
        caret_x = 5
        caret_text = before[start:]
        if caret_text:
            caret_x += screen.measure_text(caret_text)[0]
        screen.brush = SELECTED
        screen.draw(shapes.rectangle(min(154, caret_x), 35, 2, 2))
        screen.brush = MUTED
        screen.text("Group: " + str(editor.selected_group), 5, 46)
        screen.brush = SELECTED
        self.center_text("[ " + str(editor.selected_item) + " ]", 62)
        screen.brush = MUTED
        self.center_text("UP/DOWN changes group", 83)
        screen.text("%d/%d" % (editor.byte_length, editor.max_bytes), 119, 46)
        self.draw_footer("A <   B Add/Do   C >")

    def draw_details(self, title, lines, footer="A/B Back"):
        self.draw_header(title)
        screen.brush = TEXT
        y = 25
        for line in lines[:5]:
            screen.text(self.fit_text(line, 150), 5, y)
            y += 14
        self.draw_footer(footer)

    def draw_networks(self):
        rows = []
        for item in self.networks:
            lock = "Open" if security_is_open(item.get("security")) else "Lock"
            rows.append((item["ssid"], "%s %d" % (lock, item["rssi"])))
        rows.append(("Hidden network...", "Manual"))
        title = "Networks"
        if self.scan_error:
            title = "Scan failed"
        self.draw_list(title, rows, "A Back   B Select   C Rescan")

    def draw_wled_devices(self):
        rows = [
            (
                item.get("name") or "WLED",
                item.get("ip") or "?",
            )
            for item in self.wled_devices
        ]
        if not rows:
            rows = [("No WLED found", "Use Edit IP")]
        title = "WLED devices"
        if self.scan_error:
            title = "WLED scan ended"
        self.draw_list(title, rows, "A Back  B Select  C Rescan")

    def draw_wled_rgb(self):
        self.draw_header("Custom RGB")
        labels = ("Red", "Green", "Blue")
        for index in range(3):
            y = 24 + index * 18
            selected = index == self.wled_rgb_channel
            screen.brush = SELECTED if selected else TEXT
            screen.text(("> " if selected else "  ") + labels[index], 8, y)
            screen.text(str(self.wled_rgb[index]), 116, y)
        screen.brush = brushes.color(
            self.wled_rgb[0], self.wled_rgb[1], self.wled_rgb[2]
        )
        screen.draw(shapes.rectangle(54, 80, 52, 14))
        self.draw_footer("A Back B Apply C Channel")

    def draw_wled_brightness(self):
        self.draw_header("WLED brightness")
        percent = (self.wled_brightness * 100) // 255
        screen.brush = TEXT
        self.center_text(str(percent) + "%", 30)
        screen.brush = MUTED
        screen.draw(shapes.rectangle(20, 57, 120, 16))
        screen.brush = SELECTED
        width = max(1, (118 * self.wled_brightness) // 255)
        screen.draw(shapes.rectangle(21, 58, width, 14))
        screen.brush = TEXT
        self.center_text("Value " + str(self.wled_brightness) + "/255", 82)
        self.draw_footer("A Back  B Apply  UP/DOWN")

    def draw_wled_effects(self):
        """Draw only five effect rows so large controller lists do not churn heap."""

        self.draw_header("WLED effects")
        end = min(len(self.wled_effects), self.scroll + 5)
        for index in range(self.scroll, end):
            effect_id, name = self.wled_effects[index]
            offset = index - self.scroll
            y = 19 + offset * 16
            selected = index == self.cursor
            if selected:
                screen.brush = SELECTED
                screen.draw(shapes.rectangle(3, y, 154, 14))
                screen.brush = INK
            else:
                screen.brush = TEXT
            label = self.marquee_text(name, 116) if selected else self.fit_text(name, 116)
            screen.text(label, 7, y + 2)
            screen.text("#" + str(effect_id), 126, y + 2)
        if self.scroll > 0:
            screen.brush = MUTED
            screen.text("^", 151, 18)
        if self.scroll + 5 < len(self.wled_effects):
            screen.brush = MUTED
            screen.text("v", 151, 91)
        self.draw_footer("A Back B Apply C Reload")

    def draw(self):
        screen.brush = BACKGROUND
        screen.clear()
        if self.state == self.ROOT:
            self.draw_list("Badge Settings", self.root_rows(), "B Open   HOME Exit")
        elif self.state == self.CATEGORY:
            rows = [(row[0], "") for row in self.category_rows()]
            self.draw_list(self.category_title(), rows, "A Back   B Open")
        elif self.state == self.DETAILS:
            self.draw_details(self.details_title, self.details_lines)
        elif self.state == self.EDITOR:
            self.draw_editor()
        elif self.state == self.CONFIRM:
            self.draw_details(self.confirm_title, self.confirm_lines, "A Cancel   B Confirm")
        elif self.state == self.SCANNING:
            dots = "." * ((io.ticks // 400) % 4)
            self.draw_details("Wi-Fi scan", ["Scanning nearby 2.4 GHz", "networks" + dots], "A Cancel")
        elif self.state == self.NETWORKS:
            self.draw_networks()
        elif self.state == self.REVIEW:
            network_row = self.pending_network or {"ssid": "?", "security": None}
            lines = [network_row["ssid"], security_label(network_row.get("security")), "Password: " + ("Set" if self.pending_password else "None")]
            if security_is_open(network_row.get("security")):
                lines.append("Open Wi-Fi may be session-only")
            self.draw_details("Review Wi-Fi", lines, "A Back   B Continue")
        elif self.state == self.CONNECTING:
            result = self.connection_result or {"message": "Connecting"}
            self.draw_details("Connecting", [self.pending_network["ssid"], result["message"]], "A Cancel")
        elif self.state == self.RESULT:
            result = self.connection_result or {"message": "Finished", "ip": "0.0.0.0"}
            title = "Connected" if self.result_success else "Connection failed"
            lines = [result["message"]]
            if self.result_success:
                lines.append("IP: " + result.get("ip", "0.0.0.0"))
                lines.append("HOME reloads all apps")
            footer = "A Back   B Retry save" if self.result_save_failed else ("A Back   B Done" if self.result_success else "A Back B Retry C Edit")
            self.draw_details(title, lines, footer)
        elif self.state == self.WLED_SCANNING:
            scanner = self.wled_scanner
            scanned = 0 if scanner is None else getattr(scanner, "scanned", 0)
            total = 0 if scanner is None else getattr(scanner, "total", 0)
            found = len(self.wled_devices)
            self.draw_details(
                "Scanning for WLED",
                [
                    "Same local network",
                    "Checked: %d/%d" % (scanned, total),
                    "Found: " + str(found),
                    "Usually 30-40 seconds",
                ],
                "A Stop   B Results",
            )
        elif self.state == self.WLED_DEVICES:
            self.draw_wled_devices()
        elif self.state == self.WLED_COLORS:
            rows = [
                (item[0], "%d,%d,%d" % (item[1], item[2], item[3]))
                for item in WLED_COLOR_PRESETS
            ]
            self.draw_list("WLED colors", rows, "A Back   B Apply")
        elif self.state == self.WLED_RGB:
            self.draw_wled_rgb()
        elif self.state == self.WLED_EFFECTS:
            self.draw_wled_effects()
        elif self.state == self.WLED_BRIGHTNESS:
            self.draw_wled_brightness()
        elif self.state == self.WLED_REQUEST:
            self.draw_details(
                self.wled_request_title,
                [self.wled_request_message, "Please wait..."],
                "Request in progress",
            )

        if self.flash_text and not _ticks_due(io.ticks, self.flash_until):
            screen.brush = WARNING
            screen.draw(shapes.rectangle(2, 88, 156, 12))
            screen.brush = INK
            self.center_text(self.fit_text(self.flash_text, 150), 89)

    def on_exit(self):
        if self.wifi is not None:
            try:
                self.wifi.close(preserve_connection=not self.wifi.connecting)
            except Exception:
                pass
        self.wifi = None
        self.release_wled_runtime()
        self.wled_module = None
        self.clear_editor()
        self.pending_password = None
        self.pending_network = None
        self.networks = []
        self.values["GITHUB_TOKEN"] = None
        self.values["WIFI_PASSWORD"] = None
        gc.collect()


app = BadgeSettingsApp()


def update():
    app.update()


def on_exit():
    app.on_exit()
