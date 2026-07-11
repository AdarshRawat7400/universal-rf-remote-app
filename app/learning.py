"""Pure-Python state machine for reliable two-press IR learning.

The IR receiver can produce a full frame followed by several short repeat
frames while a remote button remains held.  Treating those frames as separate
button presses makes learning unreliable, so this module waits for a quiet
release gap before accepting the confirmation press.
"""

from ir.codec import (
    FRAME_MALFORMED,
    FRAME_REPEAT,
    captures_match,
    classify_capture,
    normalize_full_capture,
)


class LearningSession:
    IDLE = "idle"
    FIRST = "first"
    WAIT_RELEASE = "wait_release"
    CONFIRM = "confirm"
    COMPLETE = "complete"

    def __init__(self, release_gap_ms=220, max_pairs=512):
        release_gap_ms = int(release_gap_ms)
        max_pairs = int(max_pairs)
        if release_gap_ms < 100 or release_gap_ms > 2000:
            raise ValueError("release gap must be 100 to 2000 ms")
        if max_pairs < 4:
            raise ValueError("max_pairs must be at least four")
        self.release_gap_ms = release_gap_ms
        self.max_pairs = max_pairs
        self.cancel()

    @property
    def active(self):
        return self.state not in (self.IDLE, self.COMPLETE)

    def start(self, now_ms=0):
        self.state = self.FIRST
        self.first_capture = None
        self.first_info = None
        self.result = None
        self.result_info = None
        self.last_error = None
        self.last_capture_pairs = 0
        self.last_info = None
        self.last_activity_ms = int(now_ms)
        return "ready_first"

    def cancel(self):
        self.state = self.IDLE
        self.first_capture = None
        self.first_info = None
        self.result = None
        self.result_info = None
        self.last_error = None
        self.last_capture_pairs = 0
        self.last_info = None
        self.last_activity_ms = 0

    def tick(self, now_ms):
        """Advance from release-wait to confirmation after a quiet gap."""

        now_ms = int(now_ms)
        if (
            self.state == self.WAIT_RELEASE
            and now_ms - self.last_activity_ms >= self.release_gap_ms
        ):
            self.state = self.CONFIRM
            return "ready_confirm"
        return None

    def feed(self, pairs, now_ms):
        """Consume a captured frame and return a short UI event name."""

        now_ms = int(now_ms)
        if not self.active:
            return "inactive"

        try:
            self.last_capture_pairs = len(pairs)
        except TypeError:
            self.last_capture_pairs = 0
        info = classify_capture(pairs)
        self.last_info = info
        frame = info["frame"]

        # Any IR activity means the original remote has not been quiet long
        # enough to count the next frame as a separate confirmation press.
        if self.state == self.WAIT_RELEASE:
            self.last_activity_ms = now_ms
            return "repeat" if frame == FRAME_REPEAT else "waiting_release"

        if frame == FRAME_REPEAT:
            return "repeat"
        if frame == FRAME_MALFORMED:
            self.last_error = "recognized protocol frame is incomplete"
            return "malformed"

        try:
            normalized = normalize_full_capture(
                pairs, max_pairs=self.max_pairs
            )
        except (TypeError, ValueError) as error:
            self.last_error = str(error)
            return "invalid"

        self.last_error = None

        if self.state == self.FIRST:
            self.first_capture = normalized
            self.first_info = classify_capture(normalized)
            self.last_activity_ms = now_ms
            self.state = self.WAIT_RELEASE
            return "first_captured"

        if self.state == self.CONFIRM:
            if captures_match(self.first_capture, normalized):
                self.result = normalized
                self.result_info = self.first_info
                self.state = self.COMPLETE
                return "confirmed"

            # A different key was pressed. Starting over is less surprising
            # than silently replacing the first sample and requiring a third
            # press whose purpose is unclear to the user.
            self.first_capture = None
            self.first_info = None
            self.state = self.FIRST
            return "mismatch"

        return "inactive"
