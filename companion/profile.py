"""Validation for the badge schema-v4 interchange format.

This is deliberately independent from ``app/storage.py``: importing the badge
application on a desktop can initialize MicroPython UI modules.  Limits mirror
the firmware so every profile exported by the companion is badge-safe.
"""

import json
import re


SCHEMA_VERSION = 4
# Mirrors the badge: enough for the strongest 24 scan results plus remotes.
MAX_DEVICES = 32
MAX_BUTTONS_PER_DEVICE = 64
MAX_TOTAL_BUTTONS = 128
MAX_CAPTURE_PAIRS = 512
MAX_TOTAL_PAIRS = 2048
MAX_PROFILE_BYTES = 48 * 1024
MAX_TRANSPORT_METADATA_ITEMS = 12
SUPPORTED_TRANSPORTS = ("ir", "wifi", "ble")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProfileValidationError(ValueError):
    """Raised when data cannot be safely exchanged with the badge."""


def _integer(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileValidationError(field + " must be an integer")
    if value < minimum or value > maximum:
        raise ProfileValidationError(field + " is outside the supported range")
    return value


def _text(value, field, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise ProfileValidationError(field + " must be text")
    if (not value and not allow_empty) or len(value) > maximum:
        raise ProfileValidationError(field + " has an invalid length")
    return value


def validate_identifier(value, field="identifier", maximum=32):
    value = _text(value, field, maximum)
    if not IDENTIFIER_RE.fullmatch(value):
        raise ProfileValidationError(field + " contains invalid characters")
    return value


def validate_transport(value):
    value = validate_identifier(value, "transport", 12)
    if value not in SUPPORTED_TRANSPORTS:
        raise ProfileValidationError("unsupported device transport")
    return value


def validate_metadata(value):
    if not isinstance(value, dict) or len(value) > MAX_TRANSPORT_METADATA_ITEMS:
        raise ProfileValidationError("transport metadata must be a small object")
    result = {}
    for key, item in value.items():
        key = validate_identifier(key, "transport metadata key", 32)
        if isinstance(item, bool) or item is None:
            result[key] = item
        elif isinstance(item, int):
            result[key] = _integer(
                item, "transport metadata integer", -(1 << 63), (1 << 63) - 1
            )
        elif isinstance(item, str):
            result[key] = _text(item, "transport metadata value", 96, True)
        else:
            raise ProfileValidationError(
                "transport metadata values must be scalar"
            )
    return result


def _decoded_metadata(value):
    if not isinstance(value, dict) or len(value) > 16:
        raise ProfileValidationError("decoded metadata must be a small object")
    result = {}
    for key, item in value.items():
        key = _text(key, "decoded metadata key", 32)
        if isinstance(item, bool) or item is None:
            result[key] = item
        elif isinstance(item, int):
            result[key] = _integer(
                item, "decoded metadata integer", -(1 << 63), (1 << 63) - 1
            )
        elif isinstance(item, str):
            result[key] = _text(item, "decoded metadata value", 64, True)
        else:
            raise ProfileValidationError("decoded metadata values must be scalar")
    return result


def validate_command(command):
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
    unknown = set(command).difference(allowed)
    if unknown:
        raise ProfileValidationError(
            "unsupported command field: " + sorted(unknown)[0]
        )
    command_format = command.get("format")
    if command_format not in ("raw", "samsung32"):
        raise ProfileValidationError("unsupported command format")

    canonical = {
        "format": command_format,
        "carrier_hz": _integer(
            command.get("carrier_hz", 38_000), "carrier_hz", 20_000, 60_000
        ),
        "repeat_count": _integer(
            command.get("repeat_count", 1), "repeat_count", 1, 20
        ),
        "repeat_gap_us": _integer(
            command.get("repeat_gap_us", 40_000),
            "repeat_gap_us",
            0,
            500_000,
        ),
        "description": _text(
            command.get("description", ""), "command description", 96, True
        ),
    }
    if command.get("decoded") is not None:
        canonical["decoded"] = _decoded_metadata(command["decoded"])

    if command_format == "samsung32":
        if "pulses" in command:
            raise ProfileValidationError("samsung32 commands must not store raw pulses")
        canonical["address"] = _integer(
            command.get("address"), "samsung32 address", 0, 0xFFFF
        )
        canonical["command"] = _integer(
            command.get("command"), "samsung32 command", 0, 0xFFFF
        )
        return canonical

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
            raise ProfileValidationError(
                "each pulse must contain a mark and a space"
            )
        canonical_pulses.append(
            [
                _integer(pair[0], "mark duration", 1, 1_000_000),
                _integer(pair[1], "space duration", 1, 1_000_000),
            ]
        )

    canonical["pulses"] = canonical_pulses
    return canonical


def validate_device(device):
    if not isinstance(device, dict):
        raise ProfileValidationError("device must be an object")
    allowed = {
        "id",
        "name",
        "type",
        "transport",
        "transport_metadata",
        "buttons",
    }
    unknown = set(device).difference(allowed)
    if unknown:
        raise ProfileValidationError("unsupported device field: " + sorted(unknown)[0])
    buttons = device.get("buttons")
    if not isinstance(buttons, dict) or len(buttons) > MAX_BUTTONS_PER_DEVICE:
        raise ProfileValidationError("button count is outside the supported range")
    canonical_buttons = {}
    for name, command in buttons.items():
        name = _text(name, "button name", 48)
        canonical_buttons[name] = validate_command(command)
    return {
        "id": validate_identifier(device.get("id"), "device id", 32),
        "name": _text(device.get("name"), "device name", 48),
        "type": validate_identifier(device.get("type"), "device type", 24),
        "transport": validate_transport(device.get("transport")),
        "transport_metadata": validate_metadata(device.get("transport_metadata")),
        "buttons": canonical_buttons,
    }


def validate_profile(profile):
    """Return a detached, canonical schema-v4 profile."""

    if not isinstance(profile, dict):
        raise ProfileValidationError("profile must be an object")
    schema = profile.get("schema")
    if schema == 3:
        profile = dict(profile)
        profile["schema"] = SCHEMA_VERSION
    elif schema != SCHEMA_VERSION:
        raise ProfileValidationError("only profile schemas 3 and 4 are supported")
    unknown = set(profile).difference({"schema", "active_device", "devices"})
    if unknown:
        raise ProfileValidationError("unsupported profile field: " + sorted(unknown)[0])

    devices = profile.get("devices")
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICES:
        raise ProfileValidationError("device count is outside the supported range")
    canonical_devices = []
    identifiers = set()
    button_count = 0
    pair_count = 0
    for device in devices:
        canonical = validate_device(device)
        if canonical["id"] in identifiers:
            raise ProfileValidationError("device ids must be unique")
        identifiers.add(canonical["id"])
        button_count += len(canonical["buttons"])
        pair_count += sum(
            len(command.get("pulses", ()))
            for command in canonical["buttons"].values()
        )
        canonical_devices.append(canonical)
    if button_count > MAX_TOTAL_BUTTONS:
        raise ProfileValidationError("profile contains too many buttons")
    if pair_count > MAX_TOTAL_PAIRS:
        raise ProfileValidationError("profile contains too many pulse pairs")

    active_device = validate_identifier(
        profile.get("active_device"), "active_device", 32
    )
    if active_device not in identifiers:
        raise ProfileValidationError("active_device does not exist")
    canonical_profile = {
        "schema": SCHEMA_VERSION,
        "active_device": active_device,
        "devices": canonical_devices,
    }
    encoded = json.dumps(canonical_profile, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROFILE_BYTES:
        raise ProfileValidationError("profile exceeds the storage quota")
    return canonical_profile


def default_profile():
    return {
        "schema": SCHEMA_VERSION,
        "active_device": "device-1",
        "devices": [
            {
                "id": "device-1",
                "name": "My Remote",
                "type": "generic",
                "transport": "ir",
                "transport_metadata": {},
                "buttons": {},
            }
        ],
    }
