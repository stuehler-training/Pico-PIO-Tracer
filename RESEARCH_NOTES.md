# Research and semantic verification notes

Research snapshot: **22 July 2026**; release provenance and licensing references refreshed **16 August 2026**. The implementation targets **RP2040 PIO v0** as used by Raspberry Pi Pico/Pico W and the MicroPython `rp2.asm_pio` interface.

## Independent verification tracks

The work was divided into three independent tracks and reconciled only after each had produced its own interpretation.

### Track A — hardware semantics

Primary source: the Raspberry Pi **RP2040 Datasheet**, PIO chapter and instruction reference.

This track established the hardware model independently of MicroPython syntax:

- nine 16-bit instruction classes;
- one base cycle per instruction;
- the shared five-bit delay/side-set field;
- PC/wrap behavior;
- X, Y, ISR, OSR, and their shift counters;
- FIFO capacities and join modes;
- pin mapping and side-set precedence;
- blocking/stall semantics;
- automatic push/pull pseudocode;
- forced EXEC timing;
- relative IRQ mapping.

### Track B — MicroPython dialect and configuration

Primary sources: MicroPython documentation and the MicroPython v1.28.0 `ports/rp2/modules/rp2.py` and `ports/rp2/rp2_pio.c` implementations.

This track established what a real MicroPython source file means:

- the `asm_pio` decorator arguments and defaults;
- the exact assembler encodings and aliases;
- implicit wrap insertion;
- automatic optional side-set inference;
- pin initialiser interpretation and pin-group counts;
- state-machine constructor/init arguments;
- v1.28.0 16.8 divider calculation and range checks;
- `put`, `get`, FIFO-level, and `exec` API behavior;
- the standalone `asm_pio_encode` path used by `StateMachine.exec(str)`.

The differential encoder tests use an independently extracted reference implementation from MicroPython v1.28.0 `rp2.py`. They do not call the project's encoder to construct expected words.
The extracted reference and the reduced official-example fixtures remain under MicroPython's MIT license; see `THIRD_PARTY_NOTICES.md`.

### Track C — examples and independent cross-checks

Sources: official MicroPython v1.28.0 RP2 examples and two independent RP2040 PIO emulator projects, plus focused Raspberry Pi issue/forum investigations for ambiguous timing.

Official example dialects covered:

- 1 Hz LED/IRQ loop;
- explicit `StateMachine.exec`;
- pin-change wait/relative IRQ;
- PWM with nonblocking PULL and EXEC preload;
- compact and framing-aware UART RX;
- multi-UART TX;
- WS2812 timing with constants, side-set, and autopull.

Independent emulator material was used as a disagreement detector, not as the final authority. Where projects differed, the RP2040 datasheet pseudocode and timing figures were used to resolve the behavior.

## Language model

### Program resources

A state machine has:

- a five-bit program counter into 32-word PIO instruction memory;
- 32-bit scratch registers X and Y;
- a 32-bit ISR and OSR;
- saturating input and output shift counters;
- a TX FIFO written by the host and read by PULL/autopull;
- an RX FIFO written by PUSH/autopush and read by the host;
- pin mapping configuration and four PIO IRQ flags per PIO block (eight internal flags are addressable by instructions).

Each unjoined FIFO is four words deep. `JOIN_TX` creates an eight-word TX FIFO and disables RX; `JOIN_RX` creates an eight-word RX FIFO and disables TX.

### 16-bit format

Bits 15:13 select one of the eight top-level opcode values. PUSH and PULL share one opcode. Bits 12:8 are allocated between delay and side-set. Bits 7:0 contain operation-specific fields.

MicroPython infers optional side-set when a program has configured side-set pins but at least one instruction omits `.side()`. The optional enable consumes one of the five shared bits, leaving fewer delay bits.

### Instruction-cycle rule

A normal instruction has one execution cycle followed by its encoded delay cycles. A stalled instruction remains current and applies side-set on every attempted cycle. When a main instruction and side-set touch the same pin, side-set wins for that cycle.

The trace records the state at the end of each cycle. `pc` is the PC from which the cycle's program instruction was fetched; during an EXEC cycle it is the suspended program PC.

## Resolved edge cases

### Automatic pull before OUT

The most consequential ambiguity was whether an eligible OUT can consume FIFO data and shift it in the same cycle. The RP2040 automatic-pull pseudocode and timing figure show two steps:

1. when the OSR has reached the pull threshold, transfer available TX data into OSR while the OUT remains stalled;
2. execute the OUT on the following cycle.

After a successful OUT, if the threshold is reached and TX data is already available, a post-OUT refill can occur without an additional cycle. The model implements both paths and has separate regression tests for them.

### Explicit PULL with autopull enabled

An explicit plain PULL is suppressed as an autopull fence only when OSR is completely full, represented by output shift count zero. A partially consumed OSR is still replaceable by an unconditional explicit PULL. This distinction is easy to miss if “below threshold” is treated as “full.”

### Shift-counter reset values

After state-machine restart/reset, ISR count is zero and OSR count is 32. `MOV ISR, ...` and `MOV OSR, ...` make the destination full and reset its shift counter to zero. `OUT ISR, n` sets the ISR count to `n`.

### JMP decrement conditions

`JMP X--` and `JMP Y--` test the pre-decrement value for the branch decision, but the decrement side effect occurs even when the register starts at zero. Thus zero becomes `0xffffffff` without taking the branch.

### Relative IRQ mapping

For an encoded IRQ index `i` and state machine number within its PIO block `s`, relative mapping is:

```text
(i & 4) | ((i + s) & 3)
```

For global MicroPython state-machine ids 4–7, the low two bits still identify SM0–SM3 within PIO1.

### WAIT 1 IRQ

A successful `WAIT 1 IRQ n` clears the waited-on flag as part of completion. `WAIT 0 IRQ n` does not set it.

### EXEC

`MOV EXEC, source` and `OUT EXEC, n` queue a forced instruction. The parent program instruction completes and advances/wraps its PC; the forced instruction executes while that PC remains suspended. A forced instruction may itself change PC or queue another EXEC. Delay on the parent MOV/OUT-to-EXEC operation is ignored; delay encoded in the forced instruction is honored.


### MOV and OSR while autopull is enabled

The datasheet explicitly describes `MOV` from OSR as nondeterministic while autopull is active, and `MOV` to OSR can consume then overwrite a word which was autopulled in the same interval. A software emulator cannot reproduce a DMA/hardware race from a static source file. This implementation follows the documented non-OUT pseudocode ordering—eligible autopull first, then MOV—and emits a warning whenever either pattern is encountered.

### Fractional clock-divider timestamps

The 16.8 divider determines an exact average state-machine rate but implements fractional values with first-order delta-sigma clock enables. Trace timestamps therefore use the exact average period. They do not claim to reproduce the phase-dependent one-system-clock jitter of individual hardware enable pulses.

### Standalone EXEC assembler discrepancy

Whole-program `asm_pio` assembly uses pass-wide side-set consistency. The standalone `asm_pio_encode` path used by `StateMachine.exec(str)` does not perform the same mandatory-side-set consistency check and can encode an instruction that omits `.side()`. The parser intentionally has separate program and EXEC encoding paths so it matches current behavior rather than applying one rule everywhere.

### Frequency calculation

For positive `freq`, current MicroPython computes:

```text
divider_256 = floor(system_clock_hz * 256 / freq)
```

and accepts only divider values from `256` through `65536 * 256`. The actual frequency is `system_clock_hz * 256 / divider_256`. `freq=0` is a special zero-register divider encoding, modeled as divide-by-65536 on RP2040.

## Validation inventory

At the time of packaging, the suite includes:

- all legal instruction field combinations compared with a MicroPython v1.28.0 reference encoder;
- every legal delay/side-set pattern for ten representative operations;
- representative modifier configuration on every legal instruction core;
- official-example syntax and waveform checks;
- directed tests for every instruction family and the documented stalls;
- deterministic randomized shift, bit-reversal, pin-wrap, and IRQ properties;
- a 100-word FIFO loopback stress test;
- all five output formats and CLI integration.

Release verification results are published separately from the source distribution so environment-specific logs do not become part of the maintained project source.

## Scope decisions and uncertainties

The model intentionally stops at the digital PIO state-machine boundary. It does not guess at analog pad behavior, input synchronizer latency, DMA timing, Python scheduler timing, instruction-memory placement conflicts, or simultaneous state-machine arbitration.

`MOV STATUS` is configurable because MicroPython's public `StateMachine.init` interface does not expose the underlying status-select fields. The default zero model is explicit and warning-producing.

RP2350 PIO v1 adds features and platform differences, including more state machines and instruction/pin behavior not covered by this RP2040 model. Treat Pico 2 use as compatibility-only unless the program stays within RP2040 semantics.

## Primary online sources

- Raspberry Pi, RP2040 Datasheet: <https://pip.raspberrypi.com/documents/RP-008371-DS>
- MicroPython `rp2` documentation: <https://docs.micropython.org/en/latest/library/rp2.html>
- MicroPython `StateMachine` documentation: <https://docs.micropython.org/en/latest/library/rp2.StateMachine.html>
- MicroPython v1.28.0 PIO assembler source: <https://github.com/micropython/micropython/blob/v1.28.0/ports/rp2/modules/rp2.py>
- MicroPython v1.28.0 RP2 C implementation: <https://github.com/micropython/micropython/blob/v1.28.0/ports/rp2/rp2_pio.c>
- MicroPython v1.28.0 official RP2 examples: <https://github.com/micropython/micropython/tree/v1.28.0/examples/rp2>
- Raspberry Pi Pico SDK PIO API documentation: <https://www.raspberrypi.com/documentation/pico-sdk/hardware.html#hardware_pio>
- NathanY3G RP2040 PIO emulator: <https://github.com/NathanY3G/rp2040-pio-emulator>
- soundpaint RP2040 PIO emulator: <https://github.com/soundpaint/rp2040pio>
