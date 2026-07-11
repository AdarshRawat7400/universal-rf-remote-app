import builtins
import importlib.util
import os
import sys
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
APP_DIR = os.path.join(ROOT, "app")
PULSE_PATH = os.path.join(APP_DIR, "ir", "pulse.py")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from ir.codec import encode_samsung32


class FakePin:
    IN = 0
    OUT = 1
    PULL_UP = 2

    def __init__(self, *_args):
        pass

    def off(self):
        pass


class FakeStateMachine:
    def __init__(self, *_args, **_kwargs):
        self._active = False

    def irq(self, *_args):
        pass

    def active(self, value=None):
        if value is not None:
            self._active = bool(value)
        return self._active

    def rx_fifo(self):
        return 0

    def get(self):
        return 0

    def restart(self):
        pass

    def put(self, _value):
        pass

    def init(self, *_args, **_kwargs):
        pass


def load_pulse_module():
    previous_const = getattr(builtins, "const", None)
    dependency_names = (
        "micropython",
        "machine",
        "rp2",
        "ir.pio_rx",
        "ir.pio_tx",
    )
    previous = {name: sys.modules.get(name) for name in dependency_names}
    builtins.const = lambda value: value

    micropython = types.ModuleType("micropython")
    micropython.native = lambda function: function
    machine = types.ModuleType("machine")
    machine.Pin = FakePin
    machine.mem32 = {}
    rp2 = types.ModuleType("rp2")
    rp2.StateMachine = FakeStateMachine

    pio_rx = types.ModuleType("ir.pio_rx")
    pio_rx.FREQUENCY = 2_000_000
    pio_rx.TIMEOUT_REACHED = 0xFFFFFFFF
    pio_rx.count_to_burst_us = lambda value: value
    pio_rx.count_to_idle_us = lambda value: value
    pio_rx.pulse_reader = object()
    pio_tx = types.ModuleType("ir.pio_tx")
    pio_tx.CLOCKS_PER_CYCLE = 2
    pio_tx.pulse_sender = object()

    replacements = {
        "micropython": micropython,
        "machine": machine,
        "rp2": rp2,
        "ir.pio_rx": pio_rx,
        "ir.pio_tx": pio_tx,
    }
    sys.modules.update(replacements)
    module_name = "ir._desktop_pulse_filter_test"
    try:
        spec = importlib.util.spec_from_file_location(module_name, PULSE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(module_name, None)
        for name in dependency_names:
            old = previous[name]
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        if previous_const is None:
            del builtins.const
        else:
            builtins.const = previous_const


class PulseBoundaryFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_pulse_module()

    def receiver(self):
        return self.module.RawPulseReceiver(21, 0, 0, 200, 512)

    @staticmethod
    def feed(receiver, pairs):
        for mark, space in pairs:
            receiver._append_filtered((mark, space))
        receiver._finish_capture()

    def test_leading_edge_glitch_is_removed_from_valid_samsung_frame(self):
        receiver = self.receiver()
        expected = encode_samsung32(0x07, 0x02)
        self.feed(receiver, [[5, 500]] + expected)

        capture = receiver._captures.popleft()
        self.assertEqual(34, len(capture))
        self.assertEqual((4500, 4500), capture[0])
        self.assertEqual(1, receiver.diagnostic_counts()["boundary_glitch_count"])

    def test_terminal_edge_glitch_merges_into_prior_quiet_time(self):
        receiver = self.receiver()
        expected = [tuple(pair) for pair in encode_samsung32(0x07, 0x02)]
        receiver._sequence = list(expected)
        receiver._last_pair = (5, 500)
        receiver._finish_capture()

        capture = receiver._captures.popleft()
        self.assertEqual(34, len(capture))
        self.assertEqual(expected[-1][0], capture[-1][0])
        self.assertEqual(expected[-1][1] + 505, capture[-1][1])
        self.assertEqual(1, receiver.diagnostic_counts()["boundary_glitch_count"])

    def test_pure_boundary_noise_is_discarded_not_queued(self):
        receiver = self.receiver()
        self.feed(receiver, [[5, 500]])

        self.assertFalse(receiver._captures)
        diagnostics = receiver.diagnostic_counts()
        self.assertEqual(1, diagnostics["boundary_glitch_count"])
        self.assertEqual(1, diagnostics["discarded_count"])
        self.assertEqual("capture_too_short", diagnostics["last_error_code"])

    def test_reset_reuses_fixed_capacity_queues(self):
        receiver = self.receiver()
        counts = receiver._counts
        captures = receiver._captures
        counts.append(123)
        captures.append([(560, 560)])
        receiver._sequence.append((560, 560))

        receiver.reset()

        self.assertIs(counts, receiver._counts)
        self.assertIs(captures, receiver._captures)
        self.assertFalse(receiver._counts)
        self.assertFalse(receiver._captures)
        self.assertEqual([], receiver._sequence)

    def test_count_queue_is_sized_for_max_frame_and_terminator(self):
        receiver = self.receiver()

        self.assertEqual(514, receiver._count_capacity)
        self.assertEqual(514, receiver._counts.maxlen)

    def test_full_queue_keeps_timeout_marker(self):
        receiver = self.receiver()
        for value in range(receiver._count_capacity):
            receiver._counts.append(value)

        class OneWordStateMachine:
            def __init__(self, value):
                self.value = value
                self.available = True

            def rx_fifo(self):
                return 1 if self.available else 0

            def get(self):
                self.available = False
                return self.value

        receiver._handler(OneWordStateMachine(self.module.TIMEOUT_REACHED))

        self.assertEqual(receiver._count_capacity, len(receiver._counts))
        self.assertEqual(self.module.TIMEOUT_REACHED, receiver._counts[-1])
        self.assertEqual(
            "receiver_queue_overflow",
            receiver.diagnostic_counts()["last_error_code"],
        )

    def test_finish_capture_transfers_sequence_without_cloning(self):
        receiver = self.receiver()
        receiver._sequence = [(4500, 4500), (560, 560)]
        original = receiver._sequence

        receiver._finish_capture()

        self.assertIs(original, receiver._captures.popleft())
        self.assertIsNot(original, receiver._sequence)
        self.assertEqual([], receiver._sequence)


if __name__ == "__main__":
    unittest.main()
