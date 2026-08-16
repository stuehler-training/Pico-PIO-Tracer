from __future__ import annotations

import random
from dataclasses import replace

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.model import MASK32, SHIFT_LEFT, SHIFT_RIGHT, Instruction, StateMachineConfig, StimulusEvent, u32
from pico_pio_trace.parser import parse_source


def _base_config() -> StateMachineConfig:
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm = rp2.StateMachine(0, p, freq=1_000_000)
"""
    )
    return parsed.choose()


def test_randomised_shift_and_bit_reverse_properties_55000_assertions():
    rng = random.Random(0x2040_2026)
    base = _base_config()
    right = PIOEmulator(replace(base, out_shiftdir_override=SHIFT_RIGHT, in_shiftdir_override=SHIFT_RIGHT))
    left = PIOEmulator(replace(base, out_shiftdir_override=SHIFT_LEFT, in_shiftdir_override=SHIFT_LEFT))

    for _ in range(5000):
        value = rng.getrandbits(32)
        count = rng.randint(1, 32)
        previous_count = rng.randint(0, 32)
        mask = MASK32 if count == 32 else (1 << count) - 1

        right.osr = value
        right.osr_count = previous_count
        right._execute_out(Instruction("out", ("x", count)))
        assert right.x == value & mask
        assert right.osr == (0 if count == 32 else value >> count)
        assert right.osr_count == min(32, previous_count + count)

        left.osr = value
        left.osr_count = previous_count
        left._execute_out(Instruction("out", ("x", count)))
        expected_out = value if count == 32 else (value >> (32 - count)) & mask
        assert left.x == expected_out
        assert left.osr == (0 if count == 32 else u32(value << count))
        assert left.osr_count == min(32, previous_count + count)

        initial_isr = rng.getrandbits(32)
        input_value = rng.getrandbits(32)
        right.isr = initial_isr
        right.isr_count = previous_count
        right.x = input_value
        right._execute_in(Instruction("in", ("x", count)))
        expected_right = (input_value & mask) if count == 32 else u32((initial_isr >> count) | ((input_value & mask) << (32 - count)))
        assert right.isr == expected_right
        assert right.isr_count == min(32, previous_count + count)

        left.isr = initial_isr
        left.isr_count = previous_count
        left.x = input_value
        left._execute_in(Instruction("in", ("x", count)))
        expected_left = (input_value & mask) if count == 32 else u32((initial_isr << count) | (input_value & mask))
        assert left.isr == expected_left
        assert left.isr_count == min(32, previous_count + count)

        expected_reverse = int(f"{value:032b}"[::-1], 2)
        assert PIOEmulator._reverse32(value) == expected_reverse


def test_all_relative_irq_mappings_match_rp2040_formula():
    for sm_id in range(8):
        emulator = PIOEmulator(replace(_base_config(), sm_id=sm_id))
        for index in range(8):
            expected = (index & 0x4) | ((index + (sm_id & 0x3)) & 0x3)
            assert emulator._resolve_irq_index(index, True) == expected
            assert emulator._resolve_irq_index(index, False) == index


def test_pin_mapping_wrap_round_trip_for_random_patterns():
    rng = random.Random(0xBEEF)
    emulator = PIOEmulator(_base_config())
    for _ in range(2000):
        base = rng.randrange(32)
        count = rng.randint(1, 32)
        value = rng.getrandbits(count)
        emulator.pins = 0
        emulator.pindirs = MASK32
        emulator._write_mapped_now("pins", base, count, value)
        assert emulator._read_mapped(base, count) == value & (MASK32 if count == 32 else (1 << count) - 1)


def test_hundred_word_autopull_autopush_fifo_stress_preserves_order_and_capacity():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT, in_shiftdir=rp2.PIO.SHIFT_LEFT,
             autopull=True, pull_thresh=32, autopush=True, push_thresh=32)
def loopback():
    wrap_target()
    out(x, 32)
    in_(x, 32)
    wrap()
sm = rp2.StateMachine(0, loopback, freq=1_000_000)
"""
    )
    words = [u32(index * 0x9E3779B1) for index in range(100)]
    config = replace(parsed.choose(), initial_tx=words)
    emulator = PIOEmulator(config)
    gets = [StimulusEvent(cycle, "rx_get") for cycle in range(320)]
    trace = emulator.run(320, gets)
    assert emulator.host_rx_values[:100] == words
    assert len(emulator.host_rx_values) == 100
    assert all(record.tx_level <= config.tx_capacity for record in trace.records)
    assert all(record.rx_level <= config.rx_capacity for record in trace.records)
