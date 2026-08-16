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

## Important limitations

- One state machine is emulated at a time. Other state machines can be represented through scheduled GPIO, FIFO, and IRQ events, but they do not run concurrently in the same simulation.
- The project models RP2040 PIO v0 behavior, not RP2350 PIO v1 extensions.
- It does not model DMA timing, CPU scheduling, pad analog characteristics, metastability, synchronizer latency, voltage levels, or board-level electrical effects.
- Dynamic Python behavior that cannot be resolved safely without executing the source is omitted and reported as a warning.
- Forced instructions that depend on unknown instruction-memory contents cannot always be reconstructed.
- Results should be compared with the RP2040 datasheet and verified with hardware for production or timing-critical designs.


**Provided by [blog.stuehler-training.de](https://blog.stuehler-training.de)**
