from __future__ import annotations

from pathlib import Path

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.parser import parse_file
from pico_pio_trace.stimulus import load_stimulus

EXAMPLES = Path(__file__).parents[1] / "examples"


def test_blink_example_has_32_cycle_half_periods():
    config = parse_file(EXAMPLES / "blink.py").choose()
    trace = PIOEmulator(config).run(66)
    assert all(trace.records[i].pin_level(25) == 1 for i in range(32))
    assert all(trace.records[i].pin_level(25) == 0 for i in range(32, 64))
    assert trace.records[64].pin_level(25) == 1


def test_uart_example_emits_start_data_stop_pattern_for_0x55():
    config = parse_file(EXAMPLES / "uart_tx.py").choose()
    trace = PIOEmulator(config).run(90)
    # Sample at the start of each 8-cycle UART bit cell. Program setup takes one
    # PULL barrier cycle, then SET .side(0)[7] begins start bit at cycle 1.
    samples = [trace.records[1 + 8 * i].pin_level(0) for i in range(10)]
    assert samples == [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]


def test_fifo_loopback_example_receives_both_words():
    config = parse_file(EXAMPLES / "fifo_loopback.py").choose()
    trace = PIOEmulator(config).run(6)
    assert trace.records[2].rx_fifo == (0x12345678,)
    assert trace.records[4].rx_fifo == (0x12345678, 0xA5A5A5A5)


def test_wait_and_irq_example_with_stimulus():
    config = parse_file(EXAMPLES / "wait_and_irq.py").choose()
    events = load_stimulus(EXAMPLES / "wait_stimulus.json")
    trace = PIOEmulator(config).run(18, events)
    assert any(record.pin_level(3) == 1 for record in trace.records[6:12])
    assert trace.records[-1].pin_level(3) == 0
