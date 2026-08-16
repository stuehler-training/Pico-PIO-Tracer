from __future__ import annotations

from collections import deque

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.model import StimulusEvent
from pico_pio_trace.parser import parse_source


def test_fifo_capacities_for_join_modes():
    templates = [
        ("rp2.PIO.JOIN_NONE", 4, 4),
        ("rp2.PIO.JOIN_TX", 8, 0),
        ("rp2.PIO.JOIN_RX", 0, 8),
    ]
    for join, tx, rx in templates:
        parsed = parse_source(
            f"""
import rp2
@rp2.asm_pio(fifo_join={join})
def p():
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
        )
        config = parsed.choose()
        assert (config.tx_capacity, config.rx_capacity) == (tx, rx)


def test_blocking_pull_stalls_until_timed_host_put():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    pull()
    mov(x, osr)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(5, [StimulusEvent(2, "tx_put", 0x1234)])
    assert trace.records[0].stalled and trace.records[1].stalled
    assert not trace.records[2].stalled
    assert trace.records[3].x == 0x1234


def test_nonblocking_pull_copies_x_when_tx_empty(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 13)
    pull(noblock)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        2,
    )
    assert trace.records[1].osr == 13
    assert trace.records[1].osr_count == 0


def test_pull_ifempty_is_noop_when_osr_below_threshold(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio(pull_thresh=8)
def p():
    mov(osr, null)
    pull(ifempty, block)
    set(x, 3)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert not trace.records[1].stalled
    assert trace.records[2].x == 3


def test_blocking_push_stalls_when_rx_full():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    mov(isr, x)
    push()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    emulator.rx_fifo = deque([1, 2, 3, 4])
    trace = emulator.run(3)
    assert trace.records[1].stalled and trace.records[2].stalled
    assert trace.records[2].isr == 0
    # MOV ISR wrote X=0 and reset count; PUSH holds it rather than clearing on stall.
    assert len(trace.records[2].rx_fifo) == 4


def test_nonblocking_push_full_drops_and_clears_isr():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 7)
    mov(isr, x)
    push(noblock)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    emulator.rx_fifo = deque([1, 2, 3, 4])
    trace = emulator.run(3)
    assert not trace.records[2].stalled
    assert trace.records[2].isr == 0 and trace.records[2].isr_count == 0
    assert tuple(trace.records[2].rx_fifo) == (1, 2, 3, 4)
    assert any("drops" in event for event in trace.records[2].events)


def test_push_iffull_is_noop_below_threshold(run_source):
    _emulator, trace = run_source(
        """
import rp2
@rp2.asm_pio(push_thresh=8)
def p():
    in_(null, 4)
    push(iffull)
    set(x, 5)
sm=rp2.StateMachine(0,p,freq=1_000_000)
""",
        3,
    )
    assert trace.records[1].rx_level == 0
    assert trace.records[1].isr_count == 4
    assert trace.records[2].x == 5


def test_autopull_prefetch_stalls_out_one_cycle_then_executes():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=32, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    out(x, 32)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0x11223344)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(2)
    assert trace.records[0].stalled
    assert trace.records[0].x == 0
    assert trace.records[0].osr == 0x11223344
    assert trace.records[1].x == 0x11223344


def test_post_out_autopull_refills_same_cycle_without_extra_stall():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=8, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    out(x, 8)
    out(y, 8)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0x000000AA)
sm.put(0x000000BB)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(3)
    assert trace.records[0].stalled
    assert trace.records[1].x == 0xAA
    assert trace.records[1].osr == 0xBB and trace.records[1].osr_count == 0
    assert trace.records[2].y == 0xBB


def test_autopull_can_refill_on_non_out_cycle_asynchronously():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=32)
def p():
    nop()
    mov(x, osr)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0xCAFEBABE)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(2)
    assert trace.records[0].osr == 0xCAFEBABE
    assert not trace.records[0].stalled
    assert trace.records[1].x == 0xCAFEBABE


def test_autopush_pushes_at_threshold_and_clears_isr():
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(autopush=True, push_thresh=2, in_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    in_(pins, 1)
    in_(pins, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000,in_base=Pin(0))
"""
    )
    trace = PIOEmulator(parsed.choose()).run(
        2,
        [StimulusEvent(0, "pin", 1, pin=0), StimulusEvent(1, "pin", 0, pin=0)],
    )
    assert trace.records[1].rx_fifo == (0b10,)
    assert trace.records[1].isr == 0 and trace.records[1].isr_count == 0


def test_autopush_full_stall_does_not_shift_same_input_twice():
    parsed = parse_source(
        """
import rp2
from machine import Pin
@rp2.asm_pio(autopush=True, push_thresh=1, in_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    in_(pins, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000,in_base=Pin(0))
"""
    )
    emulator = PIOEmulator(parsed.choose())
    emulator.rx_fifo = deque([10, 11, 12, 13])
    trace = emulator.run(
        4,
        [StimulusEvent(0, "pin", 1, pin=0), StimulusEvent(2, "rx_get")],
    )
    assert trace.records[0].stalled and trace.records[0].isr == 1 and trace.records[0].isr_count == 1
    assert trace.records[1].stalled and trace.records[1].isr == 1 and trace.records[1].isr_count == 1
    assert not trace.records[2].stalled
    assert trace.records[2].rx_fifo[-1] == 1
    assert trace.records[2].isr_count == 0


def test_join_tx_disables_rx_and_autopush_stalls_forever():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(fifo_join=rp2.PIO.JOIN_TX, autopush=True, push_thresh=1)
def p():
    in_(null, 1)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(3)
    assert all(record.stalled for record in trace.records)
    assert all(record.rx_level == 0 for record in trace.records)


def test_join_rx_disables_tx_and_blocking_pull_stalls():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(fifo_join=rp2.PIO.JOIN_RX)
def p():
    pull()
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(1)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(2)
    assert all(record.stalled for record in trace.records)
    assert trace.records[0].tx_level == 0
    assert emulator.host_tx_queue


def test_official_style_auto_push_pull_sequence():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=32, autopush=True, push_thresh=32,
             out_shiftdir=rp2.PIO.SHIFT_RIGHT, in_shiftdir=rp2.PIO.SHIFT_LEFT)
def p():
    out(x, 32)
    in_(x, 32)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(1)
sm.put(2)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(6)
    assert trace.records[0].stalled and trace.records[0].osr == 1
    assert trace.records[1].x == 1 and trace.records[1].osr == 2
    assert trace.records[2].rx_fifo == (1,)
    assert trace.records[3].x == 2
    assert trace.records[4].rx_fifo == (1, 2)
    assert trace.records[5].stalled


def test_blocked_host_puts_complete_as_fifo_space_appears():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    pull()
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(1)
sm.put(2)
sm.put(3)
sm.put(4)
sm.put(5)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(3)
    assert trace.records[0].tx_fifo == (2, 3, 4)
    assert trace.records[1].tx_fifo == (3, 4, 5)
    assert any("blocked host TX put completes" in event for event in trace.records[1].events)


def test_timed_rx_get_exposes_received_word():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    set(x, 9)
    mov(isr, x)
    push()
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(4, [StimulusEvent(3, "rx_get")])
    assert emulator.host_rx_values == [9]
    assert trace.records[3].rx_level == 0

def test_autopull_plain_pull_replaces_a_partially_consumed_osr():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=32, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def p():
    out(null, 8)
    pull()
    mov(x, osr)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0x11223344)
sm.put(0xA5A5A5A5)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(4)
    assert trace.records[0].stalled and trace.records[0].osr == 0x11223344
    assert trace.records[1].osr_count == 8
    assert trace.records[2].osr == 0xA5A5A5A5
    assert trace.records[3].x == 0xA5A5A5A5


def test_autopull_plain_pull_is_a_fence_when_osr_is_full():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(autopull=True, pull_thresh=32)
def p():
    pull()
    mov(x, osr)
sm=rp2.StateMachine(0,p,freq=1_000_000)
sm.put(0x12345678)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(3)
    # On the first non-OUT cycle, asynchronous autopull fills the OSR and the
    # explicit PULL observes a full OSR, so it does not consume another word.
    assert trace.records[0].osr == 0x12345678
    assert trace.records[0].tx_level == 0
    assert trace.records[1].x == 0x12345678



def test_debug_rx_injection_fills_fifo_and_host_get_removes_front():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(
        4,
        [
            StimulusEvent(1, "rx_put", 0x11223344),
            StimulusEvent(1, "rx_fill", 0xAABBCCDD),
            StimulusEvent(2, "rx_get"),
        ],
    )
    assert trace.records[1].rx_fifo == (0x11223344, 0xAABBCCDD)
    assert trace.records[2].rx_fifo == (0xAABBCCDD,)
    assert emulator.host_rx_values == [0x11223344]
    assert "debug RX inject 0x11223344" in trace.records[1].events
    assert "host RX get -> 0x11223344" in trace.records[2].events


def test_debug_rx_injection_drops_words_beyond_capacity_with_warning():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    events = [StimulusEvent(0, "rx_put", value) for value in range(1, 6)]
    trace = PIOEmulator(parsed.choose()).run(1, events)
    assert trace.records[0].rx_fifo == (1, 2, 3, 4)
    assert any("0x00000005 dropped; RX FIFO is full" in event for event in trace.records[0].events)
    assert any("RX FIFO is full" in warning for warning in trace.warnings)


def test_debug_rx_injection_is_ignored_when_rx_fifo_is_join_disabled():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio(fifo_join=rp2.PIO.JOIN_TX)
def p():
    nop()
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    trace = PIOEmulator(parsed.choose()).run(1, [StimulusEvent(0, "rx_put", 0x55)])
    assert trace.records[0].rx_fifo == ()
    assert any("RX FIFO is disabled" in event for event in trace.records[0].events)


def test_rx_injection_precedes_pio_push_and_host_read_can_unblock_it():
    parsed = parse_source(
        """
import rp2
@rp2.asm_pio()
def p():
    mov(isr, x)
    push(block)
sm=rp2.StateMachine(0,p,freq=1_000_000)
"""
    )
    events = [StimulusEvent(1, "rx_put", value) for value in (1, 2, 3, 4)]
    events.append(StimulusEvent(2, "rx_get"))
    emulator = PIOEmulator(parsed.choose())
    trace = emulator.run(3, events)
    assert trace.records[1].stalled
    assert trace.records[1].rx_fifo == (1, 2, 3, 4)
    assert not trace.records[2].stalled
    assert trace.records[2].rx_fifo == (2, 3, 4, 0)
    assert emulator.host_rx_values == [1]
