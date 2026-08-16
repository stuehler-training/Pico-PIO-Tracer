from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from pico_pio_trace.cli import parse_pin_spec
from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.parser import parse_source
from pico_pio_trace.render import HtmlTraceOption, render_csv, render_html, render_json, render_svg, render_vcd, write_trace
from pico_pio_trace.stimulus import parse_stimulus


def _trace():
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 1)[1]
    set(pins, 0)
sm=rp2.StateMachine(0,p,freq=1_000_000,set_base=Pin(2))
"""
    )
    return PIOEmulator(parsed.choose()).run(5)


def test_html_contains_interactive_waveform_and_fifo_inspector():
    text = render_html(_trace(), [2])
    assert "PIO logic trace" in text
    assert '<svg id="wave"' in text
    assert "TX_FIFO level" in text and "RX_FIFO level" in text
    assert "Inspect cycle" in text
    assert "Interactive input editor" in text
    assert "Cycle debugger" in text
    assert "PIO disassembly" in text
    assert 'id="continue-breakpoint"' in text
    assert 'id="pio-disassembly"' in text
    assert 'id="clear-breakpoints"' in text
    assert 'id="step-back"' in text and 'id="step-forward"' in text
    assert 'id="source-code"' in text
    assert "window.__PIO_EMBEDDED_TRACE__=" in text
    assert "window.__PIO_TRACE_APP__" in text
    assert "blog.stuehler-training.de" in text
    assert "GPL-3.0-or-later" in text
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "__GPL_LICENSE_TEXT__" not in text


def test_html_embeds_multiple_selectable_pio_functions():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def first():
    nop()
@rp2.asm_pio()
def second():
    wait(1, irq, 0)
sm0 = rp2.StateMachine(0, first, freq=1_000_000)
sm1 = rp2.StateMachine(1, second, freq=2_000_000)
""",
        source_path="multi.py",
    )
    first = PIOEmulator(parsed.choose(program_name="first")).run(4)
    second = PIOEmulator(parsed.choose(program_name="second")).run(4)
    text = render_html(
        first,
        [0],
        trace_options=[HtmlTraceOption(first, [0]), HtmlTraceOption(second, [1])],
    )
    payload = re.search(
        r"window\.__PIO_EMBEDDED_TRACES__=(.*?);\nwindow\.__PIO_TRACE_VERSION__",
        text,
        re.DOTALL,
    )
    assert payload is not None
    catalog = json.loads(payload.group(1))
    assert catalog["selected"] == "first_sm0"
    assert [(item["program"], item["sm_id"]) for item in catalog["options"]] == [("first", 0), ("second", 1)]
    assert 'id="program-select"' in text
    assert 'id="irq-set"' in text and 'id="irq-clear"' in text
    assert 'id="irq-events"' in text


def test_html_embeds_source_without_allowing_a_source_string_to_close_the_script_tag():
    source = r'''
import rp2
marker = "</script><script>window.source_injection = true</script> __VIEWER_JS__ __STYLES__"
@rp2.asm_pio()
def p():
    nop()
sm = rp2.StateMachine(0, p, freq=1_000_000)
'''
    trace = PIOEmulator(parse_source(source, source_path="hostile.py").choose()).run(1)
    text = render_html(trace, [0])
    payload = text.split("window.__PIO_EMBEDDED_TRACE__=", 1)[1].split(";\nwindow.__PIO_DISPLAY_PINS__", 1)[0]
    assert r"<\/script>" in payload
    assert "</script><script>window.source_injection" not in payload
    embedded_source = json.loads(payload)["simulation"]["program"]["source_text"]
    assert embedded_source == source
    assert "__VIEWER_JS__ __STYLES__" in embedded_source


def test_static_svg_has_signal_and_fifo_rows():
    text = render_svg(_trace(), [2])
    assert text.startswith("<svg")
    assert "GPIO2" in text
    assert "TX_FIFO level" in text
    assert "class=\"wave\"" in text


def test_csv_has_pin_fifo_register_and_event_columns():
    text = render_csv(_trace(), [2])
    header = text.splitlines()[0]
    assert "gpio2" in header
    assert "tx_fifo" in header and "rx_fifo" in header
    assert "osr_count" in header
    assert len(text.splitlines()) == 6


def test_json_is_machine_readable_and_contains_metadata():
    payload = json.loads(render_json(_trace()))
    assert payload["metadata"]["program"] == "p"
    assert payload["metadata"]["cycles"] == 5
    assert payload["records"][0]["tx_level_after_host"] == 0
    assert payload["records"][0]["rx_level_after_host"] == 0
    assert payload["records"][0]["tx_level"] == 0


def test_vcd_has_standard_header_and_fifo_signals():
    text = render_vcd(_trace(), [2])
    assert "$timescale 1 ns $end" in text
    assert "gpio2" in text
    assert "tx_fifo_level" in text
    assert "$enddefinitions $end" in text
    assert "#0" in text


def test_write_trace_selects_format_from_extension(tmp_path):
    trace = _trace()
    for suffix in [".html", ".svg", ".json", ".csv", ".vcd"]:
        path = tmp_path / f"trace{suffix}"
        assert write_trace(path, trace, [2]) == path
        assert path.stat().st_size > 50


def test_pin_spec_ranges_and_reverse_ranges():
    assert parse_pin_spec("0-2,5,7-6") == [0, 1, 2, 5, 6, 7]


def test_stimulus_parser_accepts_grouped_and_hex_values():
    events = parse_stimulus(
        {
            "pins": [{"cycle": 1, "pin": 2, "value": 1}],
            "tx": [{"cycle": 2, "value": "0x55"}],
            "rx_put": [{"cycle": 3, "value": "0xaabbccdd"}],
            "rx_get": [4],
            "irq": [{"cycle": 5, "index": 1, "value": 0}],
        }
    )
    assert [event.kind for event in events] == ["pin", "tx_put", "rx_put", "rx_get", "irq"]
    assert events[1].value == 0x55
    assert events[2].value == 0xAABBCCDD


def test_cli_end_to_end_writes_html(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text(
        """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW)
def p():
    set(pins, 1)[1]
    set(pins, 0)[1]
sm=rp2.StateMachine(0,p,freq=1000,set_base=Pin(1))
""",
        encoding="utf-8",
    )
    output = tmp_path / "trace.html"
    result = subprocess.run(
        [sys.executable, "-m", "pico_pio_trace", str(source), "--cycles", "8", "--pins", "1", "-o", str(output)],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.exists()
    assert "Wrote" in result.stdout
    assert "TX_FIFO level" in output.read_text(encoding="utf-8")


def test_cli_list_programs(tmp_path):
    source = tmp_path / "demo.py"
    source.write_text("""
import rp2
@rp2.asm_pio()
def p():
    nop()
""", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pico_pio_trace", str(source), "--list"],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "p: 1 instruction" in result.stdout


def test_cli_html_embeds_all_programs_and_uses_program_as_initial_selection(tmp_path):
    source = tmp_path / "multi.py"
    source.write_text(
        """
import rp2
@rp2.asm_pio()
def alpha():
    nop()
@rp2.asm_pio()
def beta():
    wait(1, irq, 3)
sm0 = rp2.StateMachine(0, alpha, freq=1_000_000)
sm1 = rp2.StateMachine(1, beta, freq=2_000_000)
""",
        encoding="utf-8",
    )
    output = tmp_path / "multi.html"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pico_pio_trace",
            str(source),
            "--program",
            "beta",
            "--cycles",
            "4",
            "-o",
            str(output),
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    payload = re.search(
        r"window\.__PIO_EMBEDDED_TRACES__=(.*?);\nwindow\.__PIO_TRACE_VERSION__",
        text,
        re.DOTALL,
    )
    assert payload is not None
    catalog = json.loads(payload.group(1))
    assert catalog["selected"] == "beta_sm1"
    assert {item["program"] for item in catalog["options"]} == {"alpha", "beta"}
