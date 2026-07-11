import os
import sys
import importlib.util
import types
import unittest


APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from ir.diagnostics import IRDiagnostics


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, duration_ms):
        self.now += duration_ms


class IRDiagnosticsTests(unittest.TestCase):
    def diagnostics(self, clock):
        return IRDiagnostics(
            listen_timeout_ms=2_000,
            capture_stall_ms=500,
            clock_ms=clock,
            ticks_difference=lambda newer, older: newer - older,
        )

    def test_no_activity_has_a_distinct_listening_timeout(self):
        clock = FakeClock()
        diagnostics = self.diagnostics(clock)
        diagnostics.begin_listening()
        clock.advance(1_999)
        self.assertIsNone(diagnostics.check_timeout())
        clock.advance(1)
        self.assertEqual("no_ir_activity", diagnostics.check_timeout())
        snapshot = diagnostics.snapshot()
        self.assertEqual(IRDiagnostics.TIMEOUT, snapshot["state"])
        self.assertEqual(1, snapshot["listening_timeout_count"])
        self.assertIn("No IR activity", snapshot["error_message"])

    def test_activity_without_a_frame_reports_capture_stall(self):
        clock = FakeClock()
        diagnostics = self.diagnostics(clock)
        diagnostics.begin_listening()
        clock.advance(100)
        diagnostics.note_activity(4)
        clock.advance(499)
        self.assertIsNone(diagnostics.check_timeout())
        clock.advance(1)
        self.assertEqual("capture_stalled", diagnostics.check_timeout())
        snapshot = diagnostics.snapshot()
        self.assertEqual(4, snapshot["activity_count"])
        self.assertEqual(4, snapshot["session_activity_count"])

    def test_capture_clears_errors_and_preserves_cumulative_counters(self):
        clock = FakeClock()
        diagnostics = self.diagnostics(clock)
        diagnostics.begin_listening()
        diagnostics.note_activity(34)
        diagnostics.note_frame_timeout()
        diagnostics.note_discarded(code="capture_too_short")
        self.assertEqual(IRDiagnostics.ERROR, diagnostics.state)

        diagnostics.begin_listening()
        diagnostics.note_activity(34)
        diagnostics.note_frame_timeout()
        diagnostics.note_capture(34)
        snapshot = diagnostics.snapshot()
        self.assertEqual(IRDiagnostics.CAPTURED, snapshot["state"])
        self.assertIsNone(snapshot["error_code"])
        self.assertEqual(68, snapshot["activity_count"])
        self.assertEqual(1, snapshot["capture_count"])
        self.assertEqual(2, snapshot["frame_timeout_count"])
        self.assertEqual(1, snapshot["discarded_count"])
        self.assertEqual(34, snapshot["last_capture_pairs"])

    def test_transmit_and_closed_states_include_frame_counters(self):
        clock = FakeClock()
        diagnostics = self.diagnostics(clock)
        diagnostics.note_transmitting()
        self.assertEqual(IRDiagnostics.TRANSMITTING, diagnostics.state)
        diagnostics.note_transmit_complete(2)
        snapshot = diagnostics.snapshot()
        self.assertEqual(IRDiagnostics.READY, snapshot["state"])
        self.assertEqual(1, snapshot["transmit_count"])
        self.assertEqual(2, snapshot["transmit_frame_count"])
        self.assertEqual(2, snapshot["last_transmit_frames"])
        diagnostics.note_closed()
        self.assertEqual(IRDiagnostics.CLOSED, diagnostics.snapshot()["state"])

    def test_constructor_and_counter_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            IRDiagnostics(listen_timeout_ms=100)
        with self.assertRaises(ValueError):
            IRDiagnostics(capture_stall_ms=20)
        diagnostics = self.diagnostics(FakeClock())
        with self.assertRaises(ValueError):
            diagnostics.note_activity(0)
        with self.assertRaises(ValueError):
            diagnostics.note_transmit_complete(0)


class FakeReceiver:
    def __init__(self, *args):
        self.running = False
        self.events = []
        self.counts = {
            "running": False,
            "activity_count": 0,
            "frame_timeout_count": 0,
            "discarded_count": 0,
            "queue_overflow_count": 0,
            "last_error_code": None,
        }

    def start(self):
        self.running = True
        self.counts["running"] = True
        self.events.append("start")

    def stop(self):
        self.running = False
        self.counts["running"] = False
        self.events.append("stop")

    def reset(self):
        self.events.append("reset")

    def poll(self):
        return None

    def diagnostic_counts(self):
        return dict(self.counts)


class FakeSender:
    def __init__(self, *args):
        self.carrier_hz = args[-1]
        self.last_kwargs = None
        self.last = {
            "frames": 0,
            "frame_duration_us": 0,
            "repeat_period_us": None,
            "late_repeats": 0,
        }

    def set_carrier(self, carrier_hz):
        self.carrier_hz = carrier_hz

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, pairs, **kwargs):
        self.last_kwargs = kwargs
        self.last = {
            "frames": kwargs["repetitions"],
            "frame_duration_us": sum(mark + space for mark, space in pairs),
            "repeat_period_us": kwargs["repeat_period_us"],
            "late_repeats": 0,
        }
        return dict(self.last)

    def diagnostics(self):
        return dict(self.last)


def load_hardware_with_fakes():
    fake_pulse = types.ModuleType("ir.pulse")
    fake_pulse.RawPulseReceiver = FakeReceiver
    fake_pulse.RawPulseSender = FakeSender
    module_name = "ir._desktop_hardware_test"
    path = os.path.join(APP_DIR, "ir", "hardware.py")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("ir.pulse")
    sys.modules["ir.pulse"] = fake_pulse
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            del sys.modules["ir.pulse"]
        else:
            sys.modules["ir.pulse"] = previous
        del sys.modules[module_name]
    return module


class HardwarePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hardware_module = load_hardware_with_fakes()

    def test_reliable_samsung_burst_uses_start_to_start_period(self):
        hardware = self.hardware_module.IRHardware()
        pairs = [[4_500, 4_500], [560, 10_000]]
        result = hardware.send(
            pairs,
            repeat_count=1,
            protocol="SAMSUNG32",
            burst_preset="reliable",
        )
        self.assertEqual(2, result["frames"])
        self.assertEqual(2, hardware.sender.last_kwargs["repetitions"])
        self.assertEqual(110_000, hardware.sender.last_kwargs["repeat_period_us"])
        self.assertEqual(["stop", "reset"], hardware.receiver.events)
        self.assertFalse(hardware.receiver.running)
        snapshot = hardware.diagnostics_snapshot()
        self.assertEqual(1, snapshot["transmit_count"])
        self.assertEqual(2, snapshot["transmit_frame_count"])

    def test_receiver_runs_only_during_explicit_listening(self):
        hardware = self.hardware_module.IRHardware()
        self.assertFalse(hardware.receiver.running)
        self.assertEqual([], hardware.receiver.events)

        hardware.begin_listening()
        self.assertTrue(hardware.receiver.running)
        self.assertEqual(["reset", "start"], hardware.receiver.events)

        hardware.end_listening()
        self.assertFalse(hardware.receiver.running)
        self.assertEqual(
            ["reset", "start", "stop", "reset"], hardware.receiver.events
        )

    def test_transmit_restores_receiver_only_when_it_was_listening(self):
        hardware = self.hardware_module.IRHardware()
        hardware.begin_listening()
        hardware.receiver.events = []
        hardware.send([[4_500, 4_500], [560, 10_000]])

        self.assertTrue(hardware.receiver.running)
        self.assertEqual(["stop", "reset", "start"], hardware.receiver.events)

    def test_samsung_header_infers_period_and_zero_can_opt_out(self):
        hardware = self.hardware_module.IRHardware()
        pairs = [[4_500, 4_500], [560, 10_000]]
        hardware.send(pairs, repeat_count=2)
        self.assertEqual(110_000, hardware.sender.last_kwargs["repeat_period_us"])
        hardware.send(pairs, repeat_count=2, repeat_period_us=0)
        self.assertIsNone(hardware.sender.last_kwargs["repeat_period_us"])

    def test_burst_presets_are_named_and_bounded(self):
        hardware = self.hardware_module.IRHardware()
        pairs = [[4_500, 4_500], [560, 10_000]]
        with self.assertRaises(ValueError):
            hardware.send(pairs, burst_preset="unbounded")


if __name__ == "__main__":
    unittest.main()
