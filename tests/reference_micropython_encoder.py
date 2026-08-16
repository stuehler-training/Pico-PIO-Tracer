# SPDX-FileCopyrightText: 2020-2021 Damien P. George
# SPDX-License-Identifier: MIT
# Adapted for differential testing in Pico PIO Trace.

"""Independent reference encoder extracted from MicroPython's MIT-licensed rp2.py.

Pinned upstream source: MicroPython v1.28.0
https://github.com/micropython/micropython/blob/v1.28.0/ports/rp2/modules/rp2.py

Only the PIOASMEmit instruction-word logic needed for differential tests is
included.  It deliberately does not import the emulator implementation.
"""

from __future__ import annotations

from array import array


class ReferencePIOASMError(Exception):
    pass


class ReferencePIOASMEmit:
    def __init__(self, *, encoded_sideset_count: int = 0, sideset_opt: bool = False) -> None:
        self.labels: dict[object, int] = {value: value for value in range(32)}
        self.prog = array("H")
        self.sideset_count = encoded_sideset_count
        self.sideset_opt = bool(sideset_opt)
        self.delay_max = 31 >> encoded_sideset_count
        self.num_instr = 0
        self.num_sideset = 0

    def delay(self, delay: int) -> "ReferencePIOASMEmit":
        if delay > self.delay_max:
            raise ReferencePIOASMError("delay too large")
        self.prog[-1] |= delay << 8
        return self

    def side(self, value: int) -> "ReferencePIOASMEmit":
        self.num_sideset += 1
        if self.sideset_count == 0:
            raise ReferencePIOASMError("no sideset")
        if value >= (1 << self.sideset_count):
            raise ReferencePIOASMError("sideset too large")
        set_bit = 13 - self.sideset_count
        self.prog[-1] |= int(self.sideset_opt) << 12 | value << set_bit
        return self

    def word(self, instr: int, label: object | None = None) -> "ReferencePIOASMEmit":
        self.num_instr += 1
        if label is None:
            label_value = 0
        else:
            if label not in self.labels:
                raise ReferencePIOASMError(f"unknown label {label}")
            label_value = self.labels[label]
        self.prog.append(instr | label_value)
        return self

    def nop(self) -> "ReferencePIOASMEmit":
        return self.word(0xA042)

    def jmp(self, cond: int, label: object | None = None) -> "ReferencePIOASMEmit":
        if label is None:
            label = cond
            cond = 0
        return self.word(0x0000 | cond << 5, label)

    def wait(self, polarity: int, src: int, index: int) -> "ReferencePIOASMEmit":
        if src == 6:
            src = 1
        elif src != 0:
            src = 2
        return self.word(0x2000 | polarity << 7 | src << 5 | index)

    def in_(self, src: int, data: int) -> "ReferencePIOASMEmit":
        if not 0 < data <= 32:
            raise ReferencePIOASMError(f"invalid bit count {data}")
        return self.word(0x4000 | src << 5 | data & 0x1F)

    def out(self, dest: int, data: int) -> "ReferencePIOASMEmit":
        if dest == 8:
            dest = 7
        if not 0 < data <= 32:
            raise ReferencePIOASMError(f"invalid bit count {data}")
        return self.word(0x6000 | dest << 5 | data & 0x1F)

    def push(self, value: int = 0, value2: int = 0) -> "ReferencePIOASMEmit":
        value |= value2
        if not value & 1:
            value |= 0x20
        return self.word(0x8000 | (value & 0x60))

    def pull(self, value: int = 0, value2: int = 0) -> "ReferencePIOASMEmit":
        value |= value2
        if not value & 1:
            value |= 0x20
        return self.word(0x8080 | (value & 0x60))

    def mov(self, dest: int, src: int) -> "ReferencePIOASMEmit":
        if dest == 8:
            dest = 4
        return self.word(0xA000 | dest << 5 | src)

    def irq(self, mod: int, index: int | None = None) -> "ReferencePIOASMEmit":
        if index is None:
            index = mod
            mod = 0
        return self.word(0xC000 | (mod & 0x60) | index)

    def set(self, dest: int, data: int) -> "ReferencePIOASMEmit":
        return self.word(0xE000 | dest << 5 | data)

    @property
    def result(self) -> int:
        if len(self.prog) != 1:
            raise ReferencePIOASMError("expecting exactly one instruction")
        return int(self.prog[0])
