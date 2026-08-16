from __future__ import annotations

from dataclasses import replace

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.model import StimulusEvent
from pico_pio_trace.parser import parse_source


def _config(instruction: str):
    return parse_source(
        f"""
import rp2
@rp2.asm_pio()
def p():
    {instruction}
sm = rp2.StateMachine(0, p, freq=1_000_000)
"""
    ).choose()


def test_delay_and_wrap_cycle_cadence(run_source):
    _emulator, trace = run_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    wrap_target()
    set(pins, 1)[2]
    set(pins, 0)[1]
    wrap()
sm=rp2.StateMachine(0,p,freq=1000,set_base=Pin(0))
""",
        8,
    )
    assert [record.phase for record in trace.records] == ["execute", "delay", "delay", "execute", "delay", "execute", "delay", "delay"]
    assert [record.pin_level(0) for record in trace.records] == [1, 1, 1, 0, 0, 1, 1, 1]
    assert [record.pc for record in trace.records[:6]] == [0, 1, 1, 1, 0, 0]


def test_sideset_precedes_overlapping_set_write(run_source):
    _emulator, trace = run_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, sideset_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 0).side(1)
sm=rp2.StateMachine(0,p,freq=1_000_000,set_base=Pin(4),sideset_base=Pin(4))
""",
        1,
    )
    assert trace.records[0].pin_level(4) == 1
    assert any("SIDESET" in event for event in trace.records[0].events)


def test_sideset_takes_effect_while_pull_stalls(run_source):
    _emulator, trace = run_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(sideset_init=rp2.PIO.OUT_LOW)
def p():
    pull().side(1)
sm=rp2.StateMachine(0,p,freq=1_000_000,sideset_base=Pin(3))
""",
        3,
    )
    assert all(record.stalled for record in trace.records)
    assert all(record.pin_level(3) == 1 for record in trace.records)


def test_jmp_x_dec_zero_decrements_but_does_not_branch(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 0)
    jmp(x_dec, 'taken')
    set(y, 7)
    label('taken')
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[1].x == 0xFFFFFFFF
    assert trace.records[2].y == 7


def test_jmp_x_dec_nonzero_branches_on_initial_value(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 1)
    jmp(x_dec, 'taken')
    set(y, 7)
    label('taken')
    set(y, 3)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[1].x == 0
    assert trace.records[2].pc == 3
    assert trace.records[2].y == 3


def test_jmp_not_osre_uses_pull_threshold(run_source):
    emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio(pull_thresh=8)
def p():
    jmp(not_osre, 'has_data')
    set(y, 1)
    label('has_data')
    set(y, 2)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.exec('mov(osr, null)')
""",
        2,
    )
    assert trace.initial_state["osr_count"] == 0
    assert trace.records[1].pc == 2
    assert trace.records[1].y == 2


def test_wait_pin_stalls_then_completes_with_timed_stimulus():
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    wait(1, pin, 0)
    set(pins, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000,in_base=Pin(2),set_base=Pin(3))
"""
    )
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(5, [StimulusEvent(3, "pin", 1, pin=2)])
    assert [record.stalled for record in trace.records[:3]] == [True, True, True]
    assert trace.records[3].stalled is False
    assert trace.records[4].pin_level(3) == 1


def test_wait_one_irq_clears_flag_on_success():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    wait(1, irq, 2)
    set(x, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(2, [StimulusEvent(0, "irq_set", index=2)])
    assert trace.records[0].irq_flags == 0
    assert trace.records[1].x == 1


def test_irq_wait_sets_then_waits_until_cleared():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    irq(block, 1)
    set(x, 9)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(5, [StimulusEvent(3, "irq_clear", index=1)])
    assert trace.records[0].stalled and trace.records[0].irq_flags & 2
    assert trace.records[1].stalled and trace.records[2].stalled
    assert not trace.records[3].stalled
    assert trace.records[4].x == 9


def test_relative_irq_preserves_bit_two_and_adds_sm_low_bits():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    irq(rel(5))
sm=rp2.StateMachine(2,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(1)
    # index 5 (100+01) + SM2 on low two bits => 4+3 = IRQ7
    assert trace.records[0].irq_flags == 1 << 7


def test_in_shift_right_uart_order(run_source):
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    in_(pins, 1)
    in_(pins, 1)
    in_(pins, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000,in_base=Pin(0))
"""
    )
    trace = PIOEmulator(parsed.choose()).run(
        3,
        [
            StimulusEvent(0, "pin", 1, pin=0),
            StimulusEvent(1, "pin", 0, pin=0),
            StimulusEvent(2, "pin", 1, pin=0),
        ],
    )
    assert trace.records[-1].isr == 0xA0000000
    assert trace.records[-1].isr_count == 3


def test_in_shift_left(run_source):
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    in_(pins, 1)
    in_(pins, 1)
    in_(pins, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000,in_base=Pin(0))
"""
    )
    trace = PIOEmulator(parsed.choose()).run(
        3,
        [StimulusEvent(0, "pin", 1, pin=0), StimulusEvent(1, "pin", 0, pin=0), StimulusEvent(2, "pin", 1, pin=0)],
    )
    assert trace.records[-1].isr == 0b101


def test_out_shift_right_and_left_to_x():
    right = parse_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    pull()
    out(x, 8)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0xA1B2C3D4)
"""
    )
    trace = PIOEmulator(right.choose()).run(2)
    assert trace.records[1].x == 0xD4
    assert trace.records[1].osr == 0x00A1B2C3

    left = parse_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    pull()
    out(x, 8)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0xA1B2C3D4)
"""
    )
    trace = PIOEmulator(left.choose()).run(2)
    assert trace.records[1].x == 0xA1
    assert trace.records[1].osr == 0xB2C3D400


def test_out_isr_sets_shift_counter_to_bit_count(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    pull()
    out(isr, 7)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0x7f)
""",
        2,
    )
    assert trace.records[1].isr == 0x7F
    assert trace.records[1].isr_count == 7


def test_mov_to_isr_and_osr_reset_shift_counters(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 7)
    mov(isr, x)
    mov(osr, isr)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[1].isr == 7 and trace.records[1].isr_count == 0
    assert trace.records[2].osr == 7 and trace.records[2].osr_count == 0


def test_mov_invert_and_reverse(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 1)
    mov(y, invert(x))
    mov(isr, reverse(x))
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[1].y == 0xFFFFFFFE
    assert trace.records[2].isr == 0x80000000


def test_mov_pc_unconditional_jump(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 3)
    mov(pc, x)
    set(y, 1)
    set(y, 9)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[2].pc == 3
    assert trace.records[2].y == 9


def test_out_pc_unconditional_jump(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    pull()
    out(pc, 5)
    set(y, 1)
    set(y, 2)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(3)
""",
        3,
    )
    assert trace.records[2].pc == 3
    assert trace.records[2].y == 2


def test_mov_exec_executee_does_not_advance_pc(run_source):
    # 0xe027 is SET X, 7. Parent MOV advances to PC2; executee runs while PC stays 2.
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    pull()
    mov(exec, osr)
    set(y, 4)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0xe027)
""",
        4,
    )
    assert trace.records[2].instruction.startswith("EXEC:")
    assert trace.records[2].x == 7
    assert trace.records[2].pc == 2
    assert trace.records[3].pc == 2 and trace.records[3].y == 4


def test_set_pindirs_and_pin_mapping_wrap(run_source):
    _emulator, trace = run_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=(rp2.PIO.IN_LOW, rp2.PIO.IN_LOW, rp2.PIO.IN_LOW))
def p():
    set(pindirs, 0b101)
    set(pins, 0b111)
sm=rp2.StateMachine(0,p,freq=1_000_000,set_base=Pin(31))
""",
        2,
    )
    dirs = trace.records[0].pindirs
    assert dirs & (1 << 31)
    assert not dirs & 1
    assert dirs & 2
    assert trace.records[1].pins & (1 << 31)
    assert trace.records[1].pins & 1
    assert trace.records[1].pins & 2


def test_requested_frequency_can_be_quantized_like_micropython():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm=rp2.StateMachine(0,p,freq=3_000_000)
"""
    )
    base = parsed.choose()
    config = replace(base, system_clock_hz=125_000_000, actual_freq_hz=None)
    expected_div = int(125_000_000 * 256 // 3_000_000)
    assert config.actual_freq_hz == pytest.approx(125_000_000 * 256 / expected_div)


def test_system_clock_frequency_quantisation_matches_micropython_floor_and_range_checks():
    base = _config("nop()")
    config = replace(base, requested_freq_hz=2_000_000, actual_freq_hz=None, system_clock_hz=125_000_000)
    divider_256 = int(125_000_000 * 256 // 2_000_000)
    assert config.actual_freq_hz == 125_000_000 * 256 / divider_256
    with pytest.raises(ValueError, match="outside the RP2040 PIO divider range"):
        replace(base, requested_freq_hz=126_000_000, actual_freq_hz=None, system_clock_hz=125_000_000)
    with pytest.raises(ValueError, match="outside the RP2040 PIO divider range"):
        replace(base, requested_freq_hz=1_000, actual_freq_hz=None, system_clock_hz=125_000_000)


def test_zero_frequency_uses_maximum_rp2040_divider():
    base = _config("nop()")
    config = replace(base, requested_freq_hz=0, actual_freq_hz=None, system_clock_hz=125_000_000)
    assert config.actual_freq_hz == 125_000_000 / 65536


def test_fractional_divider_uses_average_period_and_emits_jitter_warning():
    config = replace(
        _config("nop()"),
        requested_freq_hz=3_000_000,
        actual_freq_hz=None,
        system_clock_hz=125_000_000,
        warnings=[],
    )
    divider_256 = int(125_000_000 * 256 // 3_000_000)
    assert divider_256 & 0xFF
    assert config.actual_freq_hz == pytest.approx(125_000_000 * 256 / divider_256)
    assert any("average cycle period" in warning for warning in config.warnings)


def test_mov_osr_with_autopull_emits_deterministic_race_warning():
    source = """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=8)
def p():
    mov(x, osr)
    mov(osr, y)
sm = rp2.StateMachine(0, p, freq=1_000_000)
sm.put(0x11223344)
"""
    config = parse_source(source).choose()
    trace = PIOEmulator(config).run(2)
    warnings = "\n".join(trace.warnings)
    assert "MOV from OSR" in warnings
    assert "MOV to OSR" in warnings


def test_trace_records_distinguish_instruction_pc_from_end_of_cycle_state_and_map_delay_source():
    source = """
import rp2
@rp2.asm_pio()
def p():
    set(x, 1)[2]
    set(y, 2)
sm = rp2.StateMachine(0, p, freq=1_000_000)
"""
    config = parse_source(source).choose()
    trace = PIOEmulator(config).run(5)

    execute = trace.records[0]
    assert execute.instruction_pc == 0
    assert execute.pc == 0
    assert execute.state_pc == 1
    assert execute.delay_remaining == 2
    assert execute.source_line == 5

    first_delay = trace.records[1]
    second_delay = trace.records[2]
    assert first_delay.phase == second_delay.phase == "delay"
    assert first_delay.instruction_pc == second_delay.instruction_pc == 0
    assert first_delay.state_pc == second_delay.state_pc == 1
    assert first_delay.source_line == second_delay.source_line == 5
    assert [first_delay.delay_remaining, second_delay.delay_remaining] == [1, 0]

    next_instruction = trace.records[3]
    assert next_instruction.instruction_pc == 1
    assert next_instruction.state_pc == 0  # implicit wrap after the second instruction


def test_debugger_state_pc_tracks_taken_jump_and_stalled_wait_without_lookahead():
    source = """
import rp2
@rp2.asm_pio()
def p():
    set(x, 1)
    jmp(x_dec, "target")
    set(y, 31)
    label("target")
    wait(1, gpio, 5)
    set(y, 7)
sm = rp2.StateMachine(0, p, freq=1_000_000)
"""
    config = parse_source(source).choose()
    trace = PIOEmulator(config).run(5)

    # SET executes at PC0 and advances normally.
    assert (trace.records[0].instruction_pc, trace.records[0].state_pc) == (0, 1)

    # X was non-zero before post-decrement, so the branch is taken directly to
    # the WAIT at PC3. The skipped SET at PC2 must never appear as debugger PC.
    jump = trace.records[1]
    assert jump.instruction_pc == 1
    assert jump.state_pc == 3
    assert jump.x == 0

    # A stalled WAIT repeatedly executes at its own PC and leaves architectural
    # PC there. This cannot be inferred reliably from the following trace row,
    # so it is recorded explicitly in every cycle.
    for record in trace.records[2:]:
        assert record.instruction_pc == 3
        assert record.state_pc == 3
        assert record.stalled is True
        assert record.source_line == 9
