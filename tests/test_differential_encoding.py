from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace

from pico_pio_trace.encoding import encode_instruction
from pico_pio_trace.model import Instruction, PIOProgram

from reference_micropython_encoder import ReferencePIOASMEmit


JMP = {"always": 0, "not_x": 1, "x_dec": 2, "not_y": 3, "y_dec": 4, "x_not_y": 5, "pin": 6, "not_osre": 7}
IN = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 6, "osr": 7}
OUT = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6, "exec": 8}
MOV_DEST = {"pins": 0, "x": 1, "y": 2, "exec": 8, "pc": 5, "isr": 6, "osr": 7}
MOV_SRC = {"pins": 0, "x": 1, "y": 2, "null": 3, "status": 5, "isr": 6, "osr": 7}
SET = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}

ReferenceCall = Callable[[ReferencePIOASMEmit], None]


def _cases() -> Iterator[tuple[Instruction, ReferenceCall]]:
    yield Instruction("nop"), lambda e: e.nop()
    for condition, code in JMP.items():
        for target in range(32):
            yield Instruction("jmp", (condition, target)), lambda e, c=code, t=target: e.jmp(c, t)
    for polarity in (0, 1):
        for source, source_code in (("gpio", 0), ("pin", 6)):
            for index in range(32):
                yield Instruction("wait", (polarity, source, index, False)), lambda e, p=polarity, s=source_code, i=index: e.wait(p, s, i)
        for relative in (False, True):
            for index in range(8):
                encoded_index = index | (0x10 if relative else 0)
                yield Instruction("wait", (polarity, "irq", index, relative)), lambda e, p=polarity, i=encoded_index: e.wait(p, 1, i)
    for source, code in IN.items():
        for count in range(1, 33):
            yield Instruction("in", (source, count)), lambda e, s=code, c=count: e.in_(s, c)
    for destination, code in OUT.items():
        for count in range(1, 33):
            yield Instruction("out", (destination, count)), lambda e, d=code, c=count: e.out(d, c)
    for conditional in (False, True):
        for block in (False, True):
            value = (0x40 if conditional else 0) | (0 if block else 1)
            yield Instruction("push", (conditional, block)), lambda e, v=value: e.push(v)
            yield Instruction("pull", (conditional, block)), lambda e, v=value: e.pull(v)
    for destination, dest_code in MOV_DEST.items():
        for source, source_code in MOV_SRC.items():
            for operation, op_code in ((None, 0), ("invert", 0x08), ("reverse", 0x10)):
                source_spec = source if operation is None else (operation, source)
                yield Instruction("mov", (destination, source_spec)), lambda e, d=dest_code, s=source_code | op_code: e.mov(d, s)
    for action, modifier in (("set", 0), ("wait", 0x20), ("clear", 0x40)):
        for relative in (False, True):
            for index in range(8):
                encoded_index = index | (0x10 if relative else 0)
                yield Instruction("irq", (action, index, relative)), lambda e, m=modifier, i=encoded_index: e.irq(m, i)
    for destination, code in SET.items():
        for value in range(32):
            yield Instruction("set", (destination, value)), lambda e, d=code, v=value: e.set(d, v)


def _program(side_count: int = 0, optional: bool = False) -> PIOProgram:
    return PIOProgram(
        "reference",
        [Instruction("nop")],
        sideset_init=None if side_count == 0 else tuple(0 for _ in range(side_count)),
        sideset_optional=optional,
    )


def _reference_word(call: ReferenceCall, instruction: Instruction, program: PIOProgram) -> int:
    emitter = ReferencePIOASMEmit(
        encoded_sideset_count=program.encoded_sideset_count,
        sideset_opt=program.sideset_optional,
    )
    call(emitter)
    if instruction.side is not None:
        emitter.side(instruction.side)
    if instruction.delay:
        emitter.delay(instruction.delay)
    return emitter.result


def test_all_legal_instruction_fields_match_current_micropython_reference_encoder():
    program = _program()
    cases = list(_cases())
    assert len(cases) == 1196
    for instruction, call in cases:
        expected = _reference_word(call, instruction, program)
        actual = encode_instruction(instruction, program)
        assert actual == expected, instruction.display()


def test_delay_and_sideset_bitfields_match_reference_across_every_legal_pattern():
    representative = [
        next(item for item in _cases() if item[0].op == op)
        for op in ("nop", "jmp", "wait", "in", "out", "push", "pull", "mov", "irq", "set")
    ]
    checked = 0
    for side_count, optional in [(0, False), *((n, False) for n in range(1, 6)), *((n, True) for n in range(1, 5))]:
        program = _program(side_count, optional)
        side_values: list[int | None]
        if side_count == 0:
            side_values = [None]
        elif optional:
            side_values = [None, *range(1 << side_count)]
        else:
            side_values = list(range(1 << side_count))
        for delay in range(program.delay_max + 1):
            for side in side_values:
                for base, call in representative:
                    instruction = replace(base, delay=delay, side=side)
                    expected = _reference_word(call, instruction, program)
                    actual = encode_instruction(instruction, program)
                    assert actual == expected, instruction.display()
                    checked += 1
    # 2,710 modifier-bearing comparisons: every legal delay/side pattern for ten opcodes.
    assert checked == 2710


def test_representative_modifier_configuration_on_every_legal_instruction_core():
    cases = list(_cases())
    configurations = [
        _program(),
        _program(1, False),
        _program(2, False),
        _program(3, True),
        _program(4, True),
    ]
    for index, (base, call) in enumerate(cases):
        program = configurations[index % len(configurations)]
        side = None
        if program.sideset_count and (not program.sideset_optional or index % 2):
            side = index % (1 << program.sideset_count)
        instruction = replace(base, delay=index % (program.delay_max + 1), side=side)
        expected = _reference_word(call, instruction, program)
        actual = encode_instruction(instruction, program)
        assert actual == expected, instruction.display()
