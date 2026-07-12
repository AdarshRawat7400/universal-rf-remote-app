"""Safe, preserving updates for the badge's root ``/secrets.py`` file.

The parser in this module recognizes only plain, top-level string assignments
for the supported settings.  It never imports or executes the settings file.
Before an updated file is promoted it is syntax-compiled (but still never
executed), then installed through a temp/backup transaction with rollback.
"""

import os

from badge_settings_model import SETTING_VALIDATORS, validate_setting


SUPPORTED_SETTINGS = (
    "WIFI_SSID",
    "WIFI_PASSWORD",
    "GITHUB_USERNAME",
    "GITHUB_TOKEN",
    "WEATHER_LOCATION",
    "WLED_IP",
    "IR_COMPANION_URL",
)
REQUIRED_IMPORT_SETTINGS = (
    ("WIFI_SSID", ""),
    ("WIFI_PASSWORD", ""),
    ("GITHUB_USERNAME", ""),
    ("GITHUB_TOKEN", ""),
)

MAX_SECRETS_BYTES = 8 * 1024
OPTIONAL_NONE_SETTINGS = (
    "WEATHER_LOCATION",
    "WLED_IP",
    "IR_COMPANION_URL",
)


class SecretsError(Exception):
    """Base error that intentionally never includes a credential value."""


class SecretsParseError(SecretsError):
    pass


class SecretsSizeError(SecretsError):
    pass


class SecretsIOError(SecretsError):
    pass


def _byte_length(text):
    try:
        return len(text.encode("utf-8"))
    except (AttributeError, UnicodeError):
        raise SecretsParseError("secrets file is not valid UTF-8 text")


def _hex_value(character):
    if "0" <= character <= "9":
        return ord(character) - ord("0")
    if "a" <= character <= "f":
        return ord(character) - ord("a") + 10
    if "A" <= character <= "F":
        return ord(character) - ord("A") + 10
    return -1


def _decode_escape(line, index):
    """Decode one conservative Python string escape at ``index``."""

    if index >= len(line):
        raise SecretsParseError("supported setting has an incomplete escape")
    character = line[index]
    simple = {
        "\\": "\\",
        "'": "'",
        '"': '"',
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    if character in simple:
        return simple[character], index + 1
    if character in "01234567":
        value = 0
        consumed = 0
        while index < len(line) and consumed < 3 and line[index] in "01234567":
            value = value * 8 + (ord(line[index]) - ord("0"))
            index += 1
            consumed += 1
        if value > 255:
            raise SecretsParseError("supported setting has an invalid octal escape")
        return chr(value), index
    sizes = {"x": 2, "u": 4, "U": 8}
    size = sizes.get(character)
    if size is None:
        raise SecretsParseError("supported setting has an unsupported escape")
    start = index + 1
    end = start + size
    if end > len(line):
        raise SecretsParseError("supported setting has an incomplete hex escape")
    value = 0
    for digit in line[start:end]:
        nibble = _hex_value(digit)
        if nibble < 0:
            raise SecretsParseError("supported setting has an invalid hex escape")
        value = value * 16 + nibble
    if value > 0x10FFFF or 0xD800 <= value <= 0xDFFF:
        raise SecretsParseError("supported setting has an invalid Unicode escape")
    try:
        return chr(value), end
    except ValueError:
        raise SecretsParseError("supported setting has an invalid Unicode escape")


def _parse_quoted_value(line, index):
    literal_start = index
    if index >= len(line) or line[index] not in "'\"":
        raise SecretsParseError("supported setting must use a string or None")
    quote = line[index]
    if line[index : index + 3] == quote * 3:
        raise SecretsParseError("multiline setting strings are not supported")
    index += 1
    characters = []
    while index < len(line):
        character = line[index]
        if character == quote:
            return "".join(characters), literal_start, index + 1
        if character == "\\":
            decoded, index = _decode_escape(line, index + 1)
            characters.append(decoded)
            continue
        if ord(character) < 32 or ord(character) == 127:
            raise SecretsParseError("supported setting has a control character")
        characters.append(character)
        index += 1
    raise SecretsParseError("supported setting has an unterminated string")


def _skip_horizontal_space(line, index):
    while index < len(line) and line[index] in " \t":
        index += 1
    return index


def _parse_weather_number(line, index):
    start = index
    if index < len(line) and line[index] in "+-":
        index += 1
    digits = 0
    while index < len(line) and "0" <= line[index] <= "9":
        index += 1
        digits += 1
    if index < len(line) and line[index] == ".":
        index += 1
        while index < len(line) and "0" <= line[index] <= "9":
            index += 1
            digits += 1
    if digits == 0:
        raise SecretsParseError("weather setting contains an invalid number")
    if index < len(line) and line[index] in "eE":
        index += 1
        if index < len(line) and line[index] in "+-":
            index += 1
        exponent_digits = 0
        while index < len(line) and "0" <= line[index] <= "9":
            index += 1
            exponent_digits += 1
        if exponent_digits == 0:
            raise SecretsParseError("weather setting contains an invalid number")
    token = line[start:index]
    try:
        value = float(token) if any(marker in token for marker in ".eE") else int(token)
    except ValueError:
        raise SecretsParseError("weather setting contains an invalid number")
    return value, index


def _parse_weather_scalar(line, index):
    index = _skip_horizontal_space(line, index)
    if index >= len(line):
        raise SecretsParseError("weather setting is incomplete")
    if line[index] in "'\"":
        value, unused_start, end = _parse_quoted_value(line, index)
        return value, end
    if line[index : index + 4] == "None":
        return None, index + 4
    if line[index] in "+-.0123456789":
        return _parse_weather_number(line, index)
    raise SecretsParseError("weather setting contains an unsafe value")


def _parse_weather_sequence(line, index):
    literal_start = index
    opener = line[index]
    closer = ")" if opener == "(" else "]"
    index += 1
    values = []
    saw_comma = False
    while True:
        index = _skip_horizontal_space(line, index)
        if index < len(line) and line[index] == closer:
            literal_end = index + 1
            break
        if index >= len(line) or len(values) >= 4:
            raise SecretsParseError("weather sequence is malformed")
        value, index = _parse_weather_scalar(line, index)
        values.append(value)
        index = _skip_horizontal_space(line, index)
        if index < len(line) and line[index] == closer:
            if opener == "(" and len(values) == 1 and not saw_comma:
                raise SecretsParseError("one-item weather tuple needs a comma")
            literal_end = index + 1
            break
        if index >= len(line) or line[index] != ",":
            raise SecretsParseError("weather sequence is missing a comma")
        saw_comma = True
        index += 1
    value = tuple(values) if opener == "(" else values
    return value, literal_start, literal_end


def _parse_weather_dict(line, index):
    literal_start = index
    index += 1
    values = {}
    while True:
        index = _skip_horizontal_space(line, index)
        if index < len(line) and line[index] == "}":
            return values, literal_start, index + 1
        if index >= len(line) or len(values) >= 8 or line[index] not in "'\"":
            raise SecretsParseError("weather dictionary is malformed")
        key, unused_start, index = _parse_quoted_value(line, index)
        if key in values:
            raise SecretsParseError("weather dictionary has a duplicate key")
        index = _skip_horizontal_space(line, index)
        if index >= len(line) or line[index] != ":":
            raise SecretsParseError("weather dictionary is missing a colon")
        value, index = _parse_weather_scalar(line, index + 1)
        values[key] = value
        index = _skip_horizontal_space(line, index)
        if index < len(line) and line[index] == "}":
            return values, literal_start, index + 1
        if index >= len(line) or line[index] != ",":
            raise SecretsParseError("weather dictionary is missing a comma")
        index += 1


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_weather_container(value):
    if isinstance(value, dict):
        allowed = ("city", "country", "lat", "lon", "name")
        if any(key not in allowed for key in value):
            raise SecretsParseError("weather dictionary has an unsupported key")
        if "city" in value:
            if not isinstance(value["city"], str) or not value["city"]:
                raise SecretsParseError("weather city is invalid")
            if "country" in value and value["country"] is not None and not isinstance(value["country"], str):
                raise SecretsParseError("weather country is invalid")
            return value
        latitude = value.get("lat")
        longitude = value.get("lon")
        if not _is_number(latitude) or not _is_number(longitude):
            raise SecretsParseError("weather coordinates are missing")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise SecretsParseError("weather coordinates are out of range")
        for key in ("name", "country"):
            if key in value and not isinstance(value[key], str):
                raise SecretsParseError("weather label is invalid")
        return value

    if not isinstance(value, (tuple, list)) or not value:
        raise SecretsParseError("weather sequence has an unsupported shape")
    first = value[0]
    if isinstance(first, str):
        if not first or len(value) > 2:
            raise SecretsParseError("weather city sequence has an unsupported shape")
        if len(value) == 2 and value[1] is not None and not isinstance(value[1], str):
            raise SecretsParseError("weather country is invalid")
        return value
    if len(value) < 2 or not _is_number(first) or not _is_number(value[1]):
        raise SecretsParseError("weather coordinate sequence is malformed")
    if not -90 <= first <= 90 or not -180 <= value[1] <= 180:
        raise SecretsParseError("weather coordinates are out of range")
    for label in value[2:]:
        if not isinstance(label, str):
            raise SecretsParseError("weather label is invalid")
    return value


def _parse_weather_container(line, index):
    if line[index] in "([":
        value, literal_start, literal_end = _parse_weather_sequence(line, index)
    else:
        value, literal_start, literal_end = _parse_weather_dict(line, index)
    _validate_weather_container(value)
    suffix = line[literal_end:]
    stripped = suffix.lstrip(" \t")
    if stripped and not stripped.startswith("#"):
        raise SecretsParseError("supported setting has trailing code")
    return value, literal_start, literal_end


def _parse_setting_literal(line, index, allow_weather_tuple=False):
    """Return ``(value, literal_start, literal_end)`` for a safe literal."""

    while index < len(line) and line[index] in " \t":
        index += 1
    literal_start = index
    if line[index : index + 4] == "None":
        literal_end = index + 4
    elif allow_weather_tuple and index < len(line) and line[index] in "([{":
        return _parse_weather_container(line, index)
    else:
        value, literal_start, literal_end = _parse_quoted_value(line, index)
        suffix = line[literal_end:]
        stripped = suffix.lstrip(" \t")
        if stripped and not stripped.startswith("#"):
            raise SecretsParseError("supported setting has trailing code")
        return value, literal_start, literal_end
    suffix = line[literal_end:]
    stripped = suffix.lstrip(" \t")
    if stripped and not stripped.startswith("#"):
        raise SecretsParseError("supported setting has trailing code")
    return None, literal_start, literal_end


def _iter_source_lines(text):
    """Yield ``(line_without_newline, absolute_start)`` without split copies."""

    start = 0
    length = len(text)
    while start < length:
        newline = text.find("\n", start)
        if newline < 0:
            end = length
            next_start = length
        else:
            end = newline
            next_start = newline + 1
        if end > start and text[end - 1] == "\r":
            end -= 1
        yield text[start:end], start
        start = next_start


def _source_state_after(line, string_state, bracket_depth):
    """Track strings and bracket continuations without executing source."""

    state = string_state
    depth = bracket_depth
    index = 0
    while index < len(line):
        if state is not None:
            delimiter = state[0]
            if len(state) == 3 and line[index : index + 3] == state:
                backslashes = 0
                previous = index - 1
                while previous >= 0 and line[previous] == "\\":
                    backslashes += 1
                    previous -= 1
                if backslashes % 2 == 0:
                    state = None
                    index += 3
                    continue
            elif len(state) == 1:
                if line[index] == "\\":
                    index += 2
                    continue
                if line[index] == delimiter:
                    state = None
                    index += 1
                    continue
            index += 1
            continue

        character = line[index]
        if character == "#":
            break
        if character not in "'\"":
            if character in "([{":
                depth += 1
            elif character in ")]}":
                depth -= 1
            index += 1
            continue
        if line[index : index + 3] == character * 3:
            state = character * 3
            index += 3
            continue
        state = character
        index += 1
    return state, depth


def _parse_assignments(text, max_bytes=MAX_SECRETS_BYTES):
    if not isinstance(text, str):
        raise SecretsParseError("secrets file must be text")
    if _byte_length(text) > max_bytes:
        raise SecretsSizeError("secrets file exceeds the size limit")

    entries = {}
    string_state = None
    bracket_depth = 0
    for line, absolute_start in _iter_source_lines(text):
        started_inside_string = string_state is not None
        started_inside_brackets = bracket_depth != 0
        string_state, bracket_depth = _source_state_after(
            line, string_state, bracket_depth
        )
        if started_inside_string or started_inside_brackets:
            # Content in a continued string or bracketed expression cannot
            # define a top-level setting, even when it looks like one.
            continue
        index = 0
        while index < len(line) and line[index] in " \t":
            index += 1
        indentation = index
        if index >= len(line) or line[index] == "#":
            continue
        identifier_start = index
        first = line[index]
        if not (
            "a" <= first <= "z" or "A" <= first <= "Z" or first == "_"
        ):
            continue
        index += 1
        while index < len(line):
            character = line[index]
            if (
                "a" <= character <= "z"
                or "A" <= character <= "Z"
                or "0" <= character <= "9"
                or character == "_"
            ):
                index += 1
            else:
                break
        name = line[identifier_start:index]
        if name not in SUPPORTED_SETTINGS:
            continue
        if indentation:
            raise SecretsParseError("supported settings must be top-level assignments")
        if name in entries:
            raise SecretsParseError("duplicate supported setting: " + name)
        while index < len(line) and line[index] in " \t":
            index += 1
        if index >= len(line) or line[index] != "=":
            raise SecretsParseError("supported setting is not a simple assignment")
        if index + 1 < len(line) and line[index + 1] == "=":
            raise SecretsParseError("supported setting is not a simple assignment")
        value, literal_start, literal_end = _parse_setting_literal(
            line, index + 1, name == "WEATHER_LOCATION"
        )
        if value is None and name not in OPTIONAL_NONE_SETTINGS:
            raise SecretsParseError("setting cannot use None: " + name)
        entries[name] = {
            "value": value,
            "start": absolute_start + literal_start,
            "end": absolute_start + literal_end,
        }
    return entries


def parse_supported_assignments(text, max_bytes=MAX_SECRETS_BYTES):
    """Parse supported values without importing or executing source text."""

    entries = _parse_assignments(text, max_bytes)
    return {name: entry["value"] for name, entry in entries.items()}


def _quote_string(value):
    """Produce one deterministic, single-line Python string literal."""

    if value is None:
        return "None"
    result = ['"']
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            result.append("\\\\")
        elif character == '"':
            result.append('\\"')
        elif codepoint < 32 or codepoint == 127:
            # Validators currently reject these, but keeping the serializer
            # total makes this helper safe if a future setting permits one.
            result.append("\\x%02x" % codepoint)
        else:
            result.append(character)
    result.append('"')
    return "".join(result)


def _compile_source(text, path):
    """Syntax-check source without ever evaluating it."""

    try:
        compile(text, path, "exec")
    except (MemoryError, SyntaxError, TypeError, ValueError):
        # Do not include the SyntaxError or its source line: it may contain a
        # credential and UI/log handlers must never receive credential text.
        raise SecretsParseError("secrets file contains invalid Python syntax")


class SecretsStore:
    """Read and atomically update supported settings while preserving the rest."""

    def __init__(self, path="/secrets.py", max_bytes=MAX_SECRETS_BYTES):
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("secrets path is invalid")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 64
            or max_bytes > MAX_SECRETS_BYTES
        ):
            raise ValueError("secrets size limit is invalid")
        self.path = path
        self.temporary_path = path + ".tmp"
        self.backup_path = path + ".bak"
        self.recovery_path = path + ".recovering"
        self.max_bytes = max_bytes

    def __repr__(self):
        return "<SecretsStore path=%r>" % self.path

    @staticmethod
    def _exists(path):
        try:
            os.stat(path)
            return True
        except OSError:
            return False

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    @staticmethod
    def _rename(source, destination):
        os.rename(source, destination)

    def _read_text(self, path):
        try:
            stat_result = os.stat(path)
            if stat_result[6] > self.max_bytes:
                raise SecretsSizeError("secrets file exceeds the size limit")
            with open(path, "rb") as handle:
                payload = handle.read()
            text = payload.decode("utf-8")
            payload = None
        except SecretsError:
            raise
        except (OSError, UnicodeError):
            raise SecretsIOError("could not read secrets file")
        if _byte_length(text) > self.max_bytes:
            raise SecretsSizeError("secrets file exceeds the size limit")
        return text

    def _validate_text(self, text):
        entries = _parse_assignments(text, self.max_bytes)
        _compile_source(text, self.path)
        return entries

    def _read_validated(self, path):
        text = self._read_text(path)
        return text, self._validate_text(text)

    def _candidate(self, path):
        if not self._exists(path):
            return "missing", None
        try:
            return "valid", self._read_validated(path)
        except SecretsError:
            return "invalid", None

    def _sync(self):
        sync = getattr(os, "sync", None)
        if sync is not None:
            try:
                sync()
            except OSError:
                pass

    def _promote_recovery(self, source, primary_exists):
        moved_primary = False
        self._remove(self.recovery_path)
        try:
            if primary_exists:
                self._rename(self.path, self.recovery_path)
                moved_primary = True
            self._rename(source, self.path)
            promoted_text, unused_entries = self._read_validated(self.path)
        except (OSError, SecretsError):
            if self._exists(self.path):
                self._remove(self.path)
            if moved_primary and self._exists(self.recovery_path):
                try:
                    self._rename(self.recovery_path, self.path)
                except OSError:
                    raise SecretsIOError("secrets recovery and rollback failed")
            raise SecretsIOError("secrets recovery failed")
        self._remove(self.recovery_path)
        self._sync()
        self._refresh_backup(promoted_text)

    def recover(self):
        """Recover an interrupted transaction and return the selected source."""

        primary_state, primary_data = self._candidate(self.path)
        if primary_state == "valid":
            self._remove(self.temporary_path)
            self._remove(self.recovery_path)
            backup_state, backup_data = self._candidate(self.backup_path)
            if backup_state != "missing" and (
                backup_state != "valid" or backup_data[0] != primary_data[0]
            ):
                self._refresh_backup(primary_data[0])
            return "primary"

        temporary_state, unused_temporary = self._candidate(self.temporary_path)
        backup_state, unused_backup = self._candidate(self.backup_path)
        if temporary_state == "valid":
            self._promote_recovery(
                self.temporary_path, primary_state != "missing"
            )
            return "temporary"
        if backup_state == "valid":
            self._promote_recovery(self.backup_path, primary_state != "missing")
            return "backup"
        if primary_state == "missing" and temporary_state == "missing" and backup_state == "missing":
            return "missing"
        raise SecretsParseError("no valid secrets file is available for recovery")

    def read_values(self):
        """Return supported values without importing or executing the file."""

        state = self.recover()
        if state == "missing":
            return {}
        unused_text, entries = self._read_validated(self.path)
        return {name: entry["value"] for name, entry in entries.items()}

    def _build_updated_text(self, text, entries, updates):
        replacements = []
        missing = []
        for name in SUPPORTED_SETTINGS:
            if name not in updates:
                continue
            entry = entries.get(name)
            if entry is None:
                missing.append(name)
            elif entry["value"] != updates[name]:
                replacements.append((entry["start"], entry["end"], updates[name]))

        replacements.sort(key=lambda replacement: replacement[0], reverse=True)
        result = text
        for start, end, value in replacements:
            result = result[:start] + _quote_string(value) + result[end:]
        if missing:
            if result and not result.endswith("\n"):
                result += "\n"
            for name in missing:
                result += name + " = " + _quote_string(updates[name]) + "\n"
        return result

    def _write_temporary(self, text):
        byte_length = _byte_length(text)
        if byte_length > self.max_bytes:
            raise SecretsSizeError("updated secrets file exceeds the size limit")
        self._remove(self.temporary_path)
        payload = text.encode("utf-8")
        try:
            with open(self.temporary_path, "wb") as handle:
                written = handle.write(payload)
                flush = getattr(handle, "flush", None)
                if flush is not None:
                    flush()
            if written is not None and written != byte_length:
                raise OSError("short write")
        except (OSError, UnicodeError):
            self._remove(self.temporary_path)
            raise SecretsIOError("could not write settings transaction")
        payload = None
        self._sync()

    def _rollback(self, had_primary):
        if had_primary and not self._exists(self.backup_path):
            raise SecretsIOError("settings rollback source is unavailable")
        if self._exists(self.path):
            self._remove(self.path)
        if had_primary and self._exists(self.backup_path):
            try:
                self._rename(self.backup_path, self.path)
            except OSError:
                raise SecretsIOError("settings update and rollback failed")

    def _refresh_backup(self, text):
        """Make backup mirror the verified primary without retaining old secrets."""

        # Once the primary commit succeeds, the former backup may contain a
        # password or token the user just replaced/cleared. Remove it before
        # any allocation that could fail. A missing backup is safer than a
        # stale credential; the primary remains fully verified either way.
        self._remove(self.backup_path)
        self._remove(self.temporary_path)
        self._sync()
        try:
            self._write_temporary(text)
            temporary_text, unused_entries = self._read_validated(
                self.temporary_path
            )
            if temporary_text != text:
                raise SecretsIOError("backup verification failed")
            self._rename(self.temporary_path, self.backup_path)
            backup_text, unused_entries = self._read_validated(self.backup_path)
            if backup_text != text:
                raise SecretsIOError("backup verification failed")
        except (MemoryError, OSError, SecretsError):
            self._remove(self.temporary_path)
            self._remove(self.backup_path)
            self._sync()
            return False
        self._sync()
        return True

    def _atomic_install(self, text, expected):
        self._write_temporary(text)
        try:
            temporary_text, temporary_entries = self._read_validated(
                self.temporary_path
            )
            if temporary_text != text:
                raise SecretsIOError("temporary settings verification failed")
            for name, value in expected.items():
                entry = temporary_entries.get(name)
                if entry is None or entry["value"] != value:
                    raise SecretsIOError("temporary settings verification failed")
        except SecretsError:
            self._remove(self.temporary_path)
            raise

        had_primary = self._exists(self.path)
        moved_primary = False
        try:
            if had_primary:
                self._remove(self.backup_path)
                self._rename(self.path, self.backup_path)
                moved_primary = True
            self._rename(self.temporary_path, self.path)
        except OSError:
            try:
                if moved_primary:
                    self._rollback(True)
            finally:
                self._remove(self.temporary_path)
            raise SecretsIOError("settings update failed; previous file restored")

        try:
            primary_text, primary_entries = self._read_validated(self.path)
            if primary_text != text:
                raise SecretsIOError("installed settings verification failed")
            for name, value in expected.items():
                entry = primary_entries.get(name)
                if entry is None or entry["value"] != value:
                    raise SecretsIOError("installed settings verification failed")
        except SecretsError:
            self._rollback(had_primary)
            raise
        self._refresh_backup(text)
        self._sync()

    def update(self, updates):
        """Atomically update supported settings and return all parsed values.

        Unknown lines, comments, spacing and unmodified literals are preserved.
        A missing ``GITHUB_TOKEN`` is always materialized as an empty string so
        upstream multi-name imports cannot fail on factory settings files.
        """

        if not isinstance(updates, dict):
            raise ValueError("settings updates must be an object")
        canonical = {}
        for name, value in updates.items():
            if name not in SETTING_VALIDATORS:
                raise ValueError("unsupported setting name")
            canonical[name] = validate_setting(name, value)

        recovery_state = self.recover()
        if recovery_state == "missing":
            text = ""
            entries = {}
        else:
            text, entries = self._read_validated(self.path)

        # Several factory apps import all four baseline Wi-Fi/GitHub names in
        # one statement. A newly created or partially configured secrets file
        # must define every name or that import discards the valid values too.
        for required_name, default_value in REQUIRED_IMPORT_SETTINGS:
            if required_name not in entries and required_name not in canonical:
                canonical[required_name] = default_value
        updated_text = self._build_updated_text(text, entries, canonical)
        if updated_text == text:
            return {name: entry["value"] for name, entry in entries.items()}

        updated_entries = self._validate_text(updated_text)
        for name, value in canonical.items():
            entry = updated_entries.get(name)
            if entry is None or entry["value"] != value:
                raise SecretsParseError("updated setting could not be verified")
        # Allocate the caller's result before committing. After promotion, an
        # allocation failure must never make the UI claim the old file survived.
        result_values = {
            name: entry["value"] for name, entry in updated_entries.items()
        }
        self._atomic_install(updated_text, canonical)
        return result_values
