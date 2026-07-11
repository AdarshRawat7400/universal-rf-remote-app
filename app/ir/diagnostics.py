"""Pure-Python IR receiver and transmitter diagnostic state.

This module intentionally avoids ``machine`` and ``rp2`` so the state model can
be exercised by desktop tests as well as used on the badge.
"""

import time


def _clock_ms():
    ticks_ms = getattr(time, "ticks_ms", None)
    if ticks_ms is not None:
        return ticks_ms()
    monotonic = getattr(time, "monotonic", None)
    if monotonic is not None:
        return int(monotonic() * 1000)
    return int(time.time() * 1000)


def _ticks_difference(newer, older):
    ticks_diff = getattr(time, "ticks_diff", None)
    if ticks_diff is not None:
        return ticks_diff(newer, older)
    return newer - older


class IRDiagnostics:
    """Track useful cumulative counters plus one listening-session state."""

    READY = "ready"
    LISTENING = "listening"
    ACTIVITY = "activity"
    CAPTURED = "captured"
    TRANSMITTING = "transmitting"
    TIMEOUT = "timeout"
    ERROR = "error"
    CLOSED = "closed"

    def __init__(
        self,
        listen_timeout_ms=8_000,
        capture_stall_ms=750,
        clock_ms=None,
        ticks_difference=None,
    ):
        listen_timeout_ms = int(listen_timeout_ms)
        capture_stall_ms = int(capture_stall_ms)
        if listen_timeout_ms < 250 or listen_timeout_ms > 60_000:
            raise ValueError("listen timeout must be 250 to 60000 ms")
        if capture_stall_ms < 50 or capture_stall_ms > 10_000:
            raise ValueError("capture stall must be 50 to 10000 ms")

        self.listen_timeout_ms = listen_timeout_ms
        self.capture_stall_ms = capture_stall_ms
        self._clock_ms = clock_ms or _clock_ms
        self._ticks_difference = ticks_difference or _ticks_difference

        self.activity_count = 0
        self.capture_count = 0
        self.frame_timeout_count = 0
        self.listening_timeout_count = 0
        self.discarded_count = 0
        self.transmit_count = 0
        self.transmit_frame_count = 0
        self.last_capture_pairs = 0
        self.last_transmit_frames = 0

        self.state = self.READY
        self.error_code = None
        self.error_message = ""
        self._listen_started_ms = None
        self._last_activity_ms = None
        self._session_activity_count = 0

    def _now(self, now_ms):
        return self._clock_ms() if now_ms is None else int(now_ms)

    def clear_error(self):
        self.error_code = None
        self.error_message = ""
        if self.state in (self.ERROR, self.TIMEOUT):
            self.state = self.READY

    def begin_listening(self, now_ms=None, timeout_ms=None):
        if timeout_ms is not None:
            timeout_ms = int(timeout_ms)
            if timeout_ms < 250 or timeout_ms > 60_000:
                raise ValueError("listen timeout must be 250 to 60000 ms")
            self.listen_timeout_ms = timeout_ms
        now_ms = self._now(now_ms)
        self.clear_error()
        self.state = self.LISTENING
        self._listen_started_ms = now_ms
        self._last_activity_ms = None
        self._session_activity_count = 0

    def note_activity(self, count=1, now_ms=None):
        count = int(count)
        if count < 1:
            raise ValueError("activity count must be positive")
        self.activity_count += count
        if self.state in (self.LISTENING, self.ACTIVITY):
            self._session_activity_count += count
            self._last_activity_ms = self._now(now_ms)
            self.state = self.ACTIVITY

    def note_frame_timeout(self, count=1):
        count = int(count)
        if count < 1:
            raise ValueError("frame timeout count must be positive")
        self.frame_timeout_count += count

    def note_discarded(self, count=1, code="invalid_capture", message=None):
        count = int(count)
        if count < 1:
            raise ValueError("discard count must be positive")
        self.discarded_count += count
        self.note_error(code, message or "IR activity did not form a valid capture")

    def note_capture(self, pair_count, now_ms=None):
        pair_count = int(pair_count)
        if pair_count < 1:
            raise ValueError("capture must contain at least one pair")
        self.capture_count += 1
        self.last_capture_pairs = pair_count
        self.clear_error()
        self.state = self.CAPTURED
        self._last_activity_ms = self._now(now_ms)

    def note_transmitting(self):
        self.clear_error()
        self.state = self.TRANSMITTING

    def note_transmit_complete(self, frame_count=1):
        frame_count = int(frame_count)
        if frame_count < 1:
            raise ValueError("transmit frame count must be positive")
        self.transmit_count += 1
        self.transmit_frame_count += frame_count
        self.last_transmit_frames = frame_count
        self.state = self.READY

    def note_ready(self):
        self.clear_error()
        self.state = self.READY

    def note_error(self, code, message):
        self.error_code = str(code or "ir_error")
        self.error_message = str(message or "IR hardware error")
        self.state = self.ERROR

    def note_closed(self):
        self.state = self.CLOSED

    def check_timeout(self, now_ms=None):
        if self.state not in (self.LISTENING, self.ACTIVITY):
            return None
        now_ms = self._now(now_ms)

        if self._last_activity_ms is not None:
            quiet_ms = self._ticks_difference(now_ms, self._last_activity_ms)
            if quiet_ms >= self.capture_stall_ms:
                self.listening_timeout_count += 1
                self.error_code = "capture_stalled"
                self.error_message = "IR activity stopped before a valid capture"
                self.state = self.TIMEOUT
                return self.error_code

        elapsed_ms = self._ticks_difference(now_ms, self._listen_started_ms)
        if elapsed_ms >= self.listen_timeout_ms:
            self.listening_timeout_count += 1
            if self._session_activity_count:
                self.error_code = "capture_timeout"
                self.error_message = "IR activity was seen but no frame completed"
            else:
                self.error_code = "no_ir_activity"
                self.error_message = "No IR activity detected"
            self.state = self.TIMEOUT
            return self.error_code
        return None

    def snapshot(self, now_ms=None):
        self.check_timeout(now_ms)
        return {
            "state": self.state,
            "listening": self.state in (self.LISTENING, self.ACTIVITY),
            "activity_count": self.activity_count,
            "session_activity_count": self._session_activity_count,
            "capture_count": self.capture_count,
            "frame_timeout_count": self.frame_timeout_count,
            "listening_timeout_count": self.listening_timeout_count,
            "discarded_count": self.discarded_count,
            "transmit_count": self.transmit_count,
            "transmit_frame_count": self.transmit_frame_count,
            "last_capture_pairs": self.last_capture_pairs,
            "last_transmit_frames": self.last_transmit_frames,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }
