# Pico-PIO-Tracer

The Pico-PIO-Tracer is an emulator, debugger and wave form viewer for your Raspberry Pi Pico PIO program. This project is mostly vibe-coded and only available for programs written in MicroPython.

<img width="1082" height="539" alt="debugger" src="https://github.com/user-attachments/assets/5907a95c-b5f2-4141-b4b3-adf21ee2b047" />


<img width="1088" height="785" alt="wave-form" src="https://github.com/user-attachments/assets/e9890362-6c8f-4bea-9814-a6291a6d5aa3" />


## Quick start

Install from the source directory:

```bash
python -m pip install .
```

Generate an interactive trace:

```bash
pico-pio-trace examples/uart_tx.py \
  --cycles 100 \
  --pins 0 \
  -o uart_tx_trace.html
```

Open `uart_tx_trace.html` in a modern browser.

## Supported PIO scope

The emulator covers the nine RP2040 PIO instruction classes:

| Instruction | Modeled behavior |
|---|---|
| `JMP` | Conditional and unconditional branches, decrement conditions, pin and OSR tests |
| `WAIT` | GPIO, mapped input-pin, and IRQ waits |
| `IN` | Shifting data into ISR, including automatic push behavior |
| `OUT` | Shifting OSR to pins, registers, PC, ISR, pin directions, or EXEC |
| `PUSH` | Blocking and nonblocking transfers to RX |
| `PULL` | Blocking and nonblocking transfers from TX |
| `MOV` | Register/pin moves, inversion, reversal, PC, and EXEC destinations |
| `IRQ` | Set, clear, and blocking set-and-wait operations |
| `SET` | Immediate writes to pins, pin directions, X, and Y |

The parser also handles statically resolvable labels, wraps, delays, side-set, optional side-set, FIFO joins, shift directions, thresholds, `StateMachine(...)` construction, two-step `.init(...)`, visible `.put(...)`, `.exec(...)`, and `.active(...)` calls, plus a conservative subset of loops, conditions, constants, and expressions.

## Highlights

### Interactive PIO debugger

- Step forward and backward through individual state-machine cycles.
- Inspect PC, X, Y, ISR, OSR, shift counters, pin latches, pin directions, IRQ flags, and FIFO contents.
- See execute, delay, stall, WAIT, automatic FIFO, and forced-EXEC phases.
- Keep the highlighted Python source line synchronized with the selected cycle.
- Review all state changes that occurred during the cycle.

### PIO disassembly and breakpoints

- View every parsed PIO instruction with its program-relative PC, encoded 16-bit word, decoded form, and source mapping.
- Set or remove breakpoints by clicking the disassembly gutter.
- Use **Continue** or `F5` to advance to the next breakpoint along the simulated control flow.
- Retain separate breakpoint sets for each selectable PIO function.

### Multiple PIO functions

When a source file contains several `@rp2.asm_pio` functions, the HTML report presents a **PIO function to debug** drop-down. Selecting another entry updates the simulation model, source mapping, disassembly, state-machine configuration, FIFO mode, GPIO mapping, IRQ state, waveform, warnings, and debugger details.

Each function keeps its own browser session, including stimuli, breakpoints, selected cycle, simulation length, viewport, and undo/redo history.

### Editable GPIO waveforms

- Drive an external line high or low.
- Release a line to high impedance (`Z`).
- Delete a transition or switch to inspection-only mode.
- Create pulses with two clicks.
- Rerun the state machine automatically after each edit.
- Observe resolved GPIO levels, external drive, output enable, contention (`X`), and WAIT behavior.

### TX and RX FIFO analysis

- Schedule one or more host-side TX writes at an exact cycle.
- Inject RX words for debugger testing.
- Schedule host reads from the RX FIFO.
- Model FIFO capacity, blocking writes/reads, join modes, `PULL`, `PUSH`, autopull, and autopush.
- Inspect FIFO contents at cycle start, after host events, and at the end of the cycle.

The intra-cycle view is important when a host write and a PIO instruction happen in the same cycle. For example, a value accepted by TX and immediately consumed by `PULL` is shown as:

```text
TX 0 → 1 → 0 / 4
```

### IRQ support

- Emulate PIO `IRQ` instructions and `WAIT ... IRQ` behavior.
- Manually set or clear IRQ0 through IRQ7 at a selected cycle.
- Release a blocking `irq(block, n)` instruction by scheduling a clear event.
- Track individual flags, the combined eight-bit IRQ state, and set/clear markers in the waveform.

### Self-contained reports

Generated HTML reports contain the parsed program, simulation state, browser-side emulator, waveform renderer, source view, and controls. After generation, no Python process, web server, or external JavaScript package is required.

Because the complete input source can be embedded in the report, treat generated HTML files as source-code artifacts when sharing them.


## Important limitations

- One state machine is emulated at a time. Other state machines can be represented through scheduled GPIO, FIFO, and IRQ events, but they do not run concurrently in the same simulation.
- The project models RP2040 PIO v0 behavior, not RP2350 PIO v1 extensions.
- It does not model DMA timing, CPU scheduling, pad analog characteristics, metastability, synchronizer latency, voltage levels, or board-level electrical effects.
- Dynamic Python behavior that cannot be resolved safely without executing the source is omitted and reported as a warning.
- Forced instructions that depend on unknown instruction-memory contents cannot always be reconstructed.
- Results should be compared with the RP2040 datasheet and verified with hardware for production or timing-critical designs.


## Documentation

- [Technical documentation](TECHNICAL_DOCUMENTATION.md)
- [Support matrix](SUPPORT_MATRIX.md)
- [Research notes](RESEARCH_NOTES.md)
- [Changelog](CHANGELOG.md)
- [Security and privacy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)


## Development

Create an editable installation and run the standard test suite:

```bash
python -m pip install -e ".[test]"
python -m pytest -q
```

For the optional real-browser tests, also install the `browser-test` extra and provide Chromium/Chrome. Node.js enables the Python/JavaScript differential tests when it is available.

The installed runtime has no third-party Python dependencies. `pytest`, Playwright, Node.js, and Chromium are development/test tools only.

Contributions should include focused tests for parser, emulator, renderer, or browser behavior. Hardware comparisons and minimal reproductions of unsupported PIO constructs are particularly useful.

## Security and source confidentiality

The tool does not execute the input Python file and contains no telemetry or network client. Generated HTML reports intentionally embed the complete input source for debugging, so review them before sharing and do not publish reports containing secrets or proprietary source. Each standalone report also carries a collapsible GPL notice for the embedded browser runtime; this does not relicense the analyzed input source or trace data. See [SECURITY.md](SECURITY.md).

## License

The Pico PIO Trace implementation, command-line tool, emulator, renderers, browser runtime, documentation, and original project examples are distributed under the **GNU General Public License See [LICENSE](LICENSE).


**Provided by [blog.stuehler-training.de](https://blog.stuehler-training.de)**

