import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from ir.codec import (
    FRAME_FULL,
    FRAME_MALFORMED,
    FRAME_RAW,
    FRAME_REPEAT,
    captures_match,
    classify_capture,
    decode_nec,
    decode_samsung32,
    describe_capture,
    encode_samsung32,
    is_repeat_frame,
    normalize_full_capture,
    normalize_pairs,
    select_full_capture,
)


def _pulse_distance_pairs(header_mark, address_word, command_word):
    code = (address_word & 0xFFFF) | ((command_word & 0xFFFF) << 16)
    pairs = [[header_mark, 4500]]
    for bit in range(32):
        pairs.append([560, 1680 if code & (1 << bit) else 560])
    pairs.append([560, 10_000])
    return pairs


def nec_pairs(address, command, extended=False):
    if extended:
        address_word = address & 0xFFFF
    else:
        address_word = (address & 0xFF) | ((address ^ 0xFF) << 8)
    command_word = (command & 0xFF) | ((command ^ 0xFF) << 8)
    return _pulse_distance_pairs(9000, address_word, command_word)


def samsung32_pairs(address, command, extended=False, command_16bit=False):
    if extended:
        address_word = address & 0xFFFF
    else:
        address_word = (address & 0xFF) | ((address & 0xFF) << 8)
    if command_16bit:
        command_word = command & 0xFFFF
    else:
        command_word = (command & 0xFF) | ((command ^ 0xFF) << 8)
    return _pulse_distance_pairs(4500, address_word, command_word)


def jitter(pairs, mark_factor=1.08, space_factor=0.94):
    return [
        [int(mark * mark_factor), int(space * space_factor)]
        for mark, space in pairs
    ]


class CodecTests(unittest.TestCase):
    def test_samsung_encoder_round_trips_common_tv_codes(self):
        for command in (0x02, 0x07, 0x0B, 0x0F, 0x10, 0x12):
            pairs = encode_samsung32(0x07, command)
            self.assertEqual(34, len(pairs))
            decoded = decode_samsung32(pairs)
            self.assertEqual(0x07, decoded["address"])
            self.assertEqual(command, decoded["command"])

    def test_samsung_encoder_validates_fields(self):
        for args in ((-1, 0), (0x10000, 0), (0, -1), (0, 0x10000)):
            with self.assertRaises(ValueError):
                encode_samsung32(*args)
        with self.assertRaises(ValueError):
            encode_samsung32(0x07, 0x02, terminal_gap_us=0)

    def test_decodes_short_nec(self):
        decoded = decode_nec(nec_pairs(0x45, 0x66))
        self.assertEqual("NEC", decoded["protocol"])
        self.assertEqual(0x45, decoded["address"])
        self.assertEqual(0x66, decoded["command"])
        self.assertFalse(decoded["extended"])

    def test_decodes_extended_nec(self):
        decoded = decode_nec(nec_pairs(0x1234, 0xA5, extended=True))
        self.assertEqual(0x1234, decoded["address"])
        self.assertEqual(0xA5, decoded["command"])
        self.assertTrue(decoded["extended"])

    def test_rejects_bad_nec_complement(self):
        pairs = nec_pairs(0x45, 0x66)
        pairs[25][1] = 1680 if pairs[25][1] == 560 else 560
        self.assertIsNone(decode_nec(pairs))
        self.assertEqual(FRAME_MALFORMED, classify_capture(pairs)["frame"])

    def test_decodes_common_samsung32(self):
        decoded = decode_samsung32(samsung32_pairs(0x07, 0x02))
        self.assertEqual("SAMSUNG32", decoded["protocol"])
        self.assertEqual(0x07, decoded["address"])
        self.assertEqual(0x02, decoded["command"])
        self.assertEqual(8, decoded["address_bits"])
        self.assertEqual(8, decoded["command_bits"])
        self.assertFalse(decoded["extended"])

    def test_decodes_samsung32_16_bit_fields(self):
        decoded = decode_samsung32(
            samsung32_pairs(0x1234, 0x6AA5, extended=True, command_16bit=True)
        )
        self.assertEqual(0x1234, decoded["address"])
        self.assertEqual(0x6AA5, decoded["command"])
        self.assertEqual(16, decoded["address_bits"])
        self.assertEqual(16, decoded["command_bits"])
        self.assertTrue(decoded["extended"])

    def test_samsung_decoder_rejects_bad_timing_and_wrong_length(self):
        pairs = samsung32_pairs(0x07, 0x02)
        pairs[12][1] = 1000
        self.assertIsNone(decode_samsung32(pairs))
        self.assertIsNone(decode_samsung32(samsung32_pairs(0x07, 0x02) + [[560, 560]]))

    def test_describes_nec_and_samsung32(self):
        self.assertEqual("NEC A:45 C:66", describe_capture(nec_pairs(0x45, 0x66)))
        self.assertEqual(
            "SAMSUNG32 A:07 C:02",
            describe_capture(samsung32_pairs(0x07, 0x02)),
        )
        self.assertEqual(
            "SAMSUNG32 A:1234 C:6AA5",
            describe_capture(
                samsung32_pairs(0x1234, 0x6AA5, extended=True, command_16bit=True)
            ),
        )

    def test_classifies_complete_protocol_frames(self):
        nec = classify_capture(nec_pairs(0x45, 0x66))
        samsung = classify_capture(samsung32_pairs(0x07, 0x02))
        self.assertEqual(("NEC", FRAME_FULL), (nec["protocol"], nec["frame"]))
        self.assertEqual(
            ("SAMSUNG32", FRAME_FULL),
            (samsung["protocol"], samsung["frame"]),
        )

    def test_classifies_nec_short_repeat_with_jitter(self):
        repeat = jitter([[9000, 2250], [560, 10_000]], 0.84, 1.16)
        classified = classify_capture(repeat)
        self.assertEqual("NEC", classified["protocol"])
        self.assertEqual(FRAME_REPEAT, classified["frame"])
        self.assertEqual("short", classified["repeat_style"])
        self.assertTrue(is_repeat_frame(repeat))
        self.assertEqual("NEC REPEAT", describe_capture(repeat))

    def test_classifies_both_samsung_short_repeat_forms(self):
        short = classify_capture(jitter([[4500, 2250], [560, 10_000]]))
        samsung_lg = classify_capture(
            jitter([[4500, 4500], [560, 560], [560, 10_000]])
        )
        self.assertEqual((FRAME_REPEAT, "short"), (short["frame"], short["repeat_style"]))
        self.assertEqual(
            (FRAME_REPEAT, "samsung_lg"),
            (samsung_lg["frame"], samsung_lg["repeat_style"]),
        )
        self.assertEqual("SAMSUNG32 REPEAT", describe_capture([[4500, 2250], [560, 9000]]))

    def test_jittered_full_frames_still_decode_and_match(self):
        first = samsung32_pairs(0x07, 0x02)
        second = jitter(first)
        self.assertEqual(0x02, decode_samsung32(second)["command"])
        self.assertEqual(FRAME_FULL, classify_capture(second)["frame"])
        self.assertTrue(captures_match(first, second))

    def test_capture_matching_rejects_malformed_values(self):
        first = nec_pairs(0x45, 0x66)
        second = nec_pairs(0x45, 0x66)
        second[4] = [None, 560]
        self.assertFalse(captures_match(first, second))

    def test_normalize_rounds_to_ten_microseconds(self):
        self.assertEqual([[560, 1690]], normalize_pairs([(557, 1686)]))

    def test_normalize_rejects_invalid_limits_and_values(self):
        with self.assertRaises(ValueError):
            normalize_pairs([(560, 560)], quantum_us=0)
        with self.assertRaises(ValueError):
            normalize_pairs([None])
        with self.assertRaises(ValueError):
            normalize_pairs([(1, 560)])

    def test_normalize_full_capture_rejects_repeat_and_truncated_frame(self):
        with self.assertRaisesRegex(ValueError, "repeat-only"):
            normalize_full_capture([[9000, 2250], [560, 10_000]])
        with self.assertRaisesRegex(ValueError, "incomplete or malformed"):
            normalize_full_capture(nec_pairs(0x45, 0x66)[:-1])

    def test_select_full_capture_skips_repeat_and_malformed_candidates(self):
        repeat = [[4500, 4500], [560, 560], [560, 10_000]]
        malformed = samsung32_pairs(0x07, 0x02)[:-1]
        full = jitter(samsung32_pairs(0x07, 0x02))
        selected = select_full_capture([repeat, malformed, full])
        self.assertEqual(normalize_pairs(full), selected)
        self.assertEqual(FRAME_FULL, classify_capture(selected)["frame"])

    def test_select_prefers_recognized_full_over_raw_and_can_disallow_raw(self):
        raw = [[1000, 1000], [800, 1200], [700, 900], [650, 850]]
        full = nec_pairs(0x45, 0x66)
        self.assertEqual(normalize_pairs(full), select_full_capture([raw, full]))
        self.assertEqual(normalize_pairs(raw), select_full_capture([raw]))
        self.assertIsNone(select_full_capture([raw], allow_raw=False))

    def test_only_repeat_or_malformed_candidates_select_nothing(self):
        captures = [
            [[9000, 2250], [560, 10_000]],
            samsung32_pairs(0x07, 0x02)[:-1],
            None,
        ]
        self.assertIsNone(select_full_capture(captures))

    def test_unknown_capture_is_raw(self):
        raw = [[1000, 1000], [800, 1200], [700, 900], [650, 850]]
        self.assertEqual(FRAME_RAW, classify_capture(raw)["frame"])
        self.assertFalse(is_repeat_frame(raw))


if __name__ == "__main__":
    unittest.main()
