from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Sequence

from .model import PinLevel, Trace, TraceRecord, fifo_join_name


@dataclass(slots=True, frozen=True)
class HtmlTraceOption:
    """One selectable trace embedded in a self-contained HTML report."""

    trace: Trace
    pins: Sequence[int] | None = None
    label: str | None = None


def _normalise_html_options(
    selected: Trace,
    selected_pins: Sequence[int],
    options: Sequence[HtmlTraceOption] | None,
) -> tuple[list[dict[str, Any]], str]:
    raw_options = list(options or [HtmlTraceOption(selected, selected_pins)])
    if not raw_options:
        raw_options = [HtmlTraceOption(selected, selected_pins)]

    selected_index = next(
        (index for index, option in enumerate(raw_options) if option.trace is selected),
        None,
    )
    if selected_index is None:
        raw_options.insert(0, HtmlTraceOption(selected, selected_pins))
        selected_index = 0

    used_keys: set[str] = set()
    result: list[dict[str, Any]] = []
    selected_key = ""
    for index, option in enumerate(raw_options):
        trace = option.trace
        program = trace.config.program
        base_key = re.sub(
            r"[^a-zA-Z0-9_.-]+",
            "_",
            f"{program.name}@sm{trace.config.sm_id}",
        ).strip("_") or f"trace-{index}"
        key = base_key
        suffix = 2
        while key in used_keys:
            key = f"{base_key}-{suffix}"
            suffix += 1
        used_keys.add(key)
        option_pins = list(
            default_pins(trace)
            if option.pins is None
            else sorted({int(pin) % 32 for pin in option.pins})
        )
        result.append(
            {
                "key": key,
                "label": option.label or f"{program.name} · SM {trace.config.sm_id}",
                "program": program.name,
                "sm_id": trace.config.sm_id,
                "trace": trace.to_dict(),
                "pins": option_pins,
            }
        )
        if index == selected_index:
            selected_key = key
    return result, selected_key


def default_pins(trace: Trace) -> list[int]:
    config = trace.config
    pins: set[int] = set()
    for base, count in (
        (config.out_base, config.out_count),
        (config.set_base, config.set_count),
        (config.sideset_base, config.sideset_count),
    ):
        if base is not None:
            pins.update((base + offset) % 32 for offset in range(count))
    if config.jmp_pin is not None:
        pins.add(config.jmp_pin % 32)
    for record in trace.records:
        mask = record.external_mask
        pins.update(pin for pin in range(32) if mask & (1 << pin))
    if not pins:
        pins.add(config.in_base % 32)
    return sorted(pins)


def _web_asset(name: str) -> str:
    return resources.files("pico_pio_trace").joinpath("web", name).read_text(encoding="utf-8")


def render_html(
    trace: Trace,
    pins: Sequence[int] | None = None,
    *,
    title: str | None = None,
    trace_options: Sequence[HtmlTraceOption] | None = None,
) -> str:
    """Render a self-contained, editable browser trace.

    The generated file embeds the decoded PIO program and a dependency-free
    JavaScript mirror of the Python emulator. GPIO transitions can therefore be
    added directly in the waveform and the complete trace is recomputed without
    a Python process or separate stimulus file.
    """
    pins = list(default_pins(trace) if pins is None else sorted({int(pin) % 32 for pin in pins}))
    data = trace.to_dict()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    pin_payload = json.dumps(pins, separators=(",", ":"))
    catalog_options, selected_key = _normalise_html_options(trace, pins, trace_options)
    catalog_payload = json.dumps(
        {"schema_version": 1, "selected": selected_key, "options": catalog_options},
        separators=(",", ":"),
    ).replace("</", "<\\/")
    program = trace.config.program
    title = title or f"PIO logic trace — {program.name}"
    warning_items = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in data["metadata"]["warnings"]
    ) or "<li>None</li>"
    source_name = html.escape(program.source_path or "in-memory source")
    initial_tx_level = trace.records[0].tx_level if trace.records else len(trace.initial_state.get("tx_fifo", ()))
    initial_rx_level = trace.records[0].rx_level if trace.records else len(trace.initial_state.get("rx_fifo", ()))

    template = _web_asset("template.html")
    replacements = {
        "__TITLE__": html.escape(title),
        "__PROGRAM__": html.escape(program.name),
        "__SOURCE__": source_name,
        "__SM_ID__": str(trace.config.sm_id),
        "__CYCLES__": f"{len(trace.records):,}",
        "__CYCLES_RAW__": str(len(trace.records)),
        "__FREQUENCY__": _format_frequency(trace.config.actual_freq_hz or 0),
        "__PERIOD__": _format_time(trace.config.period_s),
        "__DURATION__": _format_time(trace.duration_s),
        "__FIFO_CAPACITY__": f"{trace.config.tx_capacity} / {trace.config.rx_capacity}",
        "__TX_CAPACITY__": str(trace.config.tx_capacity),
        "__RX_CAPACITY__": str(trace.config.rx_capacity),
        "__INITIAL_TX_LEVEL__": str(initial_tx_level),
        "__INITIAL_RX_LEVEL__": str(initial_rx_level),
        "__FIFO_JOIN__": html.escape(fifo_join_name(program.fifo_join)),
        "__DEFAULT_END__": str(min(len(trace.records), 200)),
        "__MAX_CYCLE__": str(max(0, len(trace.records) - 1)),
        "__WARNINGS__": warning_items,
        "__TRACE_JSON__": payload,
        "__PINS_JSON__": pin_payload,
        "__TRACE_CATALOG_JSON__": catalog_payload,
        "__STYLES__": _web_asset("styles.css"),
        "__SIMULATOR_JS__": _web_asset("simulator.js"),
        "__VIEWER_JS__": _web_asset("viewer.js"),
        "__GPL_LICENSE_TEXT__": html.escape(_web_asset("GPL-3.0.txt")),
    }
    # Replace placeholders in a single pass over the original template. A
    # sequential ``str.replace`` loop would rescan inserted values; now that the
    # complete user source is embedded, source text such as ``__VIEWER_JS__``
    # must remain literal rather than being interpreted as a template marker.
    marker_pattern = re.compile("|".join(re.escape(marker) for marker in sorted(replacements, key=len, reverse=True)))
    return marker_pattern.sub(lambda match: replacements[match.group(0)], template)

def render_svg(
    trace: Trace,
    pins: Sequence[int] | None = None,
    *,
    start: int = 0,
    end: int | None = None,
    cycle_width: int = 20,
) -> str:
    pins = list(default_pins(trace) if pins is None else sorted({int(pin) % 32 for pin in pins}))
    end = len(trace.records) if end is None else min(len(trace.records), end)
    start = max(0, start)
    if end <= start:
        end = min(len(trace.records), start + 1)
    records = trace.records[start:end]
    left = 155
    row_h = 32
    top = 48
    channels: list[tuple[str, str, int | None]] = []
    for pin in pins:
        channels.extend([(f"GPIO{pin}", "pin", pin), (f"GPIO{pin} OE", "dir", pin)])
    channels.extend([
        ("TX_FIFO level", "tx", None),
        ("TX_FIFO front", "txfront", None),
        ("RX_FIFO level", "rx", None),
        ("RX_FIFO front", "rxfront", None),
        ("IRQ flags", "irq", None),
        ("PC", "pc", None),
        ("Phase", "phase", None),
    ])
    width = left + len(records) * cycle_width + 16
    height = top + len(channels) * row_h + 34
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:system-ui,sans-serif;fill:#dfe9ff}.label{font-size:12px;font-weight:600}.axis{font-size:10px;fill:#93a7ca}.grid{stroke:#273752}.major{stroke:#53698f}.wave{fill:none;stroke:#81b9ff;stroke-width:2}.dir{fill:none;stroke:#78dba9;stroke-width:2}.fifo{fill:none;stroke:#f4cb72;stroke-width:2}.bus{fill:#1a2945;stroke:#4a6189}.stall{fill:#4c2832;stroke:#a95669}.bt{font-size:9px;text-anchor:middle;dominant-baseline:middle}</style>",
        f'<rect width="{width}" height="{height}" fill="#0e1527"/>',
    ]
    major = max(1, 10 ** max(0, len(str(max(1, len(records) // 8))) - 1))
    for i in range(len(records) + 1):
        cycle = start + i
        x = left + i * cycle_width
        is_major = cycle % major == 0
        if len(records) <= 250 or is_major:
            parts.append(f'<line x1="{x}" y1="{top-20}" x2="{x}" y2="{height-20}" class="{"major" if is_major else "grid"}"/>')
        if is_major and i < len(records):
            parts.append(f'<text x="{x+2}" y="15" class="axis">{html.escape(_format_time(cycle*trace.config.period_s))}</text>')
            parts.append(f'<text x="{x+2}" y="{height-6}" class="axis">{cycle}</text>')

    for row, (label, kind, pin) in enumerate(channels):
        y = top + row * row_h
        parts.append(f'<line x1="0" y1="{y+row_h}" x2="{width}" y2="{y+row_h}" class="grid"/>')
        parts.append(f'<text x="7" y="{y+row_h/2+4}" class="label">{html.escape(label)}</text>')
        if kind in {"pin", "dir"}:
            values: list[PinLevel | int]
            if kind == "pin":
                values = [record.pin_level(pin or 0, trace.config.default_input) for record in records]
            else:
                bit = 1 << int(pin or 0)
                values = [1 if record.pindirs & bit else 0 for record in records]
            hi, lo, mid = y + 7, y + row_h - 7, y + row_h / 2
            def yy(value: PinLevel | int) -> float:
                return hi if value == 1 else lo if value == 0 else mid
            d = ""
            for i, value in enumerate(values):
                x = left + i * cycle_width
                if i == 0:
                    d += f"M{x},{yy(value)}"
                else:
                    d += f" L{x},{yy(values[i-1])} L{x},{yy(value)}"
                d += f" L{x+cycle_width},{yy(value)}"
                if value in {"Z", "X"}:
                    parts.append(f'<text x="{x+cycle_width/2}" y="{mid+3}" class="axis" text-anchor="middle">{value}</text>')
            parts.append(f'<path d="{d}" class="{"wave" if kind == "pin" else "dir"}"/>')
        elif kind in {"tx", "rx"}:
            values = [record.tx_level if kind == "tx" else record.rx_level for record in records]
            capacity = trace.config.tx_capacity if kind == "tx" else trace.config.rx_capacity
            def fy(value: int) -> float:
                return y + row_h - 6 - ((value / capacity) * (row_h - 12) if capacity else 0)
            d = ""
            for i, value in enumerate(values):
                x = left + i * cycle_width
                if i == 0:
                    d += f"M{x},{fy(value)}"
                else:
                    d += f" L{x},{fy(values[i-1])} L{x},{fy(value)}"
                d += f" L{x+cycle_width},{fy(value)}"
            parts.append(f'<path d="{d}" class="fifo"/>')
        else:
            if kind == "pc":
                values = [str(record.pc) for record in records]
            elif kind == "txfront":
                values = [f"0x{record.tx_fifo[0]:08x}" if record.tx_fifo else "empty" for record in records]
            elif kind == "rxfront":
                values = [f"0x{record.rx_fifo[0]:08x}" if record.rx_fifo else "empty" for record in records]
            elif kind == "irq":
                values = [f"0b{record.irq_flags:08b}" for record in records]
            else:
                values = ["STALL" if record.stalled else record.phase for record in records]
            i = 0
            while i < len(values):
                j = i + 1
                while j < len(values) and values[j] == values[i]:
                    j += 1
                x = left + i * cycle_width
                w = (j - i) * cycle_width
                cls = "stall" if values[i] == "STALL" else "bus"
                parts.append(f'<rect x="{x}" y="{y+4}" width="{w}" height="{row_h-8}" rx="3" class="{cls}"/>')
                if w > 18:
                    parts.append(f'<text x="{x+w/2}" y="{y+row_h/2}" class="bt">{html.escape(values[i])}</text>')
                i = j
    parts.append("</svg>")
    return "".join(parts)


def render_json(trace: Trace, *, indent: int = 2) -> str:
    return json.dumps(trace.to_dict(), indent=indent)


def render_csv(trace: Trace, pins: Sequence[int] | None = None) -> str:
    pins = list(default_pins(trace) if pins is None else sorted({int(pin) % 32 for pin in pins}))
    output = io.StringIO()
    fields = [
        "cycle",
        "time_s",
        "pc",
        "instruction_pc",
        "pc_after",
        "phase",
        "stalled",
        "stall_reason",
        "instruction_word",
        "instruction",
        *[f"gpio{pin}" for pin in pins],
        *[f"gpio{pin}_oe" for pin in pins],
        "tx_level_after_host",
        "tx_fifo_after_host",
        "tx_level",
        "tx_fifo",
        "rx_level_after_host",
        "rx_fifo_after_host",
        "rx_level",
        "rx_fifo",
        "x",
        "y",
        "isr",
        "isr_count",
        "osr",
        "osr_count",
        "irq_flags",
        "delay_remaining",
        "exec_latch",
        "pending_kind",
        "halted_reason",
        "source_line",
        "events",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in trace.records:
        row: dict[str, Any] = {
            "cycle": record.cycle,
            "time_s": f"{record.time_s:.15g}",
            "pc": record.pc,
            "instruction_pc": "" if record.instruction_pc is None else record.instruction_pc,
            "pc_after": record.state_pc,
            "phase": record.phase,
            "stalled": int(record.stalled),
            "stall_reason": record.stall_reason,
            "instruction_word": "" if record.instruction_word is None else f"0x{record.instruction_word:04x}",
            "instruction": record.instruction,
            "tx_level_after_host": record.tx_level_after_host,
            "tx_fifo_after_host": " ".join(f"0x{value:08x}" for value in record.tx_fifo_after_host),
            "tx_level": record.tx_level,
            "tx_fifo": " ".join(f"0x{value:08x}" for value in record.tx_fifo),
            "rx_level_after_host": record.rx_level_after_host,
            "rx_fifo_after_host": " ".join(f"0x{value:08x}" for value in record.rx_fifo_after_host),
            "rx_level": record.rx_level,
            "rx_fifo": " ".join(f"0x{value:08x}" for value in record.rx_fifo),
            "x": f"0x{record.x:08x}",
            "y": f"0x{record.y:08x}",
            "isr": f"0x{record.isr:08x}",
            "isr_count": record.isr_count,
            "osr": f"0x{record.osr:08x}",
            "osr_count": record.osr_count,
            "irq_flags": f"0x{record.irq_flags:02x}",
            "delay_remaining": record.delay_remaining,
            "exec_latch": "" if record.exec_latch is None else f"0x{record.exec_latch:04x}",
            "pending_kind": "" if record.pending_kind is None else record.pending_kind,
            "halted_reason": "" if record.halted_reason is None else record.halted_reason,
            "source_line": "" if record.source_line is None else record.source_line,
            "events": "; ".join(record.events),
        }
        for pin in pins:
            row[f"gpio{pin}"] = record.pin_level(pin, trace.config.default_input)
            row[f"gpio{pin}_oe"] = 1 if record.pindirs & (1 << pin) else 0
        writer.writerow(row)
    return output.getvalue()


def render_vcd(trace: Trace, pins: Sequence[int] | None = None) -> str:
    pins = list(default_pins(trace) if pins is None else sorted({int(pin) % 32 for pin in pins}))
    identifiers = _vcd_identifiers()
    signals: list[tuple[str, int, str]] = []
    for pin in pins:
        signals.append((f"gpio{pin}", 1, next(identifiers)))
        signals.append((f"gpio{pin}_oe", 1, next(identifiers)))
    for name, width in [
        ("tx_fifo_level", 4),
        ("tx_fifo_front", 32),
        ("rx_fifo_level", 4),
        ("rx_fifo_front", 32),
        ("pc", 5),
        ("irq_flags", 8),
        ("x", 32),
        ("y", 32),
        ("isr", 32),
        ("osr", 32),
    ]:
        signals.append((name, width, next(identifiers)))
    ids = {name: identifier for name, _width, identifier in signals}
    lines = [
        "$date generated by pico-pio-trace $end",
        "$version pico-pio-trace 0.6.1 $end",
        "$timescale 1 ns $end",
        "$scope module pio $end",
    ]
    for name, width, identifier in signals:
        lines.append(f"$var wire {width} {identifier} {name} $end")
    lines.extend(["$upscope $end", "$enddefinitions $end"])

    previous: dict[str, str] = {}
    last_time = -1
    for record in trace.records:
        timestamp = int(round(record.time_s * 1e9))
        if timestamp <= last_time:
            timestamp = last_time + 1
        last_time = timestamp
        changes: list[str] = []
        for pin in pins:
            level = record.pin_level(pin, trace.config.default_input)
            bit_value = "z" if level == "Z" else "x" if level == "X" else str(level)
            changes.extend(_vcd_change(f"gpio{pin}", bit_value, 1, ids, previous))
            oe = "1" if record.pindirs & (1 << pin) else "0"
            changes.extend(_vcd_change(f"gpio{pin}_oe", oe, 1, ids, previous))
        for name, value, width in (
            ("tx_fifo_level", record.tx_level, 4),
            ("tx_fifo_front", record.tx_fifo[0] if record.tx_fifo else 0, 32),
            ("rx_fifo_level", record.rx_level, 4),
            ("rx_fifo_front", record.rx_fifo[0] if record.rx_fifo else 0, 32),
            ("pc", record.pc, 5),
            ("irq_flags", record.irq_flags, 8),
            ("x", record.x, 32),
            ("y", record.y, 32),
            ("isr", record.isr, 32),
            ("osr", record.osr, 32),
        ):
            changes.extend(_vcd_change(name, format(int(value) & ((1 << width) - 1), f"0{width}b"), width, ids, previous))
        if changes:
            lines.append(f"#{timestamp}")
            lines.extend(changes)
    return "\n".join(lines) + "\n"


def write_trace(
    path: str | Path,
    trace: Trace,
    pins: Sequence[int] | None = None,
    *,
    trace_options: Sequence[HtmlTraceOption] | None = None,
) -> Path:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        content = render_html(trace, pins, trace_options=trace_options)
    elif suffix == ".svg":
        content = render_svg(trace, pins)
    elif suffix == ".json":
        content = render_json(trace)
    elif suffix == ".csv":
        content = render_csv(trace, pins)
    elif suffix == ".vcd":
        content = render_vcd(trace, pins)
    else:
        raise ValueError(f"unsupported output extension {suffix!r}; use .html, .svg, .json, .csv, or .vcd")
    path.write_text(content, encoding="utf-8", newline="")
    return path


def _vcd_change(name: str, value: str, width: int, ids: dict[str, str], previous: dict[str, str]) -> list[str]:
    if previous.get(name) == value:
        return []
    previous[name] = value
    identifier = ids[name]
    if width == 1:
        return [f"{value}{identifier}"]
    return [f"b{value} {identifier}"]


def _vcd_identifiers() -> Iterable[str]:
    alphabet = [chr(code) for code in range(33, 127)]
    length = 1
    while True:
        if length == 1:
            for char in alphabet:
                yield char
        else:
            indices = [0] * length
            while True:
                yield "".join(alphabet[index] for index in indices)
                pos = length - 1
                while pos >= 0:
                    indices[pos] += 1
                    if indices[pos] < len(alphabet):
                        break
                    indices[pos] = 0
                    pos -= 1
                if pos < 0:
                    break
        length += 1


def _format_time(seconds: float) -> str:
    value = abs(seconds)
    if value == 0:
        return "0 s"
    for scale, suffix in ((1, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns"), (1e-12, "ps")):
        if value >= scale:
            return f"{seconds / scale:.5g} {suffix}"
    return f"{seconds:.3e} s"


def _format_frequency(hz: float) -> str:
    for scale, suffix in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"), (1, "Hz")):
        if abs(hz) >= scale:
            return f"{hz / scale:.6g} {suffix}"
    return f"{hz:.6g} Hz"
