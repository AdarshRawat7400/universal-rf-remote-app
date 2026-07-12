"""Pure, bounded helpers for the standalone Badge Settings app.

This module deliberately avoids ``network`` and ``badgeware`` imports.  It can
therefore be tested on a desktop while remaining compatible with the badge's
MicroPython v1.23 runtime.
"""


MAX_SCAN_RESULTS = 32
MAX_SCAN_RECORDS = 128
MAX_EDITOR_BYTES = 512


class SettingsValidationError(ValueError):
    """A setting cannot safely be used or written to ``secrets.py``."""


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_length(value):
    try:
        return len(value.encode("utf-8"))
    except (AttributeError, UnicodeError):
        raise SettingsValidationError("setting must be valid UTF-8 text")


def _text(value, field, minimum, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise SettingsValidationError(field + " must be text")
    byte_length = _utf8_length(value)
    if byte_length == 0 and allow_empty:
        return value
    if byte_length < minimum or byte_length > maximum:
        raise SettingsValidationError(field + " has an invalid length")
    for character in value:
        codepoint = ord(character)
        if codepoint < 32 or codepoint == 127:
            raise SettingsValidationError(field + " contains a control character")
    return value


def validate_ssid(value, allow_empty=False):
    """Validate a Wi-Fi SSID using its 32-byte wire limit."""

    return _text(value, "Wi-Fi name", 1, 32, allow_empty)


def validate_wifi_password(value, allow_empty=True):
    """Validate a WPA/WPA2 passphrase or a 64-digit hexadecimal PSK.

    An empty value is useful for open networks and for deliberately clearing a
    saved password.  Non-empty passphrases are measured in UTF-8 bytes.
    """

    value = _text(value, "Wi-Fi password", 0 if allow_empty else 8, 64, allow_empty)
    if not value:
        return value
    byte_length = _utf8_length(value)
    if byte_length == 64:
        for character in value:
            if character not in "0123456789abcdefABCDEF":
                raise SettingsValidationError(
                    "a 64-character Wi-Fi password must be hexadecimal"
                )
        return value
    if byte_length < 8 or byte_length > 63:
        raise SettingsValidationError("Wi-Fi password must contain 8 to 63 bytes")
    return value


# A more familiar public spelling for callers that call the field a WPA key.
validate_wpa_password = validate_wifi_password


def validate_github_username(value, allow_empty=True):
    """Validate GitHub's documented account-name shape."""

    value = _text(value, "GitHub username", 0 if allow_empty else 1, 39, allow_empty)
    if not value:
        return value
    if value[0] == "-" or value[-1] == "-" or "--" in value:
        raise SettingsValidationError("GitHub username has invalid hyphen placement")
    for character in value:
        ascii_alphanumeric = (
            "a" <= character <= "z"
            or "A" <= character <= "Z"
            or "0" <= character <= "9"
        )
        if not (ascii_alphanumeric or character == "-"):
            raise SettingsValidationError("GitHub username contains an invalid character")
    return value


def validate_github_token(value, allow_empty=True):
    """Accept current and future opaque GitHub token forms without whitespace."""

    value = _text(value, "GitHub token", 0 if allow_empty else 20, 255, allow_empty)
    if not value:
        return value
    if len(value) < 20:
        raise SettingsValidationError("GitHub token is too short")
    for character in value:
        ascii_alphanumeric = (
            "a" <= character <= "z"
            or "A" <= character <= "Z"
            or "0" <= character <= "9"
        )
        if not (ascii_alphanumeric or character in "_-"):
            raise SettingsValidationError("GitHub token contains an invalid character")
    return value


def validate_weather_location(value, allow_empty=True):
    """Validate a human-readable city/location without prescribing a provider."""

    if value is None and allow_empty:
        return None
    value = _text(value, "weather location", 0 if allow_empty else 1, 64, allow_empty)
    if value and not value.strip():
        raise SettingsValidationError("weather location cannot be only whitespace")
    return value


def validate_ipv4(value, allow_empty=True):
    """Validate and return a canonical dotted-decimal IPv4 address."""

    if value is None and allow_empty:
        return None
    value = _text(value, "IPv4 address", 0 if allow_empty else 7, 15, allow_empty)
    if not value:
        return value
    parts = value.split(".")
    if len(parts) != 4:
        raise SettingsValidationError("IPv4 address must contain four octets")
    for part in parts:
        if not part or len(part) > 3:
            raise SettingsValidationError("IPv4 address contains an invalid octet")
        for character in part:
            if character < "0" or character > "9":
                raise SettingsValidationError("IPv4 address must contain only digits")
        number = int(part)
        if number > 255 or str(number) != part:
            raise SettingsValidationError("IPv4 address contains an invalid octet")
    first = int(parts[0])
    if value in ("0.0.0.0", "255.255.255.255") or 224 <= first <= 239:
        raise SettingsValidationError("IPv4 address is not a usable device address")
    return value


def _validate_hostname(value):
    if not value or len(value) > 253:
        raise SettingsValidationError("companion URL has an invalid host")
    labels = value.split(".")
    for label in labels:
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-":
            raise SettingsValidationError("companion URL has an invalid host")
        for character in label:
            ascii_alphanumeric = (
                "a" <= character <= "z"
                or "A" <= character <= "Z"
                or "0" <= character <= "9"
            )
            if not (ascii_alphanumeric or character == "-"):
                raise SettingsValidationError("companion URL has an invalid host")


def validate_ir_companion_url(value, allow_empty=True):
    """Validate a bounded HTTP(S) base URL without importing ``urllib``.

    Credentials, query strings and fragments are deliberately rejected.  The
    companion client appends its own API paths to this base URL.
    """

    if value is None and allow_empty:
        return None
    value = _text(value, "IR companion URL", 0 if allow_empty else 8, 160, allow_empty)
    if not value:
        return value
    if " " in value:
        raise SettingsValidationError("IR companion URL cannot contain spaces")
    value = value.rstrip("/")
    lower = value.lower()
    if lower.startswith("http://"):
        remainder = value[7:]
    elif lower.startswith("https://"):
        remainder = value[8:]
    else:
        raise SettingsValidationError("IR companion URL must use HTTP or HTTPS")
    if not remainder or "@" in remainder or "?" in remainder or "#" in remainder:
        raise SettingsValidationError("IR companion URL contains unsupported components")
    if "\\" in remainder:
        raise SettingsValidationError("IR companion URL contains an invalid separator")

    slash = remainder.find("/")
    if slash < 0:
        authority = remainder
        path = ""
    else:
        authority = remainder[:slash]
        path = remainder[slash:]
    if not authority or "//" in path or "/../" in path or path.endswith("/.."):
        raise SettingsValidationError("IR companion URL has an invalid path")

    if authority.count(":") > 1:
        raise SettingsValidationError("IR companion URL has an invalid host")
    if ":" in authority:
        host, port = authority.rsplit(":", 1)
        if not port:
            raise SettingsValidationError("IR companion URL has an invalid port")
        for character in port:
            if character < "0" or character > "9":
                raise SettingsValidationError("IR companion URL has an invalid port")
        port_number = int(port)
        if port_number < 1 or port_number > 65535:
            raise SettingsValidationError("IR companion URL has an invalid port")
    else:
        host = authority

    numeric_host = True
    for character in host:
        if not ("0" <= character <= "9" or character == "."):
            numeric_host = False
            break
    if numeric_host:
        validate_ipv4(host, False)
    else:
        _validate_hostname(host)
    return value


SETTING_VALIDATORS = {
    "WIFI_SSID": validate_ssid,
    "WIFI_PASSWORD": validate_wifi_password,
    "GITHUB_USERNAME": validate_github_username,
    "GITHUB_TOKEN": validate_github_token,
    "WEATHER_LOCATION": validate_weather_location,
    "WLED_IP": validate_ipv4,
    "IR_COMPANION_URL": validate_ir_companion_url,
}


def validate_setting(name, value):
    """Validate one supported ``secrets.py`` setting.

    Empty strings are accepted here so every setting can be explicitly cleared.
    Call the individual validator with ``allow_empty=False`` when a form needs a
    value before continuing.
    """

    validator = SETTING_VALIDATORS.get(name)
    if validator is None:
        raise SettingsValidationError("unsupported setting name")
    if value is None and name not in (
        "WEATHER_LOCATION",
        "WLED_IP",
        "IR_COMPANION_URL",
    ):
        raise SettingsValidationError("setting cannot be cleared with None")
    return validator(value, True)


_SECURITY_NAMES = {
    "open": 0,
    "none": 0,
    "wep": 1,
    "wpa": 2,
    "wpa-psk": 2,
    "wpa2": 3,
    "wpa2-psk": 3,
    "wpa/wpa2": 4,
    "wpa-wpa2": 4,
    "wpa2-enterprise": 5,
    "enterprise": 5,
    "eap": 5,
    "802.1x": 5,
    "wpa3": 6,
    "wpa3-psk": 6,
    "wpa2/wpa3": 7,
    "wpa2-wpa3": 7,
}

_SECURITY_LABELS = {
    0: "Open",
    1: "WEP",
    2: "WPA",
    3: "WPA2",
    4: "WPA/WPA2",
    5: "WPA2 Enterprise",
    6: "WPA3",
    7: "WPA2/WPA3",
}
_SECURITY_INTEGER_ALIASES = {
    # Values observed by the official badge Wi-Fi app on CYW43 firmware.
    4194304: 3,
    4194308: 3,
    4194310: 6,
}


def _normalize_security(value):
    if _is_integer(value):
        alias = _SECURITY_INTEGER_ALIASES.get(value)
        if alias is not None:
            return alias
        if 0 <= value <= 255:
            return value
        raise ValueError("security mode is out of range")
    if isinstance(value, str):
        normalized = _SECURITY_NAMES.get(value.strip().lower())
        if normalized is not None:
            return normalized
    raise ValueError("unsupported security mode")


def security_label(value):
    """Return a short, non-user-controlled label for a scan auth mode."""

    try:
        normalized = _normalize_security(value)
    except (TypeError, ValueError):
        return "Secured"
    return _SECURITY_LABELS.get(normalized, "Secured")


def security_is_open(value):
    try:
        return _normalize_security(value) == 0
    except (TypeError, ValueError):
        return False


def security_is_enterprise(value):
    try:
        return _normalize_security(value) == 5
    except (TypeError, ValueError):
        return False


def _scan_ssid(value):
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except (UnicodeError, ValueError):
            raise ValueError("SSID is not valid UTF-8")
    try:
        return validate_ssid(value, False)
    except SettingsValidationError:
        raise ValueError("SSID is invalid")


def _scan_integer(value, minimum, maximum):
    if not _is_integer(value) or value < minimum or value > maximum:
        raise ValueError("scan number is out of range")
    return value


def _scan_hidden(value):
    if isinstance(value, bool):
        return value
    if _is_integer(value) and value in (0, 1):
        return bool(value)
    raise ValueError("hidden flag is invalid")


def _normalize_scan_record(record):
    try:
        if isinstance(record, dict):
            ssid = record["ssid"]
            channel = record["channel"]
            rssi = record["rssi"]
            security = record["security"]
            hidden = record["hidden"]
        else:
            if len(record) < 6:
                return None
            ssid, channel, rssi, security, hidden = (
                record[0],
                record[2],
                record[3],
                record[4],
                record[5],
            )
        return {
            "ssid": _scan_ssid(ssid),
            "rssi": _scan_integer(rssi, -127, 0),
            "security": _normalize_security(security),
            "hidden": _scan_hidden(hidden),
            "channel": _scan_integer(channel, 1, 196),
        }
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def normalize_scan_results(records, max_results=16):
    """Normalize, deduplicate and bound native ``WLAN.scan()`` tuples.

    At most ``MAX_SCAN_RECORDS`` inputs are inspected, at most
    ``MAX_SCAN_RESULTS`` outputs can be requested, duplicate SSID/security pairs
    retain their strongest observation, and malformed/unprintable records are
    discarded.  Returned dictionaries intentionally omit BSSIDs and raw tuples.
    """

    if (
        not _is_integer(max_results)
        or max_results < 1
        or max_results > MAX_SCAN_RESULTS
    ):
        raise ValueError("max_results must be between 1 and %d" % MAX_SCAN_RESULTS)
    try:
        iterator = iter(records)
    except TypeError:
        raise ValueError("scan results must be iterable")

    unique = {}
    inspected = 0
    while inspected < MAX_SCAN_RECORDS:
        try:
            record = next(iterator)
        except StopIteration:
            break
        inspected += 1
        normalized = _normalize_scan_record(record)
        if normalized is None:
            continue
        key = (normalized["ssid"], normalized["security"])
        existing = unique.get(key)
        if (
            existing is None
            or normalized["rssi"] > existing["rssi"]
            or (
                normalized["rssi"] == existing["rssi"]
                and normalized["channel"] < existing["channel"]
            )
        ):
            unique[key] = normalized

    results = list(unique.values())
    results.sort(
        key=lambda item: (
            -item["rssi"],
            item["ssid"].lower(),
            item["security"],
            item["channel"],
        )
    )
    if len(results) > max_results:
        del results[max_results:]
    return results


class TextEditor:
    """A byte-bounded on-screen editor with no retained immutable secret copy.

    The backing value is a ``bytearray`` so ``wipe()`` can overwrite its bytes
    before releasing them.  Masked rendering never decodes the value.
    """

    ACTION_LEFT = "LEFT"
    ACTION_RIGHT = "RIGHT"
    ACTION_SPACE = "SPACE"
    ACTION_BACKSPACE = "BACKSPACE"
    ACTION_DELETE = "DELETE"
    ACTION_CLEAR = "CLEAR"
    ACTION_SHOW_HIDE = "SHOW/HIDE"
    ACTION_DONE = "DONE"
    ACTION_CANCEL = "CANCEL"

    CHARACTER_GROUPS = (
        ("abc", tuple("abcdefghijklmnopqrstuvwxyz")),
        ("ABC", tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
        ("123", tuple("0123456789")),
        ("symbols", tuple("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")),
    )

    def __init__(self, initial="", max_bytes=128, masked=False):
        if not isinstance(initial, str):
            raise ValueError("initial editor value must be text")
        if (
            not _is_integer(max_bytes)
            or max_bytes < 1
            or max_bytes > MAX_EDITOR_BYTES
        ):
            raise ValueError("max_bytes is out of range")
        if not isinstance(masked, bool):
            raise ValueError("masked must be boolean")
        try:
            encoded = initial.encode("utf-8")
        except UnicodeError:
            raise ValueError("initial editor value is not valid UTF-8")
        if len(encoded) > max_bytes:
            raise ValueError("initial editor value exceeds byte limit")
        self.max_bytes = max_bytes
        self.masked = masked
        self._mask_capable = masked
        self._buffer = bytearray(encoded)
        self._cursor = self._character_count()
        self._group_index = 0
        actions = [
            self.ACTION_LEFT,
            self.ACTION_RIGHT,
            self.ACTION_SPACE,
            self.ACTION_BACKSPACE,
            self.ACTION_DELETE,
            self.ACTION_CLEAR,
        ]
        if masked:
            actions.append(self.ACTION_SHOW_HIDE)
        actions.extend((self.ACTION_DONE, self.ACTION_CANCEL))
        self._groups = self.CHARACTER_GROUPS + (("actions", tuple(actions)),)
        self._item_indexes = [0 for unused in self._groups]
        # Do not retain the immutable encoded secret longer than initialization.
        encoded = None

    def __repr__(self):
        return "<TextEditor masked>" if self.masked else "<TextEditor>"

    @property
    def value(self):
        return bytes(self._buffer).decode("utf-8")

    @property
    def cursor(self):
        return self._cursor

    @property
    def byte_length(self):
        """Return the backing byte count without decoding a masked value."""

        return len(self._buffer)

    @property
    def selected_group(self):
        return self._groups[self._group_index][0]

    @property
    def selected_item(self):
        group = self._groups[self._group_index][1]
        return group[self._item_indexes[self._group_index]]

    def move_group(self, delta):
        if not _is_integer(delta):
            raise ValueError("group movement must be an integer")
        self._group_index = (self._group_index + delta) % len(self._groups)
        return self.selected_group

    def move_item(self, delta):
        if not _is_integer(delta):
            raise ValueError("item movement must be an integer")
        items = self._groups[self._group_index][1]
        current = self._item_indexes[self._group_index]
        self._item_indexes[self._group_index] = (current + delta) % len(items)
        return self.selected_item

    def _character_count(self):
        count = 0
        for byte in self._buffer:
            if (byte & 0xC0) != 0x80:
                count += 1
        return count

    def _byte_index(self, character_index):
        if character_index <= 0:
            return 0
        seen = 0
        for index in range(len(self._buffer)):
            if (self._buffer[index] & 0xC0) != 0x80:
                if seen == character_index:
                    return index
                seen += 1
        return len(self._buffer)

    def _insert_ascii(self, character):
        encoded = character.encode("utf-8")
        if len(self._buffer) + len(encoded) > self.max_bytes:
            return False
        byte_index = self._byte_index(self._cursor)
        old_length = len(self._buffer)
        added = len(encoded)
        self._buffer.extend(encoded)
        for index in range(old_length - 1, byte_index - 1, -1):
            self._buffer[index + added] = self._buffer[index]
        for offset in range(added):
            self._buffer[byte_index + offset] = encoded[offset]
        self._cursor += 1
        return True

    def _backspace(self):
        if self._cursor <= 0:
            return
        start = self._byte_index(self._cursor - 1)
        end = self._byte_index(self._cursor)
        for position in range(start, end):
            self._buffer[position] = 0
        del self._buffer[start:end]
        self._cursor -= 1

    def _delete(self):
        if self._cursor >= self._character_count():
            return
        start = self._byte_index(self._cursor)
        end = self._byte_index(self._cursor + 1)
        for position in range(start, end):
            self._buffer[position] = 0
        del self._buffer[start:end]

    def activate(self):
        """Activate the current key and return a UI-friendly result string."""

        item = self.selected_item
        if self.selected_group != "actions":
            return "changed" if self._insert_ascii(item) else "limit"
        if item == self.ACTION_LEFT:
            self._cursor = max(0, self._cursor - 1)
            return "changed"
        if item == self.ACTION_RIGHT:
            self._cursor = min(self._character_count(), self._cursor + 1)
            return "changed"
        if item == self.ACTION_SPACE:
            return "changed" if self._insert_ascii(" ") else "limit"
        if item == self.ACTION_BACKSPACE:
            self._backspace()
            return "changed"
        if item == self.ACTION_DELETE:
            self._delete()
            return "changed"
        if item == self.ACTION_CLEAR:
            self.wipe()
            return "changed"
        if item == self.ACTION_SHOW_HIDE and self._mask_capable:
            self.masked = not self.masked
            return "changed"
        if item == self.ACTION_CANCEL:
            self.wipe()
            return "cancel"
        return "done"

    def display_value(self):
        if not self.masked:
            return self.value
        return "*" * self._character_count()

    def wipe(self):
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        del self._buffer[:]
        self._cursor = 0
