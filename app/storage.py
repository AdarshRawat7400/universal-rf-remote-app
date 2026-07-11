"""Validated, crash-tolerant persistence for learned IR commands.

The implementation deliberately uses only APIs available in both CPython and
the badge's factory MicroPython. Schema 3 still exposes the active device through
``ProfileStore.device`` so the current badge UI remains compatible, while the
on-disk shape supports multiple IR, Wi-Fi, and BLE device entries.
"""

import json
import os
import gc

import config


SCHEMA_VERSION = 4

# One IR remote plus a full 24-result Nearby scan must fit. Discovered entries
# contain no pulse arrays, so this remains inexpensive while command/pair and
# serialized-byte quotas still bound the expensive part of the profile.
MAX_DEVICES = 32
MAX_BUTTONS_PER_DEVICE = 64
MAX_TOTAL_BUTTONS = 128
MAX_CAPTURE_PAIRS = getattr(config, "MAX_CAPTURE_PAIRS", 512)
MAX_TOTAL_PAIRS = 2048
# Validation and crash-safe writes temporarily hold more than one copy. A
# 48KiB serialized ceiling is realistic for the badge's 512KiB heap while the
# compact Samsung format keeps full presets far below it.
MAX_PROFILE_BYTES = 48 * 1024

MIN_CARRIER_HZ = 20_000
MAX_CARRIER_HZ = 60_000
MAX_REPEAT_COUNT = 20
MAX_REPEAT_GAP_US = 500_000
MAX_PULSE_US = 1_000_000
DEFAULT_REPEAT_GAP_US = 40_000

SUPPORTED_TRANSPORTS = ("ir", "wifi", "ble")
DEFAULT_DEVICE_TYPE = "generic"
DEFAULT_TRANSPORT = "ir"
MAX_TRANSPORT_METADATA_ITEMS = 12
DISCOVERY_IDENTITY_KEYS = (
    "address",
    "host",
    "uuid",
    "service_id",
    "identifier",
)

class ProfileValidationError(ValueError):
    """Raised when a profile or command is unsafe to persist."""


def _default_data():
    return {
        "schema": SCHEMA_VERSION,
        "active_device": "device-1",
        "devices": [
            {
                "id": "device-1",
                "name": "My Remote",
                "type": DEFAULT_DEVICE_TYPE,
                "transport": DEFAULT_TRANSPORT,
                "transport_metadata": {},
                "buttons": {},
            }
        ],
    }


def _is_integer(value):
    # bool is an int subclass in both CPython and MicroPython.
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value, field, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise ProfileValidationError(field + " must be text")
    if (not value and not allow_empty) or len(value) > maximum:
        raise ProfileValidationError(field + " has an invalid length")
    return value


def _ascii_identifier(value, field, maximum):
    value = _text(value, field, maximum)
    for character in value:
        # Avoid str.isalnum: it is absent from the factory badge firmware.
        ascii_alphanumeric = (
            "0" <= character <= "9"
            or "A" <= character <= "Z"
            or "a" <= character <= "z"
        )
        if not (ascii_alphanumeric or character in "-_."):
            raise ProfileValidationError(field + " contains invalid characters")
    return value


def _device_id(value):
    return _ascii_identifier(value, "device id", 32)


def _device_type(value):
    return _ascii_identifier(value, "device type", 24)


def _transport(value):
    value = _ascii_identifier(value, "transport", 12)
    if value not in SUPPORTED_TRANSPORTS:
        raise ProfileValidationError("unsupported device transport")
    return value


def _transport_metadata(value):
    """Validate small discovery metadata without prescribing one protocol."""

    if not isinstance(value, dict) or len(value) > MAX_TRANSPORT_METADATA_ITEMS:
        raise ProfileValidationError("transport metadata must be a small object")

    result = {}
    for key, item in value.items():
        key = _ascii_identifier(key, "transport metadata key", 32)
        if isinstance(item, bool) or item is None:
            result[key] = item
        elif _is_integer(item):
            if item < -(1 << 63) or item > (1 << 63) - 1:
                raise ProfileValidationError("transport metadata integer is too large")
            result[key] = item
        elif isinstance(item, str):
            result[key] = _text(item, "transport metadata value", 96, True)
        else:
            raise ProfileValidationError("transport metadata values must be scalar")
    return result


def _slug_identifier(name):
    """Create a portable lowercase ASCII id without CPython string helpers."""

    characters = []
    pending_separator = False
    for character in name:
        if "A" <= character <= "Z":
            character = chr(ord(character) + 32)
        if ("a" <= character <= "z") or ("0" <= character <= "9"):
            if pending_separator and characters:
                characters.append("-")
            characters.append(character)
            pending_separator = False
        else:
            pending_separator = True
    identifier = "".join(characters)
    if not identifier:
        identifier = "device"
    return identifier[:32]


def _same_discovered_device(first, second):
    """Match stable discovery identity while allowing RSSI/status to change."""

    for key in DISCOVERY_IDENTITY_KEYS:
        if key in first and key in second and first[key] == second[key]:
            return True
    return first == second


def _decoded_metadata(value):
    """Validate the small, flat protocol metadata emitted by the codecs."""

    if not isinstance(value, dict) or len(value) > 16:
        raise ProfileValidationError("decoded metadata must be a small object")

    result = {}
    for key, item in value.items():
        key = _text(key, "decoded metadata key", 32)
        if isinstance(item, bool) or item is None:
            result[key] = item
        elif _is_integer(item):
            if item < -(1 << 63) or item > (1 << 63) - 1:
                raise ProfileValidationError("decoded metadata integer is too large")
            result[key] = item
        elif isinstance(item, str):
            result[key] = _text(item, "decoded metadata value", 64, True)
        else:
            raise ProfileValidationError("decoded metadata values must be scalar")
    return result


def validate_command(command):
    """Return a canonical command or raise ``ProfileValidationError``.

    Raw commands include explicit carrier and repeat metadata. ``repeat_count``
    is the total number of frames to transmit (one means a normal single press),
    and ``repeat_gap_us`` is the silence inserted between repeated frames.
    """

    if not isinstance(command, dict):
        raise ProfileValidationError("command must be an object")

    allowed = {
        "format",
        "carrier_hz",
        "repeat_count",
        "repeat_gap_us",
        "description",
        "pulses",
        "address",
        "command",
        "decoded",
    }
    for key in command:
        if key not in allowed:
            raise ProfileValidationError("unsupported command field: " + str(key))

    command_format = command.get("format")
    if command_format not in ("raw", "samsung32"):
        raise ProfileValidationError("unsupported command format")

    carrier_hz = command.get("carrier_hz", getattr(config, "CARRIER_HZ", 38_000))
    if (
        not _is_integer(carrier_hz)
        or carrier_hz < MIN_CARRIER_HZ
        or carrier_hz > MAX_CARRIER_HZ
    ):
        raise ProfileValidationError("carrier_hz is outside the supported range")

    repeat_count = command.get("repeat_count", 1)
    if (
        not _is_integer(repeat_count)
        or repeat_count < 1
        or repeat_count > MAX_REPEAT_COUNT
    ):
        raise ProfileValidationError("repeat_count is outside the supported range")

    repeat_gap_us = command.get("repeat_gap_us", DEFAULT_REPEAT_GAP_US)
    if (
        not _is_integer(repeat_gap_us)
        or repeat_gap_us < 0
        or repeat_gap_us > MAX_REPEAT_GAP_US
    ):
        raise ProfileValidationError("repeat_gap_us is outside the supported range")

    description = _text(
        command.get("description", ""), "command description", 96, True
    )
    result = {
        "format": command_format,
        "carrier_hz": carrier_hz,
        "repeat_count": repeat_count,
        "repeat_gap_us": repeat_gap_us,
        "description": description,
    }
    if "decoded" in command and command["decoded"] is not None:
        result["decoded"] = _decoded_metadata(command["decoded"])

    if command_format == "samsung32":
        if "pulses" in command:
            raise ProfileValidationError("samsung32 commands must not store raw pulses")
        address = command.get("address")
        command_value = command.get("command")
        if (
            not _is_integer(address)
            or address < 0
            or address > 0xFFFF
            or not _is_integer(command_value)
            or command_value < 0
            or command_value > 0xFFFF
        ):
            raise ProfileValidationError(
                "samsung32 address and command must be 0x0000 to 0xffff"
            )
        result["address"] = address
        result["command"] = command_value
        return result

    if "address" in command or "command" in command:
        raise ProfileValidationError("raw commands cannot contain parsed fields")
    pulses = command.get("pulses")
    if (
        not isinstance(pulses, (list, tuple))
        or not pulses
        or len(pulses) > MAX_CAPTURE_PAIRS
    ):
        raise ProfileValidationError("pulse count is outside the supported range")

    canonical_pulses = []
    for pair in pulses:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ProfileValidationError("each pulse must contain a mark and a space")
        mark, space = pair
        if (
            not _is_integer(mark)
            or not _is_integer(space)
            or mark < 1
            or space < 1
            or mark > MAX_PULSE_US
            or space > MAX_PULSE_US
        ):
            raise ProfileValidationError("pulse duration is outside the supported range")
        canonical_pulses.append([mark, space])

    result["pulses"] = canonical_pulses
    return result


def _migrate_v1(data):
    for key in data:
        if key not in ("schema", "devices"):
            raise ProfileValidationError("unsupported schema 1 field: " + str(key))
    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ProfileValidationError("schema 1 devices must be a list")

    migrated_devices = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ProfileValidationError("device must be an object")
        for key in device:
            if key not in ("name", "buttons"):
                raise ProfileValidationError(
                    "unsupported schema 1 device field: " + str(key)
                )
        migrated_devices.append(
            {
                "id": "device-" + str(index + 1),
                "name": device.get("name", "My Remote"),
                "buttons": device.get("buttons", {}),
            }
        )

    return {
        "schema": 2,
        "active_device": "device-1",
        "devices": migrated_devices,
    }


def _migrate_v2(data):
    for key in data:
        if key not in ("schema", "active_device", "devices"):
            raise ProfileValidationError("unsupported schema 2 field: " + str(key))
    devices = data.get("devices")
    if not isinstance(devices, list):
        raise ProfileValidationError("schema 2 devices must be a list")

    migrated_devices = []
    for device in devices:
        if not isinstance(device, dict):
            raise ProfileValidationError("device must be an object")
        for key in device:
            if key not in ("id", "name", "buttons"):
                raise ProfileValidationError(
                    "unsupported schema 2 device field: " + str(key)
                )
        migrated_devices.append(
            {
                "id": device.get("id"),
                "name": device.get("name"),
                "type": DEFAULT_DEVICE_TYPE,
                "transport": DEFAULT_TRANSPORT,
                "transport_metadata": {},
                "buttons": device.get("buttons"),
            }
        )

    return {
        "schema": SCHEMA_VERSION,
        "active_device": data.get("active_device"),
        "devices": migrated_devices,
    }


def _migrate_v3(data):
    """Schema 4 adds compact parsed commands; existing raw data is unchanged."""

    for key in data:
        if key not in ("schema", "active_device", "devices"):
            raise ProfileValidationError("unsupported schema 3 field: " + str(key))
    return {
        "schema": SCHEMA_VERSION,
        "active_device": data.get("active_device"),
        "devices": data.get("devices"),
    }


def _canonicalize_profile(data):
    """Validate and canonicalize schema 1 through 4 without serializing.

    Older data is migrated in memory. The returned value is always schema 4 and
    shares no mutable command, pulse, or metadata containers with the input.
    """

    if not isinstance(data, dict):
        raise ProfileValidationError("profile must be an object")

    schema = data.get("schema")
    if schema == 1:
        data = _migrate_v1(data)
        data = _migrate_v2(data)
    elif schema == 2:
        data = _migrate_v2(data)
    elif schema == 3:
        data = _migrate_v3(data)
    elif schema != SCHEMA_VERSION:
        raise ProfileValidationError("unsupported profile schema")

    allowed = {"schema", "active_device", "devices"}
    for key in data:
        if key not in allowed:
            raise ProfileValidationError("unsupported profile field: " + str(key))

    devices = data.get("devices")
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICES:
        raise ProfileValidationError("device count is outside the supported range")

    canonical_devices = []
    device_ids = set()
    total_buttons = 0
    total_pairs = 0
    for device in devices:
        if not isinstance(device, dict):
            raise ProfileValidationError("device must be an object")
        for key in device:
            if key not in (
                "id",
                "name",
                "type",
                "transport",
                "transport_metadata",
                "buttons",
            ):
                raise ProfileValidationError("unsupported device field: " + str(key))

        identifier = _device_id(device.get("id"))
        if identifier in device_ids:
            raise ProfileValidationError("device ids must be unique")
        device_ids.add(identifier)

        name = _text(device.get("name"), "device name", 48)
        device_type = _device_type(device.get("type"))
        transport = _transport(device.get("transport"))
        transport_metadata = _transport_metadata(device.get("transport_metadata"))
        buttons = device.get("buttons")
        if not isinstance(buttons, dict) or len(buttons) > MAX_BUTTONS_PER_DEVICE:
            raise ProfileValidationError("button count is outside the supported range")

        canonical_buttons = {}
        for button_name, command in buttons.items():
            button_name = _text(button_name, "button name", 48)
            canonical_command = validate_command(command)
            canonical_buttons[button_name] = canonical_command
            total_buttons += 1
            total_pairs += len(canonical_command.get("pulses", ()))

        canonical_devices.append(
            {
                "id": identifier,
                "name": name,
                "type": device_type,
                "transport": transport,
                "transport_metadata": transport_metadata,
                "buttons": canonical_buttons,
            }
        )

    if total_buttons > MAX_TOTAL_BUTTONS:
        raise ProfileValidationError("profile contains too many buttons")
    if total_pairs > MAX_TOTAL_PAIRS:
        raise ProfileValidationError("profile contains too many pulse pairs")

    active_device = _device_id(data.get("active_device"))
    if active_device not in device_ids:
        raise ProfileValidationError("active_device does not exist")

    canonical = {
        "schema": SCHEMA_VERSION,
        "active_device": active_device,
        "devices": canonical_devices,
    }
    return canonical


def _utf8_length(value):
    """Return UTF-8 byte length without allocating a second full payload."""

    total = 0
    for character in value:
        codepoint = ord(character)
        if codepoint <= 0x7F:
            total += 1
        elif codepoint <= 0x7FF:
            total += 2
        elif codepoint <= 0xFFFF:
            total += 3
        else:
            total += 4
    return total


def _serialize_profile(canonical):
    """Serialize a canonical profile once and enforce its persisted quota."""

    payload = json.dumps(canonical)
    if _utf8_length(payload) > MAX_PROFILE_BYTES:
        raise ProfileValidationError("profile exceeds the storage quota")
    return payload


def _validate_profile_view(data):
    """Check aggregate invariants on a storage-owned canonical COW view.

    Changed leaves are canonicalized before insertion; unchanged leaves came
    from the previously validated ``self.data`` graph. This scan therefore
    needs no second command/pulse validation pass and allocates no profile copy.
    """

    if not isinstance(data, dict):
        raise ProfileValidationError("profile must be an object")
    for key in data:
        if key not in ("schema", "active_device", "devices"):
            raise ProfileValidationError("unsupported profile field: " + str(key))
    if data.get("schema") != SCHEMA_VERSION:
        raise ProfileValidationError("unsupported profile schema")

    devices = data.get("devices")
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICES:
        raise ProfileValidationError("device count is outside the supported range")
    active_device = _device_id(data.get("active_device"))
    active_found = False
    total_buttons = 0
    total_pairs = 0

    for index, device in enumerate(devices):
        identifier = _device_id(device.get("id"))
        for previous_index in range(index):
            if devices[previous_index].get("id") == identifier:
                raise ProfileValidationError("device ids must be unique")
        if identifier == active_device:
            active_found = True
        buttons = device.get("buttons")
        if not isinstance(buttons, dict) or len(buttons) > MAX_BUTTONS_PER_DEVICE:
            raise ProfileValidationError("button count is outside the supported range")
        for button_name, command in buttons.items():
            total_buttons += 1
            if command.get("format") == "raw":
                total_pairs += len(command["pulses"])

    if not active_found:
        raise ProfileValidationError("active_device does not exist")
    if total_buttons > MAX_TOTAL_BUTTONS:
        raise ProfileValidationError("profile contains too many buttons")
    if total_pairs > MAX_TOTAL_PAIRS:
        raise ProfileValidationError("profile contains too many pulse pairs")
    return data


def validate_profile(data):
    """Validate, canonicalize, and size-check schema 1 through 4 data."""

    canonical = _canonicalize_profile(data)
    _serialize_profile(canonical)
    return canonical


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _dirname(path):
    path_module = getattr(os, "path", None)
    if path_module is not None:
        return path_module.dirname(path)
    index = max(path.rfind("/"), path.rfind("\\"))
    if index < 0:
        return ""
    if index == 0:
        return path[:1]
    return path[:index]


def _make_directories(path):
    """Create all missing parents without requiring MicroPython os.makedirs."""

    if not path or _exists(path):
        return

    makedirs = getattr(os, "makedirs", None)
    if makedirs is not None:
        try:
            makedirs(path)
            return
        except OSError:
            if _exists(path):
                return

    parent = _dirname(path)
    if parent and parent != path:
        _make_directories(parent)
    try:
        os.mkdir(path)
    except OSError:
        if not _exists(path):
            raise


def _remove_if_present(path):
    if _exists(path):
        os.remove(path)


def _sync_file(handle):
    try:
        handle.flush()
    except (AttributeError, OSError):
        pass
    try:
        os.fsync(handle.fileno())
    except (AttributeError, OSError):
        pass


def _text_signature(value):
    """Return a bounded-memory signature for already decoded profile text."""

    first = 1
    second = 0
    for character in value:
        first = (first + ord(character)) % 65521
        second = (second + first) % 65521
    return len(value), first, second


def _file_text_signature(path):
    """Fingerprint a file in small chunks without JSON parsing or full reads."""

    first = 1
    second = 0
    length = 0
    with open(path, "r") as handle:
        while True:
            chunk = handle.read(256)
            if not chunk:
                break
            length += len(chunk)
            for character in chunk:
                first = (first + ord(character)) % 65521
                second = (second + first) % 65521
    return length, first, second


def _write_checked(handle, text, state):
    """Write one small JSON fragment while enforcing the byte quota."""

    encoded = text.encode("utf-8")
    added = len(encoded)
    if state[0] + added > MAX_PROFILE_BYTES:
        raise ProfileValidationError("profile exceeds the storage quota")
    written = handle.write(encoded)
    if written is not None and written != added:
        raise OSError("short profile write")
    state[0] += added


def _write_json_scalar(handle, value, state):
    if isinstance(value, str):
        text = json.dumps(value)
    elif value is True:
        text = "true"
    elif value is False:
        text = "false"
    elif value is None:
        text = "null"
    else:
        text = str(value)
    _write_checked(handle, text, state)


def _write_scalar_mapping(handle, mapping, state):
    _write_checked(handle, "{", state)
    first = True
    for key, value in mapping.items():
        if not first:
            _write_checked(handle, ",", state)
        first = False
        _write_json_scalar(handle, key, state)
        _write_checked(handle, ":", state)
        _write_json_scalar(handle, value, state)
    _write_checked(handle, "}", state)


def _write_command(handle, command, state):
    _write_checked(handle, '{"format":', state)
    _write_json_scalar(handle, command["format"], state)
    _write_checked(handle, ',"carrier_hz":', state)
    _write_json_scalar(handle, command["carrier_hz"], state)
    _write_checked(handle, ',"repeat_count":', state)
    _write_json_scalar(handle, command["repeat_count"], state)
    _write_checked(handle, ',"repeat_gap_us":', state)
    _write_json_scalar(handle, command["repeat_gap_us"], state)
    _write_checked(handle, ',"description":', state)
    _write_json_scalar(handle, command["description"], state)
    if "decoded" in command:
        _write_checked(handle, ',"decoded":', state)
        _write_scalar_mapping(handle, command["decoded"], state)

    if command["format"] == "samsung32":
        _write_checked(handle, ',"address":', state)
        _write_json_scalar(handle, command["address"], state)
        _write_checked(handle, ',"command":', state)
        _write_json_scalar(handle, command["command"], state)
    else:
        _write_checked(handle, ',"pulses":[', state)
        for index, pair in enumerate(command["pulses"]):
            if index:
                _write_checked(handle, ",", state)
            _write_checked(handle, "[", state)
            _write_json_scalar(handle, pair[0], state)
            _write_checked(handle, ",", state)
            _write_json_scalar(handle, pair[1], state)
            _write_checked(handle, "]", state)
            if index and index % 64 == 0:
                gc.collect()
        _write_checked(handle, "]", state)
    _write_checked(handle, "}", state)


def _write_profile_stream(handle, data):
    """Serialize the fixed schema in small pieces with regular collections."""

    state = [0]
    _write_checked(handle, '{"schema":', state)
    _write_json_scalar(handle, data["schema"], state)
    _write_checked(handle, ',"active_device":', state)
    _write_json_scalar(handle, data["active_device"], state)
    _write_checked(handle, ',"devices":[', state)
    for device_index, device in enumerate(data["devices"]):
        if device_index:
            _write_checked(handle, ",", state)
        _write_checked(handle, '{"id":', state)
        _write_json_scalar(handle, device["id"], state)
        _write_checked(handle, ',"name":', state)
        _write_json_scalar(handle, device["name"], state)
        _write_checked(handle, ',"type":', state)
        _write_json_scalar(handle, device["type"], state)
        _write_checked(handle, ',"transport":', state)
        _write_json_scalar(handle, device["transport"], state)
        _write_checked(handle, ',"transport_metadata":', state)
        _write_scalar_mapping(handle, device["transport_metadata"], state)
        _write_checked(handle, ',"buttons":{', state)
        first_button = True
        for name, command in device["buttons"].items():
            if not first_button:
                _write_checked(handle, ",", state)
            first_button = False
            _write_json_scalar(handle, name, state)
            _write_checked(handle, ":", state)
            _write_command(handle, command, state)
            gc.collect()
        _write_checked(handle, "}}", state)
        gc.collect()
    _write_checked(handle, "]}", state)
    return state[0]


class ProfileStore:
    """Load and save the active universal-remote profile safely."""

    def __init__(self, path=config.PROFILE_PATH):
        self.path = path
        self.backup_path = path + ".bak"
        self.temporary_path = path + ".tmp"
        self.data = _default_data()
        self.recovered_from = None
        self.last_error = None
        self.migrated = False
        # A validated primary is fingerprinted once. Later saves can determine
        # whether it is still the known-good file without parsing it alongside
        # the old profile, the new profile, and the serialized payload.
        self._primary_signature = None

    @property
    def device(self):
        active = self.data.get("active_device")
        for device in self.data["devices"]:
            if device.get("id") == active:
                return device
        # Validated profiles always have a matching active device. This fallback
        # keeps compatibility if callers directly replace ``data`` in memory.
        return self.data["devices"][0]

    def _find_device(self, data, device_id):
        for device in data["devices"]:
            if device["id"] == device_id:
                return device
        return None

    def _working_copy(self):
        """Return a shallow copy-on-write profile shell.

        Existing commands (especially learned raw pulse arrays) stay shared
        because routine commits validate only the changed leaves and stream the
        resulting profile view without rebuilding the unchanged object graph.
        """

        return {
            "schema": self.data["schema"],
            "active_device": self.data["active_device"],
            "devices": list(self.data["devices"]),
        }

    def _editable_device(self, working, device_id, copy_buttons=False):
        """Detach one device from ``self.data`` before mutating it."""

        for index, device in enumerate(working["devices"]):
            if device["id"] == device_id:
                editable = dict(device)
                if copy_buttons:
                    editable["buttons"] = dict(device["buttons"])
                working["devices"][index] = editable
                return editable
        return None

    def _device_summary(self, device, active_device=None):
        metadata = {}
        for key, value in device["transport_metadata"].items():
            metadata[key] = value
        if active_device is None:
            active_device = self.data.get("active_device")
        return {
            "id": device["id"],
            "name": device["name"],
            "type": device["type"],
            "transport": device["transport"],
            "transport_metadata": metadata,
            "button_count": len(device["buttons"]),
            "active": device["id"] == active_device,
        }

    def _commit_data(self, working):
        # Mutators insert canonical changed leaves into a shallow copy-on-write
        # structure. Validate that view without cloning all existing commands,
        # then stream it to disk without constructing a full JSON string.
        self.save(_canonical=working, _stream=True)
        return self

    def _new_device_id(self, name, devices, requested_id=None):
        if requested_id is not None:
            candidate = _device_id(requested_id)
            if self._find_device({"devices": devices}, candidate) is not None:
                raise ProfileValidationError("device id already exists")
            return candidate

        base = _slug_identifier(name)
        candidate = base
        suffix_number = 2
        while self._find_device({"devices": devices}, candidate) is not None:
            suffix = "-" + str(suffix_number)
            candidate = base[: 32 - len(suffix)] + suffix
            suffix_number += 1
        return candidate

    def list_devices(self):
        """Return detached, pulse-free summaries in deterministic display order."""

        active = self.data.get("active_device")
        summaries = []
        for device in self.data["devices"]:
            summaries.append(self._device_summary(device, active))
        return summaries

    def create_device(
        self,
        name,
        device_type=DEFAULT_DEVICE_TYPE,
        transport=DEFAULT_TRANSPORT,
        transport_metadata=None,
        make_active=True,
        device_id=None,
        buttons=None,
    ):
        """Create, persist, and return a new device summary."""

        name = _text(name, "device name", 48)
        device_type = _device_type(device_type)
        transport = _transport(transport)
        if transport_metadata is None:
            transport_metadata = {}
        transport_metadata = _transport_metadata(transport_metadata)
        if not isinstance(make_active, bool):
            raise ProfileValidationError("make_active must be boolean")
        if buttons is None:
            buttons = {}
        if not isinstance(buttons, dict) or len(buttons) > MAX_BUTTONS_PER_DEVICE:
            raise ProfileValidationError("button count is outside the supported range")
        submitted_buttons = {}
        for button_name, command in buttons.items():
            button_name = _text(button_name, "button name", 48)
            submitted_buttons[button_name] = validate_command(command)

        working = self._working_copy()
        identifier = self._new_device_id(name, working["devices"], device_id)
        working["devices"].append(
            {
                "id": identifier,
                "name": name,
                "type": device_type,
                "transport": transport,
                "transport_metadata": transport_metadata,
                "buttons": submitted_buttons,
            }
        )
        if make_active:
            working["active_device"] = identifier
        self._commit_data(working)
        return self._device_summary(self._find_device(self.data, identifier))

    def rename_device(self, device_id, new_name):
        """Rename a device without changing its stable id."""

        device_id = _device_id(device_id)
        new_name = _text(new_name, "device name", 48)
        working = self._working_copy()
        device = self._editable_device(working, device_id)
        if device is None:
            raise ProfileValidationError("device does not exist")
        device["name"] = new_name
        self._commit_data(working)
        return self._device_summary(self._find_device(self.data, device_id))

    def update_device_metadata(
        self, device_id, transport_metadata=None, device_type=None
    ):
        """Atomically update bounded device metadata used by UI settings."""

        device_id = _device_id(device_id)
        if transport_metadata is not None:
            transport_metadata = _transport_metadata(transport_metadata)
        if device_type is not None:
            device_type = _device_type(device_type)
        working = self._working_copy()
        device = self._editable_device(working, device_id)
        if device is None:
            raise ProfileValidationError("device does not exist")
        if transport_metadata is not None:
            device["transport_metadata"] = transport_metadata
        if device_type is not None:
            device["type"] = device_type
        self._commit_data(working)
        return self._device_summary(self._find_device(self.data, device_id))

    def set_active_device(self, device_id):
        """Select and persist the device used by the legacy UI APIs."""

        device_id = _device_id(device_id)
        selected = self._find_device(self.data, device_id)
        if selected is None:
            raise ProfileValidationError("device does not exist")
        if self.data["active_device"] == device_id:
            return self._device_summary(selected)

        working = self._working_copy()
        if working["active_device"] != device_id:
            working["active_device"] = device_id
            self._commit_data(working)
        return self._device_summary(self._find_device(self.data, device_id))

    def delete_device(self, device_id):
        """Delete a device, retaining at least one deterministic active device."""

        device_id = _device_id(device_id)
        working = self._working_copy()
        devices = working["devices"]
        deleted_index = -1
        for index in range(len(devices)):
            if devices[index]["id"] == device_id:
                deleted_index = index
                break
        if deleted_index < 0:
            raise ProfileValidationError("device does not exist")

        deleted = self._device_summary(devices[deleted_index], working["active_device"])
        if len(devices) == 1:
            self._commit_data(_default_data())
            return deleted
        devices.pop(deleted_index)
        if working["active_device"] == device_id:
            replacement_index = deleted_index
            if replacement_index >= len(devices):
                replacement_index = len(devices) - 1
            working["active_device"] = devices[replacement_index]["id"]
        self._commit_data(working)
        return deleted

    def save_discovered(
        self,
        name,
        transport,
        transport_metadata=None,
        device_type=DEFAULT_DEVICE_TYPE,
        device_id=None,
        make_active=False,
    ):
        """Persist or refresh a discovered Wi-Fi/BLE/IR device entry.

        An explicit existing ``device_id`` is updated. Without one, a non-empty
        exact transport/metadata match is refreshed; otherwise a new unique
        device is created. Existing learned buttons are always preserved.
        """

        name = _text(name, "device name", 48)
        transport = _transport(transport)
        device_type = _device_type(device_type)
        if transport_metadata is None:
            transport_metadata = {}
        transport_metadata = _transport_metadata(transport_metadata)
        if not isinstance(make_active, bool):
            raise ProfileValidationError("make_active must be boolean")

        working = self._working_copy()
        found = None
        if device_id is not None:
            device_id = _device_id(device_id)
            found = self._find_device(working, device_id)
        elif transport_metadata:
            for candidate in working["devices"]:
                if candidate["transport"] == transport and _same_discovered_device(
                    candidate["transport_metadata"], transport_metadata
                ):
                    found = candidate
                    break

        if found is None:
            return self.create_device(
                name,
                device_type=device_type,
                transport=transport,
                transport_metadata=transport_metadata,
                make_active=make_active,
                device_id=device_id,
            )

        found = self._editable_device(working, found["id"])
        found["name"] = name
        found["type"] = device_type
        found["transport"] = transport
        found["transport_metadata"] = transport_metadata
        if make_active:
            working["active_device"] = found["id"]
        identifier = found["id"]
        self._commit_data(working)
        return self._device_summary(self._find_device(self.data, identifier))

    def save_discovered_many(self, records, make_active=False):
        """Atomically save or refresh one or more discovery records.

        A 24-result scan is committed with one validated flash transaction,
        avoiding partial saves and unnecessary filesystem wear.
        """

        if not isinstance(records, (list, tuple)) or not records:
            raise ProfileValidationError("discovery records must be a non-empty list")
        if len(records) > MAX_DEVICES:
            raise ProfileValidationError("too many discovery records")
        if not isinstance(make_active, bool):
            raise ProfileValidationError("make_active must be boolean")

        canonical_records = []
        allowed = {
            "name",
            "transport",
            "transport_metadata",
            "device_type",
            "device_id",
        }
        for record in records:
            if not isinstance(record, dict):
                raise ProfileValidationError("discovery record must be an object")
            for key in record:
                if key not in allowed:
                    raise ProfileValidationError(
                        "unsupported discovery field: " + str(key)
                    )
            metadata = record.get("transport_metadata", {})
            canonical_records.append(
                {
                    "name": _text(record.get("name"), "device name", 48),
                    "transport": _transport(record.get("transport")),
                    "transport_metadata": _transport_metadata(metadata),
                    "device_type": _device_type(
                        record.get("device_type", DEFAULT_DEVICE_TYPE)
                    ),
                    "device_id": (
                        None
                        if record.get("device_id") is None
                        else _device_id(record.get("device_id"))
                    ),
                }
            )

        working = self._working_copy()
        identifiers = []
        for record in canonical_records:
            device_id = record["device_id"]
            found = None
            if device_id is not None:
                found = self._find_device(working, device_id)
            elif record["transport_metadata"]:
                for candidate in working["devices"]:
                    if (
                        candidate["transport"] == record["transport"]
                        and _same_discovered_device(
                            candidate["transport_metadata"],
                            record["transport_metadata"],
                        )
                    ):
                        found = candidate
                        break

            if found is None:
                identifier = self._new_device_id(
                    record["name"], working["devices"], device_id
                )
                found = {
                    "id": identifier,
                    "name": record["name"],
                    "type": record["device_type"],
                    "transport": record["transport"],
                    "transport_metadata": record["transport_metadata"],
                    "buttons": {},
                }
                working["devices"].append(found)
            else:
                identifier = found["id"]
                found = self._editable_device(working, identifier)
                found["name"] = record["name"]
                found["type"] = record["device_type"]
                found["transport"] = record["transport"]
                found["transport_metadata"] = record["transport_metadata"]
            identifiers.append(identifier)

        if make_active:
            working["active_device"] = identifiers[-1]
        self._commit_data(working)
        return [
            self._device_summary(self._find_device(self.data, identifier))
            for identifier in identifiers
        ]

    def replace_profile(self, profile):
        """Atomically replace all devices from a validated schema profile.

        This is used by the optional SQLite companion restore flow. Validation
        and the normal crash-safe transaction happen before the new profile is
        exposed to callers; a failed write restores the previous in-memory
        profile as well.
        """

        # This is an ownership boundary: detach arbitrary caller containers
        # once, then use the bounded streaming transaction.
        canonical = _canonicalize_profile(profile)
        self.save(_canonical=canonical, _stream=True)
        return self

    def _load_file(self, path):
        try:
            byte_length = os.stat(path)[6]
        except (IndexError, OSError, TypeError):
            byte_length = MAX_PROFILE_BYTES + 1
        if byte_length > MAX_PROFILE_BYTES:
            raise ProfileValidationError("profile exceeds the storage quota")
        with open(path, "r") as handle:
            loaded = json.load(handle)
        original_schema = loaded.get("schema") if isinstance(loaded, dict) else None
        canonical = _canonicalize_profile(loaded)
        # Current-schema source size is checked before parsing. Legacy
        # migration still uses the conservative serialized-size check.
        if original_schema != SCHEMA_VERSION:
            _serialize_profile(canonical)
        return canonical, original_schema != SCHEMA_VERSION

    def _write_temporary(self, payload):
        _make_directories(_dirname(self.path))
        _remove_if_present(self.temporary_path)
        with open(self.temporary_path, "w") as handle:
            handle.write(payload)
            _sync_file(handle)

        # The payload was already semantically validated. Verify the exact
        # bytes through a bounded-memory text fingerprint instead of loading,
        # parsing, canonicalizing and serializing the entire temp file again.
        expected = _text_signature(payload)
        try:
            actual = _file_text_signature(self.temporary_path)
        except Exception:
            _remove_if_present(self.temporary_path)
            raise
        if actual != expected:
            _remove_if_present(self.temporary_path)
            raise OSError("temporary profile verification failed")
        return expected

    def _write_temporary_object(self, data):
        """Stream a canonical profile to the temporary file with bounded RAM."""

        _make_directories(_dirname(self.path))
        _remove_if_present(self.temporary_path)
        try:
            with open(self.temporary_path, "wb") as handle:
                byte_length = _write_profile_stream(handle, data)
                _sync_file(handle)
            sync = getattr(os, "sync", None)
            if sync is not None:
                try:
                    sync()
                except OSError:
                    pass
            try:
                stored_length = os.stat(self.temporary_path)[6]
            except (IndexError, OSError, TypeError):
                stored_length = -1
            if stored_length != byte_length:
                raise OSError("temporary profile length verification failed")
            actual = _file_text_signature(self.temporary_path)
        except Exception:
            _remove_if_present(self.temporary_path)
            raise
        return actual

    def _repair_primary(self, data):
        _validate_profile_view(data)
        signature = self._write_temporary_object(data)
        try:
            _remove_if_present(self.path)
            os.rename(self.temporary_path, self.path)
        except Exception:
            _remove_if_present(self.temporary_path)
            raise
        self._primary_signature = signature

    def load(self):
        self.recovered_from = None
        self.last_error = None
        self.migrated = False
        self._primary_signature = None
        errors = []

        candidates = (
            ("primary", self.path),
            ("temporary", self.temporary_path),
            ("backup", self.backup_path),
        )
        for source, path in candidates:
            if not _exists(path):
                continue
            try:
                loaded, migrated = self._load_file(path)
            except Exception as error:
                errors.append(source + ": " + str(error))
                continue

            self.data = loaded
            self.migrated = migrated
            if source == "primary":
                # A valid primary is the last committed transaction; any temp
                # file beside it is stale and safe to discard.
                _remove_if_present(self.temporary_path)
                try:
                    self._primary_signature = _file_text_signature(self.path)
                except Exception:
                    # The profile is already loaded and valid. If it cannot be
                    # fingerprinted, a later save simply will not promote it
                    # to the known-good backup slot.
                    self._primary_signature = None
            else:
                self.recovered_from = source
                try:
                    self._repair_primary(loaded)
                except Exception as error:
                    errors.append("repair: " + str(error))
            if errors:
                self.last_error = "; ".join(errors)
            return self

        self.data = _default_data()
        self.recovered_from = "default" if errors else None
        if errors:
            self.last_error = "; ".join(errors)
        return self

    def save(self, _canonical=None, _payload=None, _stream=False):
        if _canonical is None:
            canonical = _canonicalize_profile(self.data)
            _validate_profile_view(canonical)
            new_signature = self._write_temporary_object(canonical)
        else:
            canonical = _canonical
            if _stream:
                _validate_profile_view(canonical)
                new_signature = self._write_temporary_object(canonical)
            else:
                payload = _payload
                if payload is None:
                    payload = _serialize_profile(canonical)
                new_signature = self._write_temporary(payload)

        moved_primary = False
        discarded_primary = False
        previous_signature = self._primary_signature
        try:
            primary_is_valid = False
            if _exists(self.path) and previous_signature is not None:
                try:
                    primary_is_valid = (
                        _file_text_signature(self.path) == previous_signature
                    )
                except Exception:
                    primary_is_valid = False

            if primary_is_valid:
                _remove_if_present(self.backup_path)
                os.rename(self.path, self.backup_path)
                moved_primary = True
            else:
                # Never overwrite a known-good backup with a corrupt primary.
                discarded_primary = _exists(self.path)
                _remove_if_present(self.path)

            os.rename(self.temporary_path, self.path)
        except Exception:
            _remove_if_present(self.temporary_path)
            restored_primary = False
            if moved_primary and not _exists(self.path):
                try:
                    os.rename(self.backup_path, self.path)
                    restored_primary = True
                except OSError:
                    # The previous committed data is still available at .bak.
                    pass
            kept_original = not moved_primary and not discarded_primary
            self._primary_signature = (
                previous_signature if restored_primary or kept_original else None
            )
            raise

        self.data = canonical
        self._primary_signature = new_signature
        self.recovered_from = None
        self.last_error = None
        return self

    def get_button(self, name):
        return self.device["buttons"].get(name)

    def set_device_buttons(self, device_id, commands, device_name=None):
        """Atomically update commands on one device without changing active state."""

        if not isinstance(commands, dict) or not commands:
            raise ProfileValidationError("commands must be a non-empty object")

        device_id = _device_id(device_id)
        working = self._working_copy()
        device = self._editable_device(working, device_id, copy_buttons=True)
        if device is None:
            raise ProfileValidationError("device does not exist")
        if device_name is not None:
            device["name"] = _text(device_name, "device name", 48)

        for name, command in commands.items():
            name = _text(name, "button name", 48)
            # Detach only the changed leaf. Existing commands and their pulse
            # arrays stay shared with the previous committed profile.
            device["buttons"][name] = validate_command(command)

        self._commit_data(working)
        return self._find_device(self.data, device_id)["buttons"]

    def set_buttons(self, commands, device_name=None):
        """Atomically update commands on the active device."""

        return self.set_device_buttons(
            self.data["active_device"], commands, device_name=device_name
        )

    def set_button(
        self,
        name,
        pairs,
        description,
        decoded=None,
        carrier_hz=None,
        repeat_count=1,
        repeat_gap_us=DEFAULT_REPEAT_GAP_US,
    ):
        """Validate, persist, and return one learned command.

        The first four arguments retain the original UI API. Optional metadata
        lets later protocol/UI work tune carrier and hold/repeat behavior.
        """

        name = _text(name, "button name", 48)
        if carrier_hz is None:
            carrier_hz = getattr(config, "CARRIER_HZ", 38_000)
        compact_samsung = (
            isinstance(decoded, dict)
            and decoded.get("protocol") == "SAMSUNG32"
            and decoded.get("address_bits") == 8
            and decoded.get("command_bits") == 8
            and _is_integer(decoded.get("address"))
            and _is_integer(decoded.get("command"))
        )
        if compact_samsung:
            command = {
                "format": "samsung32",
                "carrier_hz": carrier_hz,
                "repeat_count": repeat_count,
                "repeat_gap_us": repeat_gap_us,
                "description": description,
                "address": decoded["address"],
                "command": decoded["command"],
            }
        else:
            command = {
                "format": "raw",
                "carrier_hz": carrier_hz,
                "repeat_count": repeat_count,
                "repeat_gap_us": repeat_gap_us,
                "description": description,
                "pulses": pairs,
            }
            if decoded is not None:
                command["decoded"] = decoded
        command = validate_command(command)
        working = self._working_copy()
        device = self._editable_device(
            working, working["active_device"], copy_buttons=True
        )
        if device is None:
            raise ProfileValidationError("active device does not exist")
        device["buttons"][name] = command
        # The no-copy profile view enforces aggregate pulse and byte quotas and
        # leaves self.data untouched if validation or saving fails.
        self._commit_data(working)
        return self.get_button(name)
