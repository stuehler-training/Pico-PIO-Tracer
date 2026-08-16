from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence

from . import __version__
from .emulator import PIOEmulator
from .model import StateMachineConfig, parse_int
from .parser import PIOParseError, ParsedSource, parse_file
from .render import HtmlTraceOption, default_pins, write_trace
from .stimulus import load_stimulus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pico-pio-trace",
        description=(
            "Safely parse a MicroPython rp2.asm_pio source file, emulate one RP2040 PIO "
            "state machine cycle by cycle, and write a logic-analyzer trace."
        ),
    )
    parser.add_argument("source", type=Path, help="MicroPython .py source file")
    parser.add_argument("-o", "--output", type=Path, help="output .html, .svg, .json, .csv, or .vcd file")
    parser.add_argument(
        "--program",
        help="initial @asm_pio function (HTML reports still embed every parsed function for browser selection)",
    )
    parser.add_argument(
        "--single-program",
        action="store_true",
        help="embed only the selected function in HTML instead of adding the browser function selector",
    )
    parser.add_argument("--sm", type=int, help="StateMachine id to select")
    parser.add_argument("--cycles", type=int, default=200, help="state-machine cycles to simulate (default: 200)")
    parser.add_argument("--freq", type=float, help="override state-machine frequency in Hz")
    parser.add_argument(
        "--system-clock",
        type=float,
        help="system clock in Hz; quantize --freq through the RP2040 16.8 divider as MicroPython does",
    )
    parser.add_argument("--in-base", type=int, help="override IN pin base")
    parser.add_argument("--out-base", type=int, help="override OUT pin base")
    parser.add_argument("--set-base", type=int, help="override SET pin base")
    parser.add_argument("--sideset-base", type=int, help="override side-set pin base")
    parser.add_argument("--jmp-pin", type=int, help="override JMP PIN GPIO")
    parser.add_argument("--default-input", type=int, choices=(0, 1), default=None, help="value read from undriven input pins")
    parser.add_argument(
        "--tx",
        action="append",
        default=[],
        metavar="VALUE[,VALUE...]",
        help="seed TX FIFO/host queue with integer words; may be repeated",
    )
    parser.add_argument("--stimulus", type=Path, help="JSON file containing timed pin, TX, RX-get, and IRQ events")
    parser.add_argument(
        "--pins",
        help="GPIO channels to display, e.g. 0-3,7,25 (default: configured and externally driven pins)",
    )
    parser.add_argument(
        "--status-mode",
        choices=("constant_zero", "constant_one", "tx_less_than", "rx_less_than"),
        help="MOV STATUS source model",
    )
    parser.add_argument("--status-n", type=int, help="threshold for tx_less_than/rx_less_than MOV STATUS mode")
    parser.add_argument("--list", action="store_true", help="list parsed programs/state machines and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        parsed = parse_file(args.source)
        if args.list:
            _print_listing(parsed)
            return 0
        selected_config = parsed.choose(program_name=args.program, sm_id=args.sm)
        config = _apply_overrides(selected_config, args)
        stimuli = load_stimulus(args.stimulus) if args.stimulus else []
        emulator = PIOEmulator(config)
        trace = emulator.run(args.cycles, stimuli)
        pins = parse_pin_spec(args.pins) if args.pins else default_pins(trace)
        output = args.output or args.source.with_name(args.source.stem + ".pio-trace.html")
        if not output.suffix:
            output = output.with_suffix(".html")
        trace_options: list[HtmlTraceOption] | None = None
        if output.suffix.lower() in {".html", ".htm"}:
            trace_options = [HtmlTraceOption(trace, pins)]
            if not args.single_program:
                for candidate in _selectable_configs(parsed):
                    if _same_config(candidate, selected_config):
                        continue
                    candidate_config = _apply_overrides(candidate, args)
                    candidate_trace = PIOEmulator(candidate_config).run(args.cycles, stimuli)
                    candidate_pins = parse_pin_spec(args.pins) if args.pins else default_pins(candidate_trace)
                    trace_options.append(HtmlTraceOption(candidate_trace, candidate_pins))
        write_trace(output, trace, pins, trace_options=trace_options)
    except (OSError, ValueError, PIOParseError) as exc:
        print(f"pico-pio-trace: error: {exc}", file=sys.stderr)
        return 2

    print(f"Wrote {output} ({len(trace.records)} cycles, {trace.duration_s:.9g} s)")
    print(
        f"Program {config.program.name!r}, SM {config.sm_id}, "
        f"{config.actual_freq_hz:.9g} Hz, TX/RX capacity {config.tx_capacity}/{config.rx_capacity}"
    )
    warnings = list(dict.fromkeys([*parsed.warnings, *config.program.warnings, *config.warnings, *trace.warnings]))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if emulator.host_rx_values:
        print("Host RX values: " + ", ".join(f"0x{value:08x}" for value in emulator.host_rx_values))
    return 0


def _apply_overrides(config: StateMachineConfig, args: argparse.Namespace) -> StateMachineConfig:
    changes: dict[str, object] = {
        "warnings": list(config.warnings),
        "initial_tx": [*config.initial_tx, *_parse_tx_values(args.tx)],
        "initial_exec": list(config.initial_exec),
    }
    if args.freq is not None:
        changes["requested_freq_hz"] = args.freq
        changes["actual_freq_hz"] = None
    if args.system_clock is not None:
        changes["system_clock_hz"] = args.system_clock
        changes["actual_freq_hz"] = None
    for option, attribute in (
        (args.in_base, "in_base"),
        (args.out_base, "out_base"),
        (args.set_base, "set_base"),
        (args.sideset_base, "sideset_base"),
        (args.jmp_pin, "jmp_pin"),
        (args.default_input, "default_input"),
        (args.status_mode, "status_mode"),
        (args.status_n, "status_n"),
    ):
        if option is not None:
            changes[attribute] = option
    result = replace(config, **changes)
    if len(result.initial_tx) > result.tx_capacity:
        result.warnings.append(
            f"{len(result.initial_tx)} initial TX words exceed capacity {result.tx_capacity}; "
            "excess words are represented as blocked host writes and inserted as space appears"
        )
    return result


def _selectable_configs(parsed: ParsedSource) -> list[StateMachineConfig]:
    """Return each parsed PIO function with its available StateMachine config(s)."""
    result: list[StateMachineConfig] = []
    for name in parsed.programs:
        machines = [machine for machine in parsed.machines if machine.program.name == name]
        if machines:
            result.extend(machines)
        else:
            result.append(parsed.choose(program_name=name))
    return result


def _same_config(left: StateMachineConfig, right: StateMachineConfig) -> bool:
    return (
        left.program.name,
        left.program.source_line,
        left.sm_id,
        left.requested_freq_hz,
        left.in_base,
        left.out_base,
        left.set_base,
        left.sideset_base,
        left.jmp_pin,
    ) == (
        right.program.name,
        right.program.source_line,
        right.sm_id,
        right.requested_freq_hz,
        right.in_base,
        right.out_base,
        right.set_base,
        right.sideset_base,
        right.jmp_pin,
    )


def parse_pin_spec(spec: str) -> list[int]:
    pins: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            step = 1 if end >= start else -1
            pins.update(value % 32 for value in range(start, end + step, step))
        else:
            pins.add(int(part) % 32)
    if not pins:
        raise ValueError("--pins produced an empty pin set")
    return sorted(pins)


def _parse_tx_values(groups: Iterable[str]) -> list[int]:
    values: list[int] = []
    for group in groups:
        for value in group.split(","):
            value = value.strip()
            if value:
                values.append(parse_int(value) & 0xFFFF_FFFF)
    return values


def _print_listing(parsed: object) -> None:
    # Avoid importing the dataclass solely for typing in the CLI hot path.
    programs = getattr(parsed, "programs")
    machines = getattr(parsed, "machines")
    print("PIO programs:")
    for name, program in programs.items():
        print(
            f"  {name}: {len(program.instructions)} instruction(s), wrap {program.wrap_target}..{program.wrap_top}, "
            f"side-set {program.sideset_count}{' optional' if program.sideset_optional else ''}"
        )
    print("State machines:")
    if not machines:
        print("  (none parsed)")
    for machine in machines:
        print(
            f"  SM {machine.sm_id}: {machine.program.name}, {machine.actual_freq_hz:.9g} Hz, "
            f"bases in={machine.in_base} out={machine.out_base} set={machine.set_base} side={machine.sideset_base}"
        )
