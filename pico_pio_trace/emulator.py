from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .encoding import decode_instruction
from .model import (
    MASK32,
    SHIFT_RIGHT,
    Instruction,
    StateMachineConfig,
    StimulusEvent,
    Trace,
    TraceRecord,
    init_pin_bits,
    u32,
)


class EmulationError(RuntimeError):
    pass


@dataclass(slots=True)
class _Outcome:
    complete: bool = True
    stalled: bool = False
    stall_reason: str = ""
    explicit_pc: int | None = None
    queue_exec: int | None = None
    ignore_delay: bool = False
    phase: str = "execute"


class PIOEmulator:
    """Cycle-oriented emulator for one RP2040 PIO state machine.

    The model follows RP2040 PIO v0 instruction, wrap, shift-counter, FIFO,
    side-set, automatic push/pull, and EXEC timing. It intentionally models one
    state machine; shared-pin arbitration, DMA, synchronizer latency/metastability,
    and RP2350 PIO v1 extensions are outside the current scope.
    """

    def __init__(self, config: StateMachineConfig) -> None:
        self.config = config
        self.program = config.program
        self.warnings: list[str] = []
        self._warned: set[str] = set()

        self.pc = 0
        self.x = 0
        self.y = 0
        self.isr = 0
        self.osr = 0
        self.isr_count = 0
        self.osr_count = 32
        self.irq_flags = 0

        self.pins = 0
        self.pindirs = 0
        self.external_mask = 0
        self.external_values = 0

        self.tx_fifo: deque[int] = deque()
        self.rx_fifo: deque[int] = deque()
        self.host_tx_queue: deque[int] = deque()
        self.pending_rx_gets: deque[int] = deque()
        self.host_rx_values: list[int] = []

        self.delay_remaining = 0
        self.last_instruction = "reset"
        self.last_source_line: int | None = None
        self.last_instruction_pc: int | None = None
        self.exec_latch: int | None = None
        self.pending_kind: str | None = None
        self.halted_reason: str | None = None

        self._cycle_events: list[str] = []
        self._main_pin_writes: list[tuple[str, int, int, int, str]] = []
        self._side_pin_write: tuple[str, int, int, int, str] | None = None

        self._initialise_pins()
        self._initialise_tx()
        self._apply_initial_exec()
        self.initial_state = self._state_dict()

    def reset(self) -> None:
        """Reset by constructing a fresh internal state in-place."""
        fresh = type(self)(self.config)
        self.__dict__.update(fresh.__dict__)

    def run(self, cycles: int, stimuli: Iterable[StimulusEvent] | None = None) -> Trace:
        if cycles < 0:
            raise ValueError("cycles must be non-negative")
        stimulus_list = list(stimuli or ())
        schedule: dict[int, list[StimulusEvent]] = defaultdict(list)
        for event in stimulus_list:
            if event.cycle < 0:
                raise ValueError(f"stimulus cycle cannot be negative: {event}")
            schedule[int(event.cycle)].append(event)

        records: list[TraceRecord] = []
        for cycle in range(cycles):
            self._cycle_events = []
            self._main_pin_writes = []
            self._side_pin_write = None

            for event in schedule.get(cycle, ()):
                self._apply_stimulus(event)
            self._service_host_queues()

            # Preserve the exact FIFO boundary after host activity and before
            # the PIO instruction.  A blocking PULL can consume a newly added
            # TX word in the same cycle, so the end-of-cycle FIFO alone cannot
            # show that the host write was accepted.
            tx_fifo_after_host = tuple(self.tx_fifo)
            rx_fifo_after_host = tuple(self.rx_fifo)

            executed_pc = self.pc
            instruction_text = ""
            instruction_word: int | None = None
            source_line: int | None = None
            instruction_pc: int | None = None
            stalled = False
            stall_reason = ""
            phase = "execute"

            if self.halted_reason:
                phase = "halt"
                instruction_text = f"<halted: {self.halted_reason}>"
            elif self.delay_remaining:
                self._asynchronous_autopull()
                self.delay_remaining -= 1
                phase = "delay"
                instruction_text = f"<delay after {self.last_instruction}>"
                source_line = self.last_source_line
                instruction_pc = self.last_instruction_pc
                self._cycle_events.append(f"delay cycle; {self.delay_remaining} remaining")
            else:
                try:
                    instruction, origin = self._fetch_instruction()
                    instruction_text = ("EXEC: " if origin == "exec" else "") + instruction.display()
                    instruction_word = instruction.word
                    source_line = instruction.source_line
                    instruction_pc = executed_pc if origin == "program" else None
                    self.last_source_line = source_line
                    self.last_instruction_pc = instruction_pc
                    self._asynchronous_autopull(skip_for_out=instruction.op == "out")
                    self._queue_sideset(instruction)
                    outcome = self._execute_instruction(instruction)
                    stalled = outcome.stalled
                    stall_reason = outcome.stall_reason
                    phase = outcome.phase if not outcome.stalled else "stall"
                    self._commit_pin_writes()
                    if outcome.complete:
                        self._complete_instruction(instruction, origin, outcome)
                except EmulationError as exc:
                    self._commit_pin_writes()
                    self.halted_reason = str(exc)
                    self.warnings.append(str(exc))
                    phase = "error"
                    instruction_text = instruction_text or "<decode/fetch error>"
                    self._cycle_events.append(f"ERROR: {exc}")

            records.append(
                TraceRecord(
                    cycle=cycle,
                    time_s=cycle * self.config.period_s,
                    pc=executed_pc,
                    instruction=instruction_text,
                    instruction_word=instruction_word,
                    phase=phase,
                    stalled=stalled,
                    stall_reason=stall_reason,
                    pins=self.pins,
                    pindirs=self.pindirs,
                    external_mask=self.external_mask,
                    external_values=self.external_values,
                    tx_fifo_after_host=tx_fifo_after_host,
                    rx_fifo_after_host=rx_fifo_after_host,
                    tx_fifo=tuple(self.tx_fifo),
                    rx_fifo=tuple(self.rx_fifo),
                    x=self.x,
                    y=self.y,
                    isr=self.isr,
                    osr=self.osr,
                    isr_count=self.isr_count,
                    osr_count=self.osr_count,
                    irq_flags=self.irq_flags,
                    instruction_pc=instruction_pc,
                    state_pc=self.pc,
                    delay_remaining=self.delay_remaining,
                    exec_latch=self.exec_latch,
                    pending_kind=self.pending_kind,
                    halted_reason=self.halted_reason,
                    events=tuple(self._cycle_events),
                    source_line=source_line,
                )
            )

        return Trace(
            self.config,
            self.initial_state,
            records,
            warnings=list(self.warnings),
            stimuli=stimulus_list,
        )

    # ------------------------------------------------------------------
    # Initialisation and host-side events

    def _initialise_pins(self) -> None:
        # MicroPython initialises OUT, SET, then SIDESET groups in this order.
        for group, base, initialiser in (
            ("out", self.config.out_base, self.program.out_init),
            ("set", self.config.set_base, self.program.set_init),
            ("sideset", self.config.sideset_base, self.program.sideset_init),
        ):
            if base is None or initialiser is None:
                continue
            values, directions = init_pin_bits(initialiser)
            self._write_mapped_now("pins", base, len(initialiser), values)
            self._write_mapped_now("pindirs", base, len(initialiser), directions)

    def _initialise_tx(self) -> None:
        for value in self.config.initial_tx:
            if len(self.tx_fifo) < self.config.tx_capacity:
                self.tx_fifo.append(u32(value))
            else:
                self.host_tx_queue.append(u32(value))

    def _apply_initial_exec(self) -> None:
        for word in self.config.initial_exec:
            instruction = decode_instruction(word, self.program, from_exec=True)
            instruction.source_text = f"initial exec 0x{word:04x}"
            self._cycle_events = []
            self._main_pin_writes = []
            self._side_pin_write = None
            self._queue_sideset(instruction)
            try:
                outcome = self._execute_instruction(instruction)
            except EmulationError as exc:
                self._warn_once(f"initial exec 0x{word:04x} could not be applied: {exc}")
                self.pending_kind = None
                continue
            self._commit_pin_writes()
            if not outcome.complete:
                self._warn_once(
                    f"initial exec 0x{word:04x} stalled ({outcome.stall_reason}); it was not completed during static setup"
                )
                self.pending_kind = None
                continue
            if outcome.explicit_pc is not None:
                self.pc = outcome.explicit_pc & 0x1F
            if outcome.queue_exec is not None:
                self.exec_latch = outcome.queue_exec & 0xFFFF

    def _apply_stimulus(self, event: StimulusEvent) -> None:
        kind = event.kind.lower().replace("-", "_")
        if kind in {"pin", "gpio", "pin_drive"}:
            if event.pin is None:
                raise ValueError(f"pin stimulus requires a pin: {event}")
            pin = event.pin % 32
            bit = 1 << pin
            value = event.value
            if value is None or (isinstance(value, str) and value.upper() in {"Z", "RELEASE", "NONE"}):
                self.external_mask &= ~bit
                self.external_values &= ~bit
                self._cycle_events.append(f"host releases GPIO{pin}")
            else:
                numeric = int(value)
                self.external_mask |= bit
                if numeric:
                    self.external_values |= bit
                else:
                    self.external_values &= ~bit
                self._cycle_events.append(f"host drives GPIO{pin}={1 if numeric else 0}")
        elif kind in {"tx", "tx_put", "put"}:
            if event.value is None:
                raise ValueError(f"TX stimulus requires a value: {event}")
            value = u32(int(event.value) << int(event.shift))
            if len(self.tx_fifo) < self.config.tx_capacity:
                self.tx_fifo.append(value)
                self._cycle_events.append(f"host TX put 0x{value:08x}")
            else:
                self.host_tx_queue.append(value)
                self._cycle_events.append(f"host TX put 0x{value:08x} blocks; queued for FIFO space")
        elif kind in {"rx_put", "rx_fill", "rx_inject", "inject_rx"}:
            if event.value is None:
                raise ValueError(f"RX injection stimulus requires a value: {event}")
            value = u32(int(event.value) << int(event.shift))
            if self.config.rx_capacity <= 0:
                message = f"debug RX inject 0x{value:08x} ignored; RX FIFO is disabled by FIFO join mode"
                self._cycle_events.append(message)
                self._warn_once(message)
            elif len(self.rx_fifo) < self.config.rx_capacity:
                self.rx_fifo.append(value)
                self._cycle_events.append(f"debug RX inject 0x{value:08x}")
            else:
                message = f"debug RX inject 0x{value:08x} dropped; RX FIFO is full"
                self._cycle_events.append(message)
                self._warn_once(message)
        elif kind in {"rx", "rx_get", "get"}:
            shift = int(event.shift)
            if self.rx_fifo:
                value = self.rx_fifo.popleft() >> shift
                self.host_rx_values.append(value)
                self._cycle_events.append(f"host RX get -> 0x{value:08x}")
            else:
                self.pending_rx_gets.append(shift)
                self._cycle_events.append("host RX get blocks; waiting for data")
        elif kind in {"irq", "irq_set", "irq_clear"}:
            index = event.index if event.index is not None else int(event.value or 0)
            index = int(index) & 0x7
            should_set = kind != "irq_clear"
            if kind == "irq" and event.value is not None and event.index is not None:
                should_set = bool(int(event.value))
            if should_set:
                self.irq_flags |= 1 << index
                self._cycle_events.append(f"host sets IRQ{index}")
            else:
                self.irq_flags &= ~(1 << index)
                self._cycle_events.append(f"host clears IRQ{index}")
        else:
            raise ValueError(f"unknown stimulus kind {event.kind!r}")
        if event.note:
            self._cycle_events.append(event.note)

    def _service_host_queues(self) -> None:
        if self.host_tx_queue and len(self.tx_fifo) < self.config.tx_capacity:
            value = self.host_tx_queue.popleft()
            self.tx_fifo.append(value)
            self._cycle_events.append(f"blocked host TX put completes: 0x{value:08x}")
        if self.pending_rx_gets and self.rx_fifo:
            shift = self.pending_rx_gets.popleft()
            value = self.rx_fifo.popleft() >> shift
            self.host_rx_values.append(value)
            self._cycle_events.append(f"blocked host RX get completes: 0x{value:08x}")

    # ------------------------------------------------------------------
    # Fetch, completion, and timing

    def _fetch_instruction(self) -> tuple[Instruction, str]:
        if self.exec_latch is not None:
            instruction = decode_instruction(self.exec_latch, self.program, from_exec=True)
            return instruction, "exec"
        if not 0 <= self.pc < len(self.program.instructions):
            raise EmulationError(
                f"PC {self.pc} is outside parsed program {self.program.name!r}; contents of the rest of PIO instruction memory are unknown"
            )
        return self.program.instructions[self.pc], "program"

    def _complete_instruction(self, instruction: Instruction, origin: str, outcome: _Outcome) -> None:
        current_pc = self.pc
        if origin == "exec":
            self.exec_latch = None
            if outcome.explicit_pc is not None:
                self.pc = outcome.explicit_pc & 0x1F
        else:
            if outcome.explicit_pc is not None:
                self.pc = outcome.explicit_pc & 0x1F
            elif current_pc == self.program.wrap_top:
                self.pc = self.program.wrap_target
            else:
                self.pc = (current_pc + 1) & 0x1F
        if outcome.queue_exec is not None:
            self.exec_latch = outcome.queue_exec & 0xFFFF
        self.pending_kind = None
        self.last_instruction = instruction.display()
        if instruction.delay and not outcome.ignore_delay:
            self.delay_remaining = instruction.delay

    def _asynchronous_autopull(self, *, skip_for_out: bool = False) -> None:
        if (
            self.program.autopull
            and not skip_for_out
            and self.osr_count >= self.config.pull_thresh
            and self.tx_fifo
        ):
            self.osr = self.tx_fifo.popleft()
            self.osr_count = 0
            self._cycle_events.append(f"autopull refills OSR asynchronously with 0x{self.osr:08x}")

    # ------------------------------------------------------------------
    # Instruction semantics

    def _execute_instruction(self, instruction: Instruction) -> _Outcome:
        if self.pending_kind == "autopush":
            return self._complete_pending_autopush()
        if self.pending_kind == "irq_wait":
            return self._complete_pending_irq_wait(instruction)

        op = instruction.op
        if op == "nop":
            return _Outcome()
        if op == "jmp":
            return self._execute_jmp(instruction)
        if op == "wait":
            return self._execute_wait(instruction)
        if op == "in":
            return self._execute_in(instruction)
        if op == "out":
            return self._execute_out(instruction)
        if op == "push":
            return self._execute_push(instruction)
        if op == "pull":
            return self._execute_pull(instruction)
        if op == "mov":
            return self._execute_mov(instruction)
        if op == "irq":
            return self._execute_irq(instruction)
        if op == "set":
            return self._execute_set(instruction)
        raise EmulationError(f"unsupported decoded instruction {op!r}")

    def _execute_jmp(self, instruction: Instruction) -> _Outcome:
        condition, target = instruction.args
        take = False
        if condition == "always":
            take = True
        elif condition == "not_x":
            take = self.x == 0
        elif condition == "x_dec":
            take = self.x != 0
            self.x = u32(self.x - 1)
            self._cycle_events.append(f"X decremented to 0x{self.x:08x}")
        elif condition == "not_y":
            take = self.y == 0
        elif condition == "y_dec":
            take = self.y != 0
            self.y = u32(self.y - 1)
            self._cycle_events.append(f"Y decremented to 0x{self.y:08x}")
        elif condition == "x_not_y":
            take = self.x != self.y
        elif condition == "pin":
            take = bool(self._read_gpio(self.config.jmp_pin or 0))
        elif condition == "not_osre":
            take = self.osr_count < self.config.pull_thresh
        else:
            raise EmulationError(f"reserved/unknown JMP condition {condition!r}")
        if take:
            target_pc = self._resolve_target(target)
            self._cycle_events.append(f"JMP taken to {target_pc}")
            return _Outcome(explicit_pc=target_pc)
        self._cycle_events.append("JMP not taken")
        return _Outcome()

    def _execute_wait(self, instruction: Instruction) -> _Outcome:
        polarity, source, index, relative = instruction.args
        polarity = int(polarity) & 1
        index = int(index)
        if source == "gpio":
            value = self._read_gpio(index)
        elif source == "pin":
            value = self._read_gpio(self.config.in_base + index)
        elif source == "irq":
            irq_index = self._resolve_irq_index(index, bool(relative))
            value = 1 if self.irq_flags & (1 << irq_index) else 0
        else:
            raise EmulationError(f"reserved WAIT source {source!r}")
        if value != polarity:
            return _Outcome(False, True, f"WAIT {polarity} {source} {index}", phase="wait")
        if source == "irq" and polarity:
            irq_index = self._resolve_irq_index(index, bool(relative))
            self.irq_flags &= ~(1 << irq_index)
            self._cycle_events.append(f"WAIT condition met; IRQ{irq_index} cleared")
        else:
            self._cycle_events.append("WAIT condition met")
        return _Outcome()

    def _execute_in(self, instruction: Instruction) -> _Outcome:
        source, count = instruction.args
        count = int(count)
        value = self._read_in_source(str(source), count)
        bits = value & self._mask(count)
        if self.config.in_shiftdir == SHIFT_RIGHT:
            self.isr = bits if count == 32 else u32((self.isr >> count) | (bits << (32 - count)))
        else:
            self.isr = bits if count == 32 else u32((self.isr << count) | bits)
        self.isr_count = min(32, self.isr_count + count)
        self._cycle_events.append(
            f"IN shifts {count} bit(s) from {source}: ISR=0x{self.isr:08x}, count={self.isr_count}"
        )
        if self.program.autopush and self.isr_count >= self.config.push_thresh:
            if len(self.rx_fifo) >= self.config.rx_capacity:
                self.pending_kind = "autopush"
                return _Outcome(False, True, "autopush waits for RX FIFO space", phase="autopush")
            self._push_isr("autopush")
        return _Outcome()

    def _complete_pending_autopush(self) -> _Outcome:
        if len(self.rx_fifo) >= self.config.rx_capacity:
            return _Outcome(False, True, "autopush waits for RX FIFO space", phase="autopush")
        self._push_isr("autopush after stall")
        return _Outcome()

    def _execute_out(self, instruction: Instruction) -> _Outcome:
        destination, count = instruction.args
        count = int(count)
        if self.program.autopull and self.osr_count >= self.config.pull_thresh:
            if self.tx_fifo:
                self.osr = self.tx_fifo.popleft()
                self.osr_count = 0
                self._cycle_events.append(f"autopull loads OSR=0x{self.osr:08x}; OUT remains stalled this cycle")
            return _Outcome(False, True, "autopull pre-OUT stall", phase="autopull")

        if self.config.out_shiftdir == SHIFT_RIGHT:
            value = self.osr & self._mask(count)
            self.osr = 0 if count == 32 else self.osr >> count
        else:
            value = self.osr if count == 32 else (self.osr >> (32 - count)) & self._mask(count)
            self.osr = 0 if count == 32 else u32(self.osr << count)
        self.osr_count = min(32, self.osr_count + count)
        self._cycle_events.append(
            f"OUT shifts {count} bit(s): value=0x{value:08x}, OSR=0x{self.osr:08x}, count={self.osr_count}"
        )
        outcome = self._write_out_destination(str(destination), value, count)

        if self.program.autopull and self.osr_count >= self.config.pull_thresh and self.tx_fifo:
            self.osr = self.tx_fifo.popleft()
            self.osr_count = 0
            self._cycle_events.append(f"post-OUT autopull refills OSR for free with 0x{self.osr:08x}")
        return outcome

    def _execute_push(self, instruction: Instruction) -> _Outcome:
        if_full, block = instruction.args
        if bool(if_full) and self.isr_count < self.config.push_thresh:
            self._cycle_events.append("PUSH IFFULL is a no-op below threshold")
            return _Outcome()
        if len(self.rx_fifo) >= self.config.rx_capacity:
            if bool(block):
                return _Outcome(False, True, "PUSH waits for RX FIFO space", phase="push")
            lost = self.isr
            self.isr = 0
            self.isr_count = 0
            self._cycle_events.append(f"nonblocking PUSH drops 0x{lost:08x}; ISR cleared")
            return _Outcome()
        self._push_isr("PUSH")
        return _Outcome()

    def _execute_pull(self, instruction: Instruction) -> _Outcome:
        if_empty, block = instruction.args
        # With autopull enabled, PULL acts as a fence only when the OSR is
        # completely full (shift count zero).  A partially consumed OSR is not
        # protected by this rule: an unconditional PULL replaces it normally.
        if self.program.autopull and self.osr_count == 0:
            self._cycle_events.append("PULL is a no-op because autopull already left the OSR full")
            return _Outcome()
        if bool(if_empty) and self.osr_count < self.config.pull_thresh:
            self._cycle_events.append("PULL IFEMPTY is a no-op below threshold")
            return _Outcome()
        if not self.tx_fifo:
            if bool(block):
                return _Outcome(False, True, "PULL waits for TX FIFO data", phase="pull")
            self.osr = self.x
            self.osr_count = 0
            self._cycle_events.append(f"nonblocking PULL copies X to OSR: 0x{self.osr:08x}")
            return _Outcome()
        self.osr = self.tx_fifo.popleft()
        self.osr_count = 0
        self._cycle_events.append(f"PULL loads OSR=0x{self.osr:08x}")
        return _Outcome()

    def _execute_mov(self, instruction: Instruction) -> _Outcome:
        destination, source_spec = instruction.args
        operation: str | None = None
        source = source_spec
        if isinstance(source_spec, tuple):
            operation, source = source_spec
        if self.program.autopull and str(source) == "osr":
            self._warn_once(
                "MOV from OSR while autopull is enabled is hardware-racy; this trace uses the "
                "datasheet pseudocode ordering (eligible non-OUT autopull before MOV)"
            )
        if self.program.autopull and str(destination) == "osr":
            self._warn_once(
                "MOV to OSR while autopull is enabled can overwrite a concurrently autopulled word; "
                "this trace orders eligible non-OUT autopull before MOV"
            )
        value = self._read_mov_source(str(source))
        if operation == "invert":
            value = u32(~value)
        elif operation == "reverse":
            value = self._reverse32(value)
        elif operation not in (None, "none"):
            raise EmulationError(f"reserved MOV operation {operation!r}")
        self._cycle_events.append(f"MOV reads 0x{value:08x} from {source}")
        return self._write_mov_destination(str(destination), value)

    def _execute_irq(self, instruction: Instruction) -> _Outcome:
        action, index, relative = instruction.args
        irq_index = self._resolve_irq_index(int(index), bool(relative))
        bit = 1 << irq_index
        if action == "set":
            self.irq_flags |= bit
            self._cycle_events.append(f"IRQ{irq_index} set")
            return _Outcome()
        if action == "clear":
            self.irq_flags &= ~bit
            self._cycle_events.append(f"IRQ{irq_index} cleared")
            return _Outcome()
        if action == "wait":
            self.irq_flags |= bit
            self.pending_kind = "irq_wait"
            self._cycle_events.append(f"IRQ{irq_index} set; waiting for host/another SM to clear it")
            return _Outcome(False, True, f"IRQ WAIT on flag {irq_index}", phase="irq_wait")
        raise EmulationError(f"unknown IRQ action {action!r}")

    def _complete_pending_irq_wait(self, instruction: Instruction) -> _Outcome:
        action, index, relative = instruction.args
        if action != "wait":
            raise EmulationError("internal IRQ wait state does not match current instruction")
        irq_index = self._resolve_irq_index(int(index), bool(relative))
        if self.irq_flags & (1 << irq_index):
            return _Outcome(False, True, f"IRQ WAIT on flag {irq_index}", phase="irq_wait")
        self._cycle_events.append(f"IRQ WAIT completes after IRQ{irq_index} was cleared")
        return _Outcome()

    def _execute_set(self, instruction: Instruction) -> _Outcome:
        destination, value = instruction.args
        destination = str(destination)
        value = int(value) & 0x1F
        if destination == "pins":
            self._queue_main_write("pins", self.config.set_base, self.config.set_count, value, "SET PINS")
        elif destination == "pindirs":
            self._queue_main_write("pindirs", self.config.set_base, self.config.set_count, value, "SET PINDIRS")
        elif destination == "x":
            self.x = value
            self._cycle_events.append(f"SET X={value}")
        elif destination == "y":
            self.y = value
            self._cycle_events.append(f"SET Y={value}")
        else:
            raise EmulationError(f"reserved SET destination {destination!r}")
        return _Outcome()

    # ------------------------------------------------------------------
    # Data sources and destinations

    def _read_in_source(self, source: str, count: int) -> int:
        if source == "pins":
            return self._read_mapped(self.config.in_base, count)
        if source == "x":
            return self.x
        if source == "y":
            return self.y
        if source == "null":
            return 0
        if source == "isr":
            return self.isr
        if source == "osr":
            return self.osr
        raise EmulationError(f"reserved IN source {source!r}")

    def _write_out_destination(self, destination: str, value: int, count: int) -> _Outcome:
        if destination == "pins":
            self._queue_main_write("pins", self.config.out_base, self.config.out_count, value, "OUT PINS")
            return _Outcome()
        if destination == "pindirs":
            self._queue_main_write("pindirs", self.config.out_base, self.config.out_count, value, "OUT PINDIRS")
            return _Outcome()
        if destination == "x":
            self.x = value
            return _Outcome()
        if destination == "y":
            self.y = value
            return _Outcome()
        if destination == "null":
            return _Outcome()
        if destination == "pc":
            return _Outcome(explicit_pc=value & 0x1F)
        if destination == "isr":
            self.isr = u32(value)
            self.isr_count = min(32, count)
            return _Outcome()
        if destination == "exec":
            return _Outcome(queue_exec=value & 0xFFFF, ignore_delay=True)
        raise EmulationError(f"reserved OUT destination {destination!r}")

    def _read_mov_source(self, source: str) -> int:
        if source == "pins":
            return self._read_mapped(self.config.in_base, 32)
        if source == "x":
            return self.x
        if source == "y":
            return self.y
        if source == "null":
            return 0
        if source == "status":
            return self._status_value()
        if source == "isr":
            return self.isr
        if source == "osr":
            return self.osr
        raise EmulationError(f"reserved MOV source {source!r}")

    def _write_mov_destination(self, destination: str, value: int) -> _Outcome:
        value = u32(value)
        if destination == "pins":
            self._queue_main_write("pins", self.config.out_base, self.config.out_count, value, "MOV PINS")
            return _Outcome()
        if destination == "x":
            self.x = value
            return _Outcome()
        if destination == "y":
            self.y = value
            return _Outcome()
        if destination == "exec":
            return _Outcome(queue_exec=value & 0xFFFF, ignore_delay=True)
        if destination == "pc":
            return _Outcome(explicit_pc=value & 0x1F)
        if destination == "isr":
            self.isr = value
            self.isr_count = 0
            return _Outcome()
        if destination == "osr":
            self.osr = value
            self.osr_count = 0
            return _Outcome()
        raise EmulationError(f"reserved MOV destination {destination!r}")

    def _status_value(self) -> int:
        mode = self.config.status_mode
        n = int(self.config.status_n)
        if mode == "tx_less_than":
            return MASK32 if len(self.tx_fifo) < n else 0
        if mode == "rx_less_than":
            return MASK32 if len(self.rx_fifo) < n else 0
        if mode == "constant_one":
            return MASK32
        if mode == "constant_zero":
            self._warn_once(
                "MOV STATUS encountered; MicroPython does not expose EXECCTRL_STATUS_SEL/STATUS_N, so the emulator uses constant zero unless configured explicitly"
            )
            return 0
        raise EmulationError(f"unknown status mode {mode!r}")

    # ------------------------------------------------------------------
    # FIFO helpers

    def _push_isr(self, reason: str) -> None:
        if len(self.rx_fifo) >= self.config.rx_capacity:
            raise EmulationError("internal error: attempted to push to a full/disabled RX FIFO")
        value = self.isr
        self.rx_fifo.append(value)
        self.isr = 0
        self.isr_count = 0
        self._cycle_events.append(f"{reason} writes 0x{value:08x} to RX FIFO and clears ISR")

    # ------------------------------------------------------------------
    # Pin mapping, side-set, and reads

    def _queue_sideset(self, instruction: Instruction) -> None:
        if instruction.side is None:
            return
        kind = "pindirs" if self.program.side_pindir else "pins"
        self._side_pin_write = (
            kind,
            self.config.sideset_base or 0,
            self.config.sideset_count,
            int(instruction.side),
            "SIDESET PINDIRS" if kind == "pindirs" else "SIDESET PINS",
        )
        if self.config.sideset_count == 0:
            self._warn_once("side-set instruction encountered with no active sideset_base/count; no pin is changed")

    def _queue_main_write(self, kind: str, base: int | None, count: int, value: int, source: str) -> None:
        if base is None or count <= 0:
            self._warn_once(f"{source} has a configured pin count of zero; no pin is changed")
            return
        self._main_pin_writes.append((kind, base, count, value, source))

    def _commit_pin_writes(self) -> None:
        for kind, base, count, value, source in self._main_pin_writes:
            self._write_mapped_now(kind, base, count, value)
            self._cycle_events.append(f"{source} writes {count} pin(s) at GPIO{base} with 0x{value:x}")
        if self._side_pin_write is not None:
            kind, base, count, value, source = self._side_pin_write
            if count > 0:
                self._write_mapped_now(kind, base, count, value)
                self._cycle_events.append(f"{source} writes {count} pin(s) at GPIO{base} with 0x{value:x}")

    def _write_mapped_now(self, kind: str, base: int, count: int, value: int) -> None:
        target = self.pins if kind == "pins" else self.pindirs
        for offset in range(count):
            pin = (base + offset) % 32
            bit = 1 << pin
            if value & (1 << offset):
                target |= bit
            else:
                target &= ~bit
        if kind == "pins":
            self.pins = u32(target)
        else:
            self.pindirs = u32(target)

    def _read_gpio(self, pin: int) -> int:
        pin %= 32
        bit = 1 << pin
        if self.pindirs & bit:
            return 1 if self.pins & bit else 0
        if self.external_mask & bit:
            return 1 if self.external_values & bit else 0
        return self.config.default_input

    def _read_mapped(self, base: int, count: int) -> int:
        value = 0
        for offset in range(count):
            value |= self._read_gpio(base + offset) << offset
        return u32(value)

    # ------------------------------------------------------------------
    # Misc helpers

    def _resolve_target(self, target: Any) -> int:
        if isinstance(target, str):
            try:
                return self.program.labels[target]
            except KeyError as exc:
                raise EmulationError(f"unknown JMP label {target!r}") from exc
        return int(target) & 0x1F

    def _resolve_irq_index(self, index: int, relative: bool) -> int:
        index &= 0x7
        if not relative:
            return index
        return (index & 0x4) | ((index + (self.config.sm_id & 0x3)) & 0x3)

    @staticmethod
    def _mask(count: int) -> int:
        return MASK32 if count == 32 else (1 << count) - 1

    @staticmethod
    def _reverse32(value: int) -> int:
        value = u32(value)
        value = ((value & 0x55555555) << 1) | ((value >> 1) & 0x55555555)
        value = ((value & 0x33333333) << 2) | ((value >> 2) & 0x33333333)
        value = ((value & 0x0F0F0F0F) << 4) | ((value >> 4) & 0x0F0F0F0F)
        value = ((value & 0x00FF00FF) << 8) | ((value >> 8) & 0x00FF00FF)
        return u32((value << 16) | (value >> 16))

    def _state_dict(self) -> dict[str, Any]:
        return {
            "pc": self.pc,
            "x": self.x,
            "y": self.y,
            "isr": self.isr,
            "osr": self.osr,
            "isr_count": self.isr_count,
            "osr_count": self.osr_count,
            "pins": self.pins,
            "pindirs": self.pindirs,
            "external_mask": self.external_mask,
            "external_values": self.external_values,
            "irq_flags": self.irq_flags,
            "tx_fifo": list(self.tx_fifo),
            "rx_fifo": list(self.rx_fifo),
            "delay_remaining": self.delay_remaining,
            "exec_latch": self.exec_latch,
            "pending_kind": self.pending_kind,
            "halted_reason": self.halted_reason,
        }

    def _warn_once(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            self.warnings.append(message)
