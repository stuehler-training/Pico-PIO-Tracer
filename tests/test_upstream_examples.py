# SPDX-FileCopyrightText: 2013-2026 Damien P. George
# SPDX-License-Identifier: MIT
# Adapted from official MicroPython v1.28.0 RP2 examples for compatibility testing.

"""Compatibility checks adapted from MicroPython v1.28.0 official RP2 examples.

The upstream examples are MIT-licensed as part of MicroPython.  These fixtures
retain only the PIO declarations and the minimum StateMachine construction needed
for static parsing/emulation; they deliberately do not execute application code.
"""
from __future__ import annotations

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.parser import parse_source


OFFICIAL_STYLE_SOURCES = {
    "pio_1hz": """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def blink_1hz():
    irq(rel(0))
    set(pins, 1)
    set(x, 31) [5]
    label("delay_high")
    nop() [29]
    jmp(x_dec, "delay_high")
    nop()
    set(pins, 0)
    set(x, 31) [5]
    label("delay_low")
    nop() [29]
    jmp(x_dec, "delay_low")
sm = rp2.StateMachine(0, blink_1hz, freq=2000, set_base=Pin(25))
""",
    "pio_exec": """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def prog():
    pass
sm = rp2.StateMachine(0, prog, set_base=Pin(25))
sm.exec("set(pins, 1)")
sm.exec("set(pins, 0)")
""",
    "pio_pinchange": """
import rp2
from machine import Pin
@rp2.asm_pio()
def wait_pin_low():
    wrap_target()
    wait(0, pin, 0)
    irq(block, rel(0))
    wait(1, pin, 0)
    wrap()
pin16 = Pin(16)
sm0 = rp2.StateMachine(0, wait_pin_low, in_base=pin16)
""",
    "pio_pwm": """
from machine import Pin
from rp2 import PIO, StateMachine, asm_pio
@asm_pio(sideset_init=PIO.OUT_LOW)
def pwm_prog():
    pull(noblock) .side(0)
    mov(x, osr)
    mov(y, isr)
    label("pwmloop")
    jmp(x_not_y, "skip")
    nop() .side(1)
    label("skip")
    jmp(y_dec, "pwmloop")
sm = StateMachine(0, pwm_prog, freq=20_000_000, sideset_base=Pin(25))
sm.put(65535)
sm.exec("pull()")
sm.exec("mov(isr, osr)")
""",
    "pio_uart_rx": """
from machine import Pin
from rp2 import PIO, StateMachine, asm_pio
UART_BAUD = 9600
@asm_pio(autopush=True, push_thresh=8, in_shiftdir=PIO.SHIFT_RIGHT, fifo_join=PIO.JOIN_RX)
def uart_rx_mini():
    wait(0, pin, 0)
    set(x, 7) [10]
    label("bitloop")
    in_(pins, 1)
    jmp(x_dec, "bitloop") [6]
@asm_pio(in_shiftdir=PIO.SHIFT_RIGHT)
def uart_rx():
    label("start")
    wait(0, pin, 0)
    set(x, 7) [10]
    label("bitloop")
    in_(pins, 1)
    jmp(x_dec, "bitloop") [6]
    jmp(pin, "good_stop")
    irq(block, 4)
    wait(1, pin, 0)
    jmp("start")
    label("good_stop")
    push(block)
sm = StateMachine(0, uart_rx_mini, freq=8 * UART_BAUD, in_base=Pin(3), jmp_pin=Pin(3))
""",
    "pio_uart_tx": """
from machine import Pin
from rp2 import PIO, StateMachine, asm_pio
UART_BAUD = 115200
PIN_BASE = 10
@asm_pio(sideset_init=PIO.OUT_HIGH, out_init=PIO.OUT_HIGH, out_shiftdir=PIO.SHIFT_RIGHT)
def uart_tx():
    pull()
    set(x, 7) .side(0) [7]
    label("bitloop")
    out(pins, 1) [6]
    jmp(x_dec, "bitloop")
    nop() .side(1) [6]
sm = StateMachine(0, uart_tx, freq=8 * UART_BAUD, sideset_base=Pin(PIN_BASE), out_base=Pin(PIN_BASE))
sm.put(0x55)
""",
    "pio_ws2812": """
from machine import Pin
import rp2
@rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW, out_shiftdir=rp2.PIO.SHIFT_LEFT, autopull=True, pull_thresh=24)
def ws2812():
    T1 = 2
    T2 = 5
    T3 = 3
    wrap_target()
    label("bitloop")
    out(x, 1) .side(0) [T3 - 1]
    jmp(not_x, "do_zero") .side(1) [T1 - 1]
    jmp("bitloop") .side(1) [T2 - 1]
    label("do_zero")
    nop() .side(0) [T2 - 1]
    wrap()
sm = rp2.StateMachine(0, ws2812, freq=8_000_000, sideset_base=Pin(22))
sm.put(0x12_34_56 << 8)
""",
}


EXPECTED_PROGRAM_LENGTHS = {
    "pio_1hz": {"blink_1hz": 10},
    "pio_exec": {"prog": 0},
    "pio_pinchange": {"wait_pin_low": 3},
    "pio_pwm": {"pwm_prog": 6},
    "pio_uart_rx": {"uart_rx_mini": 4, "uart_rx": 9},
    "pio_uart_tx": {"uart_tx": 5},
    "pio_ws2812": {"ws2812": 4},
}


@pytest.mark.parametrize("name", sorted(OFFICIAL_STYLE_SOURCES))
def test_micropython_v1_28_example_pio_dialects_parse_and_encode(name: str):
    parsed = parse_source(OFFICIAL_STYLE_SOURCES[name], source_path=f"upstream:{name}.py")
    assert {program: len(value.instructions) for program, value in parsed.programs.items()} == EXPECTED_PROGRAM_LENGTHS[name]
    for program in parsed.programs.values():
        assert all(instruction.word is not None for instruction in program.instructions)
        assert all(0 <= int(instruction.word) <= 0xFFFF for instruction in program.instructions)


def test_micropython_v1_28_uart_tx_example_has_expected_ten_bit_cells():
    config = parse_source(OFFICIAL_STYLE_SOURCES["pio_uart_tx"]).choose()
    trace = PIOEmulator(config).run(90)
    samples = [trace.records[1 + 8 * bit].pin_level(10) for bit in range(10)]
    assert samples == [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]


def test_micropython_v1_28_ws2812_example_uses_optional_sideset_and_autopull():
    config = parse_source(OFFICIAL_STYLE_SOURCES["pio_ws2812"]).choose()
    assert config.program.sideset_optional is False
    assert config.program.autopull is True
    assert config.program.pull_thresh == 24
    trace = PIOEmulator(config).run(32)
    assert any(record.pin_level(22) == 1 for record in trace.records)
    assert any(record.pin_level(22) == 0 for record in trace.records)


def test_micropython_v1_28_uart_tx_example_infers_optional_sideset():
    program = parse_source(OFFICIAL_STYLE_SOURCES["pio_uart_tx"]).programs["uart_tx"]
    assert program.sideset_count == 1
    assert program.sideset_optional is True
    assert program.delay_max == 7
