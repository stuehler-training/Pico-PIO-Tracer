(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.PicoPIOBrowser = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const MASK32 = 0xffff_ffff;
  const SHIFT_RIGHT = 1;
  const IN_LOW = 0;
  const IN_HIGH = 1;
  const OUT_LOW = 2;
  const OUT_HIGH = 3;

  const JMP_CONDITIONS = [
    "always",
    "not_x",
    "x_dec",
    "not_y",
    "y_dec",
    "x_not_y",
    "pin",
    "not_osre",
  ];
  const WAIT_SOURCES = ["gpio", "pin", "irq"];
  const IN_SOURCES = ["pins", "x", "y", "null", "reserved_4", "reserved_5", "isr", "osr"];
  const OUT_DESTINATIONS = ["pins", "x", "y", "null", "pindirs", "pc", "isr", "exec"];
  const MOV_DESTINATIONS = ["pins", "x", "y", "reserved_3", "exec", "pc", "isr", "osr"];
  const MOV_SOURCES = ["pins", "x", "y", "null", "reserved_4", "status", "isr", "osr"];
  const SET_DESTINATIONS = ["pins", "x", "y", "reserved_3", "pindirs", "reserved_5", "reserved_6", "reserved_7"];

  function u32(value) {
    return Number(value) >>> 0;
  }

  function bitMask(pin) {
    return (1 << (Number(pin) & 31)) >>> 0;
  }

  function hex(value, width = 8) {
    return `0x${u32(value).toString(16).padStart(width, "0")}`;
  }

  function parseInteger(value) {
    if (typeof value === "number") return value;
    if (typeof value === "bigint") return Number(value);
    if (value == null) return 0;
    const text = String(value).trim().replaceAll("_", "");
    if (!text) return 0;
    const result = Number(text);
    if (!Number.isFinite(result)) throw new Error(`invalid integer value ${JSON.stringify(value)}`);
    return result;
  }

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function mask(count) {
    count = Number(count);
    return count === 32 ? MASK32 : u32(2 ** count - 1);
  }

  function reverse32(value) {
    value = u32(value);
    value = u32(((value & 0x55555555) << 1) | ((value >>> 1) & 0x55555555));
    value = u32(((value & 0x33333333) << 2) | ((value >>> 2) & 0x33333333));
    value = u32(((value & 0x0f0f0f0f) << 4) | ((value >>> 4) & 0x0f0f0f0f));
    value = u32(((value & 0x00ff00ff) << 8) | ((value >>> 8) & 0x00ff00ff));
    return u32((value << 16) | (value >>> 16));
  }

  function formatArg(arg) {
    if (Array.isArray(arg) && arg.length === 2 && ["invert", "reverse", "rel"].includes(arg[0])) {
      return `${arg[0]}(${formatArg(arg[1])})`;
    }
    if (typeof arg === "string") {
      const symbols = new Set([
        "pins", "x", "y", "null", "pindirs", "pc", "status", "isr", "osr", "exec",
        "gpio", "pin", "irq", "not_x", "x_dec", "not_y", "y_dec", "x_not_y", "not_osre",
        "block", "noblock", "iffull", "ifempty", "clear",
      ]);
      return symbols.has(arg) ? arg : JSON.stringify(arg);
    }
    return String(arg);
  }

  function displayInstruction(instruction) {
    if (instruction.display) return instruction.display;
    const op = instruction.op;
    const args = instruction.args || [];
    let argsText = "";
    if (op === "jmp") {
      const [condition, target] = args;
      argsText = condition === "always" ? formatArg(target) : `${condition}, ${formatArg(target)}`;
    } else if (op === "wait") {
      const [polarity, source, index, relative] = args;
      argsText = `${polarity}, ${source}, ${relative ? `rel(${index})` : index}`;
    } else if (op === "push" || op === "pull") {
      const [conditional, block] = args;
      const parts = [];
      if (conditional) parts.push(op === "push" ? "iffull" : "ifempty");
      if (!block) parts.push("noblock");
      argsText = parts.join(", ");
    } else if (op === "irq") {
      const [action, index, relative] = args;
      const indexText = relative ? `rel(${index})` : String(index);
      argsText = action === "set" ? indexText : `${action === "wait" ? "block" : "clear"}, ${indexText}`;
    } else {
      argsText = args.map(formatArg).join(", ");
    }
    const shownOp = op === "in" ? "in_" : op;
    let result = `${shownOp}(${argsText})`;
    if (instruction.side != null) result += `.side(${instruction.side})`;
    if (instruction.delay) result += `[${instruction.delay}]`;
    return result;
  }

  function decodeDelayAndSideset(word, program) {
    const field = (word >>> 8) & 0x1f;
    const delayBits = Number(program.delay_bits);
    const delayMask = 2 ** delayBits - 1;
    const delay = field & delayMask;
    if (!program.sideset_count) return [delay, null];
    const sideMask = 2 ** Number(program.sideset_count) - 1;
    let side = (field >>> delayBits) & sideMask;
    if (program.sideset_optional) {
      const enabled = Boolean((field >>> (delayBits + Number(program.sideset_count))) & 1);
      if (!enabled) side = null;
    }
    return [delay, side];
  }

  function decodeInstruction(rawWord, program, fromExec = false) {
    const word = Number(rawWord) & 0xffff;
    const [delay, side] = decodeDelayAndSideset(word, program);
    const opcode = (word >>> 13) & 0x7;
    const operand = word & 0xff;
    let op;
    let args;
    if (opcode === 0) {
      op = "jmp";
      args = [JMP_CONDITIONS[(operand >>> 5) & 0x7], operand & 0x1f];
    } else if (opcode === 1) {
      op = "wait";
      const polarity = (operand >>> 7) & 1;
      const sourceCode = (operand >>> 5) & 0x3;
      const source = WAIT_SOURCES[sourceCode] || `reserved_${sourceCode}`;
      let index = operand & 0x1f;
      const relative = source === "irq" && Boolean(index & 0x10);
      if (relative) index &= 0x7;
      args = [polarity, source, index, relative];
    } else if (opcode === 2) {
      op = "in";
      args = [IN_SOURCES[(operand >>> 5) & 0x7], (operand & 0x1f) || 32];
    } else if (opcode === 3) {
      op = "out";
      args = [OUT_DESTINATIONS[(operand >>> 5) & 0x7], (operand & 0x1f) || 32];
    } else if (opcode === 4) {
      const isPull = Boolean(operand & 0x80);
      op = isPull ? "pull" : "push";
      args = [Boolean(operand & 0x40), Boolean(operand & 0x20)];
    } else if (opcode === 5) {
      op = "mov";
      const destination = MOV_DESTINATIONS[(operand >>> 5) & 0x7];
      const operationCode = operand & 0x18;
      const source = MOV_SOURCES[operand & 0x7];
      let sourceSpec = source;
      if (operationCode === 0x08) sourceSpec = ["invert", source];
      else if (operationCode === 0x10) sourceSpec = ["reverse", source];
      else if (operationCode !== 0) sourceSpec = [`reserved_op_${operationCode >>> 3}`, source];
      args = [destination, sourceSpec];
    } else if (opcode === 6) {
      op = "irq";
      const clear = Boolean(operand & 0x40);
      const wait = Boolean(operand & 0x20);
      const action = clear ? "clear" : wait ? "wait" : "set";
      args = [action, operand & 0x7, Boolean(operand & 0x10)];
    } else {
      op = "set";
      args = [SET_DESTINATIONS[(operand >>> 5) & 0x7], operand & 0x1f];
    }
    const instruction = { op, args, delay, side, word, pc: null, source_line: null, source_text: "", from_exec: fromExec };
    instruction.display = displayInstruction(instruction);
    return instruction;
  }

  class EmulationError extends Error {
    constructor(message) {
      super(message);
      this.name = "EmulationError";
    }
  }

  function outcome(changes = {}) {
    return Object.assign(
      {
        complete: true,
        stalled: false,
        stall_reason: "",
        explicit_pc: null,
        queue_exec: null,
        ignore_delay: false,
        phase: "execute",
      },
      changes,
    );
  }

  class PIOEmulator {
    constructor(model) {
      if (!model || Number(model.schema_version) !== 1) {
        throw new Error("unsupported or missing browser simulation model");
      }
      this.model = clone(model);
      this.config = this.model.config;
      this.program = this.model.program;
      this.warnings = [];
      this.warned = new Set();

      this.pc = 0;
      this.x = 0;
      this.y = 0;
      this.isr = 0;
      this.osr = 0;
      this.isr_count = 0;
      this.osr_count = 32;
      this.irq_flags = 0;

      this.pins = 0;
      this.pindirs = 0;
      this.external_mask = 0;
      this.external_values = 0;

      this.tx_fifo = [];
      this.rx_fifo = [];
      this.host_tx_queue = [];
      this.pending_rx_gets = [];
      this.host_rx_values = [];

      this.delay_remaining = 0;
      this.last_instruction = "reset";
      this.last_source_line = null;
      this.last_instruction_pc = null;
      this.exec_latch = null;
      this.pending_kind = null;
      this.halted_reason = null;

      this.cycle_events = [];
      this.main_pin_writes = [];
      this.side_pin_write = null;

      this.initialisePins();
      this.initialiseTx();
      this.applyInitialExec();
      this.initial_state = this.stateDict();
    }

    run(cycles, stimuli = []) {
      cycles = Number(cycles);
      if (!Number.isInteger(cycles) || cycles < 0) throw new Error("cycles must be a non-negative integer");
      const stimulusList = (stimuli || []).map((event) => clone(event));
      const schedule = new Map();
      for (const event of stimulusList) {
        const cycle = Number(event.cycle || 0);
        if (!Number.isInteger(cycle) || cycle < 0) throw new Error(`stimulus cycle cannot be negative: ${JSON.stringify(event)}`);
        if (!schedule.has(cycle)) schedule.set(cycle, []);
        schedule.get(cycle).push(event);
      }

      const records = [];
      for (let cycle = 0; cycle < cycles; cycle += 1) {
        this.cycle_events = [];
        this.main_pin_writes = [];
        this.side_pin_write = null;

        for (const event of schedule.get(cycle) || []) this.applyStimulus(event);
        this.serviceHostQueues();

        // Capture the host/PIO boundary explicitly. A word written to TX can
        // be consumed by PULL or autopull in this very cycle; retaining this
        // snapshot lets the debugger show both the accepted host write and
        // the resulting end-of-cycle FIFO state.
        const txFifoAfterHost = this.tx_fifo.map(u32);
        const rxFifoAfterHost = this.rx_fifo.map(u32);

        const executedPc = this.pc;
        let instructionText = "";
        let instructionWord = null;
        let sourceLine = null;
        let instructionPc = null;
        let stalled = false;
        let stallReason = "";
        let phase = "execute";

        if (this.halted_reason) {
          phase = "halt";
          instructionText = `<halted: ${this.halted_reason}>`;
        } else if (this.delay_remaining) {
          this.asynchronousAutopull(false);
          this.delay_remaining -= 1;
          phase = "delay";
          instructionText = `<delay after ${this.last_instruction}>`;
          sourceLine = this.last_source_line;
          instructionPc = this.last_instruction_pc;
          this.cycle_events.push(`delay cycle; ${this.delay_remaining} remaining`);
        } else {
          try {
            const [instruction, origin] = this.fetchInstruction();
            instructionText = `${origin === "exec" ? "EXEC: " : ""}${displayInstruction(instruction)}`;
            instructionWord = instruction.word == null ? null : Number(instruction.word);
            sourceLine = instruction.source_line == null ? null : Number(instruction.source_line);
            instructionPc = origin === "program" ? executedPc : null;
            this.last_source_line = sourceLine;
            this.last_instruction_pc = instructionPc;
            this.asynchronousAutopull(instruction.op === "out");
            this.queueSideset(instruction);
            const result = this.executeInstruction(instruction);
            stalled = Boolean(result.stalled);
            stallReason = result.stall_reason || "";
            phase = result.stalled ? "stall" : result.phase;
            this.commitPinWrites();
            if (result.complete) this.completeInstruction(instruction, origin, result);
          } catch (error) {
            if (!(error instanceof EmulationError)) throw error;
            this.commitPinWrites();
            this.halted_reason = error.message;
            this.warnings.push(error.message);
            phase = "error";
            instructionText = instructionText || "<decode/fetch error>";
            this.cycle_events.push(`ERROR: ${error.message}`);
          }
        }

        records.push({
          cycle,
          time_s: cycle * Number(this.config.period_s),
          pc: executedPc,
          instruction: instructionText,
          instruction_word: instructionWord,
          phase,
          stalled,
          stall_reason: stallReason,
          pins: u32(this.pins),
          pindirs: u32(this.pindirs),
          external_mask: u32(this.external_mask),
          external_values: u32(this.external_values),
          tx_fifo_after_host: txFifoAfterHost,
          rx_fifo_after_host: rxFifoAfterHost,
          tx_level_after_host: txFifoAfterHost.length,
          rx_level_after_host: rxFifoAfterHost.length,
          tx_fifo: this.tx_fifo.map(u32),
          rx_fifo: this.rx_fifo.map(u32),
          tx_level: this.tx_fifo.length,
          rx_level: this.rx_fifo.length,
          x: u32(this.x),
          y: u32(this.y),
          isr: u32(this.isr),
          osr: u32(this.osr),
          isr_count: this.isr_count,
          osr_count: this.osr_count,
          irq_flags: this.irq_flags & 0xff,
          instruction_pc: instructionPc,
          state_pc: this.pc,
          delay_remaining: this.delay_remaining,
          exec_latch: this.exec_latch,
          pending_kind: this.pending_kind,
          halted_reason: this.halted_reason,
          events: [...this.cycle_events],
          source_line: sourceLine,
        });
      }

      return {
        initial_state: clone(this.initial_state),
        records,
        warnings: [...this.warnings],
        host_rx_values: [...this.host_rx_values],
        stimuli: stimulusList,
      };
    }

    initialisePins() {
      for (const [base, initialiser] of [
        [this.config.out_base, this.program.out_init],
        [this.config.set_base, this.program.set_init],
        [this.config.sideset_base, this.program.sideset_init],
      ]) {
        if (base == null || initialiser == null) continue;
        let values = 0;
        let directions = 0;
        initialiser.forEach((entry, offset) => {
          const numeric = Number(entry);
          if (numeric === IN_HIGH || numeric === OUT_HIGH) values |= 1 << offset;
          if (numeric === OUT_LOW || numeric === OUT_HIGH) directions |= 1 << offset;
        });
        this.writeMappedNow("pins", Number(base), initialiser.length, values);
        this.writeMappedNow("pindirs", Number(base), initialiser.length, directions);
      }
    }

    initialiseTx() {
      for (const rawValue of this.config.initial_tx || []) {
        const value = u32(parseInteger(rawValue));
        if (this.tx_fifo.length < Number(this.config.tx_capacity)) this.tx_fifo.push(value);
        else this.host_tx_queue.push(value);
      }
    }

    applyInitialExec() {
      for (const rawWord of this.config.initial_exec || []) {
        const word = Number(rawWord) & 0xffff;
        const instruction = decodeInstruction(word, this.program, true);
        instruction.source_text = `initial exec 0x${word.toString(16).padStart(4, "0")}`;
        this.cycle_events = [];
        this.main_pin_writes = [];
        this.side_pin_write = null;
        this.queueSideset(instruction);
        let result;
        try {
          result = this.executeInstruction(instruction);
        } catch (error) {
          if (!(error instanceof EmulationError)) throw error;
          this.warnOnce(`initial exec 0x${word.toString(16).padStart(4, "0")} could not be applied: ${error.message}`);
          this.pending_kind = null;
          continue;
        }
        this.commitPinWrites();
        if (!result.complete) {
          this.warnOnce(
            `initial exec 0x${word.toString(16).padStart(4, "0")} stalled (${result.stall_reason}); it was not completed during static setup`,
          );
          this.pending_kind = null;
          continue;
        }
        if (result.explicit_pc != null) this.pc = Number(result.explicit_pc) & 0x1f;
        if (result.queue_exec != null) this.exec_latch = Number(result.queue_exec) & 0xffff;
      }
    }

    applyStimulus(event) {
      const kind = String(event.kind ?? event.type ?? "").toLowerCase().replaceAll("-", "_");
      if (["pin", "gpio", "pin_drive"].includes(kind)) {
        if (event.pin == null) throw new Error(`pin stimulus requires a pin: ${JSON.stringify(event)}`);
        const pin = Number(event.pin) & 31;
        const bit = bitMask(pin);
        const value = event.value;
        const release = value == null || (typeof value === "string" && ["Z", "RELEASE", "NONE"].includes(value.toUpperCase()));
        if (release) {
          this.external_mask = u32(this.external_mask & ~bit);
          this.external_values = u32(this.external_values & ~bit);
          this.cycle_events.push(`host releases GPIO${pin}`);
        } else {
          const numeric = parseInteger(value);
          this.external_mask = u32(this.external_mask | bit);
          if (numeric) this.external_values = u32(this.external_values | bit);
          else this.external_values = u32(this.external_values & ~bit);
          this.cycle_events.push(`host drives GPIO${pin}=${numeric ? 1 : 0}`);
        }
      } else if (["tx", "tx_put", "put"].includes(kind)) {
        if (event.value == null) throw new Error(`TX stimulus requires a value: ${JSON.stringify(event)}`);
        const shift = Number(event.shift || 0);
        const value = u32(parseInteger(event.value) * 2 ** shift);
        if (this.tx_fifo.length < Number(this.config.tx_capacity)) {
          this.tx_fifo.push(value);
          this.cycle_events.push(`host TX put ${hex(value)}`);
        } else {
          this.host_tx_queue.push(value);
          this.cycle_events.push(`host TX put ${hex(value)} blocks; queued for FIFO space`);
        }
      } else if (["rx_put", "rx_fill", "rx_inject", "inject_rx"].includes(kind)) {
        if (event.value == null) throw new Error(`RX injection stimulus requires a value: ${JSON.stringify(event)}`);
        const shift = Number(event.shift || 0);
        const value = u32(parseInteger(event.value) * 2 ** shift);
        if (Number(this.config.rx_capacity) <= 0) {
          const message = `debug RX inject ${hex(value)} ignored; RX FIFO is disabled by FIFO join mode`;
          this.cycle_events.push(message);
          this.warnOnce(message);
        } else if (this.rx_fifo.length < Number(this.config.rx_capacity)) {
          this.rx_fifo.push(value);
          this.cycle_events.push(`debug RX inject ${hex(value)}`);
        } else {
          const message = `debug RX inject ${hex(value)} dropped; RX FIFO is full`;
          this.cycle_events.push(message);
          this.warnOnce(message);
        }
      } else if (["rx", "rx_get", "get"].includes(kind)) {
        const shift = Number(event.shift || 0);
        if (this.rx_fifo.length) {
          const value = u32(this.rx_fifo.shift()) >>> shift;
          this.host_rx_values.push(value);
          this.cycle_events.push(`host RX get -> ${hex(value)}`);
        } else {
          this.pending_rx_gets.push(shift);
          this.cycle_events.push("host RX get blocks; waiting for data");
        }
      } else if (["irq", "irq_set", "irq_clear"].includes(kind)) {
        let index = event.index != null ? Number(event.index) : parseInteger(event.value || 0);
        index &= 0x7;
        let shouldSet = kind !== "irq_clear";
        if (kind === "irq" && event.value != null && event.index != null) shouldSet = Boolean(parseInteger(event.value));
        const bit = 1 << index;
        if (shouldSet) {
          this.irq_flags |= bit;
          this.cycle_events.push(`host sets IRQ${index}`);
        } else {
          this.irq_flags &= ~bit;
          this.cycle_events.push(`host clears IRQ${index}`);
        }
      } else {
        throw new Error(`unknown stimulus kind ${JSON.stringify(kind)}`);
      }
      if (event.note) this.cycle_events.push(String(event.note));
    }

    serviceHostQueues() {
      if (this.host_tx_queue.length && this.tx_fifo.length < Number(this.config.tx_capacity)) {
        const value = this.host_tx_queue.shift();
        this.tx_fifo.push(value);
        this.cycle_events.push(`blocked host TX put completes: ${hex(value)}`);
      }
      if (this.pending_rx_gets.length && this.rx_fifo.length) {
        const shift = this.pending_rx_gets.shift();
        const value = u32(this.rx_fifo.shift()) >>> shift;
        this.host_rx_values.push(value);
        this.cycle_events.push(`blocked host RX get completes: ${hex(value)}`);
      }
    }

    fetchInstruction() {
      if (this.exec_latch != null) return [decodeInstruction(this.exec_latch, this.program, true), "exec"];
      if (this.pc < 0 || this.pc >= this.program.instructions.length) {
        throw new EmulationError(
          `PC ${this.pc} is outside parsed program ${JSON.stringify(this.program.name)}; contents of the rest of PIO instruction memory are unknown`,
        );
      }
      return [this.program.instructions[this.pc], "program"];
    }

    completeInstruction(instruction, origin, result) {
      const currentPc = this.pc;
      if (origin === "exec") {
        this.exec_latch = null;
        if (result.explicit_pc != null) this.pc = Number(result.explicit_pc) & 0x1f;
      } else if (result.explicit_pc != null) {
        this.pc = Number(result.explicit_pc) & 0x1f;
      } else if (currentPc === Number(this.program.wrap_top)) {
        this.pc = Number(this.program.wrap_target);
      } else {
        this.pc = (currentPc + 1) & 0x1f;
      }
      if (result.queue_exec != null) this.exec_latch = Number(result.queue_exec) & 0xffff;
      this.pending_kind = null;
      this.last_instruction = displayInstruction(instruction);
      if (instruction.delay && !result.ignore_delay) this.delay_remaining = Number(instruction.delay);
    }

    asynchronousAutopull(skipForOut = false) {
      if (
        this.program.autopull &&
        !skipForOut &&
        this.osr_count >= Number(this.config.pull_thresh) &&
        this.tx_fifo.length
      ) {
        this.osr = u32(this.tx_fifo.shift());
        this.osr_count = 0;
        this.cycle_events.push(`autopull refills OSR asynchronously with ${hex(this.osr)}`);
      }
    }

    executeInstruction(instruction) {
      if (this.pending_kind === "autopush") return this.completePendingAutopush();
      if (this.pending_kind === "irq_wait") return this.completePendingIrqWait(instruction);
      switch (instruction.op) {
        case "nop": return outcome();
        case "jmp": return this.executeJmp(instruction);
        case "wait": return this.executeWait(instruction);
        case "in": return this.executeIn(instruction);
        case "out": return this.executeOut(instruction);
        case "push": return this.executePush(instruction);
        case "pull": return this.executePull(instruction);
        case "mov": return this.executeMov(instruction);
        case "irq": return this.executeIrq(instruction);
        case "set": return this.executeSet(instruction);
        default: throw new EmulationError(`unsupported decoded instruction ${JSON.stringify(instruction.op)}`);
      }
    }

    executeJmp(instruction) {
      const [condition, target] = instruction.args;
      let take = false;
      if (condition === "always") take = true;
      else if (condition === "not_x") take = this.x === 0;
      else if (condition === "x_dec") {
        take = this.x !== 0;
        this.x = u32(this.x - 1);
        this.cycle_events.push(`X decremented to ${hex(this.x)}`);
      } else if (condition === "not_y") take = this.y === 0;
      else if (condition === "y_dec") {
        take = this.y !== 0;
        this.y = u32(this.y - 1);
        this.cycle_events.push(`Y decremented to ${hex(this.y)}`);
      } else if (condition === "x_not_y") take = this.x !== this.y;
      else if (condition === "pin") take = Boolean(this.readGpio(this.config.jmp_pin || 0));
      else if (condition === "not_osre") take = this.osr_count < Number(this.config.pull_thresh);
      else throw new EmulationError(`reserved/unknown JMP condition ${JSON.stringify(condition)}`);
      if (take) {
        const targetPc = this.resolveTarget(target);
        this.cycle_events.push(`JMP taken to ${targetPc}`);
        return outcome({ explicit_pc: targetPc });
      }
      this.cycle_events.push("JMP not taken");
      return outcome();
    }

    executeWait(instruction) {
      const [rawPolarity, source, rawIndex, relative] = instruction.args;
      const polarity = Number(rawPolarity) & 1;
      const index = Number(rawIndex);
      let value;
      if (source === "gpio") value = this.readGpio(index);
      else if (source === "pin") value = this.readGpio(Number(this.config.in_base) + index);
      else if (source === "irq") {
        const irqIndex = this.resolveIrqIndex(index, Boolean(relative));
        value = this.irq_flags & (1 << irqIndex) ? 1 : 0;
      } else throw new EmulationError(`reserved WAIT source ${JSON.stringify(source)}`);
      if (value !== polarity) {
        return outcome({ complete: false, stalled: true, stall_reason: `WAIT ${polarity} ${source} ${index}`, phase: "wait" });
      }
      if (source === "irq" && polarity) {
        const irqIndex = this.resolveIrqIndex(index, Boolean(relative));
        this.irq_flags &= ~(1 << irqIndex);
        this.cycle_events.push(`WAIT condition met; IRQ${irqIndex} cleared`);
      } else this.cycle_events.push("WAIT condition met");
      return outcome();
    }

    executeIn(instruction) {
      const [source, rawCount] = instruction.args;
      const count = Number(rawCount);
      const value = this.readInSource(String(source), count);
      const bits = u32(value) & mask(count);
      if (Number(this.config.in_shiftdir) === SHIFT_RIGHT) {
        this.isr = count === 32 ? u32(bits) : u32((this.isr >>> count) | u32(bits << (32 - count)));
      } else {
        this.isr = count === 32 ? u32(bits) : u32((this.isr << count) | bits);
      }
      this.isr_count = Math.min(32, this.isr_count + count);
      this.cycle_events.push(`IN shifts ${count} bit(s) from ${source}: ISR=${hex(this.isr)}, count=${this.isr_count}`);
      if (this.program.autopush && this.isr_count >= Number(this.config.push_thresh)) {
        if (this.rx_fifo.length >= Number(this.config.rx_capacity)) {
          this.pending_kind = "autopush";
          return outcome({ complete: false, stalled: true, stall_reason: "autopush waits for RX FIFO space", phase: "autopush" });
        }
        this.pushIsr("autopush");
      }
      return outcome();
    }

    completePendingAutopush() {
      if (this.rx_fifo.length >= Number(this.config.rx_capacity)) {
        return outcome({ complete: false, stalled: true, stall_reason: "autopush waits for RX FIFO space", phase: "autopush" });
      }
      this.pushIsr("autopush after stall");
      return outcome();
    }

    executeOut(instruction) {
      const [destination, rawCount] = instruction.args;
      const count = Number(rawCount);
      if (this.program.autopull && this.osr_count >= Number(this.config.pull_thresh)) {
        if (this.tx_fifo.length) {
          this.osr = u32(this.tx_fifo.shift());
          this.osr_count = 0;
          this.cycle_events.push(`autopull loads OSR=${hex(this.osr)}; OUT remains stalled this cycle`);
        }
        return outcome({ complete: false, stalled: true, stall_reason: "autopull pre-OUT stall", phase: "autopull" });
      }

      let value;
      if (Number(this.config.out_shiftdir) === SHIFT_RIGHT) {
        value = this.osr & mask(count);
        this.osr = count === 32 ? 0 : this.osr >>> count;
      } else {
        value = count === 32 ? this.osr : (this.osr >>> (32 - count)) & mask(count);
        this.osr = count === 32 ? 0 : u32(this.osr << count);
      }
      value = u32(value);
      this.osr_count = Math.min(32, this.osr_count + count);
      this.cycle_events.push(`OUT shifts ${count} bit(s): value=${hex(value)}, OSR=${hex(this.osr)}, count=${this.osr_count}`);
      const result = this.writeOutDestination(String(destination), value, count);
      if (this.program.autopull && this.osr_count >= Number(this.config.pull_thresh) && this.tx_fifo.length) {
        this.osr = u32(this.tx_fifo.shift());
        this.osr_count = 0;
        this.cycle_events.push(`post-OUT autopull refills OSR for free with ${hex(this.osr)}`);
      }
      return result;
    }

    executePush(instruction) {
      const [ifFull, block] = instruction.args;
      if (Boolean(ifFull) && this.isr_count < Number(this.config.push_thresh)) {
        this.cycle_events.push("PUSH IFFULL is a no-op below threshold");
        return outcome();
      }
      if (this.rx_fifo.length >= Number(this.config.rx_capacity)) {
        if (Boolean(block)) return outcome({ complete: false, stalled: true, stall_reason: "PUSH waits for RX FIFO space", phase: "push" });
        const lost = this.isr;
        this.isr = 0;
        this.isr_count = 0;
        this.cycle_events.push(`nonblocking PUSH drops ${hex(lost)}; ISR cleared`);
        return outcome();
      }
      this.pushIsr("PUSH");
      return outcome();
    }

    executePull(instruction) {
      const [ifEmpty, block] = instruction.args;
      if (this.program.autopull && this.osr_count === 0) {
        this.cycle_events.push("PULL is a no-op because autopull already left the OSR full");
        return outcome();
      }
      if (Boolean(ifEmpty) && this.osr_count < Number(this.config.pull_thresh)) {
        this.cycle_events.push("PULL IFEMPTY is a no-op below threshold");
        return outcome();
      }
      if (!this.tx_fifo.length) {
        if (Boolean(block)) return outcome({ complete: false, stalled: true, stall_reason: "PULL waits for TX FIFO data", phase: "pull" });
        this.osr = u32(this.x);
        this.osr_count = 0;
        this.cycle_events.push(`nonblocking PULL copies X to OSR: ${hex(this.osr)}`);
        return outcome();
      }
      this.osr = u32(this.tx_fifo.shift());
      this.osr_count = 0;
      this.cycle_events.push(`PULL loads OSR=${hex(this.osr)}`);
      return outcome();
    }

    executeMov(instruction) {
      const [destination, sourceSpec] = instruction.args;
      let operation = null;
      let source = sourceSpec;
      if (Array.isArray(sourceSpec)) [operation, source] = sourceSpec;
      if (this.program.autopull && String(source) === "osr") {
        this.warnOnce(
          "MOV from OSR while autopull is enabled is hardware-racy; this trace uses the datasheet pseudocode ordering (eligible non-OUT autopull before MOV)",
        );
      }
      if (this.program.autopull && String(destination) === "osr") {
        this.warnOnce(
          "MOV to OSR while autopull is enabled can overwrite a concurrently autopulled word; this trace orders eligible non-OUT autopull before MOV",
        );
      }
      let value = this.readMovSource(String(source));
      if (operation === "invert") value = u32(~value);
      else if (operation === "reverse") value = reverse32(value);
      else if (operation != null && operation !== "none") throw new EmulationError(`reserved MOV operation ${JSON.stringify(operation)}`);
      this.cycle_events.push(`MOV reads ${hex(value)} from ${source}`);
      return this.writeMovDestination(String(destination), value);
    }

    executeIrq(instruction) {
      const [action, index, relative] = instruction.args;
      const irqIndex = this.resolveIrqIndex(Number(index), Boolean(relative));
      const bit = 1 << irqIndex;
      if (action === "set") {
        this.irq_flags |= bit;
        this.cycle_events.push(`IRQ${irqIndex} set`);
        return outcome();
      }
      if (action === "clear") {
        this.irq_flags &= ~bit;
        this.cycle_events.push(`IRQ${irqIndex} cleared`);
        return outcome();
      }
      if (action === "wait") {
        this.irq_flags |= bit;
        this.pending_kind = "irq_wait";
        this.cycle_events.push(`IRQ${irqIndex} set; waiting for host/another SM to clear it`);
        return outcome({ complete: false, stalled: true, stall_reason: `IRQ WAIT on flag ${irqIndex}`, phase: "irq_wait" });
      }
      throw new EmulationError(`unknown IRQ action ${JSON.stringify(action)}`);
    }

    completePendingIrqWait(instruction) {
      const [action, index, relative] = instruction.args;
      if (action !== "wait") throw new EmulationError("internal IRQ wait state does not match current instruction");
      const irqIndex = this.resolveIrqIndex(Number(index), Boolean(relative));
      if (this.irq_flags & (1 << irqIndex)) {
        return outcome({ complete: false, stalled: true, stall_reason: `IRQ WAIT on flag ${irqIndex}`, phase: "irq_wait" });
      }
      this.cycle_events.push(`IRQ WAIT completes after IRQ${irqIndex} was cleared`);
      return outcome();
    }

    executeSet(instruction) {
      const [rawDestination, rawValue] = instruction.args;
      const destination = String(rawDestination);
      const value = Number(rawValue) & 0x1f;
      if (destination === "pins") this.queueMainWrite("pins", this.config.set_base, this.config.set_count, value, "SET PINS");
      else if (destination === "pindirs") this.queueMainWrite("pindirs", this.config.set_base, this.config.set_count, value, "SET PINDIRS");
      else if (destination === "x") {
        this.x = value;
        this.cycle_events.push(`SET X=${value}`);
      } else if (destination === "y") {
        this.y = value;
        this.cycle_events.push(`SET Y=${value}`);
      } else throw new EmulationError(`reserved SET destination ${JSON.stringify(destination)}`);
      return outcome();
    }

    readInSource(source, count) {
      if (source === "pins") return this.readMapped(Number(this.config.in_base), count);
      if (source === "x") return this.x;
      if (source === "y") return this.y;
      if (source === "null") return 0;
      if (source === "isr") return this.isr;
      if (source === "osr") return this.osr;
      throw new EmulationError(`reserved IN source ${JSON.stringify(source)}`);
    }

    writeOutDestination(destination, value, count) {
      if (destination === "pins") {
        this.queueMainWrite("pins", this.config.out_base, this.config.out_count, value, "OUT PINS");
        return outcome();
      }
      if (destination === "pindirs") {
        this.queueMainWrite("pindirs", this.config.out_base, this.config.out_count, value, "OUT PINDIRS");
        return outcome();
      }
      if (destination === "x") { this.x = u32(value); return outcome(); }
      if (destination === "y") { this.y = u32(value); return outcome(); }
      if (destination === "null") return outcome();
      if (destination === "pc") return outcome({ explicit_pc: value & 0x1f });
      if (destination === "isr") {
        this.isr = u32(value);
        this.isr_count = Math.min(32, count);
        return outcome();
      }
      if (destination === "exec") return outcome({ queue_exec: value & 0xffff, ignore_delay: true });
      throw new EmulationError(`reserved OUT destination ${JSON.stringify(destination)}`);
    }

    readMovSource(source) {
      if (source === "pins") return this.readMapped(Number(this.config.in_base), 32);
      if (source === "x") return this.x;
      if (source === "y") return this.y;
      if (source === "null") return 0;
      if (source === "status") return this.statusValue();
      if (source === "isr") return this.isr;
      if (source === "osr") return this.osr;
      throw new EmulationError(`reserved MOV source ${JSON.stringify(source)}`);
    }

    writeMovDestination(destination, rawValue) {
      const value = u32(rawValue);
      if (destination === "pins") {
        this.queueMainWrite("pins", this.config.out_base, this.config.out_count, value, "MOV PINS");
        return outcome();
      }
      if (destination === "x") { this.x = value; return outcome(); }
      if (destination === "y") { this.y = value; return outcome(); }
      if (destination === "exec") return outcome({ queue_exec: value & 0xffff, ignore_delay: true });
      if (destination === "pc") return outcome({ explicit_pc: value & 0x1f });
      if (destination === "isr") {
        this.isr = value;
        this.isr_count = 0;
        return outcome();
      }
      if (destination === "osr") {
        this.osr = value;
        this.osr_count = 0;
        return outcome();
      }
      throw new EmulationError(`reserved MOV destination ${JSON.stringify(destination)}`);
    }

    statusValue() {
      const mode = this.config.status_mode;
      const n = Number(this.config.status_n);
      if (mode === "tx_less_than") return this.tx_fifo.length < n ? MASK32 : 0;
      if (mode === "rx_less_than") return this.rx_fifo.length < n ? MASK32 : 0;
      if (mode === "constant_one") return MASK32;
      if (mode === "constant_zero") {
        this.warnOnce(
          "MOV STATUS encountered; MicroPython does not expose EXECCTRL_STATUS_SEL/STATUS_N, so the emulator uses constant zero unless configured explicitly",
        );
        return 0;
      }
      throw new EmulationError(`unknown status mode ${JSON.stringify(mode)}`);
    }

    pushIsr(reason) {
      if (this.rx_fifo.length >= Number(this.config.rx_capacity)) {
        throw new EmulationError("internal error: attempted to push to a full/disabled RX FIFO");
      }
      const value = u32(this.isr);
      this.rx_fifo.push(value);
      this.isr = 0;
      this.isr_count = 0;
      this.cycle_events.push(`${reason} writes ${hex(value)} to RX FIFO and clears ISR`);
    }

    queueSideset(instruction) {
      if (instruction.side == null) return;
      const kind = this.program.side_pindir ? "pindirs" : "pins";
      this.side_pin_write = [
        kind,
        Number(this.config.sideset_base || 0),
        Number(this.config.sideset_count),
        Number(instruction.side),
        kind === "pindirs" ? "SIDESET PINDIRS" : "SIDESET PINS",
      ];
      if (!Number(this.config.sideset_count)) {
        this.warnOnce("side-set instruction encountered with no active sideset_base/count; no pin is changed");
      }
    }

    queueMainWrite(kind, base, count, value, source) {
      if (base == null || Number(count) <= 0) {
        this.warnOnce(`${source} has a configured pin count of zero; no pin is changed`);
        return;
      }
      this.main_pin_writes.push([kind, Number(base), Number(count), u32(value), source]);
    }

    commitPinWrites() {
      for (const [kind, base, count, value, source] of this.main_pin_writes) {
        this.writeMappedNow(kind, base, count, value);
        this.cycle_events.push(`${source} writes ${count} pin(s) at GPIO${base} with 0x${u32(value).toString(16)}`);
      }
      if (this.side_pin_write != null) {
        const [kind, base, count, value, source] = this.side_pin_write;
        if (count > 0) {
          this.writeMappedNow(kind, base, count, value);
          this.cycle_events.push(`${source} writes ${count} pin(s) at GPIO${base} with 0x${u32(value).toString(16)}`);
        }
      }
    }

    writeMappedNow(kind, base, count, value) {
      let target = kind === "pins" ? this.pins : this.pindirs;
      for (let offset = 0; offset < Number(count); offset += 1) {
        const pin = (Number(base) + offset) & 31;
        const bit = bitMask(pin);
        if (u32(value) & bitMask(offset)) target = u32(target | bit);
        else target = u32(target & ~bit);
      }
      if (kind === "pins") this.pins = u32(target);
      else this.pindirs = u32(target);
    }

    readGpio(rawPin) {
      const pin = Number(rawPin) & 31;
      const bit = bitMask(pin);
      if (this.pindirs & bit) return this.pins & bit ? 1 : 0;
      if (this.external_mask & bit) return this.external_values & bit ? 1 : 0;
      return Number(this.config.default_input) ? 1 : 0;
    }

    readMapped(base, count) {
      let value = 0;
      for (let offset = 0; offset < Number(count); offset += 1) {
        if (this.readGpio(Number(base) + offset)) value = u32(value | bitMask(offset));
      }
      return u32(value);
    }

    resolveTarget(target) {
      if (typeof target === "string") {
        if (!(target in this.program.labels)) throw new EmulationError(`unknown JMP label ${JSON.stringify(target)}`);
        return Number(this.program.labels[target]);
      }
      return Number(target) & 0x1f;
    }

    resolveIrqIndex(rawIndex, relative) {
      const index = Number(rawIndex) & 0x7;
      if (!relative) return index;
      return (index & 0x4) | ((index + (Number(this.config.sm_id) & 0x3)) & 0x3);
    }

    stateDict() {
      return {
        pc: this.pc,
        x: u32(this.x),
        y: u32(this.y),
        isr: u32(this.isr),
        osr: u32(this.osr),
        isr_count: this.isr_count,
        osr_count: this.osr_count,
        pins: u32(this.pins),
        pindirs: u32(this.pindirs),
        external_mask: u32(this.external_mask),
        external_values: u32(this.external_values),
        irq_flags: this.irq_flags & 0xff,
        tx_fifo: this.tx_fifo.map(u32),
        rx_fifo: this.rx_fifo.map(u32),
        delay_remaining: this.delay_remaining,
        exec_latch: this.exec_latch,
        pending_kind: this.pending_kind,
        halted_reason: this.halted_reason,
      };
    }

    warnOnce(message) {
      if (!this.warned.has(message)) {
        this.warned.add(message);
        this.warnings.push(message);
      }
    }
  }

  function simulate(model, cycles, stimuli) {
    return new PIOEmulator(model).run(cycles, stimuli);
  }

  return {
    MASK32,
    SHIFT_RIGHT,
    EmulationError,
    PIOEmulator,
    decodeInstruction,
    displayInstruction,
    parseInteger,
    simulate,
    u32,
  };
});
