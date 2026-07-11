"""Full Universal IR application for the GitHub Universe 2025 badge."""

import os
import sys
import gc


APP_DIR = "/system/apps/universal_ir"
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from badgeware import PixelFont, brushes, io, run, screen, shapes

import config
import ui_components as view
from app_model import AppModel
from presets import STANDARD_BUTTON_LABELS, samsung_tv_profile
from storage import ProfileStore


HOME_ITEMS = (
    ("My devices", "devices"),
    ("Add IR remote", "add"),
    ("Nearby radios", "nearby"),
    ("IR diagnostics", "diagnostics"),
    ("SQLite backup", "sync"),
    ("About & limits", "about"),
)

ADD_ITEMS = (
    ("Samsung TV", "35-key preset"),
    ("Blank IR remote", "learn keys"),
)

NEARBY_OPTIONS = (
    ("Wi-Fi + BLE", ("wifi", "ble")),
    ("Wi-Fi access points", ("wifi",)),
    ("BLE advertisers", ("ble",)),
)

NEARBY_ACTIONS = (
    "Save selected",
    "Save all results",
    "Select all",
    "Clear selection",
    "Scan again",
)

SYNC_ITEMS = (
    ("Backup to SQLite", "badge -> companion"),
    ("Restore from SQLite", "companion -> badge"),
    ("Configuration help", "secrets.py"),
)

ABOUT_ROWS = (
    ("IR transmit", "built in"),
    ("IR learn", "2-press verify"),
    ("Smart remotes", "Power may be IR only"),
    ("Samsung Power", "reliable x2"),
    ("Wi-Fi scan", "APs only"),
    ("BLE scan", "advertisers"),
    ("Sub-GHz RF", "needs add-on"),
    ("Badge storage", "crash safe"),
    ("SQLite", "companion"),
)

NAME_CHARACTERS = " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_ ."
MAX_EDITOR_NAME_LENGTH = 48
NAV_REPEAT_MS = 180
LEARN_STAGE_TIMEOUT_MS = 30_000
LISTEN_TEST_TIMEOUT_MS = 15_000


def heap_free_bytes():
    """Return MicroPython free heap, or ``None`` on the desktop simulator."""

    getter = getattr(gc, "mem_free", None)
    if getter is None:
        return None
    try:
        return int(getter())
    except Exception:
        return None


def error_with_heap(error):
    message = str(error)
    free = heap_free_bytes()
    if free is not None:
        message += " (%dK free)" % (free // 1024)
    return message


class UniversalIRApp:
    def __init__(self):
        screen.font = PixelFont.load("/system/assets/fonts/ark.ppf")
        self.store = ProfileStore().load()
        setup_message = self._upgrade_default_profile()
        self.device_summaries = self.store.list_devices()
        self.button_names_cache_device = None
        self.button_names_cache = None

        # Hardware, codec/learning and HTTP modules are deliberately loaded on
        # first use. Importing all of them while a profile is resident leaves
        # too little MicroPython heap for a storage transaction on the badge.
        self.hardware = None
        self.hardware_error = None
        self.hardware_attempted = False

        # Radio discovery is imported only when opened. Keeping bluetooth,
        # network and the 18KB scanner module out of the normal IR path leaves
        # substantially more RAM for learned pulse profiles.
        self.discovery = None
        self.companion = None
        self.companion_error = None
        self.companion_attempted = False
        self.model = AppModel(visible_rows=5)
        self.learning = None

        self.last_navigation_at = -NAV_REPEAT_MS
        self.last_send_at = -config.HOLD_REPEAT_MS
        self.message = setup_message or ""
        self.message_started_at = io.ticks
        self.message_until = io.ticks + (3500 if setup_message else 0)

        self.action_device_id = self.store.device["id"]
        self.learn_button = None
        self.learn_stage_started_at = 0
        self.last_learn_capture = None
        self.last_learn_pairs = 0
        self.last_learn_preview = ""
        self.nearby_results = []
        self.nearby_transports = ("wifi", "ble")
        self.discovery_error_announced = False
        self.last_discovery_poll_at = -250
        self.listen_baseline = 0
        self.listen_last_description = "None yet"
        self.listen_last_pairs = 0
        self.editor_device_id = None
        self.editor_chars = []
        self.editor_position = 0

    def ensure_hardware(self):
        """Load PIO/IR support only when an IR feature is actually used."""

        if self.hardware is not None:
            return True
        if self.hardware_attempted:
            return False
        self.hardware_attempted = True
        gc.collect()
        try:
            from ir.hardware import IRHardware

            self.hardware = IRHardware()
            self.hardware_error = None
        except Exception as error:
            # The desktop simulator intentionally has no rp2/PIO module.
            self.hardware = None
            self.hardware_error = error_with_heap(error)
            # A fragmented heap can recover after the failed constructor and
            # collection. Let the user retry instead of latching that one
            # transient MemoryError for the rest of the app session.
            if isinstance(error, MemoryError):
                self.hardware_attempted = False
        gc.collect()
        return self.hardware is not None

    def ensure_learning(self):
        """Load the codec-heavy learning state machine on first learn only."""

        if self.learning is not None:
            return True
        gc.collect()
        try:
            from learning import LearningSession

            self.learning = LearningSession(
                release_gap_ms=config.LEARNING_RELEASE_GAP_MS,
                max_pairs=config.MAX_CAPTURE_PAIRS,
            )
        except Exception as error:
            self.flash("Learning unavailable: " + error_with_heap(error), 5000)
            return False
        gc.collect()
        return True

    def release_ir_runtime(self):
        """Free capture queues and verified frames before a flash transaction."""

        hardware = self.hardware
        self.hardware = None
        self.hardware_attempted = False
        self.hardware_error = None
        if hardware is not None:
            try:
                hardware.close()
            except Exception:
                pass

        learning = self.learning
        self.learning = None
        if learning is not None:
            try:
                learning.cancel()
            except Exception:
                pass
        # MicroPython keeps local slots as GC roots until they are explicitly
        # cleared. Drop these final references before collecting so the closed
        # receiver queues and learning session are reclaimed for the save.
        hardware = None
        learning = None
        self.learn_button = None
        self.last_learn_capture = None
        self.last_learn_pairs = 0
        self.last_learn_preview = ""
        gc.collect()

    def ensure_companion(self):
        """Load optional HTTP/SQLite-sync support only on its own screen."""

        if self.companion is not None:
            return True
        if self.companion_attempted:
            return False
        self.companion_attempted = True
        gc.collect()
        try:
            from companion_sync import CompanionSync

            self.companion = CompanionSync()
            self.companion_error = None
        except Exception as error:
            self.companion = None
            self.companion_error = str(error)
        gc.collect()
        return self.companion is not None

    def learning_active(self):
        return self.learning is not None and self.learning.active

    def _upgrade_default_profile(self):
        """Extend and compact only explicitly identified Samsung presets."""

        try:
            devices = self.store.list_devices()
            active = self.store.device
            metadata = active.get("transport_metadata") or {}
            tagged = metadata.get("preset") == "samsung-tv-v1"
            power = active["buttons"].get("Power")
            decoded = power.get("decoded") if isinstance(power, dict) else None
            legacy = (
                active["transport"] == "ir"
                and active["name"] == "Samsung TV"
                and isinstance(decoded, dict)
                and decoded.get("protocol") == "SAMSUNG32"
                and decoded.get("address") == 0x07
                and decoded.get("command") == 0x02
            )
            if tagged or legacy:
                preset = samsung_tv_profile()
                updates = {}
                compacted = 0
                added = 0
                for name, compact in preset.items():
                    existing = active["buttons"].get(name)
                    if existing is None:
                        updates[name] = compact
                        added += 1
                        continue
                    existing_decoded = existing.get("decoded") or {}
                    if (
                        existing.get("format") == "raw"
                        and existing_decoded.get("protocol") == "SAMSUNG32"
                        and existing_decoded.get("address") == compact["address"]
                        and existing_decoded.get("command") == compact["command"]
                    ):
                        replacement = dict(compact)
                        for key in (
                            "carrier_hz",
                            "repeat_count",
                            "repeat_gap_us",
                            "description",
                        ):
                            if key in existing:
                                replacement[key] = existing[key]
                        updates[name] = replacement
                        compacted += 1
                if updates:
                    self.store.set_buttons(updates)
                    if compacted:
                        return "Optimized %d; added %d keys" % (compacted, added)
                    return "Added %d Samsung keys" % added
            if (
                len(devices) == 1
                and devices[0]["id"] == "device-1"
                and devices[0]["name"] == "My Remote"
                and devices[0]["button_count"] == 0
            ):
                return "Add an IR remote to start"
        except Exception as error:
            return "Profile setup: " + str(error)
        return None

    def flash(self, message, duration_ms=3000):
        self.message = str(message)[:96]
        self.message_started_at = io.ticks
        self.message_until = io.ticks + int(duration_ms)

    def current_message(self):
        if self.message and io.ticks <= self.message_until:
            return self.message
        return None

    def draw_footer(self, left="", center="", right="", danger=False):
        view.footer(
            left,
            center,
            right,
            message=self.current_message(),
            danger=danger,
            message_elapsed_ms=io.ticks - self.message_started_at,
        )

    def navigation_delta(self):
        if io.BUTTON_UP in io.pressed:
            self.last_navigation_at = io.ticks
            return -1
        if io.BUTTON_DOWN in io.pressed:
            self.last_navigation_at = io.ticks
            return 1
        if io.ticks - self.last_navigation_at >= NAV_REPEAT_MS:
            if io.BUTTON_UP in io.held:
                self.last_navigation_at = io.ticks
                return -1
            if io.BUTTON_DOWN in io.held:
                self.last_navigation_at = io.ticks
                return 1
        return 0

    def move_in(self, count):
        delta = self.navigation_delta()
        if delta:
            self.model.move(count, delta)
        else:
            self.model.clamp(count)

    def device_by_id(self, device_id):
        for device in self.store.data["devices"]:
            if device["id"] == device_id:
                return device
        return None

    def refresh_devices(self):
        self.device_summaries = self.store.list_devices()

    def invalidate_button_names(self):
        self.button_names_cache_device = None
        self.button_names_cache = None

    def summary_by_id(self, device_id):
        for summary in self.device_summaries:
            if summary["id"] == device_id:
                return summary
        return None

    def unique_name(self, base):
        names = [item["name"] for item in self.device_summaries]
        if base not in names:
            return base
        number = 2
        while base + " " + str(number) in names:
            number += 1
        return base + " " + str(number)

    def active_button_names(self):
        device_id = self.store.device["id"]
        if (
            self.button_names_cache_device == device_id
            and self.button_names_cache is not None
        ):
            return self.button_names_cache
        names = list(STANDARD_BUTTON_LABELS)
        for name in self.store.device["buttons"]:
            if name not in names:
                names.append(name)
        self.button_names_cache_device = device_id
        self.button_names_cache = names
        return names

    def selected_device_summary(self):
        devices = self.device_summaries
        self.model.clamp(len(devices))
        if not devices:
            return None
        return devices[self.model.cursor]

    def selected_button_name(self):
        names = self.active_button_names()
        self.model.clamp(len(names))
        return names[self.model.cursor]

    def device_action_rows(self, summary):
        if summary is None:
            return ()
        device = self.device_by_id(summary["id"])
        power = device["buttons"].get("Power") if device is not None else None
        decoded = power.get("decoded") if isinstance(power, dict) else {}
        samsung_power = isinstance(power, dict) and (
            power.get("format") == "samsung32"
            or (isinstance(decoded, dict) and decoded.get("protocol") == "SAMSUNG32")
        )
        samsung_preset = (
            summary["transport_metadata"].get("preset") == "samsung-tv-v1"
        )
        if summary["transport"] == "ir":
            rows = [
                ("Open remote", "open", ""),
                ("Rename", "rename", ""),
            ]
            if samsung_power:
                mode = summary["transport_metadata"].get(
                    "power_burst", "reliable"
                )
                labels = {"single": "x1", "reliable": "x2", "strong": "x3"}
                rows.append(
                    ("Power strength", "power_burst", labels.get(mode, "x2"))
                )
            if samsung_power or samsung_preset:
                stored_command = None
                if isinstance(power, dict):
                    if power.get("format") == "samsung32":
                        stored_command = power.get("command")
                    elif isinstance(decoded, dict):
                        stored_command = decoded.get("command")
                if isinstance(stored_command, int):
                    repair_detail = "%02X>02" % stored_command
                else:
                    repair_detail = "->02"
                rows.append(("Repair Power code", "repair_power", repair_detail))
            rows.append(("Delete", "delete", ""))
            return tuple(rows)
        return (
            ("View details", "open", ""),
            ("Rename", "rename", ""),
            ("Delete", "delete", ""),
        )

    def cycle_power_burst(self, summary):
        metadata = dict(summary["transport_metadata"])
        modes = ("single", "reliable", "strong")
        current = metadata.get("power_burst", "reliable")
        try:
            index = modes.index(current)
        except ValueError:
            index = 1
        mode = modes[(index + 1) % len(modes)]
        metadata["power_burst"] = mode
        self.release_ir_runtime()
        try:
            self.store.update_device_metadata(
                summary["id"], transport_metadata=metadata
            )
        except Exception as error:
            self.flash("Setting failed: " + error_with_heap(error), 5000)
            return
        self.refresh_devices()
        labels = {"single": "x1", "reliable": "x2", "strong": "x3"}
        self.flash("Samsung Power strength " + labels[mode], 3000)

    def repair_samsung_power(self):
        summary = self.summary_by_id(self.action_device_id)
        if summary is None:
            self.flash("Device no longer exists")
            self.model.go_home()
            return
        self.release_ir_runtime()
        try:
            self.store.set_device_buttons(
                summary["id"], samsung_tv_profile(("Power",))
            )
        except Exception as error:
            self.flash("Power repair failed: " + error_with_heap(error), 5000)
            self.model.back()
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.model.back()
        self.flash("Power restored: E0E040BF", 4500)

    def open_device(self, summary, replace=False):
        self.release_ir_runtime()
        try:
            self.store.set_active_device(summary["id"])
        except Exception as error:
            self.flash("Select failed: " + error_with_heap(error), 5000)
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.action_device_id = summary["id"]
        route = (
            self.model.REMOTE
            if summary["transport"] == "ir"
            else self.model.DEVICE_DETAIL
        )
        if replace:
            self.model.replace(route)
        else:
            self.model.open(route)

    def create_preset_device(self):
        self.release_ir_runtime()
        try:
            name = self.unique_name("Samsung TV")
            summary = self.store.create_device(
                name,
                device_type="television",
                transport="ir",
                transport_metadata={
                    "preset": "samsung-tv-v1",
                    "power_burst": "reliable",
                },
                make_active=True,
                buttons=samsung_tv_profile(),
            )
        except Exception as error:
            self.flash("Create failed: " + error_with_heap(error), 5000)
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.action_device_id = summary["id"]
        self.model.replace(self.model.REMOTE)
        self.flash("Samsung remote created")

    def create_blank_device(self):
        self.release_ir_runtime()
        try:
            name = self.unique_name("IR Remote")
            summary = self.store.create_device(
                name,
                device_type="remote",
                transport="ir",
                make_active=True,
            )
        except Exception as error:
            self.flash("Create failed: " + error_with_heap(error), 5000)
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.action_device_id = summary["id"]
        self.model.replace(self.model.REMOTE)
        self.flash("Choose a key; C learns")

    def start_learning(self):
        name = self.selected_button_name()
        # Compile the codec/learning module before allocating the receiver's
        # large fixed-capacity queues. This avoids a high parser+queue peak on
        # the constrained MicroPython heap.
        if not self.ensure_learning():
            return
        if not self.ensure_hardware():
            self.flash("RX unavailable: " + str(self.hardware_error), 5000)
            return
        if self.store.device.get("transport") != "ir":
            self.flash("This device is not an IR remote")
            return
        gc.collect()
        try:
            self.hardware.begin_listening(timeout_ms=LEARN_STAGE_TIMEOUT_MS)
            self.learning.start(io.ticks)
        except Exception as error:
            self.flash("Listen failed: " + error_with_heap(error), 5000)
            return
        self.learn_button = name
        self.learn_stage_started_at = io.ticks
        self.last_learn_capture = None
        self.last_learn_pairs = 0
        self.last_learn_preview = ""

    def cancel_learning(self, message="Learning cancelled"):
        if self.learning is not None:
            self.learning.cancel()
        if self.hardware is not None:
            try:
                self.hardware.end_listening()
            except Exception:
                pass
        self.learn_button = None
        self.flash(message)

    def handle_learning_capture(self, capture):
        from ir.codec import classify_capture, describe_capture

        self.last_learn_pairs = len(capture)
        preview = []
        for pair in capture[:2]:
            try:
                preview.append("%d/%d" % (int(pair[0]), int(pair[1])))
            except (IndexError, TypeError, ValueError):
                preview.append("invalid")
        self.last_learn_preview = " ".join(preview)
        self.last_learn_capture = classify_capture(capture)
        stage_before = self.learning.state
        event = self.learning.feed(capture, io.ticks)
        if event == "first_captured":
            self.learn_stage_started_at = io.ticks
            self.flash("Release the original remote", 1800)
        elif event == "repeat":
            self.learn_stage_started_at = io.ticks
            if stage_before != "wait_release":
                self.flash("Repeat only; tap the key briefly", 3000)
        elif event == "waiting_release":
            self.learn_stage_started_at = io.ticks
        elif event == "malformed":
            self.learn_stage_started_at = io.ticks
            self.flash("Incomplete IR frame: %d pairs" % len(capture), 2600)
        elif event == "invalid":
            self.learn_stage_started_at = io.ticks
            reason = self.learning.last_error or "invalid capture"
            if len(capture) < 4:
                message = "Ignored IR fragment: %d pairs" % len(capture)
            elif "below the normalization quantum" in reason:
                message = "Ignored receiver edge glitch"
            else:
                message = "IR rejected: " + reason
            self.flash(message, 3000)
        elif event == "mismatch":
            self.learn_stage_started_at = io.ticks
            try:
                self.hardware.begin_listening(timeout_ms=LEARN_STAGE_TIMEOUT_MS)
            except Exception:
                pass
            self.flash("Different key; start again", 2500)
        elif event == "confirmed":
            capture = self.learning.result
            info = self.learning.result_info or {}
            decoded = info.get("decoded")
            description = describe_capture(capture)
            name = self.learn_button
            # Learning retains two normalized frames and the receiver queues.
            # Release those references before storage streams the transaction.
            self.release_ir_runtime()
            try:
                self.store.set_button(
                    name,
                    capture,
                    description,
                    decoded,
                    carrier_hz=config.CARRIER_HZ,
                    repeat_count=1,
                    repeat_gap_us=config.TX_INTER_FRAME_GAP_MS * 1000,
                )
            except Exception as error:
                self.flash("Save failed: " + error_with_heap(error), 6000)
            else:
                self.refresh_devices()
                self.invalidate_button_names()
                self.flash("Learned " + name + " - " + description, 4500)

    def update_learning_timer(self):
        if not self.learning_active():
            return
        event = self.learning.tick(io.ticks)
        if event == "ready_confirm":
            self.learn_stage_started_at = io.ticks
            try:
                self.hardware.begin_listening(timeout_ms=LEARN_STAGE_TIMEOUT_MS)
            except Exception as error:
                self.cancel_learning("RX restart failed: " + str(error))
                return
            self.flash("Press the same key again", 2200)
        if io.ticks - self.learn_stage_started_at >= LEARN_STAGE_TIMEOUT_MS:
            diagnostics = self.safe_diagnostics()
            code = diagnostics.get("error_code") if diagnostics else None
            if code == "capture_stalled":
                message = "IR seen, but frame was incomplete"
            elif self.learning.last_error:
                message = "Only IR fragments; try original Power"
            elif code == "no_ir_activity":
                message = "No IR; Smart remote? Try Power"
            else:
                message = "No complete IR frame detected"
            self.cancel_learning(message)

    def safe_diagnostics(self):
        if self.hardware is None:
            return None
        try:
            return self.hardware.diagnostics_snapshot()
        except Exception as error:
            self.hardware_error = error_with_heap(error)
            return None

    def poll_ir(self):
        if self.hardware is None:
            return
        if not (
            self.learning_active() or self.model.route == self.model.LISTEN_TEST
        ):
            return
        try:
            capture = self.hardware.poll_capture()
        except Exception as error:
            if self.learning_active():
                self.cancel_learning("RX failed: " + str(error))
            else:
                self.flash("RX failed: " + str(error), 5000)
            return
        if capture is None:
            return
        if self.learning_active():
            self.handle_learning_capture(capture)
        elif self.model.route == self.model.LISTEN_TEST:
            from ir.codec import describe_capture

            self.listen_last_pairs = len(capture)
            self.listen_last_description = describe_capture(capture)

    def send_button(self, name, holding=False):
        command = self.store.get_button(name)
        if command is None:
            self.flash("%s is empty; press C to learn" % name, 3500)
            return False
        if not self.ensure_hardware():
            self.flash("IR unavailable: " + str(self.hardware_error), 5000)
            return False

        decoded = command.get("decoded") or {}
        command_format = command.get("format", "raw")
        if command_format == "samsung32":
            try:
                from ir.codec import encode_samsung32

                pulses = encode_samsung32(command["address"], command["command"])
            except Exception as error:
                self.flash("Encode failed: " + str(error), 5000)
                return False
            protocol = "SAMSUNG32"
        else:
            pulses = command.get("pulses")
            protocol = decoded.get("protocol")
        burst = None
        if not holding and name == "Power" and protocol == "SAMSUNG32":
            metadata = self.store.device.get("transport_metadata") or {}
            burst = metadata.get("power_burst", "reliable")
            if burst not in config.TX_BURST_PRESETS:
                burst = "reliable"
        try:
            result = self.hardware.send(
                pulses,
                carrier_hz=command.get("carrier_hz", config.CARRIER_HZ),
                repeat_count=1 if holding else command.get("repeat_count", 1),
                repeat_gap_us=command.get(
                    "repeat_gap_us", config.TX_INTER_FRAME_GAP_MS * 1000
                ),
                protocol=protocol,
                burst_preset=burst,
            )
        except Exception as error:
            self.flash("Send failed: " + str(error), 5000)
            return False

        self.last_send_at = io.ticks
        frames = result.get("frames", 1) if isinstance(result, dict) else 1
        if holding:
            self.flash("Holding " + name, 900)
        elif frames > 1:
            self.flash("Sent %s x%d" % (name, frames), 2200)
        else:
            self.flash("Sent " + name, 1600)
        return True

    def begin_listen_test(self):
        if not self.ensure_hardware():
            self.flash("RX unavailable: " + str(self.hardware_error), 5000)
            return
        before = self.safe_diagnostics() or {}
        self.listen_baseline = before.get("capture_count", 0)
        self.listen_last_description = "None yet"
        self.listen_last_pairs = 0
        try:
            self.hardware.begin_listening(timeout_ms=LISTEN_TEST_TIMEOUT_MS)
        except Exception as error:
            self.flash("RX start failed: " + str(error), 5000)
            return
        if self.model.route != self.model.LISTEN_TEST:
            self.model.open(self.model.LISTEN_TEST)
        self.flash("Aim original remote at IR receiver", 2500)

    def start_nearby_scan(self, transports):
        self.nearby_transports = transports
        self.model.clear_result_selection()
        self.discovery_error_announced = False
        try:
            if self.discovery is None:
                from discovery import NearbyDiscovery

                self.discovery = NearbyDiscovery(max_results=24)
            self.discovery.start(transports, clear=True)
            self.nearby_results = self.discovery.results
        except Exception as error:
            self.flash("Scan failed: " + str(error), 5000)
            return
        self.model.open(self.model.NEARBY_RESULTS)

    def save_discovery_results(self, results):
        if not results:
            self.flash("Select at least one result")
            return
        records = []
        for result in results:
            metadata = {
                "address": result["address"],
                "signal": result["signal"],
                "capability": result["capability"],
            }
            device_type = (
                "access-point"
                if result["transport"] == "wifi"
                else "ble-advertiser"
            )
            records.append(
                {
                    "name": result["name"],
                    "transport": result["transport"],
                    "transport_metadata": metadata,
                    "device_type": device_type,
                }
            )
        if self.discovery is not None:
            self.discovery.stop()
        self.discovery = None
        self.nearby_results = []
        self.model.clear_result_selection()
        results = None
        self.release_ir_runtime()
        try:
            saved = self.store.save_discovered_many(records)
        except Exception as error:
            self.flash("Save failed: " + error_with_heap(error), 6000)
            return
        self.refresh_devices()
        self.model.go_home()
        self.model.open(self.model.DEVICES)
        self.flash("Saved %d discovered item(s)" % len(saved), 3500)

    def backup_to_companion(self):
        self.ensure_companion()
        if self.companion is None or not self.companion.configured:
            self.flash("Set IR_COMPANION_URL in secrets.py", 5000)
            return
        self.release_ir_runtime()
        self.flash("Backing up to SQLite...", 1200)
        try:
            acknowledgement = self.companion.push_profile(self.store.data)
        except Exception as error:
            self.flash("Backup failed: " + str(error), 6000)
            return
        self.flash(
            "SQLite backup complete: %d device(s)"
            % acknowledgement["device_count"],
            4500,
        )

    def restore_from_companion(self):
        self.ensure_companion()
        if self.companion is None or not self.companion.configured:
            self.flash("Set IR_COMPANION_URL in secrets.py", 5000)
            self.model.back()
            return
        self.release_ir_runtime()
        self.flash("Restoring SQLite backup...", 1200)
        try:
            profile = self.companion.pull_profile()
            self.store.replace_profile(profile)
        except Exception as error:
            self.flash("Restore failed: " + error_with_heap(error), 6000)
            self.model.back()
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.action_device_id = self.store.device["id"]
        self.model.go_home()
        self.flash(
            "Restored %d device(s) from SQLite" % len(profile["devices"]),
            5000,
        )

    def open_name_editor(self, device_id):
        summary = self.summary_by_id(device_id)
        if summary is None:
            self.flash("Device no longer exists")
            return
        self.editor_device_id = device_id
        self.editor_chars = list(summary["name"][:MAX_EDITOR_NAME_LENGTH]) or ["A"]
        self.editor_position = 0
        self.model.open(self.model.NAME_EDITOR)

    def update_name_editor_input(self):
        if not self.editor_chars:
            self.editor_chars = ["A"]
        if io.BUTTON_UP in io.pressed or io.BUTTON_DOWN in io.pressed:
            delta = -1 if io.BUTTON_UP in io.pressed else 1
            current = self.editor_chars[self.editor_position]
            character_index = NAME_CHARACTERS.find(current)
            if character_index < 0:
                character_index = 0
            character_index = (character_index + delta) % len(NAME_CHARACTERS)
            self.editor_chars[self.editor_position] = NAME_CHARACTERS[character_index]
        if io.BUTTON_B in io.pressed:
            if self.editor_position + 1 < len(self.editor_chars):
                self.editor_position += 1
            elif len(self.editor_chars) < MAX_EDITOR_NAME_LENGTH:
                self.editor_chars.append("A")
                self.editor_position += 1
        if io.BUTTON_A in io.pressed:
            if self.editor_position > 0:
                self.editor_position -= 1
            else:
                self.model.back()
        if io.BUTTON_C in io.pressed:
            name = "".join(self.editor_chars).strip()
            if not name:
                self.flash("Name cannot be empty")
                return
            self.release_ir_runtime()
            try:
                self.store.rename_device(self.editor_device_id, name)
            except Exception as error:
                self.flash("Rename failed: " + error_with_heap(error), 5000)
                return
            self.refresh_devices()
            self.model.back()
            self.flash("Renamed to " + name)

    def go_back(self):
        route = self.model.route
        if route == self.model.HOME:
            return
        if route == self.model.NEARBY_RESULTS:
            if self.discovery is not None:
                self.discovery.stop()
        if route == self.model.LISTEN_TEST and self.hardware is not None:
            try:
                self.hardware.end_listening()
            except Exception:
                pass
        self.model.back()

    def update_home_input(self):
        self.move_in(len(HOME_ITEMS))
        if io.BUTTON_B not in io.pressed:
            return
        action = HOME_ITEMS[self.model.cursor][1]
        if action == "devices":
            self.model.open(self.model.DEVICES)
        elif action == "add":
            self.model.open(self.model.ADD_DEVICE)
        elif action == "nearby":
            self.model.open(self.model.NEARBY_OPTIONS)
        elif action == "diagnostics":
            self.ensure_hardware()
            self.model.open(self.model.DIAGNOSTICS)
        elif action == "sync":
            self.ensure_companion()
            self.model.open(self.model.SYNC)
        elif action == "about":
            self.model.open(self.model.ABOUT)

    def update_devices_input(self):
        devices = self.device_summaries
        self.move_in(len(devices))
        if not devices:
            return
        selected = devices[self.model.cursor]
        if io.BUTTON_B in io.pressed:
            self.open_device(selected)
        elif io.BUTTON_C in io.pressed:
            self.action_device_id = selected["id"]
            self.model.open(self.model.DEVICE_ACTIONS)

    def update_remote_input(self):
        names = self.active_button_names()
        self.move_in(len(names))
        name = names[self.model.cursor]
        if io.BUTTON_B in io.pressed:
            self.send_button(name)
        elif (
            io.BUTTON_B in io.held
            and name in config.REPEATABLE_BUTTONS
            and io.ticks - self.last_send_at >= config.HOLD_REPEAT_MS
        ):
            self.send_button(name, holding=True)
        if io.BUTTON_C in io.pressed:
            self.start_learning()

    def update_device_actions_input(self):
        summary = self.summary_by_id(self.action_device_id)
        if summary is None:
            self.go_back()
            return
        actions = self.device_action_rows(summary)
        self.move_in(len(actions))
        if io.BUTTON_B not in io.pressed:
            return
        action = actions[self.model.cursor][1]
        if action == "open":
            if summary["transport"] == "ir":
                self.open_device(summary, replace=True)
            else:
                # Actions was opened from this same detail screen; avoid a
                # duplicate history entry that makes Back appear unresponsive.
                self.model.back()
        elif action == "rename":
            self.open_name_editor(summary["id"])
        elif action == "power_burst":
            self.cycle_power_burst(summary)
        elif action == "repair_power":
            self.model.open(self.model.POWER_REPAIR_CONFIRM)
        elif action == "delete":
            self.model.open(self.model.DELETE_CONFIRM)

    def update_delete_input(self):
        if io.BUTTON_B not in io.pressed:
            return
        summary = self.summary_by_id(self.action_device_id)
        if summary is None:
            self.model.go_home()
            self.model.open(self.model.DEVICES)
            return
        self.release_ir_runtime()
        try:
            self.store.delete_device(summary["id"])
        except Exception as error:
            self.flash("Delete failed: " + error_with_heap(error), 5000)
            self.model.back()
            return
        self.refresh_devices()
        self.invalidate_button_names()
        self.model.go_home()
        self.model.open(self.model.DEVICES)
        self.flash("Deleted " + summary["name"])

    def update_add_input(self):
        self.move_in(len(ADD_ITEMS))
        if io.BUTTON_B in io.pressed:
            if self.model.cursor == 0:
                self.create_preset_device()
            else:
                self.create_blank_device()

    def update_nearby_options_input(self):
        self.move_in(len(NEARBY_OPTIONS))
        if io.BUTTON_B in io.pressed:
            self.start_nearby_scan(NEARBY_OPTIONS[self.model.cursor][1])

    def update_nearby_results_input(self):
        self.move_in(len(self.nearby_results))
        if io.BUTTON_B in io.pressed and self.nearby_results:
            self.model.toggle_result(self.nearby_results[self.model.cursor])
        if io.BUTTON_C in io.pressed:
            self.model.open(self.model.NEARBY_ACTIONS)

    def update_nearby_actions_input(self):
        self.move_in(len(NEARBY_ACTIONS))
        if io.BUTTON_B not in io.pressed:
            return
        action = self.model.cursor
        if action == 0:
            self.save_discovery_results(
                self.model.selected_from(self.nearby_results)
            )
        elif action == 1:
            self.save_discovery_results(self.nearby_results)
        elif action == 2:
            count = self.model.select_all_results(self.nearby_results)
            self.model.back()
            self.flash("Selected all %d" % count)
        elif action == 3:
            self.model.clear_result_selection()
            self.model.back()
            self.flash("Selection cleared")
        else:
            self.discovery.start(self.nearby_transports, clear=True)
            self.nearby_results = []
            self.discovery_error_announced = False
            self.model.back()
            self.flash("Scanning again")

    def update_diagnostics_input(self):
        self.move_in(14)
        if io.BUTTON_B in io.pressed:
            self.begin_listen_test()
        elif io.BUTTON_C in io.pressed:
            self.send_button("Power")

    def update_sync_input(self):
        self.move_in(len(SYNC_ITEMS))
        if io.BUTTON_B not in io.pressed:
            return
        if self.model.cursor == 0:
            self.backup_to_companion()
        elif self.model.cursor == 1:
            if self.companion is None or not self.companion.configured:
                self.flash("Set IR_COMPANION_URL in secrets.py", 5000)
            else:
                self.model.open(self.model.RESTORE_CONFIRM)
        else:
            self.flash("Add IR_COMPANION_URL to /secrets.py", 5000)

    def update_input(self):
        if self.learning_active():
            if io.BUTTON_A in io.pressed:
                self.cancel_learning()
            return

        route = self.model.route
        if route == self.model.NAME_EDITOR:
            self.update_name_editor_input()
            return
        if route == self.model.DELETE_CONFIRM:
            if io.BUTTON_A in io.pressed:
                self.model.back()
            else:
                self.update_delete_input()
            return
        if route == self.model.POWER_REPAIR_CONFIRM:
            if io.BUTTON_A in io.pressed:
                self.model.back()
            elif io.BUTTON_B in io.pressed:
                self.repair_samsung_power()
            return
        if route == self.model.RESTORE_CONFIRM:
            if io.BUTTON_A in io.pressed:
                self.model.back()
            elif io.BUTTON_B in io.pressed:
                self.restore_from_companion()
            return
        if io.BUTTON_A in io.pressed:
            self.go_back()
            return

        if route == self.model.HOME:
            self.update_home_input()
        elif route == self.model.DEVICES:
            self.update_devices_input()
        elif route == self.model.REMOTE:
            self.update_remote_input()
        elif route == self.model.DEVICE_DETAIL:
            if io.BUTTON_C in io.pressed:
                self.model.open(self.model.DEVICE_ACTIONS)
        elif route == self.model.DEVICE_ACTIONS:
            self.update_device_actions_input()
        elif route == self.model.ADD_DEVICE:
            self.update_add_input()
        elif route == self.model.NEARBY_OPTIONS:
            self.update_nearby_options_input()
        elif route == self.model.NEARBY_RESULTS:
            self.update_nearby_results_input()
        elif route == self.model.NEARBY_ACTIONS:
            self.update_nearby_actions_input()
        elif route == self.model.DIAGNOSTICS:
            self.update_diagnostics_input()
        elif route == self.model.SYNC:
            self.update_sync_input()
        elif route == self.model.LISTEN_TEST:
            if io.BUTTON_B in io.pressed:
                self.begin_listen_test()
        elif route == self.model.ABOUT:
            self.move_in(len(ABOUT_ROWS))

    def draw_home(self):
        view.clear()
        view.header(config.APP_NAME, str(len(self.device_summaries)))
        rows = []
        for label, _action in HOME_ITEMS:
            detail = ""
            if label == "My devices":
                detail = str(len(self.device_summaries))
            rows.append({"label": label, "detail": detail})
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("", "B Open", "HOME Exit")

    def draw_devices(self):
        view.clear()
        devices = self.device_summaries
        view.header("Saved devices", str(len(devices)))
        if devices:
            start, end = self.model.visible_range(len(devices))
        else:
            start, end = 0, 0
        rows = []
        for item in devices[start:end]:
            transport = item["transport"].upper()
            if item["transport"] == "ir":
                detail = "%s %d" % (transport, item["button_count"])
            else:
                detail = transport
            rows.append(
                {
                    "label": item["name"],
                    "detail": detail,
                    "active": item["active"],
                }
            )
        if rows:
            view.menu_rows(
                rows,
                self.model.cursor - start,
                0,
                has_above=start > 0,
                has_below=end < len(devices),
            )
        else:
            view.empty_state("No saved devices", "Add an IR remote")
        self.draw_footer("A Back", "B Open", "C Actions")

    def draw_remote(self):
        view.clear()
        device = self.store.device
        view.header(device["name"], "IR")
        names = self.active_button_names()
        start, end = self.model.visible_range(len(names))
        rows = []
        for name in names[start:end]:
            command = self.store.get_button(name)
            detail = "READY" if command else "LEARN"
            if name == "Power" and command:
                decoded = command.get("decoded") or {}
                if (
                    command.get("format") == "samsung32"
                    or decoded.get("protocol") == "SAMSUNG32"
                ):
                    mode = device.get("transport_metadata", {}).get(
                        "power_burst", "reliable"
                    )
                    detail = {"single": "x1", "reliable": "x2", "strong": "x3"}.get(
                        mode, "x2"
                    )
            rows.append(
                {
                    "label": name,
                    "detail": detail,
                    "checked": command is not None,
                }
            )
        view.menu_rows(
            rows,
            self.model.cursor - start,
            0,
            has_above=start > 0,
            has_below=end < len(names),
        )
        self.draw_footer("A Back", "B Send", "C Learn")

    def draw_device_detail(self):
        view.clear()
        summary = self.summary_by_id(self.action_device_id)
        if summary is None:
            view.header("Device")
            view.empty_state("Device missing")
            self.draw_footer("A Back")
            return
        view.header(summary["name"], summary["transport"].upper())
        metadata = summary["transport_metadata"]
        lines = (
            ("Type", summary["type"]),
            ("Address", metadata.get("address", "unknown")),
            ("Signal", str(metadata.get("signal", "n/a"))),
            ("Control", "not configured"),
        )
        y = 22
        for label, value in lines:
            screen.brush = view.MUTED
            screen.text(label + ":", 6, y)
            screen.brush = view.TEXT
            screen.text(view.fit_text(value, 102), 54, y)
            y += 18
        screen.brush = view.WARNING
        screen.text("Discovery does not pair/control", 6, 91)
        self.draw_footer("A Back", "", "C Actions")

    def draw_device_actions(self):
        view.clear()
        summary = self.summary_by_id(self.action_device_id)
        name = summary["name"] if summary else "Device"
        view.header("Device actions", view.fit_text(name, 55))
        actions = self.device_action_rows(summary)
        rows = []
        for label, action, detail in actions:
            rows.append(
                {
                    "label": label,
                    "detail": detail,
                    "danger": action == "delete",
                }
            )
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("A Back", "B Choose", "")

    def draw_delete_confirm(self):
        view.clear()
        summary = self.summary_by_id(self.action_device_id)
        name = summary["name"] if summary else "this device"
        view.header("Delete device?", "!")
        screen.brush = view.TEXT
        view.centered_text(view.fit_text(name, 145), 34)
        screen.brush = view.DANGER
        view.centered_text("This cannot be undone", 53)
        view.status_pill("B confirms delete", 75, view.DANGER)
        self.draw_footer("A Cancel", "B Delete", "", danger=True)

    def draw_power_repair_confirm(self):
        view.clear()
        summary = self.summary_by_id(self.action_device_id)
        name = summary["name"] if summary else "Samsung remote"
        view.header("Repair Power code?", "IR")
        screen.brush = view.TEXT
        view.centered_text(view.fit_text(name, 145), 25)
        screen.brush = view.WARNING
        view.centered_text("Set Power to E0E040BF", 44)
        screen.brush = view.MUTED
        view.centered_text("All other keys stay unchanged", 63)
        view.status_pill("B confirms repair", 81, view.WARNING)
        self.draw_footer("A Cancel", "B Repair", "")

    def draw_name_editor(self):
        view.clear()
        view.header(
            "Rename device",
            "%d/%d" % (self.editor_position + 1, MAX_EDITOR_NAME_LENGTH),
        )
        name = "".join(self.editor_chars)
        screen.brush = view.TEXT
        view.centered_text(view.marquee_text(name, 148), 28)
        current = self.editor_chars[self.editor_position]
        if current == " ":
            current = "SPACE"
        view.status_pill(current, 51, view.PANEL_SELECTED)
        screen.brush = view.MUTED
        view.centered_text("UP/DOWN changes character", 72)
        view.centered_text("B moves/adds  C saves", 85)
        self.draw_footer("A Prev", "B Next", "C Save")

    def draw_add(self):
        view.clear()
        view.header("Add IR remote")
        rows = [
            {"label": label, "detail": detail} for label, detail in ADD_ITEMS
        ]
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("A Back", "B Create", "")

    def draw_nearby_options(self):
        view.clear()
        view.header("Nearby discovery")
        rows = [
            {"label": label, "detail": "scan"}
            for label, _transports in NEARBY_OPTIONS
        ]
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        screen.brush = view.WARNING
        screen.text("IR and sub-GHz cannot advertise", 5, 88)
        self.draw_footer("A Back", "B Scan", "")

    def nearby_status_text(self):
        if self.discovery.is_scanning:
            return "Scanning... %d found" % len(self.nearby_results)
        if self.nearby_results:
            return "%d result(s); discovery only" % len(self.nearby_results)
        states = self.discovery.status
        messages = []
        for transport in self.nearby_transports:
            item = states[transport]
            if item["state"] not in ("complete", "idle"):
                messages.append(transport.upper() + " " + item["state"])
        return ", ".join(messages) or "No advertisers found"

    def draw_nearby_results(self):
        view.clear()
        badge = "%d/%d" % (
            len(self.model.selected_results),
            len(self.nearby_results),
        )
        view.header("Nearby radios", badge)
        if self.nearby_results:
            start, end = self.model.visible_range(len(self.nearby_results))
        else:
            start, end = 0, 0
        rows = []
        for result in self.nearby_results[start:end]:
            kind = "AP" if result["transport"] == "wifi" else "BLE"
            rows.append(
                {
                    "label": result["name"],
                    "detail": "%s %d" % (kind, result["signal"]),
                    "checked": self.model.is_result_selected(result),
                }
            )
        if rows:
            view.menu_rows(
                rows,
                self.model.cursor - start,
                0,
                has_above=start > 0,
                has_below=end < len(self.nearby_results),
            )
        else:
            view.empty_state(self.nearby_status_text())
        self.draw_footer("A Back", "B Toggle", "C Actions")

    def draw_nearby_actions(self):
        view.clear()
        view.header("Scan actions", str(len(self.model.selected_results)))
        rows = []
        for index, label in enumerate(NEARBY_ACTIONS):
            detail = ""
            if index == 0:
                detail = str(len(self.model.selected_results))
            elif index in (1, 2):
                detail = str(len(self.nearby_results))
            rows.append({"label": label, "detail": detail})
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("A Back", "B Choose", "")

    def draw_diagnostics(self):
        view.clear()
        view.header("IR diagnostics")
        snapshot = self.safe_diagnostics()
        if snapshot is None:
            view.empty_state("IR hardware unavailable", str(self.hardware_error))
        else:
            state = snapshot.get("state", "unknown").upper()
            free_heap = heap_free_bytes()
            heap_text = "n/a" if free_heap is None else "%dK" % (free_heap // 1024)
            repeat_period = snapshot.get("last_repeat_period_us")
            repeat_text = (
                "none"
                if repeat_period is None
                else "%d ms" % (int(repeat_period) // 1000)
            )
            rows = (
                {"label": "State", "detail": state},
                {
                    "label": "Heap free",
                    "detail": heap_text,
                },
                {
                    "label": "Receiver",
                    "detail": "RUNNING"
                    if snapshot.get("receiver_running")
                    else "STOPPED",
                },
                {"label": "Carrier", "detail": "%dHz" % snapshot.get("carrier_hz", 0)},
                {"label": "IR activity", "detail": str(snapshot.get("activity_count", 0))},
                {"label": "RX captures", "detail": str(snapshot.get("capture_count", 0))},
                {"label": "Edge glitches", "detail": str(snapshot.get("boundary_glitches", 0))},
                {"label": "Frame timeouts", "detail": str(snapshot.get("frame_timeout_count", 0))},
                {"label": "Listen timeouts", "detail": str(snapshot.get("listening_timeout_count", 0))},
                {
                    "label": "Discarded",
                    "detail": str(snapshot.get("discarded_count", 0)),
                    "danger": bool(snapshot.get("discarded_count", 0)),
                },
                {"label": "TX frames", "detail": str(snapshot.get("transmit_frame_count", 0))},
                {"label": "Repeat period", "detail": repeat_text},
                {
                    "label": "Late repeats",
                    "detail": str(snapshot.get("late_repeats", 0)),
                    "danger": bool(snapshot.get("late_repeats", 0)),
                },
                {
                    "label": "Last error",
                    "detail": snapshot.get("error_code") or "none",
                    "danger": bool(snapshot.get("error_code")),
                },
            )
            start, _end = self.model.visible_range(len(rows))
            view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("A Back", "B Listen", "C Test Power")

    def draw_sync(self):
        view.clear()
        configured = self.companion is not None and self.companion.configured
        view.header("SQLite companion", "READY" if configured else "SETUP")
        rows = []
        for label, detail in SYNC_ITEMS:
            rows.append(
                {
                    "label": label,
                    "detail": detail,
                    "disabled": not configured and label != "Configuration help",
                }
            )
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        screen.brush = view.SUCCESS if configured else view.WARNING
        if configured:
            screen.text(view.fit_text(self.companion.base_url, 150), 5, 88)
        else:
            screen.text("Set URL in root secrets.py", 5, 88)
        self.draw_footer("A Back", "B Choose", "")

    def draw_restore_confirm(self):
        view.clear()
        view.header("Restore backup?", "!")
        screen.brush = view.TEXT
        view.centered_text("Replace every saved device", 33)
        screen.brush = view.WARNING
        view.centered_text("Local changes will be lost", 52)
        view.status_pill("B confirms restore", 75, view.DANGER)
        self.draw_footer("A Cancel", "B Restore", "", danger=True)

    def draw_listen_test(self):
        view.clear()
        view.header("Receiver listen test", "RX")
        snapshot = self.safe_diagnostics() or {}
        captures = max(0, snapshot.get("capture_count", 0) - self.listen_baseline)
        state = snapshot.get("state", "unavailable").upper()
        color = view.SUCCESS if captures else view.WARNING
        view.status_pill(state, 23, color)
        screen.brush = view.MUTED
        screen.text("Activity", 6, 46)
        screen.text("Frames", 6, 61)
        screen.text("Last", 6, 76)
        screen.brush = view.TEXT
        screen.text(str(snapshot.get("session_activity_count", 0)), 60, 46)
        screen.text(str(captures), 60, 61)
        screen.text(view.fit_text(self.listen_last_description, 96), 60, 76)
        error = snapshot.get("error_code")
        if error:
            screen.brush = view.DANGER
            screen.text(view.fit_text(error, 150), 5, 91)
        else:
            screen.brush = view.MUTED
            screen.text("Press a key on original remote", 5, 91)
        self.draw_footer("A Back", "B Restart", "")

    def draw_learning(self):
        view.clear()
        view.header("Learn " + str(self.learn_button), "IR")
        state = self.learning.state
        if state == "first":
            title = "1/2 PRESS ORIGINAL KEY"
            detail = "Aim at receiver on badge"
        elif state == "wait_release":
            title = "RELEASE THE KEY"
            detail = "Waiting for quiet signal"
        else:
            title = "2/2 PRESS SAME KEY"
            detail = "Confirms a reliable capture"
        view.status_pill(title, 30, view.WARNING)
        screen.brush = view.TEXT
        view.centered_text(detail, 54)
        snapshot = self.safe_diagnostics() or {}
        screen.brush = view.MUTED
        view.centered_text(
            "Activity %d  Frames %d"
            % (
                snapshot.get("session_activity_count", 0),
                snapshot.get("capture_count", 0),
            ),
            72,
        )
        if self.last_learn_capture and not self.learning.last_error:
            protocol = self.last_learn_capture.get("protocol") or "RAW"
            screen.brush = view.SUCCESS
            view.centered_text(
                "Seen: %s (%d pairs)" % (protocol, self.last_learn_pairs), 87
            )
        elif self.last_learn_pairs:
            screen.brush = view.WARNING
            detail = "%dp %s" % (self.last_learn_pairs, self.last_learn_preview)
            view.centered_text(view.marquee_text(detail, 150), 87)
        else:
            screen.brush = view.MUTED
            view.centered_text("Smart remote? Try Power first", 87)
        self.draw_footer("A Cancel", "", "")

    def draw_about(self):
        view.clear()
        view.header("Capabilities & limits")
        rows = [
            {"label": label, "detail": detail} for label, detail in ABOUT_ROWS
        ]
        start, _end = self.model.visible_range(len(rows))
        view.menu_rows(rows, self.model.cursor, start)
        self.draw_footer("A Back", "UP/DOWN", "")

    def draw(self):
        if self.learning_active():
            self.draw_learning()
            return
        route = self.model.route
        if route == self.model.HOME:
            self.draw_home()
        elif route == self.model.DEVICES:
            self.draw_devices()
        elif route == self.model.REMOTE:
            self.draw_remote()
        elif route == self.model.DEVICE_DETAIL:
            self.draw_device_detail()
        elif route == self.model.DEVICE_ACTIONS:
            self.draw_device_actions()
        elif route == self.model.DELETE_CONFIRM:
            self.draw_delete_confirm()
        elif route == self.model.POWER_REPAIR_CONFIRM:
            self.draw_power_repair_confirm()
        elif route == self.model.NAME_EDITOR:
            self.draw_name_editor()
        elif route == self.model.ADD_DEVICE:
            self.draw_add()
        elif route == self.model.NEARBY_OPTIONS:
            self.draw_nearby_options()
        elif route == self.model.NEARBY_RESULTS:
            self.draw_nearby_results()
        elif route == self.model.NEARBY_ACTIONS:
            self.draw_nearby_actions()
        elif route == self.model.DIAGNOSTICS:
            self.draw_diagnostics()
        elif route == self.model.LISTEN_TEST:
            self.draw_listen_test()
        elif route == self.model.SYNC:
            self.draw_sync()
        elif route == self.model.RESTORE_CONFIRM:
            self.draw_restore_confirm()
        elif route == self.model.ABOUT:
            self.draw_about()

    def update(self):
        if self.model.route in (
            self.model.NEARBY_RESULTS,
            self.model.NEARBY_ACTIONS,
        ):
            if (
                self.discovery is not None
                and (
                    self.discovery.is_scanning
                    or not self.discovery_error_announced
                )
                and io.ticks - self.last_discovery_poll_at >= 200
            ):
                self.nearby_results = self.discovery.poll()
                self.last_discovery_poll_at = io.ticks
                if not self.discovery.is_scanning:
                    errors = []
                    status = self.discovery.status
                    for transport in self.nearby_transports:
                        state = status[transport]["state"]
                        if state not in ("complete", "idle"):
                            errors.append(transport.upper() + " " + state)
                    if errors:
                        self.flash("; ".join(errors), 6000)
                    self.discovery_error_announced = True
        self.update_learning_timer()
        self.poll_ir()
        self.update_input()
        self.draw()

    def close(self):
        if self.learning is not None:
            self.learning.cancel()
        if self.discovery is not None:
            self.discovery.stop()
        if self.hardware is not None:
            self.hardware.close()


app = UniversalIRApp()


def update():
    app.update()


def on_exit():
    app.close()


if __name__ == "__main__":
    run(update)
