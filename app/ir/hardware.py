"""Badge-specific IR hardware lifecycle."""

import time

import config
from .diagnostics import IRDiagnostics
from .pulse import RawPulseReceiver, RawPulseSender


def _sleep_ms(duration_ms):
    sleep_ms = getattr(time, "sleep_ms", None)
    if sleep_ms is not None:
        sleep_ms(duration_ms)
    else:
        time.sleep(duration_ms / 1000)


def _near(actual, expected, tolerance=0.25):
    return abs(actual - expected) <= expected * tolerance


def _infer_protocol(pairs):
    """Recognize repeat timing from a raw header without consuming iterators."""
    if not isinstance(pairs, (list, tuple)) or not pairs:
        return None
    try:
        mark_us = int(pairs[0][0])
        space_us = int(pairs[0][1])
    except (IndexError, TypeError, ValueError):
        return None
    if _near(mark_us, 4_500) and _near(space_us, 4_500):
        return "SAMSUNG32"
    if _near(mark_us, 9_000) and _near(space_us, 4_500):
        return "NEC"
    return None


def _repeat_period(protocol, pairs):
    protocol = protocol or _infer_protocol(pairs)
    if protocol is None:
        return None
    protocol = str(protocol).upper().replace("_", "")
    periods = config.TX_PROTOCOL_REPEAT_PERIOD_US
    if protocol in periods:
        return periods[protocol]
    # Accept the codec spelling while also tolerating UI labels with spaces.
    return periods.get(protocol.replace(" ", ""))


def _burst_frames(preset):
    name = str(preset).lower()
    presets = config.TX_BURST_PRESETS
    if name not in presets:
        raise ValueError("unknown burst preset: " + name)
    frames = int(presets[name])
    maximum = int(config.TX_MAX_BURST_FRAMES)
    if frames < 1 or frames > maximum:
        raise ValueError("burst preset exceeds the safe frame limit")
    return frames


class IRHardware:
    def __init__(self):
        self._closed = False
        self.diagnostics = IRDiagnostics(
            config.RX_LISTEN_TIMEOUT_MS,
            config.RX_CAPTURE_STALL_MS,
        )
        self.receiver = RawPulseReceiver(
            config.RX_PIN,
            config.RX_PIO,
            config.RX_STATE_MACHINE,
            config.GLITCH_FILTER_US,
            config.MAX_CAPTURE_PAIRS,
        )
        self.sender = RawPulseSender(
            config.TX_PIN,
            config.TX_PIO,
            config.TX_STATE_MACHINE,
            config.CARRIER_HZ,
        )
        self._receiver_counts = self.receiver.diagnostic_counts()
        self.diagnostics.note_ready()
        # Keep RX stopped until Learn or Listen is explicitly opened. An idle
        # IRQ otherwise fills its queue while the app is not polling it and
        # steals the heap needed by later profile saves.

    @property
    def transmit_enabled(self):
        return self.sender is not None

    def reset_capture(self):
        self.begin_listening()

    def begin_listening(self, timeout_ms=None):
        if self._closed:
            raise RuntimeError("IR hardware is closed")
        self._sync_receiver_diagnostics()
        self.receiver.reset()
        self.receiver.start()
        self._receiver_counts = self.receiver.diagnostic_counts()
        self.diagnostics.begin_listening(timeout_ms=timeout_ms)

    def end_listening(self):
        """Stop RX and discard queued captures when leaving a listen screen."""

        if self._closed:
            return
        self.receiver.stop()
        self.receiver.reset()
        self._receiver_counts = self.receiver.diagnostic_counts()
        self.diagnostics.note_ready()

    def poll_capture(self):
        if self._closed:
            return None
        try:
            capture = self.receiver.poll()
            self._sync_receiver_diagnostics()
        except Exception as error:
            self.diagnostics.note_error("receiver_poll_failed", str(error))
            raise
        if capture is not None:
            self.diagnostics.note_capture(len(capture))
        else:
            self.diagnostics.check_timeout()
        return capture

    def _sync_receiver_diagnostics(self):
        current = self.receiver.diagnostic_counts()
        previous = self._receiver_counts
        activity = current["activity_count"] - previous["activity_count"]
        if activity > 0:
            self.diagnostics.note_activity(activity)
        frame_timeouts = (
            current["frame_timeout_count"] - previous["frame_timeout_count"]
        )
        if frame_timeouts > 0:
            self.diagnostics.note_frame_timeout(frame_timeouts)
        discarded = current["discarded_count"] - previous["discarded_count"]
        queue_overflows = (
            current["queue_overflow_count"] - previous["queue_overflow_count"]
        )
        if queue_overflows > 0:
            self.diagnostics.note_discarded(
                queue_overflows,
                "receiver_queue_overflow",
                "IR receiver input queue overflowed",
            )
        if discarded > 0:
            code = current["last_error_code"] or "invalid_capture"
            self.diagnostics.note_discarded(discarded, code)
        self._receiver_counts = current

    def diagnostics_snapshot(self):
        if not self._closed:
            self._sync_receiver_diagnostics()
        snapshot = self.diagnostics.snapshot()
        receiver = self.receiver.diagnostic_counts()
        sender = self.sender.diagnostics()
        snapshot["receiver_running"] = receiver["running"]
        snapshot["boundary_glitches"] = receiver.get("boundary_glitch_count", 0)
        snapshot["carrier_hz"] = self.sender.carrier_hz
        snapshot["last_repeat_period_us"] = sender["repeat_period_us"]
        snapshot["late_repeats"] = sender["late_repeats"]
        return snapshot

    def send(
        self,
        pairs,
        carrier_hz=None,
        repetitions=None,
        inter_frame_gap_ms=None,
        repeat_count=None,
        repeat_gap_us=None,
        protocol=None,
        repeat_period_us=None,
        burst_preset=None,
    ):
        """Send a command while preventing the receiver from hearing itself.

        The optional arguments preserve the original ``send(pairs)`` API while
        allowing each stored command to specify its own carrier and repeat
        behavior.
        """
        if self._closed:
            raise RuntimeError("IR hardware is closed")

        if repeat_count is not None:
            if repetitions is not None and int(repetitions) != int(repeat_count):
                raise ValueError("conflicting repeat counts")
            repetitions = repeat_count
        if burst_preset is not None:
            repetitions = _burst_frames(burst_preset)
        if repeat_gap_us is not None and inter_frame_gap_ms is not None:
            if int(repeat_gap_us) != int(inter_frame_gap_ms) * 1000:
                raise ValueError("conflicting repeat gaps")

        if carrier_hz is None:
            carrier_hz = config.CARRIER_HZ
        if repetitions is None:
            repetitions = config.TX_REPETITIONS
        if repeat_gap_us is None and inter_frame_gap_ms is None:
            inter_frame_gap_ms = config.TX_INTER_FRAME_GAP_MS
        if repeat_gap_us is None:
            repeat_gap_us = int(inter_frame_gap_ms) * 1000
        if repeat_period_us is None and int(repetitions) > 1:
            repeat_period_us = _repeat_period(protocol, pairs)
        elif repeat_period_us is not None and int(repeat_period_us) == 0:
            # Zero explicitly opts out of protocol timing and keeps the legacy
            # end-to-start gap behavior.
            repeat_period_us = None

        receiver_was_running = self.receiver.diagnostic_counts().get(
            "running", False
        )
        try:
            self.diagnostics.note_transmitting()
            self.receiver.stop()
            try:
                self.sender.set_carrier(carrier_hz)
                self.sender.start()
                result = self.sender.send(
                    pairs,
                    repetitions=repetitions,
                    repeat_gap_us=repeat_gap_us,
                    repeat_period_us=repeat_period_us,
                )
            finally:
                try:
                    self.sender.stop()
                finally:
                    _sleep_ms(config.RX_RECOVERY_MS)
                    try:
                        self.receiver.reset()
                    finally:
                        if receiver_was_running:
                            self.receiver.start()
                        self._receiver_counts = self.receiver.diagnostic_counts()
        except Exception as error:
            self.diagnostics.note_error("transmit_failed", str(error))
            raise
        self.diagnostics.note_transmit_complete(result["frames"])
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.receiver.stop()
        finally:
            self.sender.stop()
            self.diagnostics.note_closed()
