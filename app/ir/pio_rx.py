# SPDX-FileCopyrightText: 2025 Christopher Parrott for Pimoroni Ltd
# SPDX-License-Identifier: MIT
"""PIO program for reading demodulated IR mark/space timings.

Derived from the MIT-licensed pulse receiver in badger/home/ir-beacon.
"""

import rp2

FREQUENCY = const(2_000_000)
BURST_BITS = const(14)
IDLE_BITS = const(13)
BURST_COUNT_TIMEOUT = const((2 ** BURST_BITS) - 1)
IDLE_COUNT_TIMEOUT = const((2 ** IDLE_BITS) - 1)
TIMEOUT_REACHED = const(0xFFFFFFFF)


def count_to_burst_us(count):
    return int(BURST_COUNT_TIMEOUT - (count - 5)) * 2 * 1_000_000 / FREQUENCY


def count_to_idle_us(count):
    # When the PIO idle counter expires, ``jmp(y_dec, ...)`` leaves Y
    # underflowed. Only its low 16 bits are packed into the FIFO, producing
    # 0xffff here. Clamp that terminal value to the counter's final valid
    # sample so a learned frame ends with an ~8 ms positive quiet period
    # instead of a negative duration.
    if count > IDLE_COUNT_TIMEOUT:
        count = 5
    else:
        count = max(5, count)
    return int(IDLE_COUNT_TIMEOUT - (count - 5)) * 2 * 1_000_000 / FREQUENCY


@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_LEFT, fifo_join=rp2.PIO.JOIN_RX)
def pulse_reader():
    wait(1, pin, 0)
    wait(0, pin, 0)

    nop().delay(5)
    label("low_setup")
    mov(osr, invert(null))
    out(x, BURST_BITS).delay(1)

    label("while_low")
    jmp(pin, "high_setup")
    jmp(x_dec, "while_low")
    jmp("emit_timeout")

    label("high_setup")
    nop().delay(5)
    mov(osr, invert(null))
    out(y, IDLE_BITS).delay(1)

    label("while_high")
    jmp(pin, "still_high")
    in_(x, 16)
    in_(y, 16)
    push()
    irq(rel(0)).delay(1)
    jmp("low_setup")

    label("still_high")
    jmp(y_dec, "while_high")

    # Preserve the final mark and the receiver's terminal idle. The upstream
    # beacon reader only emits a timeout marker here because its NEC decoder
    # does not need the trailing mark. A learning remote must retain it so a
    # raw replay reproduces the complete frame.
    in_(x, 16)
    in_(y, 16)
    push()
    irq(rel(0)).delay(1)

    label("emit_timeout")
    mov(isr, invert(null))
    push()
    irq(rel(0))
