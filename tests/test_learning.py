import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))

from learning import LearningSession


def nec_pairs(address=0x45, command=0x66):
    code = (address & 0xFF) | ((address ^ 0xFF) << 8)
    code |= (command | ((command ^ 0xFF) << 8)) << 16
    pairs = [[9000, 4500]]
    for bit in range(32):
        pairs.append([560, 1680 if code & (1 << bit) else 560])
    pairs.append([560, 10_000])
    return pairs


class LearningSessionTests(unittest.TestCase):
    def test_waits_for_release_and_ignores_repeat_frames(self):
        session = LearningSession(release_gap_ms=200)
        session.start(0)
        self.assertEqual("first_captured", session.feed(nec_pairs(), 10))
        self.assertEqual("repeat", session.feed([[9000, 2250], [560, 9000]], 100))
        self.assertIsNone(session.tick(299))
        self.assertEqual("ready_confirm", session.tick(300))

        jittered = [[int(mark * 1.06), int(space * 0.95)] for mark, space in nec_pairs()]
        self.assertEqual("confirmed", session.feed(jittered, 350))
        self.assertEqual(LearningSession.COMPLETE, session.state)
        self.assertEqual("NEC", session.result_info["protocol"])

    def test_repeated_full_frames_do_not_count_as_confirmation(self):
        session = LearningSession(release_gap_ms=200)
        session.start(0)
        self.assertEqual("first_captured", session.feed(nec_pairs(), 10))
        self.assertEqual("waiting_release", session.feed(nec_pairs(), 150))
        self.assertIsNone(session.tick(349))
        self.assertEqual("ready_confirm", session.tick(350))

    def test_mismatch_restarts_two_press_sequence(self):
        session = LearningSession()
        session.start(0)
        session.feed(nec_pairs(command=0x10), 10)
        session.tick(300)
        self.assertEqual("mismatch", session.feed(nec_pairs(command=0x20), 310))
        self.assertEqual(LearningSession.FIRST, session.state)
        self.assertIsNone(session.first_capture)

    def test_short_or_malformed_capture_is_not_saved(self):
        session = LearningSession()
        session.start(0)
        self.assertEqual("invalid", session.feed([[100, 100], [100, 100]], 10))
        self.assertEqual(2, session.last_capture_pairs)
        self.assertEqual("capture is too short to be a full frame", session.last_error)
        truncated = nec_pairs()[:-1]
        self.assertEqual("malformed", session.feed(truncated, 20))
        self.assertEqual("recognized protocol frame is incomplete", session.last_error)
        self.assertEqual(LearningSession.FIRST, session.state)

    def test_subquantum_edge_reports_reason_and_valid_frame_clears_it(self):
        session = LearningSession()
        session.start(0)
        capture = [[5, 500]] + nec_pairs()
        self.assertEqual("invalid", session.feed(capture, 10))
        self.assertIn("below the normalization quantum", session.last_error)
        self.assertEqual(35, session.last_capture_pairs)

        self.assertEqual("first_captured", session.feed(nec_pairs(), 20))
        self.assertIsNone(session.last_error)

    def test_cancel_and_constructor_validation(self):
        with self.assertRaises(ValueError):
            LearningSession(release_gap_ms=50)
        session = LearningSession()
        session.start()
        session.cancel()
        self.assertFalse(session.active)
        self.assertEqual("inactive", session.feed(nec_pairs(), 1))


if __name__ == "__main__":
    unittest.main()
