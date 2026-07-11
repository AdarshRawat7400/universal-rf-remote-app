"""Pure-Python IR capture validation and protocol recognition."""


FRAME_FULL = "full"
FRAME_REPEAT = "repeat"
FRAME_RAW = "raw"
FRAME_MALFORMED = "malformed"


def _near(actual, expected, tolerance=0.30):
    return abs(actual - expected) <= expected * tolerance


def _pair_at(pairs, index):
    """Return a positive integer timing pair, or ``None`` if it is invalid."""

    try:
        pair = pairs[index]
        if len(pair) != 2:
            return None
        mark = int(pair[0])
        space = int(pair[1])
    except (IndexError, TypeError, ValueError):
        return None
    if mark <= 0 or space <= 0:
        return None
    return mark, space


def _capture_length(pairs):
    try:
        return len(pairs)
    except TypeError:
        return 0


def normalize_pairs(pairs, quantum_us=10, max_pairs=512):
    try:
        quantum_us = int(quantum_us)
        max_pairs = int(max_pairs)
    except (TypeError, ValueError):
        raise ValueError("normalization limits must be integers")
    if quantum_us <= 0 or max_pairs <= 0:
        raise ValueError("normalization limits must be positive")

    count = _capture_length(pairs)
    if count == 0 or count > max_pairs:
        raise ValueError("capture length is outside supported range")

    normalized = []
    for pair in pairs:
        try:
            if len(pair) != 2:
                raise ValueError
            mark = int(pair[0])
            space = int(pair[1])
        except (TypeError, ValueError):
            raise ValueError("each pulse must contain numeric mark and space values")
        if mark <= 0 or space <= 0:
            raise ValueError("pulse durations must be positive")
        mark = int(round(mark / quantum_us) * quantum_us)
        space = int(round(space / quantum_us) * quantum_us)
        if mark <= 0 or space <= 0:
            raise ValueError("pulse durations are below the normalization quantum")
        normalized.append([mark, space])
    return normalized


def captures_match(first, second, tolerance=0.30):
    if not first or not second or abs(len(first) - len(second)) > 2:
        return False

    count = min(len(first), len(second))
    if count < 4:
        return False

    try:
        for index in range(count):
            for part in range(2):
                expected = max(1, int(first[index][part]))
                actual = int(second[index][part])
                if abs(actual - expected) > expected * tolerance:
                    return False
    except (IndexError, TypeError, ValueError):
        return False
    return True


def _decode_32_bit_pulse_distance(pairs, header_mark, header_space):
    """Decode the common LSB-first NEC/Samsung 32-bit pulse format."""

    count = _capture_length(pairs)
    if count < 33:
        return None
    header = _pair_at(pairs, 0)
    if header is None:
        return None
    if not _near(header[0], header_mark) or not _near(header[1], header_space):
        return None

    code = 0
    for bit_index in range(32):
        pair = _pair_at(pairs, bit_index + 1)
        if pair is None or not _near(pair[0], 560):
            return None
        if _near(pair[1], 560):
            bit = 0
        elif _near(pair[1], 1680):
            bit = 1
        else:
            return None
        code |= bit << bit_index
    return code


def decode_nec(pairs):
    """Return NEC metadata or ``None`` when the capture is not valid NEC."""

    code = _decode_32_bit_pulse_distance(pairs, 9000, 4500)
    if code is None:
        return None

    address_low = code & 0xFF
    address_high = (code >> 8) & 0xFF
    command = (code >> 16) & 0xFF
    command_inverse = (code >> 24) & 0xFF
    if command_inverse != (command ^ 0xFF):
        return None

    short_address = address_high == (address_low ^ 0xFF)
    address = address_low if short_address else address_low | (address_high << 8)
    return {
        "protocol": "NEC",
        "address": address,
        "command": command,
        "code": code,
        "extended": not short_address,
    }


def decode_samsung32(pairs):
    """Return Samsung32 metadata or ``None`` for a non-Samsung frame.

    Samsung32 transmits a 16-bit address followed by either an 8-bit command
    and its inverse, or a complete 16-bit command. Common 8-bit addresses are
    duplicated in both address bytes.
    """

    count = _capture_length(pairs)
    if count not in (33, 34):
        return None
    code = _decode_32_bit_pulse_distance(pairs, 4500, 4500)
    if code is None:
        return None

    address_low = code & 0xFF
    address_high = (code >> 8) & 0xFF
    command_low = (code >> 16) & 0xFF
    command_high = (code >> 24) & 0xFF

    short_address = address_high == address_low
    address = address_low if short_address else address_low | (address_high << 8)
    short_command = command_high == (command_low ^ 0xFF)
    command = command_low if short_command else command_low | (command_high << 8)
    return {
        "protocol": "SAMSUNG32",
        "address": address,
        "command": command,
        "code": code,
        "extended": not short_address,
        "address_bits": 8 if short_address else 16,
        "command_bits": 8 if short_command else 16,
    }


def encode_samsung32(address, command, terminal_gap_us=10_000):
    """Encode a Samsung32 address/command as raw mark/space pairs.

    Eight-bit Samsung addresses are transmitted twice. Eight-bit commands are
    followed by their inverse. Sixteen-bit values are preserved for less
    common extended remotes.
    """

    try:
        address = int(address)
        command = int(command)
        terminal_gap_us = int(terminal_gap_us)
    except (TypeError, ValueError):
        raise ValueError("Samsung address, command and gap must be integers")
    if address < 0 or address > 0xFFFF:
        raise ValueError("Samsung address must be 0x0000 to 0xffff")
    if command < 0 or command > 0xFFFF:
        raise ValueError("Samsung command must be 0x0000 to 0xffff")
    if terminal_gap_us < 1 or terminal_gap_us > 1_000_000:
        raise ValueError("terminal gap is outside the supported range")

    address_word = address
    if address <= 0xFF:
        address_word |= address << 8
    command_word = command
    if command <= 0xFF:
        command_word |= (command ^ 0xFF) << 8
    code = address_word | (command_word << 16)

    pairs = [[4500, 4500]]
    for bit_index in range(32):
        pairs.append([560, 1680 if code & (1 << bit_index) else 560])
    pairs.append([560, terminal_gap_us])
    return pairs


def _has_complete_stop(pairs):
    """A learned 32-bit frame must include its final 560 us stop mark."""

    if _capture_length(pairs) != 34:
        return False
    stop = _pair_at(pairs, 33)
    return stop is not None and _near(stop[0], 560)


def _is_nec_repeat(pairs):
    if _capture_length(pairs) != 2:
        return False
    header = _pair_at(pairs, 0)
    stop = _pair_at(pairs, 1)
    return (
        header is not None
        and stop is not None
        and _near(header[0], 9000)
        and _near(header[1], 2250)
        and _near(stop[0], 560)
    )


def _samsung_repeat_style(pairs):
    count = _capture_length(pairs)
    if count == 2:
        header = _pair_at(pairs, 0)
        stop = _pair_at(pairs, 1)
        if (
            header is not None
            and stop is not None
            and _near(header[0], 4500)
            and _near(header[1], 2250)
            and _near(stop[0], 560)
        ):
            return "short"

    # SamsungLG-style repeat: header, one zero bit, then a stop mark.
    if count == 3:
        header = _pair_at(pairs, 0)
        zero = _pair_at(pairs, 1)
        stop = _pair_at(pairs, 2)
        if (
            header is not None
            and zero is not None
            and stop is not None
            and _near(header[0], 4500)
            and _near(header[1], 4500)
            and _near(zero[0], 560)
            and _near(zero[1], 560)
            and _near(stop[0], 560)
        ):
            return "samsung_lg"
    return None


def _classification(protocol, frame, decoded=None, repeat_style=None):
    return {
        "protocol": protocol,
        "frame": frame,
        "decoded": decoded,
        "repeat_style": repeat_style,
    }


def classify_capture(pairs):
    """Classify one isolated capture as full, repeat, raw, or malformed.

    A Samsung remote may repeat the entire full frame while its key is held.
    Such a frame is intentionally reported as ``full`` because no information
    in an isolated capture distinguishes it from the initial frame.
    """

    if _is_nec_repeat(pairs):
        return _classification("NEC", FRAME_REPEAT, repeat_style="short")

    samsung_repeat = _samsung_repeat_style(pairs)
    if samsung_repeat is not None:
        return _classification(
            "SAMSUNG32", FRAME_REPEAT, repeat_style=samsung_repeat
        )

    decoded = decode_nec(pairs)
    if decoded is not None:
        if _has_complete_stop(pairs):
            return _classification("NEC", FRAME_FULL, decoded=decoded)
        return _classification("NEC", FRAME_MALFORMED)

    decoded = decode_samsung32(pairs)
    if decoded is not None:
        if _has_complete_stop(pairs):
            return _classification("SAMSUNG32", FRAME_FULL, decoded=decoded)
        return _classification("SAMSUNG32", FRAME_MALFORMED)

    header = _pair_at(pairs, 0)
    if header is not None:
        if _near(header[0], 9000) and (
            _near(header[1], 4500) or _near(header[1], 2250)
        ):
            return _classification("NEC", FRAME_MALFORMED)
        if _near(header[0], 4500) and (
            _near(header[1], 4500) or _near(header[1], 2250)
        ):
            return _classification("SAMSUNG32", FRAME_MALFORMED)

    return _classification(None, FRAME_RAW)


def is_repeat_frame(pairs):
    """Return ``True`` only for a recognized protocol short-repeat frame."""

    return classify_capture(pairs)["frame"] == FRAME_REPEAT


def normalize_full_capture(pairs, quantum_us=10, max_pairs=512):
    """Normalize one learnable full frame and reject repeats or malformed data."""

    normalized = normalize_pairs(pairs, quantum_us, max_pairs)
    classification = classify_capture(normalized)
    if classification["frame"] == FRAME_REPEAT:
        raise ValueError("repeat-only capture cannot be learned")
    if classification["frame"] == FRAME_MALFORMED:
        raise ValueError("recognized protocol frame is incomplete or malformed")
    if len(normalized) < 4:
        raise ValueError("capture is too short to be a full frame")
    return normalized


def select_full_capture(captures, quantum_us=10, max_pairs=512, allow_raw=True):
    """Return the best normalized full capture from an iterable of frames.

    Recognized protocol frames take priority over an unknown raw fallback.
    Repeat-only, incomplete, and malformed candidates are ignored. ``None`` is
    returned when no suitable candidate exists.
    """

    if captures is None:
        return None

    raw_fallback = None
    try:
        iterator = iter(captures)
    except TypeError:
        return None

    for capture in iterator:
        try:
            normalized = normalize_full_capture(capture, quantum_us, max_pairs)
        except (TypeError, ValueError):
            continue
        classification = classify_capture(normalized)
        if classification["frame"] == FRAME_FULL:
            return normalized
        if allow_raw and raw_fallback is None:
            raw_fallback = normalized
    return raw_fallback


def describe_capture(pairs):
    classification = classify_capture(pairs)
    decoded = classification["decoded"]
    if decoded and decoded["protocol"] == "NEC":
        width = 4 if decoded["extended"] else 2
        return "NEC A:%0*x C:%02X" % (
            width,
            decoded["address"],
            decoded["command"],
        )
    if decoded and decoded["protocol"] == "SAMSUNG32":
        return "SAMSUNG32 A:%0*X C:%0*X" % (
            decoded["address_bits"] // 4,
            decoded["address"],
            decoded["command_bits"] // 4,
            decoded["command"],
        )
    if classification["frame"] == FRAME_REPEAT:
        return "%s REPEAT" % classification["protocol"]
    return "RAW %d pairs" % _capture_length(pairs)
