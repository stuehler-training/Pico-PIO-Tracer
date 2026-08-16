from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.model import StimulusEvent
from pico_pio_trace.parser import parse_source


NODE = shutil.which("node")
ROOT = Path(__file__).parents[1]
SIMULATOR = ROOT / "pico_pio_trace" / "web" / "simulator.js"
RUNNER = Path(__file__).with_name("browser_sim_runner.cjs")


def _case(name: str, source: str, cycles: int, stimuli: list[StimulusEvent] | None = None, **overrides):
    config = parse_source(source).choose()
    if overrides:
        config = replace(config, **overrides)
    emulator = PIOEmulator(config)
    stimulus_list = list(stimuli or [])
    trace = emulator.run(cycles, stimulus_list)
    return {
        "name": name,
        "model": trace.simulation_dict(),
        "cycles": cycles,
        "stimuli": [asdict(event) for event in stimulus_list],
        "expected": {
            "initial_state": dict(trace.initial_state),
            "records": [record.to_dict() for record in trace.records],
            "warnings": trace.warnings,
            "host_rx_values": emulator.host_rx_values,
        },
    }


def _differential_cases():
    return [
        _case(
            "wait_set_sideset",
            """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=rp2.PIO.OUT_LOW, sideset_init=rp2.PIO.OUT_LOW)
def p():
    wait(1, gpio, 2).side(0)
    set(pins, 1).side(1)[2]
    wait(0, gpio, 2).side(0)
    set(pins, 0).side(1)
sm=rp2.StateMachine(0, p, freq=1_000_000, set_base=Pin(3), sideset_base=Pin(4))
""",
            24,
            [StimulusEvent(3, "pin", 1, pin=2), StimulusEvent(12, "pin", 0, pin=2)],
        ),
        _case(
            "jmp_mov_delay",
            """
import rp2
@rp2.asm_pio()
def p():
    set(x, 3)
    set(y, 1)
    label("loop")
    mov(isr, invert(x))
    mov(osr, reverse(isr))
    jmp(x_dec, "loop")
    jmp(x_not_y, "done")
    set(y, 7)
    label("done")
    nop()[1]
sm=rp2.StateMachine(1, p, freq=2_000_000)
""",
            35,
        ),
        _case(
            "in_autopush_joined_rx",
            """
import rp2
from machine import Pin
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_RIGHT, autopush=True, push_thresh=8, fifo_join=rp2.PIO.JOIN_RX)
def p():
    label("loop")
    in_(pins, 4)
    in_(x, 4)
    jmp("loop")
sm=rp2.StateMachine(0, p, freq=1_000_000, in_base=Pin(5))
""",
            36,
            [
                StimulusEvent(0, "pin", 1, pin=5),
                StimulusEvent(0, "pin", 0, pin=6),
                StimulusEvent(0, "pin", 1, pin=7),
                StimulusEvent(0, "pin", 1, pin=8),
                StimulusEvent(15, "rx_get"),
                StimulusEvent(25, "rx_get", shift=4),
            ],
        ),
        _case(
            "out_autopull_joined_tx",
            """
import rp2
from machine import Pin
@rp2.asm_pio(out_init=(rp2.PIO.OUT_LOW,) * 4, out_shiftdir=rp2.PIO.SHIFT_RIGHT,
             autopull=True, pull_thresh=8, fifo_join=rp2.PIO.JOIN_TX)
def p():
    label("loop")
    out(pins, 4)
    out(x, 4)
    mov(pins, x)
    jmp("loop")
sm=rp2.StateMachine(0, p, freq=1_000_000, out_base=Pin(10))
sm.put(0x12345678)
sm.put(0x89abcdef)
""",
            45,
            [StimulusEvent(10, "tx_put", 0x0F1E2D3C), StimulusEvent(11, "tx_put", 0x99887766)],
        ),
        _case(
            "explicit_push_pull_and_host_queues",
            """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    pull(block)
    out(x, 8)
    mov(isr, x)
    push(block)
    pull(noblock)
    nop()
sm=rp2.StateMachine(0, p, freq=1_000_000)
""",
            28,
            [
                StimulusEvent(3, "tx_put", 0xA5),
                StimulusEvent(10, "rx_get"),
                StimulusEvent(14, "tx_put", 0x5A),
                StimulusEvent(20, "rx_get"),
            ],
        ),
        _case(
            "irq_wait_relative",
            """
import rp2
@rp2.asm_pio()
def p():
    irq(block, rel(1))
    wait(1, irq, rel(2))
    irq(clear, rel(2))
    nop()
sm=rp2.StateMachine(3, p, freq=1_000_000)
""",
            22,
            [StimulusEvent(4, "irq_clear", index=0), StimulusEvent(9, "irq_set", index=1)],
        ),
        _case(
            "exec_and_initial_exec",
            """
import rp2
@rp2.asm_pio()
def p():
    set(x, 1)
    mov(exec, x)
    pull(block)
    out(exec, 16)
    nop()
sm=rp2.StateMachine(0, p, freq=1_000_000)
sm.exec("set(y, 7)")
sm.put(0xe022)
""",
            20,
        ),
        _case(
            "pindirs_wait_pin_and_jmp_pin",
            """
import rp2
from machine import Pin
@rp2.asm_pio(set_init=(rp2.PIO.OUT_LOW,) * 3, out_init=(rp2.PIO.OUT_LOW,) * 3)
def p():
    set(pindirs, 0b101)
    wait(1, pin, 1)
    out(pindirs, 3)
    jmp(pin, "high")
    set(x, 4)
    label("high")
    set(y, 5)
sm=rp2.StateMachine(0, p, freq=1_000_000, in_base=Pin(30), set_base=Pin(31), out_base=Pin(31), jmp_pin=Pin(6))
sm.put(0b010)
""",
            24,
            [StimulusEvent(4, "pin", 1, pin=31), StimulusEvent(8, "pin", 1, pin=6)],
        ),
        _case(
            "manual_rx_injection",
            """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm=rp2.StateMachine(0, p, freq=1_000_000)
""",
            8,
            [
                StimulusEvent(1, "rx_put", 0x11223344),
                StimulusEvent(1, "rx_fill", 0xAABBCCDD),
                StimulusEvent(3, "rx_get"),
                StimulusEvent(4, "rx_put", 1),
                StimulusEvent(4, "rx_put", 2),
                StimulusEvent(4, "rx_put", 3),
                StimulusEvent(4, "rx_put", 4),
            ],
        ),
        _case(
            "mov_status",
            """
import rp2
@rp2.asm_pio()
def p():
    mov(x, status)
    push(noblock)
    nop()
sm=rp2.StateMachine(0, p, freq=1_000_000)
""",
            12,
            status_mode="constant_one",
        ),
    ]


@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser/Python differential testing")
def test_browser_emulator_matches_python_cycle_by_cycle():
    cases = _differential_cases()
    serialised = json.loads(json.dumps(cases))
    result = subprocess.run(
        [NODE, str(RUNNER), str(SIMULATOR)],
        input=json.dumps([{key: case[key] for key in ("model", "cycles", "stimuli")} for case in serialised]),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    for case, browser_result in zip(serialised, actual, strict=True):
        assert browser_result == case["expected"], case["name"]


def test_trace_serialises_browser_model_and_stimuli():
    case = _differential_cases()[0]
    assert case["model"]["schema_version"] == 1
    assert case["model"]["program"]["instructions"][0]["op"] == "wait"
    assert case["model"]["config"]["period_s"] == 1e-6
    assert case["stimuli"][0]["pin"] == 2

@pytest.mark.skipif(NODE is None, reason="Node.js is required for browser/Python differential testing")
def test_browser_emulator_matches_python_with_randomised_host_events():
    import random

    rng = random.Random(0x2040_2026)
    cases = []
    for iteration in range(18):
        mode = iteration % 3
        if mode == 0:
            source = """
import rp2
from machine import Pin
@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    label("loop")
    wait(1, gpio, 2)
    in_(pins, 4)
    wait(0, gpio, 2)
    mov(x, isr)
    jmp("loop")
sm=rp2.StateMachine(0, p, freq=3_000_000, in_base=Pin(4))
"""
            events = [
                StimulusEvent(
                    rng.randrange(0, 120),
                    "pin",
                    rng.choice([0, 1, None]),
                    pin=rng.choice([2, 4, 5, 6, 7]),
                )
                for _ in range(28)
            ]
        elif mode == 1:
            source = """
import rp2
@rp2.asm_pio(out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    label("loop")
    pull(noblock)
    out(x, 5)
    mov(isr, x)
    push(noblock)
    jmp("loop")
sm=rp2.StateMachine(0, p, freq=4_000_000)
"""
            events = []
            for _ in range(30):
                cycle = rng.randrange(0, 120)
                if rng.random() < 0.58:
                    events.append(StimulusEvent(cycle, "tx_put", rng.getrandbits(32), shift=rng.randrange(0, 3)))
                else:
                    events.append(StimulusEvent(cycle, "rx_get", shift=rng.randrange(0, 5)))
        else:
            source = """
import rp2
@rp2.asm_pio()
def p():
    label("loop")
    wait(1, irq, 0)
    irq(clear, 1)
    irq(block, 2)
    nop()[1]
    jmp("loop")
sm=rp2.StateMachine(2, p, freq=2_500_000)
"""
            events = [
                StimulusEvent(
                    rng.randrange(0, 120),
                    rng.choice(["irq_set", "irq_clear"]),
                    index=rng.randrange(0, 4),
                )
                for _ in range(35)
            ]
        # The Python stimulus loader performs a stable cycle sort. Match that
        # order here while retaining same-cycle insertion ordering.
        events = sorted(events, key=lambda event: event.cycle)
        cases.append(_case(f"random_{iteration}", source, 120, events))

    serialised = json.loads(json.dumps(cases))
    result = subprocess.run(
        [NODE, str(RUNNER), str(SIMULATOR)],
        input=json.dumps([{key: case[key] for key in ("model", "cycles", "stimuli")} for case in serialised]),
        text=True,
        capture_output=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    for case, browser_result in zip(serialised, actual, strict=True):
        assert browser_result == case["expected"], case["name"]
