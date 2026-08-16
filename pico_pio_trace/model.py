from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Literal, Mapping, Sequence

MASK32 = 0xFFFF_FFFF

SHIFT_LEFT = 0
SHIFT_RIGHT = 1
JOIN_NONE = 0
JOIN_TX = 1
JOIN_RX = 2

IN_LOW = 0
IN_HIGH = 1
OUT_LOW = 2
OUT_HIGH = 3

PinLevel = Literal[0, 1, "Z", "X"]


def u32(value: int) -> int:
    return value & MASK32


def normalise_threshold(value: int) -> int:
    """PIO encodes 32 as zero in five bits; expose it as the natural value 32."""
    if value == 0:
        return 32
    if not 1 <= value <= 32:
        raise ValueError(f"PIO shift threshold must be in 1..32, got {value}")
    return value


def normalise_init(value: Any) -> tuple[int, ...] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return (int(value),)
    if isinstance(value, int):
        return (value,)
    if isinstance(value, (tuple, list)):
        return tuple(int(item) for item in value)
    raise TypeError(f"pin initialiser must be int, tuple, list, or None; got {type(value).__name__}")


@dataclass(slots=True)
class Instruction:
    """A decoded RP2040 PIO instruction.

    ``args`` uses human-readable symbolic values (for example ``("pins", 1)``).
    ``word`` is the complete 16-bit machine word including delay/side-set bits.
    """

    op: str
    args: tuple[Any, ...] = ()
    delay: int = 0
    side: int | None = None
    word: int | None = None
    pc: int | None = None
    source_line: int | None = None
    source_text: str = ""
    from_exec: bool = False

    def display(self) -> str:
        op = self.op
        if op == "jmp":
            condition, target = self.args
            args = _format_arg(target) if condition == "always" else f"{condition}, {_format_arg(target)}"
        elif op == "wait":
            polarity, source, index, *relative = self.args
            index_text = f"rel({index})" if relative and relative[0] else str(index)
            args = f"{polarity}, {source}, {index_text}"
        elif op in {"push", "pull"}:
            conditional, block = self.args
            parts: list[str] = []
            if conditional:
                parts.append("iffull" if op == "push" else "ifempty")
            if not block:
                parts.append("noblock")
            args = ", ".join(parts)
        elif op == "irq":
            action, index, *relative = self.args
            index_text = f"rel({index})" if relative and relative[0] else str(index)
            args = index_text if action == "set" else f"{'block' if action == 'wait' else 'clear'}, {index_text}"
        else:
            args = ", ".join(_format_arg(arg) for arg in self.args)
        display_op = "in_" if op == "in" else op
        core = f"{display_op}({args})"
        if self.side is not None:
            core += f".side({self.side})"
        if self.delay:
            core += f"[{self.delay}]"
        return core


@dataclass(slots=True)
class PIOProgram:
    name: str
    instructions: list[Instruction]
    labels: dict[str, int] = field(default_factory=dict)
    wrap_target: int = 0
    wrap_top: int | None = None
    out_init: tuple[int, ...] | None = None
    set_init: tuple[int, ...] | None = None
    sideset_init: tuple[int, ...] | None = None
    side_pindir: bool = False
    in_shiftdir: int = SHIFT_LEFT
    out_shiftdir: int = SHIFT_LEFT
    autopush: bool = False
    autopull: bool = False
    push_thresh: int = 32
    pull_thresh: int = 32
    fifo_join: int = JOIN_NONE
    sideset_optional: bool = False
    source_path: str | None = None
    source_line: int | None = None
    source_end_line: int | None = None
    source_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.out_init = normalise_init(self.out_init)
        self.set_init = normalise_init(self.set_init)
        self.sideset_init = normalise_init(self.sideset_init)
        self._validate_pin_initialiser("out_init", self.out_init, 32)
        self._validate_pin_initialiser("set_init", self.set_init, 5)
        self._validate_pin_initialiser("sideset_init", self.sideset_init, 5)
        if self.encoded_sideset_count > 5:
            raise ValueError(
                "optional side-set consumes an enable bit, so at most four side-set pins "
                "can be used when any instruction omits .side()"
            )
        if int(self.in_shiftdir) not in {SHIFT_LEFT, SHIFT_RIGHT}:
            raise ValueError(f"in_shiftdir must be SHIFT_LEFT or SHIFT_RIGHT, got {self.in_shiftdir}")
        if int(self.out_shiftdir) not in {SHIFT_LEFT, SHIFT_RIGHT}:
            raise ValueError(f"out_shiftdir must be SHIFT_LEFT or SHIFT_RIGHT, got {self.out_shiftdir}")
        self.in_shiftdir = int(self.in_shiftdir)
        self.out_shiftdir = int(self.out_shiftdir)
        if int(self.fifo_join) not in {JOIN_NONE, JOIN_TX, JOIN_RX}:
            raise ValueError(f"fifo_join must be JOIN_NONE, JOIN_TX, or JOIN_RX, got {self.fifo_join}")
        self.fifo_join = int(self.fifo_join)
        self.push_thresh = normalise_threshold(int(self.push_thresh))
        self.pull_thresh = normalise_threshold(int(self.pull_thresh))
        if self.wrap_top is None:
            self.wrap_top = max(0, len(self.instructions) - 1)
        if len(self.instructions) > 32:
            raise ValueError(f"PIO program {self.name!r} has {len(self.instructions)} instructions; maximum is 32")
        if self.instructions:
            if not 0 <= self.wrap_target < len(self.instructions):
                raise ValueError("wrap_target is outside the program")
            if not 0 <= int(self.wrap_top) < len(self.instructions):
                raise ValueError("wrap_top is outside the program")
        for pc, instruction in enumerate(self.instructions):
            instruction.pc = pc

    @staticmethod
    def _validate_pin_initialiser(name: str, values: tuple[int, ...] | None, maximum: int) -> None:
        if values is None:
            return
        if len(values) > maximum:
            raise ValueError(f"{name} configures {len(values)} pins; RP2040 PIO permits at most {maximum}")
        for entry in values:
            if int(entry) not in {IN_LOW, IN_HIGH, OUT_LOW, OUT_HIGH}:
                raise ValueError(
                    f"{name} entries must be PIO.IN_LOW, IN_HIGH, OUT_LOW, or OUT_HIGH (0..3); got {entry}"
                )

    @property
    def sideset_count(self) -> int:
        return 0 if self.sideset_init is None else len(self.sideset_init)

    @property
    def encoded_sideset_count(self) -> int:
        return self.sideset_count + (1 if self.sideset_count and self.sideset_optional else 0)

    @property
    def delay_bits(self) -> int:
        return 5 - self.encoded_sideset_count

    @property
    def delay_max(self) -> int:
        return (1 << self.delay_bits) - 1


@dataclass(slots=True)
class StateMachineConfig:
    """Resolved configuration for one simulated state machine."""

    program: PIOProgram
    sm_id: int = 0
    requested_freq_hz: float = 1_000_000.0
    actual_freq_hz: float | None = None
    system_clock_hz: float | None = None
    in_base: int = 0
    out_base: int | None = None
    set_base: int | None = None
    sideset_base: int | None = None
    jmp_pin: int | None = None
    default_input: int = 0
    status_mode: str = "constant_zero"
    status_n: int = 0
    initial_tx: list[int] = field(default_factory=list)
    initial_exec: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Optional StateMachine.init overrides. None means use decorator setting.
    in_shiftdir_override: int | None = None
    out_shiftdir_override: int | None = None
    push_thresh_override: int | None = None
    pull_thresh_override: int | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.sm_id <= 7:
            raise ValueError("RP2040 state machine id must be in 0..7")
        for attr in ("in_base", "out_base", "set_base", "sideset_base", "jmp_pin"):
            value = getattr(self, attr)
            if value is not None:
                setattr(self, attr, int(value) % 32)
        self.default_input = 1 if self.default_input else 0
        self.requested_freq_hz = float(self.requested_freq_hz)
        if self.requested_freq_hz < 0:
            raise ValueError("frequency must be zero or greater")
        if self.actual_freq_hz is None:
            self.actual_freq_hz = self._resolve_actual_frequency()
        self.actual_freq_hz = float(self.actual_freq_hz)
        if self.actual_freq_hz <= 0:
            raise ValueError("actual frequency must be greater than zero")
        for attr in ("in_shiftdir_override", "out_shiftdir_override"):
            value = getattr(self, attr)
            if value is not None and int(value) not in {SHIFT_LEFT, SHIFT_RIGHT}:
                raise ValueError(f"{attr} must be SHIFT_LEFT or SHIFT_RIGHT, got {value}")
            if value is not None:
                setattr(self, attr, int(value))
        if self.push_thresh_override is not None:
            self.push_thresh_override = normalise_threshold(int(self.push_thresh_override))
        if self.pull_thresh_override is not None:
            self.pull_thresh_override = normalise_threshold(int(self.pull_thresh_override))
        if self.status_mode not in {"constant_zero", "constant_one", "tx_less_than", "rx_less_than"}:
            raise ValueError(f"unknown MOV STATUS mode {self.status_mode!r}")
        self.status_n = int(self.status_n)
        if not 0 <= self.status_n <= 15:
            raise ValueError("MOV STATUS threshold must be in 0..15")
        self.initial_tx = [u32(v) for v in self.initial_tx]
        self.initial_exec = [int(v) & 0xFFFF for v in self.initial_exec]

    def _resolve_actual_frequency(self) -> float:
        # MicroPython accepts freq=0 as a special request for CLKDIV=0.  On
        # RP2040 that encoding represents the maximum divider, 65536.
        system_clock_hz = 125_000_000.0 if self.system_clock_hz is None else float(self.system_clock_hz)
        if system_clock_hz <= 0:
            raise ValueError("system clock must be greater than zero")
        if self.requested_freq_hz == 0:
            if self.system_clock_hz is None:
                self.warnings.append(
                    "StateMachine freq=0 uses the maximum divider; trace timing assumes a 125 MHz RP2040 system clock"
                )
            return system_clock_hz / 65536.0
        if self.system_clock_hz is None:
            return self.requested_freq_hz
        # Current MicroPython floors sys_clk*256/freq into the RP2040 16.8
        # divider and rejects (rather than clamps) values outside 1..65536.
        divider_256 = int(system_clock_hz * 256 // self.requested_freq_hz)
        if not 256 <= divider_256 <= 65536 * 256:
            raise ValueError(
                f"frequency {self.requested_freq_hz:g} Hz is outside the RP2040 PIO divider range "
                f"for a {system_clock_hz:g} Hz system clock"
            )
        if divider_256 & 0xFF:
            self.warnings.append(
                "fractional PIO clock-divider timing is displayed at its average cycle period; "
                "real RP2040 clock-enable intervals have one-system-clock delta-sigma jitter"
            )
        return system_clock_hz * 256.0 / divider_256

    @property
    def period_s(self) -> float:
        return 1.0 / float(self.actual_freq_hz)

    @property
    def in_shiftdir(self) -> int:
        return self.program.in_shiftdir if self.in_shiftdir_override is None else self.in_shiftdir_override

    @property
    def out_shiftdir(self) -> int:
        return self.program.out_shiftdir if self.out_shiftdir_override is None else self.out_shiftdir_override

    @property
    def push_thresh(self) -> int:
        return self.program.push_thresh if self.push_thresh_override is None else normalise_threshold(self.push_thresh_override)

    @property
    def pull_thresh(self) -> int:
        return self.program.pull_thresh if self.pull_thresh_override is None else normalise_threshold(self.pull_thresh_override)

    @property
    def out_count(self) -> int:
        return len(self.program.out_init or ()) if self.out_base is not None else 0

    @property
    def set_count(self) -> int:
        return len(self.program.set_init or ()) if self.set_base is not None else 0

    @property
    def sideset_count(self) -> int:
        return len(self.program.sideset_init or ()) if self.sideset_base is not None else 0

    @property
    def tx_capacity(self) -> int:
        if self.program.fifo_join == JOIN_RX:
            return 0
        return 8 if self.program.fifo_join == JOIN_TX else 4

    @property
    def rx_capacity(self) -> int:
        if self.program.fifo_join == JOIN_TX:
            return 0
        return 8 if self.program.fifo_join == JOIN_RX else 4


@dataclass(slots=True, frozen=True)
class StimulusEvent:
    cycle: int
    kind: str
    value: int | str | None = None
    pin: int | None = None
    index: int | None = None
    shift: int = 0
    note: str = ""


@dataclass(slots=True)
class TraceRecord:
    cycle: int
    time_s: float
    pc: int
    instruction: str
    instruction_word: int | None
    phase: str
    stalled: bool
    stall_reason: str
    pins: int
    pindirs: int
    external_mask: int
    external_values: int
    # FIFO contents after all host-side events scheduled for this cycle have
    # been applied, but before the PIO instruction (or asynchronous automatic
    # FIFO transfer) is evaluated.  Keeping this boundary snapshot makes a
    # same-cycle put visible even when PULL/autopull consumes the word before
    # the end-of-cycle debugger sample.
    tx_fifo_after_host: tuple[int, ...]
    rx_fifo_after_host: tuple[int, ...]
    tx_fifo: tuple[int, ...]
    rx_fifo: tuple[int, ...]
    x: int
    y: int
    isr: int
    osr: int
    isr_count: int
    osr_count: int
    irq_flags: int
    instruction_pc: int | None
    state_pc: int
    delay_remaining: int
    exec_latch: int | None
    pending_kind: str | None
    halted_reason: str | None
    events: tuple[str, ...] = ()
    source_line: int | None = None

    @property
    def tx_level(self) -> int:
        return len(self.tx_fifo)

    @property
    def rx_level(self) -> int:
        return len(self.rx_fifo)

    @property
    def tx_level_after_host(self) -> int:
        return len(self.tx_fifo_after_host)

    @property
    def rx_level_after_host(self) -> int:
        return len(self.rx_fifo_after_host)

    def pin_level(self, pin: int, default_input: int = 0) -> PinLevel:
        pin %= 32
        bit = 1 << pin
        output_enabled = bool(self.pindirs & bit)
        external_driven = bool(self.external_mask & bit)
        output_value = 1 if self.pins & bit else 0
        external_value = 1 if self.external_values & bit else 0
        if output_enabled:
            if external_driven and external_value != output_value:
                return "X"
            return output_value
        if external_driven:
            return external_value
        return "Z"

    def pin_read_value(self, pin: int, default_input: int = 0) -> int:
        level = self.pin_level(pin, default_input)
        if level == "Z":
            return 1 if default_input else 0
        if level == "X":
            return 0
        return int(level)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tx_level_after_host"] = self.tx_level_after_host
        data["rx_level_after_host"] = self.rx_level_after_host
        data["tx_level"] = self.tx_level
        data["rx_level"] = self.rx_level
        return data


@dataclass(slots=True)
class Trace:
    config: StateMachineConfig
    initial_state: Mapping[str, Any]
    records: list[TraceRecord]
    warnings: list[str] = field(default_factory=list)
    stimuli: list[StimulusEvent] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return len(self.records) * self.config.period_s

    def pin_levels(self, pin: int) -> list[PinLevel]:
        return [record.pin_level(pin, self.config.default_input) for record in self.records]

    def simulation_dict(self) -> dict[str, Any]:
        """Return the complete, JSON-safe model needed for browser-side reruns."""
        program = self.config.program
        return {
            "schema_version": 1,
            "config": {
                "sm_id": self.config.sm_id,
                "requested_freq_hz": self.config.requested_freq_hz,
                "actual_freq_hz": self.config.actual_freq_hz,
                "period_s": self.config.period_s,
                "in_base": self.config.in_base,
                "out_base": self.config.out_base,
                "set_base": self.config.set_base,
                "sideset_base": self.config.sideset_base,
                "jmp_pin": self.config.jmp_pin,
                "default_input": self.config.default_input,
                "status_mode": self.config.status_mode,
                "status_n": self.config.status_n,
                "initial_tx": list(self.config.initial_tx),
                "initial_exec": list(self.config.initial_exec),
                "in_shiftdir": self.config.in_shiftdir,
                "out_shiftdir": self.config.out_shiftdir,
                "push_thresh": self.config.push_thresh,
                "pull_thresh": self.config.pull_thresh,
                "out_count": self.config.out_count,
                "set_count": self.config.set_count,
                "sideset_count": self.config.sideset_count,
                "tx_capacity": self.config.tx_capacity,
                "rx_capacity": self.config.rx_capacity,
            },
            "program": {
                "name": program.name,
                "labels": dict(program.labels),
                "wrap_target": program.wrap_target,
                "wrap_top": program.wrap_top,
                "out_init": None if program.out_init is None else list(program.out_init),
                "set_init": None if program.set_init is None else list(program.set_init),
                "sideset_init": None if program.sideset_init is None else list(program.sideset_init),
                "side_pindir": program.side_pindir,
                "autopush": program.autopush,
                "autopull": program.autopull,
                "fifo_join": program.fifo_join,
                "sideset_optional": program.sideset_optional,
                "sideset_count": program.sideset_count,
                "source_path": program.source_path,
                "source_line": program.source_line,
                "source_end_line": program.source_end_line,
                "source_text": program.source_text,
                "encoded_sideset_count": program.encoded_sideset_count,
                "delay_bits": program.delay_bits,
                "instructions": [_instruction_to_json(instruction) for instruction in program.instructions],
            },
        }

    def to_dict(self) -> dict[str, Any]:
        program = self.config.program
        return {
            "metadata": {
                "program": program.name,
                "sm_id": self.config.sm_id,
                "requested_freq_hz": self.config.requested_freq_hz,
                "actual_freq_hz": self.config.actual_freq_hz,
                "period_s": self.config.period_s,
                "cycles": len(self.records),
                "duration_s": self.duration_s,
                "wrap_target": program.wrap_target,
                "wrap_top": program.wrap_top,
                "fifo_join": program.fifo_join,
                "tx_capacity": self.config.tx_capacity,
                "rx_capacity": self.config.rx_capacity,
                "autopull": program.autopull,
                "autopush": program.autopush,
                "pull_thresh": self.config.pull_thresh,
                "push_thresh": self.config.push_thresh,
                "warnings": list(dict.fromkeys([*program.warnings, *self.config.warnings, *self.warnings])),
            },
            "simulation": self.simulation_dict(),
            "stimuli": [asdict(event) for event in self.stimuli],
            "initial_state": dict(self.initial_state),
            "records": [record.to_dict() for record in self.records],
        }


def _instruction_to_json(instruction: Instruction) -> dict[str, Any]:
    return {
        "op": instruction.op,
        "args": _json_safe(instruction.args),
        "delay": instruction.delay,
        "side": instruction.side,
        "word": instruction.word,
        "pc": instruction.pc,
        "source_line": instruction.source_line,
        "source_text": instruction.source_text,
        "display": instruction.display(),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return value


def _format_arg(arg: Any) -> str:
    if isinstance(arg, str):
        if arg in {
            "pins",
            "x",
            "y",
            "null",
            "pindirs",
            "pc",
            "status",
            "isr",
            "osr",
            "exec",
            "gpio",
            "pin",
            "irq",
            "not_x",
            "x_dec",
            "not_y",
            "y_dec",
            "x_not_y",
            "not_osre",
            "block",
            "noblock",
            "iffull",
            "ifempty",
            "clear",
        }:
            return arg
        return repr(arg)
    if isinstance(arg, tuple) and len(arg) == 2 and arg[0] in {"invert", "reverse", "rel"}:
        return f"{arg[0]}({_format_arg(arg[1])})"
    return str(arg)


def fifo_join_name(value: int) -> str:
    return {JOIN_NONE: "JOIN_NONE", JOIN_TX: "JOIN_TX", JOIN_RX: "JOIN_RX"}.get(value, f"UNKNOWN({value})")


def init_pin_bits(initialiser: Sequence[int] | None) -> tuple[int, int]:
    """Return (values, directions) packed LSB-first for a MicroPython init tuple."""
    values = 0
    directions = 0
    for index, entry in enumerate(initialiser or ()):
        value = int(entry)
        values |= (value & 1) << index
        directions |= ((value >> 1) & 1) << index
    return values, directions


def parse_int(value: int | str) -> int:
    if isinstance(value, int):
        return value
    return int(value.strip().replace("_", ""), 0)


def unique_warnings(*groups: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for warning in group:
            if warning not in seen:
                seen.add(warning)
                result.append(warning)
    return result
