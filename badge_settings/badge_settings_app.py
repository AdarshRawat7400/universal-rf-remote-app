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
                ("IP: " + str(self.values.get("WLED_IP") or "Not set"), "wled_status"),
                ("Edit IP", "wled_ip"),
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
            self.show_details("WLED controller", [self.values.get("WLED_IP") or "Not configured", "Direct IPv4 only"])
        elif action == "wled_ip":
            self.begin_editor("setting", "WLED_IP", "WLED IPv4", self.values.get("WLED_IP") or "", 15)
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

    def update(self):
        if self.state == self.SCANNING and _ticks_due(io.ticks, self.scan_due):
            self.perform_scan()
        if self.state == self.CONNECTING:
            self.poll_connection()
        self.handle_input()
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
