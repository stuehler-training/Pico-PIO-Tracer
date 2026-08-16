from __future__ import annotations

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.encoding import decode_instruction
from pico_pio_trace.parser import PIOParseError, parse_source


def _program(body: str, decorator: str = ""):
    source = f"""
import rp2
@rp2.asm_pio({decorator})
def p():
{body}
"""
    return parse_source(source).programs["p"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("    nop()", 0xA042),
        ("    set(pins, 1)", 0xE001),
        ("    jmp('there')\n    label('there')\n    nop()", 0x0001),
        ("    jmp(x_dec, 'there')\n    nop()\n    label('there')\n    nop()", 0x0042),
        ("    wait(1, pin, 3)", 0x20A3),
        ("    in_(pins, 32)", 0x4000),
        ("    out(x, 8)", 0x6028),
        ("    push()", 0x8020),
        ("    push(iffull, block)", 0x8060),
        ("    push(noblock)", 0x8000),
        ("    pull()", 0x80A0),
        ("    mov(x, invert(y))", 0xA02A),
        ("    irq(clear, rel(2))", 0xC052),
        ("    set(y, 31)", 0xE05F),
    ],
)
def test_instruction_encodings_match_micropython_bitfields(body, expected):
    program = _program(body)
    assert program.instructions[0].word == expected


def test_non_optional_sideset_and_delay_encoding():
    program = _program("    set(pins, 1).side(1)[3]", "sideset_init=rp2.PIO.OUT_LOW")
    assert program.sideset_optional is False
    assert program.delay_max == 15
    assert program.instructions[0].word == 0xF301


def test_optional_sideset_and_delay_encoding():
    program = _program(
        "    set(pins, 1).side(1)[3]\n    nop()[2]",
        "sideset_init=rp2.PIO.OUT_LOW",
    )
    assert program.sideset_optional is True
    assert program.delay_max == 7
    assert program.instructions[0].word == 0xFB01
    assert program.instructions[1].word == 0xA242


def test_static_constants_arithmetic_and_loop_expand_safely():
    source = """
import rp2
COUNT = 2 + 1
DELAY = (1 << 2) - 1
@rp2.asm_pio()
def p():
    for i in range(COUNT):
        set(x, i)[DELAY]
"""
    program = parse_source(source).programs["p"]
    assert [instruction.args for instruction in program.instructions] == [("x", 0), ("x", 1), ("x", 2)]
    assert all(instruction.delay == 3 for instruction in program.instructions)


def test_explicit_and_implicit_wrap():
    explicit = _program("    nop()\n    wrap_target()\n    nop()\n    wrap()\n    nop()")
    assert explicit.wrap_target == 1
    assert explicit.wrap_top == 1
    implicit = _program("    nop()\n    nop()")
    assert implicit.wrap_target == 0
    assert implicit.wrap_top == 1


def test_raw_word_with_label_is_decoded_for_emulation():
    program = _program("    word(0x0000, 'target')\n    label('target')\n    set(x, 3)")
    first = program.instructions[0]
    assert first.word == 0x0001
    assert first.op == "jmp"
    assert first.args == ("always", 1)


def test_decoder_round_trip_for_representative_words():
    program = _program("    nop()")
    for word in [0x0003, 0x20A2, 0x4008, 0x60E1, 0x80E0, 0xA0C7, 0xC051, 0xE09F]:
        decoded = decode_instruction(word, program)
        assert decoded.word == word


def test_duplicate_label_rejected():
    with pytest.raises(PIOParseError, match="duplicate"):
        _program("    label('x')\n    nop()\n    label('x')\n    nop()")


def test_unknown_call_inside_program_rejected_without_execution():
    with pytest.raises(PIOParseError, match="unsupported PIO instruction"):
        _program("    __import__('os').system('echo should-not-run')")


def test_state_machine_configuration_and_pin_initialisers():
    source = """
import rp2
from machine import Pin
@rp2.asm_pio(out_init=(rp2.PIO.OUT_LOW, rp2.PIO.OUT_HIGH), set_init=rp2.PIO.OUT_LOW,
             sideset_init=rp2.PIO.OUT_HIGH, out_shiftdir=rp2.PIO.SHIFT_RIGHT,
             autopull=True, pull_thresh=8, fifo_join=rp2.PIO.JOIN_TX)
def p():
    out(pins, 2).side(1)
sm = rp2.StateMachine(3, p, freq=2_000_000, in_base=Pin(4), out_base=Pin(6),
                      set_base=Pin(9), sideset_base=Pin(10), jmp_pin=Pin(11))
sm.put(0x12, shift=8)
sm.exec("set(x, 7)")
sm.active(1)
"""
    parsed = parse_source(source)
    config = parsed.machines[0]
    assert config.sm_id == 3
    assert config.actual_freq_hz == 2_000_000
    assert (config.in_base, config.out_base, config.set_base, config.sideset_base, config.jmp_pin) == (4, 6, 9, 10, 11)
    assert config.out_count == 2
    assert config.tx_capacity == 8 and config.rx_capacity == 0
    assert config.initial_tx == [0x1200]
    assert config.initial_exec == [0xE027]


def test_state_machine_shift_overrides_are_parsed():
    source = """
from rp2 import asm_pio, PIO, StateMachine
@asm_pio(in_shiftdir=PIO.SHIFT_LEFT, out_shiftdir=PIO.SHIFT_LEFT)
def p():
    nop()
sm = StateMachine(0, p, freq=1_000_000, in_shiftdir=PIO.SHIFT_RIGHT,
                  out_shiftdir=PIO.SHIFT_RIGHT, push_thresh=4, pull_thresh=12)
"""
    config = parse_source(source).machines[0]
    assert config.in_shiftdir == 1
    assert config.out_shiftdir == 1
    assert config.push_thresh == 4
    assert config.pull_thresh == 12


def test_put_after_active_is_kept_with_timing_warning():
    source = """
import rp2
@rp2.asm_pio()
def p():
    pull()
sm = rp2.StateMachine(0, p, freq=1_000_000)
sm.active(1)
sm.put(0x55)
"""
    config = parse_source(source).machines[0]
    assert config.initial_tx == [0x55]
    assert any("cycle-zero" in warning for warning in config.warnings)


def test_no_machine_gets_synthetic_configuration():
    parsed = parse_source("""
import rp2
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 1)
""")
    config = parsed.choose()
    assert config.actual_freq_hz == 1_000_000
    assert config.set_base == 0
    assert any("synthetic" in warning for warning in config.warnings)

@pytest.mark.parametrize(
    ("decorator", "message"),
    [
        ("out_init=(0,)*33", "out_init configures 33"),
        ("set_init=(0,)*6", "set_init configures 6"),
        ("sideset_init=(0,)*6", "sideset_init configures 6"),
        ("out_init=(4,)", "entries must be"),
        ("in_shiftdir=9", "in_shiftdir"),
        ("out_shiftdir=-1", "out_shiftdir"),
        ("fifo_join=7", "fifo_join"),
    ],
)
def test_invalid_program_configuration_is_rejected(decorator, message):
    with pytest.raises(ValueError, match=message):
        _program("    nop()", decorator)


def test_optional_sideset_cannot_use_five_data_pins():
    with pytest.raises(ValueError, match="at most four"):
        _program("    nop().side(0)\n    nop()", "sideset_init=(0,0,0,0,0)")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("    wait(2, gpio, 0)", "polarity"),
        ("    wait(1, gpio, 32)", "GPIO/PIN index"),
        ("    wait(1, irq, 8)", "IRQ index"),
        ("    wait(1, gpio, rel(2))", "only valid for WAIT IRQ"),
        ("    irq(8)", "IRQ index"),
        ("    push(x)", "invalid push modifier"),
        ("    pull(block, noblock)", "both block and noblock"),
        ("    irq(iffull, 0)", "invalid irq modifier"),
        ("    word(0x10000)", "word.*0xffff"),
    ],
)
def test_invalid_instruction_fields_are_rejected(body, message):
    with pytest.raises((PIOParseError, ValueError), match=message):
        _program(body)


def test_rp2040_state_machine_id_range_is_checked():
    source = """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm = rp2.StateMachine(8, p, freq=1_000_000)
"""
    with pytest.raises(ValueError, match="0..7"):
        parse_source(source)


def test_delay_method_syntax_matches_bracket_syntax_and_chains_with_sideset():
    method = _program("    nop().side(1).delay(3)", "sideset_init=rp2.PIO.OUT_LOW")
    bracket = _program("    nop().side(1)[3]", "sideset_init=rp2.PIO.OUT_LOW")
    reverse_order = _program("    nop().delay(3).side(1)", "sideset_init=rp2.PIO.OUT_LOW")
    assert method.instructions[0].word == bracket.instructions[0].word == reverse_order.instructions[0].word


def test_state_machine_constructor_then_init_is_resolved_statically():
    source = """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 1)
sm = rp2.StateMachine(2)
sm.init(p, freq=2_500_000, set_base=Pin(7))
"""
    parsed = parse_source(source)
    assert len(parsed.machines) == 1
    config = parsed.machines[0]
    assert config.program.name == "p"
    assert config.sm_id == 2
    assert config.actual_freq_hz == 2_500_000
    assert config.set_base == 7


def test_state_machine_constructor_and_init_accept_positional_frequency():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm0 = rp2.StateMachine(0, p, 2_000_000)
sm1 = rp2.StateMachine(1)
sm1.init(p, 3_000_000)
"""
    )
    assert [machine.requested_freq_hz for machine in parsed.machines] == [2_000_000, 3_000_000]


def test_explicit_default_frequency_minus_one_uses_assumed_rp2040_clock():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm = rp2.StateMachine(0, p, -1)
"""
    )
    config = parsed.choose()
    assert config.actual_freq_hz == 125_000_000
    assert any("freq=-1" in warning for warning in config.warnings)



def test_pin_assignments_ignore_mode_pull_and_value_for_state_machine_bases():
    parsed = parse_source(
        """
from machine import Pin
import rp2

controller_pin = Pin(17, Pin.OUT, Pin.PULL_DOWN)
debug_pin = Pin(14, Pin.OUT, Pin.PULL_DOWN, value=0)
input_pin = Pin(15, Pin.IN, Pin.PULL_UP)

@rp2.asm_pio(
    set_init=rp2.PIO.OUT_HIGH,
    out_init=rp2.PIO.OUT_LOW,
    sideset_init=rp2.PIO.OUT_LOW,
)
def p():
    out(pins, 1).side(1)

sm = rp2.StateMachine(
    2,
    p,
    freq=100_000_000,
    in_base=input_pin,
    set_base=controller_pin,
    out_base=controller_pin,
    sideset_base=debug_pin,
)
sm.put(0xffff)
sm.active(1)
"""
    )
    assert parsed.warnings == []
    assert len(parsed.machines) == 1
    config = parsed.machines[0]
    assert config.program.name == "p"
    assert config.sm_id == 2
    assert config.requested_freq_hz == 100_000_000
    assert config.in_base == 15
    assert config.set_base == 17
    assert config.out_base == 17
    assert config.sideset_base == 14
    assert config.initial_tx == [0xffff]


def test_pin_keyword_id_is_supported_without_evaluating_pad_configuration():
    parsed = parse_source(
        """
from machine import Pin
import rp2
base = Pin(id=9, mode=Pin.OUT, pull=Pin.PULL_DOWN, value=1)
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 1)
sm = rp2.StateMachine(0, p, set_base=base)
"""
    )
    assert parsed.choose().set_base == 9



def test_runtime_while_state_machine_calls_are_reported_but_not_executed():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    pull(noblock)
sm = rp2.StateMachine(0, p)
sm.put(1)
sm.active(1)
while True:
    if sm.tx_fifo() == 0:
        sm.put(dynamic_value)
"""
    )
    config = parsed.choose()
    assert config.initial_tx == [1]
    assert any(
        "runtime Python control flow" in warning
        and "sm.put()" in warning
        and "sm.tx_fifo()" in warning
        and "stimulus JSON" in warning
        for warning in parsed.warnings
    )


def test_statically_false_runtime_while_does_not_warn():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm = rp2.StateMachine(0, p)
while False:
    sm.put(123)
"""
    )
    assert not any("runtime Python control flow" in warning for warning in parsed.warnings)


def test_complete_source_and_program_range_are_embedded_for_the_html_debugger():
    source = """import rp2

@rp2.asm_pio()
def highlighted_program():
    set(x, 1)
    nop()

sm = rp2.StateMachine(0, highlighted_program, freq=1_000_000)
"""
    parsed = parse_source(source, source_path="debug_source.py")
    program = parsed.programs["highlighted_program"]
    assert program.source_text == source
    assert program.source_path == "debug_source.py"
    assert program.source_line == 4
    assert program.source_end_line == 6
    model = PIOEmulator(parsed.choose()).run(2).simulation_dict()
    assert model["program"]["source_text"] == source
    assert model["program"]["source_end_line"] == 6
