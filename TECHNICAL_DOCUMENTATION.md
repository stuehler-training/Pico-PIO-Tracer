# Pico PIO Trace — Technical Documentation

`pico-pio-trace` is a safe static parser, cycle-oriented RP2040 PIO emulator, and logic-analyzer renderer for Raspberry Pi Pico programs written with MicroPython's `rp2.asm_pio` dialect.

It reads a normal MicroPython `.py` file **without importing or executing it**, finds PIO programs and `StateMachine` configuration that can be determined statically, emulates one state machine at a time, and writes an interactive waveform. When the file contains multiple PIO functions, one HTML report embeds all of them and provides a function/state-machine selector. The trace includes GPIO levels and output enables, program counter, instruction/stall phases, registers, IRQ flags, and both TX/RX FIFO levels and contents. The browser debugger also provides a decoded PIO disassembly with mouse breakpoints and Continue-to-breakpoint navigation.

![scope](https://img.shields.io/badge/scope-RP2040%20PIO%20v0-informational) ![python](https://img.shields.io/badge/python-3.10%2B-informational) ![license](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

## What “Pico PIO language” means

The Raspberry Pi Pico does not have a separate general-purpose “Pico language.” This project targets the **PIO assembly language** implemented by the RP2040 Programmable I/O blocks and exposed in MicroPython by `rp2.asm_pio`.

An RP2040 has two PIO blocks, each containing four state machines. A state machine runs a compact 16-bit instruction set from shared 32-word instruction memory. The nine instruction classes are:

| Instruction | Main purpose |
|---|---|
| `JMP` | Conditional branch, including X/Y decrement loops and pin/OSR tests |
| `WAIT` | Stall for a GPIO, mapped input pin, or IRQ condition |
| `IN` | Shift a source into the input shift register (ISR) |
| `OUT` | Shift the output shift register (OSR) to pins/registers/PC/EXEC |
| `PUSH` | Transfer ISR to the RX FIFO |
| `PULL` | Transfer TX FIFO data to OSR |
| `MOV` | Move/bit-invert/bit-reverse between PIO registers and pins |
| `IRQ` | Set, clear, or set-and-wait on a PIO IRQ flag |
| `SET` | Write a five-bit immediate to pins, pin directions, X, or Y |

Every instruction normally consumes one state-machine cycle. Its shared five-bit delay/side-set field can add delay cycles and/or update side-set pins. Blocking FIFO operations, `WAIT`, IRQ wait, automatic push, and automatic pull can extend an instruction across more cycles.

## Installation

From the source directory:

```bash
python -m pip install .
```

For development and tests:

```bash
python -m pip install -e .
python -m pytest -q
```

The package has no runtime dependencies outside the Python standard library.

## First trace

```bash
pico-pio-trace examples/uart_tx.py \
  --cycles 100 \
  --pins 0 \
  -o uart_tx_trace.html
```

Open `uart_tx_trace.html` in a browser. It is self-contained, works offline, and includes a browser-side PIO simulator for editing external GPIO transitions and timed TX/RX FIFO/IRQ events directly in the report.

The equivalent module invocation is:

```bash
python -m pico_pio_trace examples/uart_tx.py --cycles 100 --pins 0 -o uart_tx_trace.html
```

The small compatibility entry point `pio_trace.py` is also included:

```bash
python pio_trace.py examples/blink.py --cycles 100 -o blink.html
```

## Interactive logic-analyzer and GPIO editor

The HTML report is both a viewer and an offline input editor. The generated file embeds the parsed PIO program, state-machine configuration, initial FIFO state, stimuli, Python-generated trace, and a dependency-free JavaScript implementation of the cycle model. No Python process or web server is needed after the HTML has been generated.

To create a trace without writing a stimulus file:

```bash
pico-pio-trace my_program.py --cycles 21000 --pins 14-17 -o trace.html
```

Open `trace.html`, choose **Drive high**, **Drive low**, **Release**, or **Delete transition**, and click a GPIO waveform at the desired cycle. The selected external state persists until the next transition on that GPIO. Therefore a pulse is made with two clicks—for example, drive GPIO15 high at cycle 100 and low at cycle 200. An edit is applied before the PIO instruction executed in the clicked cycle.

After each edit, the browser reruns the complete state machine and updates:

- GPIO resolved levels, external-drive levels, and output-enable rows, including `Z` and externally contended `X`;
- `WAIT` targets, red stalled spans, and green condition-satisfied cycles;
- PC and execute/delay/stall phases;
- X, Y, ISR, OSR, shift counters, and IRQ flags;
- TX/RX FIFO levels, front words, and complete contents;
- instruction, source line, events, and stall reason in the cycle inspector.

The editor includes undo/redo, automatic or manual reruns, adjustable simulation length, separate GPIO/FIFO/IRQ event tables, and optional save/load of stimulus JSON. GPIO-editing shortcuts are `1`, `0`, `Z`, `I`, Delete, Ctrl/Cmd-Z, and Ctrl/Cmd-Y. The display start/end window controls only what is drawn; **Simulation cycles** controls how many cycles are recomputed.

### Selecting a PIO function

If the input file contains more than one `@rp2.asm_pio` function, the report shows a **PIO function to debug** drop-down at the top of the debugger source pane, directly above the **PIO disassembly** and **Input source** views. Every statically parsed function is embedded. An instantiated function uses its parsed `StateMachine` configuration; a function that is defined but not instantiated is included with the same conservative synthetic configuration used by `--program`.

The source view also contains a non-interactive highlight guide. **Current instruction** identifies the source line executed or stalled during the selected cycle. **Selected PIO function** marks the complete source range belonging to the selected function. The narrow color bars are explanatory markers, not checkboxes.

Each selectable function has its own browser session. GPIO, FIFO, and IRQ edits, breakpoints, undo/redo history, selected cycle, simulation length, and viewport are preserved when you switch away and return. The disassembly, source mapping, pin list, frequency, FIFO capacities/join mode, warnings, and debugger state all update to the selected function. `--program NAME` chooses the function shown initially. Use `--single-program` when file size or simulation time matters more than embedding the other parsed functions.

### Manual TX and RX FIFO editor

The **Manual FIFO events** section follows the selected debugger cycle by default. Enter one or more 32-bit words and press **Add word(s) to TX FIFO** or **Inject word(s) into RX FIFO**. Values may be hexadecimal, binary, octal, or decimal; underscores and signed values are accepted, and multiple words can be separated with commas, semicolons, or whitespace. Pressing Enter in the word field schedules a TX write.

FIFO events are applied in written order before the PIO instruction at the selected cycle. TX entries model host `StateMachine.put()` operations: a write made while TX is full remains blocked and completes when FIFO space appears. **RX injection is intentionally a debugger-only operation** because the RP2040 host interface normally reads RX rather than writing it. Injected words append while RX has room; excess words are reported and dropped when the RX FIFO is full. **Host-read one RX word** schedules the normal host-side RX read operation, including blocking behavior if RX is empty.

The TX and RX level rows show `T+`, `R+`, and `R−` markers for manual TX puts, RX injections, and RX reads. Their waveform now preserves the three relevant boundaries inside each cycle: cycle start, after host events, and end of cycle. A single TX word that is accepted and immediately consumed by `PULL` is therefore drawn as a visible `0 → 1 → 0` pulse instead of disappearing from an end-of-cycle-only sample. All events participate in undo/redo, reset, save/load, and browser re-simulation. If `JOIN_RX` or `JOIN_TX` disables one FIFO direction, the corresponding graphical controls are disabled.

The summary card and FIFO-editor badge display a compact **cycle start → post-host-event → end-of-cycle** level path over the fixed capacity. Consecutive duplicate levels are collapsed. For example, `TX 0 → 1 → 0 / 4` means a host put added one word and the selected PIO instruction consumed it in the same cycle; `RX 0 → 2 / 4` means two injected words remained at the end of the cycle. The detailed FIFO inspector lists the complete TX and RX contents at all three boundaries. These views update when you step, jump, add/remove an event, undo/redo, or rerun the simulation. FIFO capacity remains a hardware/configuration property and changes only with the selected FIFO-join configuration.

Generate a small FIFO-focused report with:

```bash
pico-pio-trace examples/fifo_editor_demo.py --cycles 40 -o fifo_editor.html
```

That example now contains both `fifo_editor_demo` and `irq_editor_demo`, so the generated report also demonstrates the function selector.

### Manual IRQ editor

The **Manual IRQ events** section follows the selected debugger cycle by default. Choose IRQ0 through IRQ7 and press **Set / trigger IRQ** or **Clear IRQ**. The event is scheduled before the PIO instruction at that cycle, exactly like command-line stimulus events.

The report displays the complete eight-bit IRQ state in the summary card, debugger, data table, and an `IRQ flags` waveform row. Flags referenced by the selected PIO program or by manual events also receive individual `IRQn` digital rows. `I+` and `I−` markers show scheduled set and clear operations.

The IRQ model follows RP2040 behavior already implemented by the emulator: a successful `wait(1, irq, n)` consumes and clears the matching flag, while `irq(block, n)` sets the flag and stalls until the host or another state machine clears it. This makes blocking IRQ handshakes directly testable from the self-contained report. IRQ events participate in undo/redo, reset, save/load, and per-function session preservation.

### Cycle debugger and source view

Immediately below the waveform, the HTML report displays a complete decoded **PIO disassembly** followed by the complete input Python file with line numbers. Each disassembly row shows the PIO PC, encoded 16-bit word, decoded instruction, and mapped source line. The currently selected disassembly instruction and PIO source line follow the logic-analyzer cursor. Rows and source lines are marked differently when a `WAIT` is stalled, when its condition is satisfied, or when the selected cycle is an instruction delay cycle. Delay cycles remain associated with the instruction whose delay field created them.

Click the circular gutter beside any disassembly instruction to set or remove a breakpoint. Press **Continue** or `F5` to move to the next simulated cycle that executes any active breakpoint PC. Repeated Continue operations follow actual control flow, including jumps and implicit wrapping. When Continue starts on a stalled breakpoint, retries and the completion of that same instruction are skipped so the debugger does not immediately stop on every stalled cycle. A breakpoint is mirrored as a red marker beside its mapped Python source line. **Clear breakpoints** removes all breakpoints for the selected PIO function.

Use **Step backward** and **Step forward** to move exactly one PIO state-machine cycle at a time. The Left and Right Arrow keys perform the same operation; Home and End jump to the first and last simulated cycles. The selected-cycle input can be used for a direct jump. When stepping or continuing outside the visible waveform window, the viewer automatically moves the window so the selected cycle remains visible.

The highlighted waveform band represents the complete selected cycle. Debugger state is defined precisely:

1. the cycle starts at `cycle / frequency`;
2. external GPIO, host FIFO, and IRQ stimuli scheduled for that cycle are applied;
3. the PIO instruction executes, stalls, or consumes a delay cycle;
4. the registers, FIFOs, IRQs, GPIO state, and PC shown by the debugger are sampled at the **end** of the cycle.

The debugger separately displays **Instruction PC** and **PC after cycle**. This avoids a common ambiguity around taken jumps, implicit wrapping, `WAIT` stalls, forced EXEC instructions, and delay cycles. It also shows every tracked change from the start to the end of the selected cycle, including X/Y, ISR/OSR and shift counts, pin latches and directions, external inputs, IRQ flags, TX/RX FIFO contents, delay state, the EXEC latch, pending operations, and halt state. A GPIO table shows the resolved line state, value read by PIO, external drive, output enable, and PIO output latch for every displayed pin.

Because the self-contained report embeds the complete input file, sharing the HTML also shares that source code. Treat the generated report with the same confidentiality as the original `.py` file.

The direct waveform tools edit external GPIO transitions. Timed TX writes, debugger RX injections, and host RX reads can be created with the graphical FIFO editor. IRQ flags can be set and cleared with the graphical IRQ editor. Existing embedded events are editable/resettable, and saving the result makes it reusable from the command line.

Other output formats are selected by filename extension:

| Extension | Output |
|---|---|
| `.html` | Interactive self-contained logic-analyzer report |
| `.svg` | Static vector waveform |
| `.csv` | One row per cycle, including GPIOs, FIFO contents, registers, and events |
| `.json` | Full structured trace and metadata |
| `.vcd` | GTKWave-compatible waveform with GPIO, FIFO, PC, IRQ, and register signals |

Example:

```bash
pico-pio-trace examples/fifo_loopback.py --cycles 20 -o loopback.vcd
```

## Selecting a program or state machine

List what the parser found:

```bash
pico-pio-trace my_program.py --list
```

Select by decorated function name or state-machine id:

```bash
pico-pio-trace my_program.py --program uart_tx --sm 0 --cycles 200 -o trace.html
```

Selection rules are:

1. a matching parsed `StateMachine` is preferred;
2. otherwise `--program` creates a synthetic configuration for that PIO program;
3. with no explicit selection, the first parsed state machine is used;
4. when no state-machine construction is statically visible, the first PIO program gets a synthetic 1 MHz configuration and a warning;
5. for HTML output, the selected configuration is shown initially and the remaining parsed functions/configurations are embedded as selector options unless `--single-program` is supplied.

## FIFO input and timed stimulus

Seed one or more TX words at cycle zero:

```bash
pico-pio-trace my_program.py --tx 0x55,0xaa --tx 1234 --cycles 300 -o trace.html
```

For command-line automation or reproducible test fixtures, use a JSON stimulus file. GPIO, FIFO, and IRQ events can also be created directly in the HTML editor:

```json
{
  "pins": [
    {"cycle": 0, "pin": 3, "value": 1, "note": "idle"},
    {"cycle": 8, "pin": 3, "value": 0, "note": "start bit"},
    {"cycle": 24, "pin": 3, "value": "Z", "note": "release line"}
  ],
  "tx": [
    {"cycle": 4, "value": "0x55"},
    {"cycle": 40, "value": 170, "shift": 0}
  ],
  "rx_put": [
    {"cycle": 10, "value": "0x11223344", "note": "debug-only RX preload"}
  ],
  "rx_get": [12, {"cycle": 80, "shift": 8}],
  "irq": [
    {"cycle": 30, "index": 1, "value": 1},
    {"cycle": 35, "index": 1, "value": 0}
  ],
  "irq_set": [
    {"cycle": 50, "index": 2}
  ],
  "irq_clear": [
    {"cycle": 55, "index": 2}
  ]
}
```

Run it with:

```bash
pico-pio-trace my_program.py --stimulus stimulus.json --cycles 120 -o trace.html
```

A host `put` made while the TX FIFO is full is retained as a blocking host request and completes when space appears. A host `get` made while RX is empty behaves similarly. `rx_put` is the simulation-only RX injection described above; it appends when room exists and reports/drops a word when RX is full or disabled. The report records these events and shows the FIFO state after each simulated cycle.

## Timing and frequency

A record represents the state at the end of one PIO state-machine cycle. Record `N` starts at `N / frequency` seconds.

By default the frequency written in the parsed `StateMachine` call is used directly. Supply the system clock to model MicroPython's 16.8 fixed-point divider rounding:

```bash
pico-pio-trace my_program.py \
  --freq 3000000 \
  --system-clock 125000000 \
  --cycles 100 \
  -o trace.html
```

The divider is floored exactly as in the current MicroPython RP2 port, and out-of-range frequencies are rejected rather than silently clamped. MicroPython's special `freq=0` divider encoding is modeled as the maximum divider of 65536; without an explicit system clock the trace assumes the RP2040 default 125 MHz clock and emits a warning. For a fractional 16.8 divider, timestamps use the exact **average** rate; the individual one-system-clock delta-sigma jitter pattern is not reconstructed, and the trace says so explicitly.

## Pin mapping overrides

Static `Pin(n)` values are normally taken from the file. They can be overridden:

```bash
pico-pio-trace my_program.py \
  --in-base 2 --out-base 6 --set-base 10 --sideset-base 14 --jmp-pin 18 \
  --pins 2-7,10,14,18 \
  --cycles 250 -o mapped.html
```

PIO pin groups wrap modulo 32, matching RP2040 PIO v0. An undriven input is modeled as zero unless `--default-input 1` is specified.

## `MOV STATUS`

MicroPython does not expose the PIO `EXECCTRL.STATUS_SEL` and `STATUS_N` fields through `StateMachine.init`. The default trace therefore models `MOV ..., STATUS` as zero and emits a warning when it is used. Explicit models are available:

```bash
pico-pio-trace my_program.py --status-mode tx_less_than --status-n 2 -o trace.html
```

Modes are `constant_zero`, `constant_one`, `tx_less_than`, and `rx_less_than`.

## Static parser coverage

The parser deliberately uses Python's AST instead of executing the input file. It recognizes:

- `@rp2.asm_pio(...)` and imported `@asm_pio(...)` decorators;
- all nine RP2040 instruction classes, `nop()`, `word()`, labels, explicit/implicit wrap;
- bracket delays and `.delay(n)`;
- `.side(n)`, optional side-set inference, and side-set pin-direction mode;
- integer/string/tuple/list literals and safe constant arithmetic;
- safe `range` loops and statically decidable `if` statements inside PIO functions;
- `Pin(n)` and `Pin(n, mode, pull, value=...)` GPIO identifiers without executing pad configuration, plus `PIO` constants, `const`, `rel`, `invert`, and `reverse` expressions;
- `StateMachine(id, program, freq, ...)` and the two-step `StateMachine(id); sm.init(program, freq, ...)` form;
- statically visible `sm.put(...)`, `sm.exec(...)`, and `sm.active(...)` calls.

Unsupported dynamic Python does not run. Where possible it is ignored with a warning; unsupported statements inside a PIO function are rejected because omitting one could silently alter machine code. Dynamic program lookup such as `globals()[name]`, arbitrary helper calls, comprehensions, file/network I/O, and runtime control flow are not interpreted.
Top-level runtime loops containing `StateMachine` FIFO or control calls produce an explicit warning. Their timing cannot be inferred from static source: external GPIO activity and cycle-exact TX/RX FIFO/IRQ activity can be entered in the HTML editor or loaded from stimulus JSON.

## Emulation coverage

The cycle model includes:

- PC increment, explicit jump, and automatic wrap;
- all legal JMP conditions, including decrement side effects;
- GPIO/PIN/IRQ waits and relative IRQ mapping;
- left/right ISR and OSR shifts, 32-bit count encoding, and saturation of shift counters;
- blocking and nonblocking PUSH/PULL, conditional thresholds, and joined/disabled FIFOs;
- pre-OUT automatic-pull stalls, post-OUT zero-cost refill, asynchronous refill, and automatic-push stalls;
- the explicit-PULL/autopull fence rule when OSR is completely full;
- `MOV` invert/reverse/status and `MOV/OUT EXEC` execution timing;
- SET/OUT/MOV pin and pin-direction mapping with modulo-32 wrap;
- side-set on normal, delay, and stalled instruction cycles, with side-set winning overlapping pin writes;
- timed host GPIO, FIFO, and IRQ events.

See [SUPPORT_MATRIX.md](SUPPORT_MATRIX.md) for the detailed matrix and [RESEARCH_NOTES.md](RESEARCH_NOTES.md) for source-by-source semantic notes.

## Verification performed

The included suite combines several independent checks:

- complete legal instruction-field comparison against an independently extracted copy of the current upstream MicroPython assembler;
- 5,102 encoder comparisons in total, including every legal core field and every legal delay/side-set pattern for representative opcodes;
- current official MicroPython RP2 examples for IRQ blink, EXEC, pin change, PWM, UART RX/TX, and WS2812;
- deterministic randomized properties for shift directions, shift counters, 32-bit reversal, pin mapping, and relative IRQ mapping;
- a 100-word automatic-pull/automatic-push FIFO loopback stress test;
- directed cycle tests for delay, wrap, stalls, FIFO joins, nonblocking loss behavior, EXEC, IRQ, side-set precedence, automatic refill timing, and host blocking;
- render and command-line tests for HTML, SVG, CSV, JSON, and VCD;
- exact cycle-by-cycle differential tests between the Python emulator and the embedded JavaScript emulator across all nine instruction classes, FIFO joins, automatic shifting, side-set, WAIT, IRQ, EXEC, pin directions, and randomized host events;
- headless Chromium interaction tests that click real SVG GPIO rows, verify WAIT resolution and undo, place disassembly breakpoints and Continue with mouse/F5, switch between independently edited PIO functions, trigger/consume a WAIT IRQ, and clear a blocking IRQ handshake.

Run the exact suite shipped with this archive:

```bash
python -m pytest -q
```

The research and implementation used three independent verification tracks: the Raspberry Pi RP2040 datasheet for hardware-cycle semantics, current MicroPython documentation/source for dialect and configuration behavior, and official examples plus independent emulator investigations for cross-checking edge cases. Agreement between sources was required for subtle behaviors such as automatic pull timing; discrepancies are documented instead of hidden.

## Important limitations

This is a focused RP2040 PIO v0 model, not a complete Pico board simulator.

- One state machine is emulated at a time. A single HTML report can embed and switch between multiple parsed PIO functions/configurations, but they are simulated independently rather than concurrently. Cross-state-machine effects can be represented through timed GPIO/IRQ/FIFO stimuli.
- DMA, CPU scheduling, Python execution time, PIO instruction-memory allocation conflicts, and multi-state-machine pin-write arbitration are not modeled.
- Input synchronizer latency, metastability, electrical pull resistors, analog behavior, drive strength, and pad timing are not modeled.
- Fractional clock-divider traces use the exact average cycle period, not the hardware's per-cycle delta-sigma clock-enable jitter pattern.
- GPIO contention is shown as `X` in the report, but the emulated PIO input read uses its output latch for an output-enabled pin.
- Top-level `StateMachine.exec` calls are applied as static setup because the surrounding Python timing cannot be inferred safely; a warning explains this. Use HTML GPIO edits, timed stimuli, or put the behavior in PIO code for cycle-exact traces.
- RP2350/Pico 2 PIO v1 extensions, its third PIO block, GPIO-base window, and extended instruction behaviors are outside scope. RP2040-compatible PIO programs may still parse, but the model intentionally validates state-machine ids 0–7 and 32 GPIO mappings.
- Reserved raw instruction encodings are not promised to match undocumented silicon behavior.

A software trace should be validated on hardware before it is used for safety-critical timing, electrical limits, or production sign-off.

## Project layout

```text
pico_pio_trace/        parser, encoder/decoder, emulator, renderers, CLI
examples/              runnable source and stimulus examples
tests/                 directed, differential, randomized, and integration tests
pio_trace.py            compatibility command entry point
RESEARCH_NOTES.md       language and semantic verification notes
SUPPORT_MATRIX.md       implemented and deliberately omitted features
```

## License

The Pico PIO Trace implementation is distributed under GNU GPL version 3 or later (`GPL-3.0-or-later`). Three files adapted from MicroPython v1.28.0 remain MIT-licensed. See [LICENSE](LICENSE), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [LICENSES/MICROPYTHON-MIT.txt](LICENSES/MICROPYTHON-MIT.txt). The package-level expression is `GPL-3.0-or-later AND MIT` because both licenses occur in the source distribution.
