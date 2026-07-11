# SPDX-FileCopyrightText: 2025 Christopher Parrott for Pimoroni Ltd
# SPDX-License-Identifier: MIT
"""Raw IR pulse capture and replay using RP2350 PIO."""

from collections import deque
import time

import micropython
import rp2
from machine import Pin, mem32
from rp2 import StateMachine

from .pio_rx import (
    FREQUENCY,
    TIMEOUT_REACHED,
    count_to_burst_us,
    count_to_idle_us,
    pulse_reader,
)
from .pio_tx import CLOCKS_PER_CYCLE, pulse_sender


def _now_us():
    ticks_us = getattr(time, "ticks_us", None)
    if ticks_us is not None:
        return ticks_us()
    monotonic = getattr(time, "monotonic", None)
    if monotonic is not None:
        return int(monotonic() * 1_000_000)
    return int(time.time() * 1_000_000)


def _elapsed_us(newer, older):
    ticks_diff = getattr(time, "ticks_diff", None)
    if getattr(time, "ticks_us", None) is not None and ticks_diff is not None:
        return ticks_diff(newer, older)
    return newer - older


def _sleep_us(duration_us):
    if duration_us <= 0:
        return
    sleep_us = getattr(time, "sleep_us", None)
    if sleep_us is not None:
        sleep_us(duration_us)
    else:
        time.sleep(duration_us / 1_000_000)


class RawPulseReceiver:
    """Capture demodulated IR as ``(mark_us, space_us)`` pairs."""

    def __init__(self, pin_num, pio=0, sm=0, glitch_filter_us=200, max_pairs=512):
        self._running = False
        self._activity_count = 0
        self._frame_timeout_count = 0
        self._discarded_count = 0
        self._queue_overflow_count = 0
        self._boundary_glitch_count = 0
        self._last_error_code = None
        self._capture_overflowed = False
        # One complete capture can contain ``max_pairs`` valid pairs, one
        # extra pair used to detect an overlong frame, and its timeout marker.
        # Keeping this queue at the exact required capacity saves roughly 2KB
        # on the RP2350 compared with the old 1024-entry allocation.
        self._count_capacity = max_pairs + 2
        self._counts = deque((), self._count_capacity)
        self._captures = deque((), 4)
        self._sequence = []
        self._last_pair = None
        self._filter = glitch_filter_us
        self._max_pairs = max_pairs
        pin = Pin(pin_num, Pin.IN, Pin.PULL_UP)
        self._sm = rp2.StateMachine(
            sm + (pio * 4), pulse_reader, freq=FREQUENCY, in_base=pin, jmp_pin=pin
        )

    def start(self):
        if self._running:
            return
        self._sm.irq(self._handler)
        try:
            self._sm.active(1)
        except Exception:
            self._sm.irq(None)
            raise
        self._running = True

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._sm.active(0)
        finally:
            self._sm.irq(None)

    def reset(self):
        was_running = self._running
        if was_running:
            self.stop()
        try:
            # Reuse the fixed-capacity queues. Constructing replacement deques
            # briefly keeps both old and new backing buffers alive and can
            # exhaust the badge heap exactly when the user starts learning.
            while self._counts:
                self._counts.popleft()
            while self._captures:
                self._captures.popleft()
            self._sequence.clear()
            self._last_pair = None
            self._capture_overflowed = False
            self._last_error_code = None
            while self._sm.rx_fifo() > 0:
                self._sm.get()
            self._sm.restart()
        finally:
            if was_running:
                self.start()

    @micropython.native
    def _handler(self, sm):
        while sm.rx_fifo() > 0:
            packed = sm.get()
            if len(self._counts) >= self._count_capacity:
                self._queue_overflow_count += 1
                self._last_error_code = "receiver_queue_overflow"
                # Always retain the terminator so an overlong frame can be
                # completed and discarded instead of poisoning the next one.
                if packed == TIMEOUT_REACHED:
                    self._counts.popleft()
                    self._counts.append(packed)
            else:
                self._counts.append(packed)

    def _finish_capture(self):
        if self._last_pair is not None:
            mark, space = self._last_pair
            if mark < self._filter:
                # A terminal edge blip should not poison an otherwise valid
                # capture. Preserve its elapsed quiet time on the prior pair.
                self._boundary_glitch_count += 1
                if self._sequence:
                    last_mark, last_space = self._sequence[-1]
                    self._sequence[-1] = (
                        last_mark,
                        last_space + mark + space,
                    )
            elif len(self._sequence) < self._max_pairs:
                self._sequence.append(self._last_pair)
            else:
                self._capture_overflowed = True
            self._last_pair = None

        if self._capture_overflowed:
            self._discarded_count += 1
            self._last_error_code = "capture_overflow"
        elif 2 <= len(self._sequence) <= self._max_pairs:
            # Transfer the list rather than cloning its pointer array at the
            # peak of capture memory use.
            capture = self._sequence
            self._sequence = []
            self._captures.append(capture)
        else:
            self._discarded_count += 1
            self._last_error_code = "capture_too_short"
        if self._sequence:
            self._sequence.clear()
        self._capture_overflowed = False

    def _append_filtered(self, pair):
        mark, space = pair
        if self._last_pair is None:
            # The streaming filter normally compares adjacent pairs. The first
            # edge has no predecessor, so explicitly discard a sub-threshold
            # AGC/ambient blip and let the real protocol header become pair 0.
            if mark < self._filter:
                self._boundary_glitch_count += 1
                self._last_error_code = "boundary_glitch"
                return
            self._last_pair = pair
            return

        last_mark, last_space = self._last_pair

        if last_space < self._filter:
            self._last_pair = (mark + last_mark + last_space, space)
            return

        if mark < self._filter:
            self._last_pair = (last_mark, last_space + mark + space)
            return

        if len(self._sequence) < self._max_pairs:
            self._sequence.append(self._last_pair)
        else:
            self._capture_overflowed = True
        self._last_pair = pair

    def poll(self):
        while self._counts:
            packed = self._counts.popleft()
            if packed == TIMEOUT_REACHED:
                self._frame_timeout_count += 1
                self._finish_capture()
                continue

            self._activity_count += 1
            pair = (
                int(count_to_burst_us((packed >> 16) & 0xFFFF)),
                int(count_to_idle_us(packed & 0xFFFF)),
            )
            self._append_filtered(pair)

        if self._captures:
            return self._captures.popleft()
        return None

    def diagnostic_counts(self):
        return {
            "running": self._running,
            "activity_count": self._activity_count,
            "frame_timeout_count": self._frame_timeout_count,
            "discarded_count": self._discarded_count,
            "queue_overflow_count": self._queue_overflow_count,
            "boundary_glitch_count": self._boundary_glitch_count,
            "last_error_code": self._last_error_code,
        }


PIO_BASE = (0x50200000, 0x50300000, 0x50400000)
PIO_FDEBUG_OFFSET = const(0x00000008)
PIO_FDEBUG_TXSTALL_LSB = const(24)


class RawPulseSender:
    """Transmit mark/space pairs with a configurable carrier frequency."""

    def __init__(self, pin_num, pio=0, sm=1, carrier_hz=38_000):
        if pio < 0 or pio > 1:
            raise ValueError("PIO must be 0 or 1")
        if sm < 0 or sm > 3:
            raise ValueError("state machine must be 0 to 3")

        self._pin = Pin(pin_num)
        self._active = False
        self._carrier_hz = self._validate_carrier(carrier_hz)
        self._pio_freq = self._carrier_hz * CLOCKS_PER_CYCLE
        self._pio_reg = PIO_BASE[pio] | PIO_FDEBUG_OFFSET
        self._sm_mask = 1 << (PIO_FDEBUG_TXSTALL_LSB + sm)
        self._sm = StateMachine(
            sm + (pio * 4),
            pulse_sender,
            freq=self._pio_freq,
            sideset_base=self._pin,
        )
        self._last_send = {
            "frames": 0,
            "frame_duration_us": 0,
            "repeat_period_us": None,
            "late_repeats": 0,
        }

    def start(self):
        if self._active:
            return
        self._sm.active(1)
        self._active = True

    def stop(self):
        if self._active:
            self._sm.active(0)
            self._active = False
        # Ensure the LED cannot remain asserted after an interrupted send.
        self._pin.off()

    @property
    def carrier_hz(self):
        return self._carrier_hz

    @staticmethod
    def _validate_carrier(carrier_hz):
        carrier_hz = int(carrier_hz)
        if carrier_hz < 20_000 or carrier_hz > 100_000:
            raise ValueError("carrier frequency must be 20000 to 100000 Hz")
        return carrier_hz

    def set_carrier(self, carrier_hz):
        """Reconfigure the PIO clock between commands when needed."""
        carrier_hz = self._validate_carrier(carrier_hz)
        if carrier_hz == self._carrier_hz:
            return

        was_active = self._active
        if was_active:
            self.stop()
        try:
            pio_freq = carrier_hz * CLOCKS_PER_CYCLE
            self._sm.init(
                pulse_sender,
                freq=pio_freq,
                sideset_base=self._pin,
            )
            self._carrier_hz = carrier_hz
            self._pio_freq = pio_freq
        finally:
            if was_active:
                self.start()

    def _to_count(self, duration_us):
        value = round(
            ((duration_us * self._pio_freq) / (CLOCKS_PER_CYCLE * 1_000_000)) - 2
        )
        return max(0, min(0xFFFF, value))

    def _put(self, mark_us, space_us):
        mark = self._to_count(mark_us)
        space = self._to_count(space_us)
        self._sm.put((mark << 16) | space)

    def _wait(self, timeout_ms):
        mem32[self._pio_reg] = self._sm_mask
        ticks_ms = getattr(time, "ticks_ms", None)
        ticks_diff = getattr(time, "ticks_diff", None)
        started = ticks_ms() if ticks_ms is not None else time.monotonic()
        while not (mem32[self._pio_reg] & self._sm_mask):
            if ticks_ms is not None:
                elapsed = ticks_diff(ticks_ms(), started)
            else:
                elapsed = (time.monotonic() - started) * 1000
            if elapsed > timeout_ms:
                raise RuntimeError("timed out waiting for IR transmission")

    def send(
        self,
        pairs,
        carrier_hz=None,
        repetitions=1,
        inter_frame_gap_ms=None,
        repeat_count=None,
        repeat_gap_us=None,
        repeat_period_us=None,
    ):
        """Send complete frames and wait for each one to leave the PIO.

        ``repeat_gap_us`` retains its historical end-to-start meaning.
        ``repeat_period_us`` schedules repeats start-to-start and takes
        precedence when supplied, matching protocols such as Samsung32.
        """
        if carrier_hz is not None:
            self.set_carrier(carrier_hz)

        if repeat_count is not None:
            if repetitions != 1 and int(repetitions) != int(repeat_count):
                raise ValueError("conflicting repeat counts")
            repetitions = repeat_count
        if repeat_gap_us is not None and inter_frame_gap_ms is not None:
            if int(repeat_gap_us) != int(inter_frame_gap_ms) * 1000:
                raise ValueError("conflicting repeat gaps")
        if repeat_gap_us is None:
            if inter_frame_gap_ms is None:
                repeat_gap_us = 40_000
            else:
                repeat_gap_us = int(inter_frame_gap_ms) * 1000

        repetitions = int(repetitions)
        repeat_gap_us = int(repeat_gap_us)
        if repetitions < 1 or repetitions > 64:
            raise ValueError("repetitions must be 1 to 64")
        if repeat_gap_us < 0:
            raise ValueError("inter-frame gap cannot be negative")
        if repeat_period_us is not None:
            repeat_period_us = int(repeat_period_us)
            if repeat_period_us < 20_000 or repeat_period_us > 1_000_000:
                raise ValueError("repeat period must be 20000 to 1000000 us")

        frame = []
        frame_duration_us = 0
        for pair in pairs:
            if len(pair) != 2:
                raise ValueError("each pulse must contain a mark and a space")
            mark_us = int(pair[0])
            space_us = int(pair[1])
            if mark_us <= 0 or space_us <= 0:
                raise ValueError("pulse durations must be positive")
            frame.append((mark_us, space_us))
            frame_duration_us += mark_us + space_us
        if not frame:
            raise ValueError("cannot send an empty pulse frame")
        if repeat_period_us is not None and repeat_period_us <= frame_duration_us:
            raise ValueError("repeat period must exceed frame duration")

        timeout_ms = max(100, int(frame_duration_us / 1000) + 100)
        late_repeats = 0
        for index in range(repetitions):
            frame_started_us = _now_us()
            for mark_us, space_us in frame:
                self._put(mark_us, space_us)
            self._wait(timeout_ms)
            if index + 1 < repetitions:
                if repeat_period_us is not None:
                    elapsed_us = _elapsed_us(_now_us(), frame_started_us)
                    delay_us = repeat_period_us - elapsed_us
                    if delay_us <= 0:
                        late_repeats += 1
                    else:
                        _sleep_us(delay_us)
                elif repeat_gap_us:
                    _sleep_us(repeat_gap_us)

        self._last_send = {
            "frames": repetitions,
            "frame_duration_us": frame_duration_us,
            "repeat_period_us": repeat_period_us,
            "late_repeats": late_repeats,
        }
        return dict(self._last_send)

    def diagnostics(self):
        return dict(self._last_send)
