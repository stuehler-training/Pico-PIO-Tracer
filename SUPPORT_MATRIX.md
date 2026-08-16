# RP2040 PIO support matrix

Status values: **Yes** = implemented and tested; **Partial** = intentionally bounded approximation; **No** = outside this release's scope.

## Source parsing and assembly

| Feature | Status | Notes |
|---|---:|---|
| `@rp2.asm_pio` / imported `@asm_pio` | Yes | Keyword decorator arguments supported |
| Nine RP2040 instruction classes | Yes | JMP, WAIT, IN, OUT, PUSH, PULL, MOV, IRQ, SET |
| `nop()` | Yes | Encodes as `mov(y, y)` exactly as MicroPython |
| `word()` and label OR field | Yes | Decoded back to semantic instructions for emulation |
| `label`, `wrap_target`, `wrap` | Yes | Implicit wrap also supported |
| Delay `[n]` and `.delay(n)` | Yes | Validated against available delay bits |
| Side-set and optional inference | Yes | Optional enable bit inferred like current MicroPython |
| Side-set pin directions | Yes | `side_pindir=True` |
| Local constants and arithmetic | Yes | Safe AST evaluator; no arbitrary execution |
| Static `range` loops / `if` | Yes | Bounded expansion |
| Arbitrary Python in PIO function | No | Rejected rather than silently misassembled |
| Function parameters/helper calls | No | MicroPython's decorator calls a program with no arguments; arbitrary helpers would require execution |
| `StateMachine(...)` config | Yes | id/program/frequency positional or keyword, pin bases, shift overrides, thresholds |
| Two-step `StateMachine(id); init(...)` | Yes | Program and optional positional frequency |
| Static `put`, `exec`, `active` | Partial | Captured as initial setup; runtime Python timing is not inferred |
| Dynamic lookup/comprehensions/classes | Partial | PIO declarations still parse; dynamic machine construction may fall back to synthetic config |

## Instruction execution

| Feature | Status | Notes |
|---|---:|---|
| One-cycle base instruction timing | Yes | One record per state-machine cycle |
| Delay cycles | Yes | PC stays at the post-instruction value |
| Side-set during stalls | Yes | Reapplied each stalled execution cycle |
| Side-set overlap precedence | Yes | Side-set writes commit after SET/OUT/MOV writes |
| JMP conditions | Yes | Includes unconditional decrement side effects and OSRE threshold |
| WAIT GPIO/PIN/IRQ | Yes | `WAIT 1 IRQ` clears the flag when satisfied |
| IN left/right | Yes | ISR and saturating shift counter |
| OUT left/right | Yes | OSR, count, zero-extension to mapped pin group |
| PUSH/PULL | Yes | Blocking, nonblocking, IFFULL/IFEMPTY |
| MOV invert/reverse | Yes | 32-bit operations |
| MOV STATUS | Partial | Explicit configurable model; default zero with warning |
| IRQ set/clear/wait | Yes | Relative mapping uses state-machine low bits |
| SET | Yes | Pins, PINDIRS, X, Y |
| MOV/OUT to PC | Yes | Explicit PC update |
| MOV/OUT to EXEC | Yes | Forced instruction executes without advancing PC |
| Reserved opcodes/fields | Partial | Most halt clearly; undocumented silicon behavior is not modeled |

## FIFOs and automatic shifting

| Feature | Status | Notes |
|---|---:|---|
| TX/RX four-word FIFOs | Yes | Full contents/front word plus post-host-event boundary snapshot recorded |
| `JOIN_TX` / `JOIN_RX` | Yes | Eight-word joined FIFO; opposite direction disabled |
| Host blocking put/get | Yes | Pending host requests complete when space/data appears |
| Debug RX injection | Yes | Simulation-only timed append; excess words are reported/dropped when RX is full or disabled |
| Automatic push | Yes | Post-IN push and full-RX stall without double-shifting |
| Automatic pull before OUT | Yes | Data transfer and OUT occur on separate cycles per datasheet pseudocode |
| Zero-cost post-OUT refill | Yes | When TX data is already available |
| Asynchronous refill | Yes | During non-OUT and delay cycles when eligible |
| Explicit PULL/autopull fence | Yes | No-op only when OSR shift count is zero/full |
| DMA DREQ/pacing | No | Host events are explicit stimuli |

## Pins, time, and integration

| Feature | Status | Notes |
|---|---:|---|
| IN/OUT/SET/SIDESET/JMP bases | Yes | Static file values or CLI overrides |
| Modulo-32 pin-group wrap | Yes | RP2040 PIO v0 behavior |
| Initial pin values/directions | Yes | OUT then SET then SIDESET group order as MicroPython configures them |
| External GPIO drive/release | Yes | Timed stimuli or persistent transitions clicked directly into HTML GPIO rows |
| Output contention display | Partial | Shown as `X`; electrical resolution not modeled |
| Floating input | Partial | Configurable digital default, no pull-resistor/electrical model |
| 16.8 clock divider | Yes | Optional `--system-clock`; MicroPython floor and range behavior |
| Input synchronizer | No | No two-cycle latency/metastability model |
| Multiple selectable functions/configs in one HTML | Yes | Every parsed PIO function is embedded and selectable; each is simulated independently with its own browser edit session |
| Concurrent multiple state machines | No | One selected machine is executed at a time; represent peer interactions through edited GPIO/IRQ/FIFO stimuli |
| Shared IRQ/pin arbitration | No | Timed external IRQ/pin stimuli only |
| PIO instruction-memory placement | Partial | Program-relative PCs; allocation conflicts not modeled |
| RP2350 PIO v1 | No | RP2040-only validation and semantics |

## Outputs and browser editing

| Feature/output | Status | Notes |
|---|---:|---|
| Interactive HTML | Yes | Self-contained offline report; FIFO cycle-start, post-host-event, and end-of-cycle contents in inspector |
| PIO function/state-machine selector | Yes | Selector appears above the disassembly/source pane when multiple parsed functions/configurations are embedded; edits, breakpoints, and view state are preserved per selection |
| PIO disassembly and mouse breakpoints | Yes | PC, 16-bit word, decoded instruction, and source mapping; clickable breakpoint gutter with source-line mirror |
| Continue to next breakpoint | Yes | Button and F5 shortcut follow recorded control flow and avoid repeated stops on retries of the current stalled instruction |
| Full input-source debugger | Yes | Complete Python file, line numbers, program range, and current PIO source line are embedded and shown below the waveform |
| Cycle step backward/forward | Yes | Buttons, direct cycle entry, Left/Right Arrow, Home/End, and automatic viewport following |
| End-of-cycle architectural state | Yes | Explicit instruction PC and post-cycle PC, register/GPIO/IRQ state, FIFO boundary snapshots, and before/after change list |
| Stall and delay source mapping | Yes | WAIT stalled/met highlighting and delay cycles remain mapped to the originating source instruction |
| Click external GPIO high/low/release | Yes | Persistent transition begins before the instruction at the selected cycle |
| Delete transition / undo / redo / reset | Yes | History is maintained in the browser session |
| Browser-side PIO rerun | Yes | Recomputes the complete state-machine trace without Python |
| WAIT dependency visualization | Yes | Target row, stalled spans, condition-met markers, and condition details |
| Save/load stimulus JSON | Yes | Supports GPIO, TX put, RX injection/read, and IRQ event documents |
| Direct graphical TX/RX controls | Yes | Multi-word TX put, debug RX injection, and host RX read; same-cycle consumption remains visible as an intra-cycle FIFO transition |
| Direct graphical IRQ controls | Yes | Schedule IRQ0..IRQ7 set/trigger and clear events at the selected cycle |
| IRQ rows, markers, and event table | Yes | Eight-bit flag bus, used-flag digital rows, `I+`/`I−` markers, and deletable event entries |
| FIFO event markers/tables | Yes | `T+`, `R+`, and `R−` waveform markers plus editable event tables |
| Static SVG | Yes | GPIO/FIFO rows plus the eight-bit IRQ flag bus |
| CSV | Yes | Post-host-event and end-of-cycle levels/complete contents per cycle |
| JSON | Yes | Full trace state plus browser simulation model and stimuli |
| VCD | Yes | FIFO level/front plus GPIO/register signals |
