from __future__ import annotations

from dataclasses import replace
from typing import Any

from .model import Instruction, PIOProgram

JMP_CONDITIONS = {
    "always": 0,
    "not_x": 1,
    "x_dec": 2,
    "not_y": 3,
    "y_dec": 4,
    "x_not_y": 5,
    "pin": 6,
    "not_osre": 7,
}
JMP_CONDITIONS_REV = {value: key for key, value in JMP_CONDITIONS.items()}

GENERAL_CODES = {
    "pins": 0,
    "x": 1,
    "y": 2,
    "null": 3,
    "pindirs": 4,
    "pc": 5,
    "status": 5,
    "isr": 6,
    "osr": 7,
    "exec": 8,
}

WAIT_SOURCE_CODES = {"gpio": 0, "pin": 1, "irq": 2}
WAIT_SOURCE_CODES_REV = {value: key for key, value in WAIT_SOURCE_CODES.items()}

IN_SOURCE_CODES = {"pins": 0, "x": 1, "y": 2, "null": 3, "isr": 6, "osr": 7}
IN_SOURCE_CODES_REV = {value: key for key, value in IN_SOURCE_CODES.items()}

OUT_DEST_CODES = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6, "exec": 7}
OUT_DEST_CODES_REV = {value: key for key, value in OUT_DEST_CODES.items()}

MOV_DEST_CODES = {"pins": 0, "x": 1, "y": 2, "exec": 4, "pc": 5, "isr": 6, "osr": 7}
MOV_DEST_CODES_REV = {value: key for key, value in MOV_DEST_CODES.items()}
MOV_SOURCE_CODES = {"pins": 0, "x": 1, "y": 2, "null": 3, "status": 5, "isr": 6, "osr": 7}
MOV_SOURCE_CODES_REV = {value: key for key, value in MOV_SOURCE_CODES.items()}

SET_DEST_CODES = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}
SET_DEST_CODES_REV = {value: key for key, value in SET_DEST_CODES.items()}


class EncodingError(ValueError):
    pass


def encode_instruction(
    instruction: Instruction,
    program: PIOProgram,
    *,
    allow_missing_sideset: bool = False,
) -> int:
    """Encode one semantic instruction using MicroPython-compatible RP2040 rules.

    ``asm_pio`` program assembly requires every instruction to carry ``.side()``
    when side-set is mandatory.  ``StateMachine.exec()``/``asm_pio_encode()`` is
    deliberately looser: a standalone instruction with no side modifier simply
    leaves the encoded side-set field at zero.
    """
    op = instruction.op.lower()
    args = instruction.args

    if op == "nop":
        core = 0xA042
    elif op == "jmp":
        condition, target = args
        condition_code = _lookup(JMP_CONDITIONS, str(condition), "JMP condition")
        target_value = _target_value(target, program)
        core = 0x0000 | (condition_code << 5) | target_value
    elif op == "wait":
        polarity, source, index, *relative_arg = args
        polarity_value = int(polarity)
        if polarity_value not in {0, 1}:
            raise EncodingError(f"WAIT polarity must be 0 or 1, got {polarity}")
        source_code = _lookup(WAIT_SOURCE_CODES, str(source), "WAIT source")
        relative = bool(relative_arg[0]) if relative_arg else False
        index_value = int(index)
        if source_code == WAIT_SOURCE_CODES["irq"]:
            if not 0 <= index_value <= 7:
                raise EncodingError(f"WAIT IRQ index must be in 0..7, got {index_value}")
            if relative:
                index_value |= 0x10
        else:
            if relative:
                raise EncodingError("rel() is only valid for WAIT IRQ")
            if not 0 <= index_value <= 31:
                raise EncodingError(f"WAIT GPIO/PIN index must be in 0..31, got {index_value}")
        core = 0x2000 | (polarity_value << 7) | (source_code << 5) | index_value
    elif op == "in":
        source, count = args
        count_code = _count_code(count)
        core = 0x4000 | (_lookup(IN_SOURCE_CODES, str(source), "IN source") << 5) | count_code
    elif op == "out":
        destination, count = args
        count_code = _count_code(count)
        core = 0x6000 | (_lookup(OUT_DEST_CODES, str(destination), "OUT destination") << 5) | count_code
    elif op == "push":
        if_full, block = _push_pull_args(args, conditional_name="iffull")
        core = 0x8000 | (0x40 if if_full else 0) | (0x20 if block else 0)
    elif op == "pull":
        if_empty, block = _push_pull_args(args, conditional_name="ifempty")
        core = 0x8080 | (0x40 if if_empty else 0) | (0x20 if block else 0)
    elif op == "mov":
        destination, source = args
        dest_code = _lookup(MOV_DEST_CODES, str(destination), "MOV destination")
        operation = None
        source_name = source
        if isinstance(source, tuple):
            operation, source_name = source
        source_code = _lookup(MOV_SOURCE_CODES, str(source_name), "MOV source")
        if operation == "invert":
            source_code |= 0x08
        elif operation == "reverse":
            source_code |= 0x10
        elif operation not in (None, "none"):
            raise EncodingError(f"unknown MOV operation {operation!r}")
        core = 0xA000 | (dest_code << 5) | source_code
    elif op == "irq":
        action, index, *relative_arg = args
        relative = bool(relative_arg[0]) if relative_arg else False
        index_value = int(index)
        if not 0 <= index_value <= 7:
            raise EncodingError(f"IRQ index must be in 0..7, got {index_value}")
        if relative:
            index_value |= 0x10
        modifier = {"set": 0x00, "wait": 0x20, "clear": 0x40}.get(str(action))
        if modifier is None:
            raise EncodingError(f"unknown IRQ action {action!r}")
        core = 0xC000 | modifier | index_value
    elif op == "set":
        destination, value = args
        value = int(value)
        if not 0 <= value <= 31:
            raise EncodingError(f"SET data must be in 0..31, got {value}")
        core = 0xE000 | (_lookup(SET_DEST_CODES, str(destination), "SET destination") << 5) | value
    elif op == "word":
        if not args:
            raise EncodingError("word() requires a 16-bit value")
        core = int(args[0])
        if not 0 <= core <= 0xFFFF:
            raise EncodingError(f"word() value must be in 0..0xffff, got {core}")
        if len(args) > 1:
            core |= _target_value(args[1], program)
        # MicroPython ORs chained delay/side-set modifiers into a raw word.
        return _apply_delay_and_sideset(
            core, instruction, program, allow_missing_sideset=allow_missing_sideset
        )
    else:
        raise EncodingError(f"unsupported PIO instruction {instruction.op!r}")

    return _apply_delay_and_sideset(
        core, instruction, program, allow_missing_sideset=allow_missing_sideset
    )


def decode_instruction(word: int, program: PIOProgram, *, from_exec: bool = False) -> Instruction:
    word &= 0xFFFF
    delay, side = decode_delay_and_sideset(word, program)
    opcode = (word >> 13) & 0x7
    operand = word & 0xFF

    if opcode == 0:
        condition = JMP_CONDITIONS_REV[(operand >> 5) & 0x7]
        target = operand & 0x1F
        instruction = Instruction("jmp", (condition, target), delay, side)
    elif opcode == 1:
        polarity = (operand >> 7) & 1
        source_code = (operand >> 5) & 0x3
        source = WAIT_SOURCE_CODES_REV.get(source_code, f"reserved_{source_code}")
        index = operand & 0x1F
        relative = source == "irq" and bool(index & 0x10)
        if relative:
            index &= 0x7
        instruction = Instruction("wait", (polarity, source, index, relative), delay, side)
    elif opcode == 2:
        source_code = (operand >> 5) & 0x7
        source = IN_SOURCE_CODES_REV.get(source_code, f"reserved_{source_code}")
        count = operand & 0x1F or 32
        instruction = Instruction("in", (source, count), delay, side)
    elif opcode == 3:
        destination_code = (operand >> 5) & 0x7
        destination = OUT_DEST_CODES_REV.get(destination_code, f"reserved_{destination_code}")
        count = operand & 0x1F or 32
        instruction = Instruction("out", (destination, count), delay, side)
    elif opcode == 4:
        is_pull = bool(operand & 0x80)
        conditional = bool(operand & 0x40)
        block = bool(operand & 0x20)
        instruction = Instruction("pull" if is_pull else "push", (conditional, block), delay, side)
    elif opcode == 5:
        destination_code = (operand >> 5) & 0x7
        destination = MOV_DEST_CODES_REV.get(destination_code, f"reserved_{destination_code}")
        operation_code = operand & 0x18
        source_code = operand & 0x7
        source = MOV_SOURCE_CODES_REV.get(source_code, f"reserved_{source_code}")
        if operation_code == 0x08:
            source_spec: Any = ("invert", source)
        elif operation_code == 0x10:
            source_spec = ("reverse", source)
        elif operation_code == 0:
            source_spec = source
        else:
            source_spec = (f"reserved_op_{operation_code >> 3}", source)
        instruction = Instruction("mov", (destination, source_spec), delay, side)
    elif opcode == 6:
        clear = bool(operand & 0x40)
        wait = bool(operand & 0x20)
        action = "clear" if clear else "wait" if wait else "set"
        relative = bool(operand & 0x10)
        index = operand & 0x7
        instruction = Instruction("irq", (action, index, relative), delay, side)
    else:
        destination_code = (operand >> 5) & 0x7
        destination = SET_DEST_CODES_REV.get(destination_code, f"reserved_{destination_code}")
        value = operand & 0x1F
        instruction = Instruction("set", (destination, value), delay, side)

    instruction.word = word
    instruction.from_exec = from_exec
    return instruction


def decode_delay_and_sideset(word: int, program: PIOProgram) -> tuple[int, int | None]:
    field = (word >> 8) & 0x1F
    delay_bits = program.delay_bits
    delay_mask = (1 << delay_bits) - 1
    delay = field & delay_mask
    if program.sideset_count == 0:
        return delay, None

    side_mask = (1 << program.sideset_count) - 1
    side = (field >> delay_bits) & side_mask
    if program.sideset_optional:
        enabled = bool((field >> (delay_bits + program.sideset_count)) & 1)
        if not enabled:
            side = None
    return delay, side


def with_encoded_word(instruction: Instruction, program: PIOProgram) -> Instruction:
    return replace(instruction, word=encode_instruction(instruction, program))


def _apply_delay_and_sideset(
    core: int,
    instruction: Instruction,
    program: PIOProgram,
    *,
    allow_missing_sideset: bool = False,
) -> int:
    if instruction.delay < 0 or instruction.delay > program.delay_max:
        raise EncodingError(
            f"delay {instruction.delay} exceeds {program.delay_max} for {program.encoded_sideset_count} encoded side-set bits"
        )
    result = core | (int(instruction.delay) << 8)
    if program.sideset_count:
        if instruction.side is None:
            if not program.sideset_optional and not allow_missing_sideset:
                raise EncodingError("all instructions must specify .side() when side-set is non-optional")
        else:
            side = int(instruction.side)
            if not 0 <= side < (1 << program.sideset_count):
                raise EncodingError(f"side-set value {side} does not fit in {program.sideset_count} bits")
            result |= side << (8 + program.delay_bits)
            if program.sideset_optional:
                result |= 1 << 12
    elif instruction.side is not None:
        raise EncodingError("instruction uses .side() but the decorator has no sideset_init")
    return result & 0xFFFF


def _target_value(target: Any, program: PIOProgram) -> int:
    if isinstance(target, str):
        if target not in program.labels:
            raise EncodingError(f"unknown PIO label {target!r}")
        value = program.labels[target]
    else:
        value = int(target)
    if not 0 <= value <= 31:
        raise EncodingError(f"JMP target must be in 0..31, got {value}")
    return value


def _count_code(count: Any) -> int:
    count = int(count)
    if not 1 <= count <= 32:
        raise EncodingError(f"bit count must be in 1..32, got {count}")
    return count & 0x1F


def _lookup(mapping: dict[str, int], key: str, description: str) -> int:
    try:
        return mapping[key]
    except KeyError as exc:
        raise EncodingError(f"unknown {description}: {key!r}") from exc


def _push_pull_args(args: tuple[Any, ...], *, conditional_name: str) -> tuple[bool, bool]:
    if len(args) == 2 and all(isinstance(value, bool) for value in args):
        return bool(args[0]), bool(args[1])
    conditional = False
    block = True
    explicit_block = False
    for arg in args:
        text = str(arg)
        if text == conditional_name:
            conditional = True
        elif text == "block":
            block = True
            explicit_block = True
        elif text == "noblock":
            block = False
            explicit_block = True
        elif text in ("0", "None"):
            continue
        else:
            raise EncodingError(f"unknown PUSH/PULL modifier {arg!r}")
    if not explicit_block:
        block = True
    return conditional, block
