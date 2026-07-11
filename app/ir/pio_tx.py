# SPDX-FileCopyrightText: 2025 Christopher Parrott for Pimoroni Ltd
# SPDX-License-Identifier: MIT
"""PIO program for carrier-modulated IR transmission.

Derived from the MIT-licensed pulse sender in badger/home/ir-beacon.
"""

import rp2

CLOCKS_PER_CYCLE = const(2)


@rp2.asm_pio(
    sideset_init=rp2.PIO.OUT_LOW,
    autopull=True,
    pull_thresh=32,
    fifo_join=rp2.PIO.JOIN_TX,
)
def pulse_sender():
    out(y, 16).delay(1)
    label("high_count_check")
    nop().side(1)
    jmp(y_dec, "high_count_check").side(0)

    out(x, 16).side(1)
    nop().side(0)
    label("low_count_check")
    nop()
    jmp(x_dec, "low_count_check")
