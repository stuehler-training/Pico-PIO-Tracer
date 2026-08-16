# SPDX-FileCopyrightText: 2013-2026 Damien P. George
# SPDX-License-Identifier: MIT
# Adapted from MicroPython v1.28.0 examples/rp2/pio_uart_tx.py.

import time
from machine import Pin
from rp2 import PIO, StateMachine, asm_pio

UART_BAUD = 115200
PIO_FREQUENCY_CLOCK = 1000000 # (1 us)


@asm_pio(sideset_init=PIO.OUT_HIGH, out_init=PIO.OUT_HIGH, out_shiftdir=PIO.SHIFT_RIGHT)
def uart_tx():
    # Block with TX deasserted until data available
    pull()
    # Initialise bit counter, assert start bit for 8 cycles
    set(x, 7)  .side(0)       [7]
    # Shift out 8 data bits, 8 execution cycles per bit
    label("bitloop")
    out(pins, 1)              [6]
    jmp(x_dec, "bitloop")
    # Assert stop bit for 8 cycles total (incl 1 for pull())
    nop()      .side(1)       [6]


# CLOCK CYCLE 1 us
@asm_pio(set_init=rp2.PIO.OUT_HIGH, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def activate_clock():
    set(x, 15)
    wait(1, gpio, 15) 
    wait(0, gpio, 15) [4]
    label("loop")
    set(pins, 0) [5]
    set(pins, 1) [4]
    jmp(x_dec, "loop")
    set(pins, 1)
    irq(0)

# UART TX StateMachine
sm_tx = StateMachine(0, uart_tx, freq=8 * UART_BAUD, sideset_base=Pin(16), out_base=Pin(16))
sm_tx.active(1)

sm_clock = rp2.StateMachine(1, activate_clock, freq=PIO_FREQUENCY_CLOCK, set_base=Pin(14))
sm_clock.active(1)

# We can print characters from each UART by pushing them to the TX FIFO
def pio_uart_print(sm, s):
    for c in s:
        sm.put(ord(c))

# Print a different message from each UART
pio_uart_print(sm_tx, "secret\n")
