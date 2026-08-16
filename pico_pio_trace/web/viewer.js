(function () {
  "use strict";

  const legacyTrace = window.__PIO_EMBEDDED_TRACE__;
  const legacyPins = [...(window.__PIO_DISPLAY_PINS__ || [])].map((pin) => Number(pin) & 31);
  const rawCatalog = window.__PIO_EMBEDDED_TRACES__;
  const simulator = window.PicoPIOBrowser;
  const traceOptions = rawCatalog && Array.isArray(rawCatalog.options) && rawCatalog.options.length
    ? rawCatalog.options
    : [{ key: "default", label: legacyTrace && legacyTrace.metadata ? legacyTrace.metadata.program : "PIO program", trace: legacyTrace, pins: legacyPins }];
  const traceOptionsByKey = new Map(traceOptions.map((option) => [String(option.key), option]));
  let activeTraceKey = String(rawCatalog && rawCatalog.selected || traceOptions[0].key);
  if (!traceOptionsByKey.has(activeTraceKey)) activeTraceKey = String(traceOptions[0].key);
  let activeOption = traceOptionsByKey.get(activeTraceKey);
  let embedded = activeOption && activeOption.trace;
  let displayPins = [...((activeOption && activeOption.pins) || legacyPins)].map((pin) => Number(pin) & 31);
  if (!embedded || !embedded.simulation || !simulator) {
    document.body.innerHTML = "<pre>Interactive trace data is missing or corrupt.</pre>";
    return;
  }

  let model = embedded.simulation;
  let meta = embedded.metadata;
  let baseWarnings = [...(meta.warnings || [])];
  let baselineStimuli = normaliseStimuli(embedded.stimuli || []);
  let stimuli = deepClone(baselineStimuli);
  let records = embedded.records || [];
  let initialState = embedded.initial_state || {};
  let runtimeWarnings = [];
  let hostRxValues = [];
  let activeTool = "1";
  let selectedCycle = 0;
  let drawState = null;
  let undoStack = [];
  let redoStack = [];
  let nextOrder = Math.max(0, ...stimuli.map((event) => Number(event._order || 0))) + 1;
  let breakpoints = new Set();
  let simulationToken = 0;
  const traceSessions = new Map();

  const wave = document.getElementById("wave");
  const tooltip = document.getElementById("wave-tooltip");
  const sourceCode = document.getElementById("source-code");
  const disassembly = document.getElementById("pio-disassembly");
  const NS = "http://www.w3.org/2000/svg";
  const sourceLineElements = new Map();
  const instructionPcsByLine = new Map();
  const disassemblyRowsByPc = new Map();

  function deepClone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function esc(value) {
    return String(value).replace(/[&<>"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[char]);
  }

  function svgElement(name, attributes = {}, text = "") {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (text !== "") node.textContent = text;
    return node;
  }

  function fmtTime(seconds) {
    const absolute = Math.abs(seconds);
    if (absolute === 0) return "0 s";
    const units = [[1, "s"], [1e-3, "ms"], [1e-6, "µs"], [1e-9, "ns"], [1e-12, "ps"]];
    for (const [scale, suffix] of units) {
      if (absolute >= scale) return `${(seconds / scale).toPrecision(5).replace(/\.?0+$/, "")} ${suffix}`;
    }
    return `${seconds.toExponential(3)} s`;
  }

  function fmtFrequency(hz) {
    const absolute = Math.abs(Number(hz) || 0);
    const units = [[1e9, "GHz"], [1e6, "MHz"], [1e3, "kHz"], [1, "Hz"]];
    for (const [scale, suffix] of units) {
      if (absolute >= scale) return `${(Number(hz) / scale).toPrecision(6).replace(/\.?0+$/, "")} ${suffix}`;
    }
    return `${Number(hz || 0).toPrecision(6).replace(/\.?0+$/, "")} Hz`;
  }

  function fifoJoinName(value) {
    return ({ 0: "JOIN_NONE", 1: "JOIN_TX", 2: "JOIN_RX" })[Number(value)] || `UNKNOWN(${value})`;
  }

  function hex(value, width = 8) {
    return `0x${(Number(value) >>> 0).toString(16).padStart(width, "0")}`;
  }

  function normaliseFifoWords(values) {
    return [...(values || [])].map((value) => Number(value) >>> 0);
  }

  function fifoBoundaryState(record, index = selectedCycle) {
    if (!record) {
      return {
        startTx: [], startRx: [],
        hostTx: [], hostRx: [],
        endTx: [], endRx: [],
      };
    }
    const start = stateBeforeCycle(index);
    const endTx = normaliseFifoWords(record.tx_fifo);
    const endRx = normaliseFifoWords(record.rx_fifo);
    return {
      startTx: normaliseFifoWords(start.tx_fifo),
      startRx: normaliseFifoWords(start.rx_fifo),
      // Reports generated before 0.6.1 have no explicit host boundary. Use
      // the end-of-cycle value as a conservative backward-compatible fallback.
      hostTx: Array.isArray(record.tx_fifo_after_host)
        ? normaliseFifoWords(record.tx_fifo_after_host)
        : endTx,
      hostRx: Array.isArray(record.rx_fifo_after_host)
        ? normaliseFifoWords(record.rx_fifo_after_host)
        : endRx,
      endTx,
      endRx,
    };
  }

  function compactLevelPath(levels) {
    const result = [];
    for (const rawLevel of levels) {
      const level = Math.max(0, Number(rawLevel) || 0);
      if (!result.length || result[result.length - 1] !== level) result.push(level);
    }
    return result;
  }

  function levelPathText(levels) {
    return compactLevelPath(levels).join(" → ");
  }

  function fifoWordsText(values) {
    return values.length ? values.map((value) => hex(value)).join(" · ") : "(empty)";
  }

  function updateFifoUsage(record) {
    const txCapacity = Math.max(0, Number(meta.tx_capacity) || 0);
    const rxCapacity = Math.max(0, Number(meta.rx_capacity) || 0);
    const boundary = fifoBoundaryState(record);
    const txLevels = [boundary.startTx.length, boundary.hostTx.length, boundary.endTx.length];
    const rxLevels = [boundary.startRx.length, boundary.hostRx.length, boundary.endRx.length];
    const txLevelText = levelPathText(txLevels) || "0";
    const rxLevelText = levelPathText(rxLevels) || "0";
    const usage = `TX ${txLevelText} / ${txCapacity} · RX ${rxLevelText} / ${rxCapacity}`;

    const card = document.getElementById("card-fifo-usage");
    if (card) card.textContent = usage;

    const txNode = document.getElementById("fifo-tx-level");
    const rxNode = document.getElementById("fifo-rx-level");
    const txCapacityNode = document.getElementById("fifo-tx-capacity");
    const rxCapacityNode = document.getElementById("fifo-rx-capacity");
    if (txCapacityNode) txCapacityNode.textContent = String(txCapacity);
    if (rxCapacityNode) rxCapacityNode.textContent = String(rxCapacity);
    if (txNode) {
      txNode.textContent = txLevelText;
      txNode.classList.toggle("full", txCapacity > 0 && Math.max(...txLevels) >= txCapacity);
    }
    if (rxNode) {
      rxNode.textContent = rxLevelText;
      rxNode.classList.toggle("full", rxCapacity > 0 && Math.max(...rxLevels) >= rxCapacity);
    }

    const summary = document.getElementById("fifo-level-summary");
    if (summary) {
      summary.title = record
        ? `Cycle ${selectedCycle} FIFO levels (cycle start → after host events → end of cycle): TX ${txLevelText} of ${txCapacity}; RX ${rxLevelText} of ${rxCapacity}.`
        : `No simulated cycle selected. TX capacity ${txCapacity}; RX capacity ${rxCapacity}.`;
    }
  }

  function formatIrqFlags(value) {
    return `0b${(Number(value) & 0xff).toString(2).padStart(8, "0")}`;
  }

  function updateIrqUsage(record) {
    const flags = record ? Number(record.irq_flags) & 0xff : 0;
    const text = formatIrqFlags(flags);
    const card = document.getElementById("card-irq-flags");
    const summary = document.getElementById("irq-flags");
    if (card) card.textContent = text;
    if (summary) summary.textContent = text;
    const wrapper = document.getElementById("irq-level-summary");
    if (wrapper) wrapper.title = record
      ? `End of cycle ${selectedCycle}: ${text}. Set IRQ flags: ${[...Array(8).keys()].filter((index) => flags & (1 << index)).map((index) => `IRQ${index}`).join(", ") || "none"}.`
      : "No simulated cycle selected.";
  }

  function currentViewState() {
    return {
      cycles: Math.max(1, Math.floor(Number(document.getElementById("simulation-cycles").value) || records.length || 1)),
      start: Math.max(0, Math.floor(Number(document.getElementById("start").value) || 0)),
      end: Math.max(1, Math.floor(Number(document.getElementById("end").value) || Math.min(records.length, 200) || 1)),
      scale: Number(document.getElementById("scale").value) || 24,
    };
  }

  function saveActiveSession() {
    if (!activeTraceKey) return;
    traceSessions.set(activeTraceKey, {
      embedded,
      displayPins,
      model,
      meta,
      baseWarnings,
      baselineStimuli,
      stimuli,
      records,
      initialState,
      runtimeWarnings,
      hostRxValues,
      selectedCycle,
      undoStack,
      redoStack,
      nextOrder,
      breakpoints: [...breakpoints],
      view: currentViewState(),
    });
  }

  function newSession(option) {
    const trace = option.trace;
    const baseline = normaliseStimuli(trace.stimuli || []);
    const optionRecords = trace.records || [];
    return {
      embedded: trace,
      displayPins: [...(option.pins || [])].map((pin) => Number(pin) & 31),
      model: trace.simulation,
      meta: trace.metadata,
      baseWarnings: [...((trace.metadata && trace.metadata.warnings) || [])],
      baselineStimuli: baseline,
      stimuli: deepClone(baseline),
      records: optionRecords,
      initialState: trace.initial_state || {},
      runtimeWarnings: [],
      hostRxValues: [],
      selectedCycle: 0,
      undoStack: [],
      redoStack: [],
      nextOrder: Math.max(0, ...baseline.map((event) => Number(event._order || 0))) + 1,
      breakpoints: [],
      view: {
        cycles: Math.max(1, Number(trace.metadata && trace.metadata.cycles) || optionRecords.length || 1),
        start: 0,
        end: Math.max(1, Math.min(optionRecords.length || 1, 200)),
        scale: 24,
      },
    };
  }

  function restoreSession(session) {
    ({
      embedded,
      displayPins,
      model,
      meta,
      baseWarnings,
      baselineStimuli,
      stimuli,
      records,
      initialState,
      runtimeWarnings,
      hostRxValues,
      selectedCycle,
      undoStack,
      redoStack,
      nextOrder,
    } = session);
    breakpoints = new Set((session.breakpoints || []).map((pc) => Number(pc)).filter(Number.isInteger));
    document.getElementById("simulation-cycles").value = session.view.cycles;
    document.getElementById("start").value = session.view.start;
    document.getElementById("end").value = session.view.end;
    document.getElementById("scale").value = session.view.scale;
  }

  function updateTraceChrome() {
    const title = `PIO logic trace — ${meta.program}`;
    document.title = title;
    document.getElementById("page-title").textContent = title;
    const subtitle = document.getElementById("trace-subtitle");
    const programName = document.createElement("span");
    programName.className = "mono";
    programName.textContent = String(meta.program);
    subtitle.replaceChildren(
      programName,
      document.createTextNode(` from ${model.program.source_path || "in-memory source"}; RP2040 PIO state machine ${meta.sm_id}`),
    );
    document.getElementById("card-cycles").textContent = records.length.toLocaleString();
    document.getElementById("card-frequency").textContent = fmtFrequency(meta.actual_freq_hz);
    document.getElementById("card-period").textContent = fmtTime(meta.period_s);
    document.getElementById("card-duration").textContent = fmtTime(records.length * Number(meta.period_s));
    document.getElementById("card-fifo-join").textContent = fifoJoinName(meta.fifo_join);
    const maximum = Math.max(0, records.length - 1);
    document.getElementById("cycle").max = maximum;
    document.getElementById("debug-cycle").max = maximum;
    document.getElementById("program-select").value = activeTraceKey;
  }

  function updateFifoControlAvailability() {
    const txButton = document.getElementById("fifo-add-tx");
    const rxButton = document.getElementById("fifo-add-rx");
    const rxGetButton = document.getElementById("fifo-rx-get");
    txButton.disabled = Number(meta.tx_capacity) <= 0;
    rxButton.disabled = Number(meta.rx_capacity) <= 0;
    rxGetButton.disabled = Number(meta.rx_capacity) <= 0;
    txButton.title = txButton.disabled ? "TX FIFO is disabled by FIFO join mode" : "";
    rxButton.title = rxButton.disabled ? "RX FIFO is disabled by FIFO join mode" : "";
    rxGetButton.title = rxGetButton.disabled ? "RX FIFO is disabled by FIFO join mode" : "";
  }

  function selectProgram(key) {
    key = String(key);
    if (!traceOptionsByKey.has(key) || key === activeTraceKey) return false;
    saveActiveSession();
    simulationToken += 1;
    activeTraceKey = key;
    activeOption = traceOptionsByKey.get(key);
    const session = traceSessions.get(key) || newSession(activeOption);
    restoreSession(session);
    traceSessions.set(key, session);
    selectedCycle = Math.max(0, Math.min(records.length - 1, selectedCycle));
    tooltip.hidden = true;
    updateTraceChrome();
    updateFifoControlAvailability();
    updateHistoryButtons();
    renderSourceCode();
    renderDisassembly();
    renderWarnings();
    renderStimulusTables();
    draw();
    inspect(selectedCycle);
    setStatus(`Selected ${meta.program} on state machine ${meta.sm_id}.`, "ready");
    return true;
  }

  function initialiseProgramSelector() {
    const select = document.getElementById("program-select");
    select.replaceChildren(...traceOptions.map((option) => {
      const element = document.createElement("option");
      element.value = String(option.key);
      element.textContent = String(option.label || option.program || option.key);
      return element;
    }));
    select.value = activeTraceKey;
    document.getElementById("program-selector-wrap").hidden = traceOptions.length <= 1;
  }

  function bit(pin) {
    return (1 << (Number(pin) & 31)) >>> 0;
  }

  function pinLevel(record, pin) {
    const mask = bit(pin);
    const outputEnabled = Boolean(record.pindirs & mask);
    const externallyDriven = Boolean(record.external_mask & mask);
    const output = record.pins & mask ? 1 : 0;
    const external = record.external_values & mask ? 1 : 0;
    if (outputEnabled) return externallyDriven && external !== output ? "X" : output;
    if (externallyDriven) return external;
    return "Z";
  }

  function externalLevel(record, pin) {
    const mask = bit(pin);
    if (!(record.external_mask & mask)) return "Z";
    return record.external_values & mask ? 1 : 0;
  }

  function readPin(record, pin) {
    const mask = bit(pin);
    if (record.pindirs & mask) return record.pins & mask ? 1 : 0;
    if (record.external_mask & mask) return record.external_values & mask ? 1 : 0;
    return Number(model.config.default_input) ? 1 : 0;
  }

  function instructionPcSet() {
    return new Set((model.program.instructions || []).map((instruction) => Number(instruction.pc)).filter(Number.isInteger));
  }

  function normaliseBreakpointPc(rawPc) {
    const pc = Number(rawPc);
    if (!Number.isInteger(pc) || !instructionPcSet().has(pc)) {
      throw new Error(`PIO PC ${JSON.stringify(rawPc)} is not present in the selected program`);
    }
    return pc;
  }

  function breakpointLabel(count = breakpoints.size) {
    return `${count} breakpoint${count === 1 ? "" : "s"}`;
  }

  function updateBreakpointVisuals() {
    for (const [pc, row] of disassemblyRowsByPc) {
      const active = breakpoints.has(pc);
      row.classList.toggle("has-breakpoint", active);
      const button = row.querySelector(".breakpoint-toggle");
      if (button) {
        button.setAttribute("aria-pressed", String(active));
        button.setAttribute("aria-label", `${active ? "Remove" : "Set"} breakpoint at PIO PC ${pc}`);
        button.title = `${active ? "Remove" : "Set"} breakpoint at PIO PC ${pc}`;
      }
    }

    sourceCode.querySelectorAll(".breakpoint-source").forEach((row) => row.classList.remove("breakpoint-source"));
    for (const [line, pcs] of instructionPcsByLine) {
      if (pcs.some((pc) => breakpoints.has(Number(pc))) && sourceLineElements.has(line)) {
        sourceLineElements.get(line).classList.add("breakpoint-source");
      }
    }

    const summary = document.getElementById("breakpoint-summary");
    if (summary) summary.textContent = breakpointLabel();
    const clearButton = document.getElementById("clear-breakpoints");
    if (clearButton) clearButton.disabled = breakpoints.size === 0;
  }

  function renderDisassembly() {
    disassemblyRowsByPc.clear();
    disassembly.replaceChildren();
    const instructions = [...(model.program.instructions || [])].sort((left, right) => Number(left.pc) - Number(right.pc));
    if (!instructions.length) {
      const empty = document.createElement("div");
      empty.className = "source-empty";
      empty.textContent = "No decoded PIO instructions are available for this function.";
      disassembly.appendChild(empty);
      updateBreakpointVisuals();
      return;
    }

    for (const instruction of instructions) {
      const pc = Number(instruction.pc);
      if (!Number.isInteger(pc)) continue;
      const row = document.createElement("div");
      row.className = "disassembly-row";
      row.dataset.pc = String(pc);
      row.setAttribute("role", "listitem");
      row.title = `PIO PC ${pc}: ${instruction.display || simulator.displayInstruction(instruction)}`;

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "breakpoint-toggle";
      toggle.dataset.pc = String(pc);
      toggle.setAttribute("aria-pressed", "false");
      const dot = document.createElement("span");
      dot.className = "breakpoint-dot";
      dot.setAttribute("aria-hidden", "true");
      const hidden = document.createElement("span");
      hidden.className = "sr-only";
      hidden.textContent = `Toggle breakpoint at PIO PC ${pc}`;
      toggle.append(dot, hidden);
      toggle.addEventListener("click", () => toggleBreakpoint(pc));

      const pcCell = document.createElement("span");
      pcCell.className = "disassembly-cell disassembly-pc";
      pcCell.textContent = String(pc);

      const wordCell = document.createElement("span");
      wordCell.className = "disassembly-cell disassembly-word";
      wordCell.textContent = instruction.word == null ? "—" : hex(instruction.word, 4);

      const instructionCell = document.createElement("code");
      instructionCell.className = "disassembly-cell disassembly-instruction";
      instructionCell.textContent = instruction.display || simulator.displayInstruction(instruction);

      const sourceCell = document.createElement("span");
      sourceCell.className = "disassembly-cell disassembly-source";
      const sourceLine = Number(instruction.source_line);
      sourceCell.textContent = Number.isInteger(sourceLine) && sourceLine > 0 ? `L${sourceLine}` : "—";
      sourceCell.title = Number.isInteger(sourceLine) && sourceLine > 0 ? `Input source line ${sourceLine}` : "No direct source mapping";

      row.append(toggle, pcCell, wordCell, instructionCell, sourceCell);
      disassembly.appendChild(row);
      disassemblyRowsByPc.set(pc, row);
    }
    updateBreakpointVisuals();
  }

  function toggleBreakpoint(rawPc, force = null) {
    const pc = normaliseBreakpointPc(rawPc);
    const shouldEnable = force == null ? !breakpoints.has(pc) : Boolean(force);
    if (shouldEnable) breakpoints.add(pc);
    else breakpoints.delete(pc);
    updateBreakpointVisuals();
    updateDebuggerControls();
    setStatus(`${shouldEnable ? "Breakpoint set" : "Breakpoint removed"} at PIO PC ${pc} · ${breakpointLabel()}.`, "ready");
    return shouldEnable;
  }

  function clearAllBreakpoints() {
    if (!breakpoints.size) return false;
    breakpoints.clear();
    updateBreakpointVisuals();
    updateDebuggerControls();
    setStatus("All breakpoints cleared for the selected PIO function.", "ready");
    return true;
  }

  function centreDisassemblyRow(row) {
    if (!row || !document.getElementById("follow-source").checked) return;
    const top = row.offsetTop - disassembly.offsetTop;
    const bottom = top + row.offsetHeight;
    const visibleTop = disassembly.scrollTop + 12;
    const visibleBottom = disassembly.scrollTop + disassembly.clientHeight - 12;
    if (top >= visibleTop && bottom <= visibleBottom) return;
    disassembly.scrollTop = Math.max(0, top - disassembly.clientHeight / 2 + row.offsetHeight / 2);
  }

  function highlightDisassembly(record) {
    disassembly.querySelectorAll(".current-disassembly,.stall-disassembly,.delay-disassembly,.met-disassembly").forEach((row) => {
      row.classList.remove("current-disassembly", "stall-disassembly", "delay-disassembly", "met-disassembly");
      row.removeAttribute("aria-current");
    });
    if (!record || record.instruction_pc == null) return;
    const pc = Number(record.instruction_pc);
    const row = disassemblyRowsByPc.get(pc);
    if (!row) return;
    row.classList.add("current-disassembly");
    row.setAttribute("aria-current", "true");
    const wait = decodeWait(record);
    if (record.stalled) row.classList.add("stall-disassembly");
    else if (wait && wait.met) row.classList.add("met-disassembly");
    else if (record.phase === "delay") row.classList.add("delay-disassembly");
    centreDisassemblyRow(row);
  }

  function isBreakpointRecord(record) {
    if (!record || record.instruction_pc == null || record.phase === "delay") return false;
    const pc = Number(record.instruction_pc);
    return Number.isInteger(pc) && breakpoints.has(pc);
  }

  function findNextBreakpointCycle(rawStart = selectedCycle) {
    if (!records.length || !breakpoints.size) return null;
    const start = Math.max(-1, Math.min(records.length - 1, Math.floor(Number(rawStart))));
    const current = start >= 0 ? records[start] : null;
    let currentBreakpointPc = current && isBreakpointRecord(current) ? Number(current.instruction_pc) : null;

    for (let index = start + 1; index < records.length; index += 1) {
      const record = records[index];
      const pc = record.instruction_pc == null ? null : Number(record.instruction_pc);
      if (currentBreakpointPc != null && pc === currentBreakpointPc) {
        const previous = records[index - 1];
        // A stalled instruction is retried at the same PC. Continue should run
        // through those retries (and the completion cycle) instead of stopping
        // again immediately. A same-PC execution after a completed cycle is a
        // new loop iteration and may legitimately hit the breakpoint again.
        if (record.phase === "delay" || (previous && (previous.stalled || previous.phase === "delay"))) continue;
        currentBreakpointPc = null;
      } else if (currentBreakpointPc != null) {
        currentBreakpointPc = null;
      }
      if (isBreakpointRecord(record)) return index;
    }
    return null;
  }

  function continueToBreakpoint() {
    if (!breakpoints.size) {
      setStatus("Set a breakpoint in the PIO disassembly before continuing.", "running");
      return null;
    }
    const cycle = findNextBreakpointCycle(selectedCycle);
    if (cycle == null) {
      setStatus(`No later breakpoint hit exists in the ${records.length.toLocaleString()} simulated cycles. Increase Simulation cycles or choose another breakpoint.`, "running");
      return null;
    }
    const pc = Number(records[cycle].instruction_pc);
    jumpToCycle(cycle, false);
    setStatus(`Breakpoint hit at PIO PC ${pc}, cycle ${cycle} (${fmtTime(records[cycle].time_s)}).`, "ready");
    return cycle;
  }

  function renderSourceCode() {
    sourceLineElements.clear();
    instructionPcsByLine.clear();
    sourceCode.replaceChildren();

    for (const instruction of model.program.instructions || []) {
      const line = Number(instruction.source_line);
      if (!Number.isInteger(line) || line < 1) continue;
      if (!instructionPcsByLine.has(line)) instructionPcsByLine.set(line, []);
      instructionPcsByLine.get(line).push(Number(instruction.pc));
    }

    const source = String(model.program.source_text || "").replaceAll("\r\n", "\n").replaceAll("\r", "\n");
    if (!source) {
      const empty = document.createElement("div");
      empty.className = "source-empty";
      empty.textContent = "The complete input source was not embedded in this trace. Regenerate the HTML with pico-pio-trace 0.3.0 or newer.";
      sourceCode.appendChild(empty);
      document.getElementById("source-location").textContent = String(model.program.source_path || "Source unavailable");
      updateBreakpointVisuals();
      return;
    }

    const lines = source.split("\n");
    const programStart = Number(model.program.source_line);
    const programEnd = Number(model.program.source_end_line);
    lines.forEach((text, index) => {
      const lineNumber = index + 1;
      const row = document.createElement("div");
      row.className = "source-line";
      row.id = `source-line-${lineNumber}`;
      row.dataset.line = String(lineNumber);
      if (Number.isInteger(programStart) && Number.isInteger(programEnd) && lineNumber >= programStart && lineNumber <= programEnd) {
        row.classList.add("program-line");
      }
      if (instructionPcsByLine.has(lineNumber)) {
        row.classList.add("instruction-source");
        row.title = `PIO instruction PC ${instructionPcsByLine.get(lineNumber).join(", ")}`;
      }

      const number = document.createElement("span");
      number.className = "line-number";
      number.textContent = String(lineNumber);
      number.setAttribute("aria-hidden", "true");
      const code = document.createElement("code");
      code.textContent = text || " ";
      row.append(number, code);
      sourceCode.appendChild(row);
      sourceLineElements.set(lineNumber, row);
    });
    document.getElementById("source-location").textContent = String(model.program.source_path || "in-memory source");
    updateBreakpointVisuals();
  }

  function sourceLineForRecord(record, index) {
    let line = Number(record && record.source_line);
    if (Number.isInteger(line) && line > 0) return line;

    const instructionPc = record && record.instruction_pc;
    if (instructionPc != null) {
      const instruction = (model.program.instructions || [])[Number(instructionPc)];
      line = Number(instruction && instruction.source_line);
      if (Number.isInteger(line) && line > 0) return line;
    }

    if (record && record.phase === "delay") {
      for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
        const candidate = Number(records[cursor] && records[cursor].source_line);
        if (Number.isInteger(candidate) && candidate > 0) return candidate;
        if (records[cursor] && records[cursor].phase !== "delay") break;
      }
    }
    return null;
  }

  function centreSourceLine(row) {
    if (!row || !document.getElementById("follow-source").checked) return;
    // ``offsetTop`` is relative to the row's offset parent, which is not
    // guaranteed to be the scrolling source element. Convert both offsets to
    // the same coordinate system before comparing against ``scrollTop``.
    const top = row.offsetTop - sourceCode.offsetTop;
    const bottom = top + row.offsetHeight;
    const visibleTop = sourceCode.scrollTop + 28;
    const visibleBottom = sourceCode.scrollTop + sourceCode.clientHeight - 28;
    if (top >= visibleTop && bottom <= visibleBottom) return;
    sourceCode.scrollTop = Math.max(0, top - sourceCode.clientHeight / 2 + row.offsetHeight / 2);
  }

  function highlightSource(record, index) {
    sourceCode.querySelectorAll(".current-source,.delay-source,.stall-source,.met-source").forEach((row) => {
      row.classList.remove("current-source", "delay-source", "stall-source", "met-source");
    });
    const line = sourceLineForRecord(record, index);
    const path = String(model.program.source_path || "in-memory source");
    const pcText = record.instruction_pc == null ? "forced/generated instruction" : `PIO PC ${record.instruction_pc}`;
    if (line == null || !sourceLineElements.has(line)) {
      document.getElementById("source-location").textContent = `${path} · no direct source line · ${pcText}`;
      return;
    }
    const row = sourceLineElements.get(line);
    row.classList.add("current-source");
    const wait = decodeWait(record);
    if (record.stalled) row.classList.add("stall-source");
    else if (wait && wait.met) row.classList.add("met-source");
    else if (record.phase === "delay") row.classList.add("delay-source");
    const phase = record.stalled ? "WAIT stalled" : record.phase === "delay" ? "delay cycle" : record.phase;
    document.getElementById("source-location").textContent = `${path}:${line} · ${pcText} · ${phase}`;
    centreSourceLine(row);
  }

  function normaliseState(raw) {
    raw = raw || {};
    return {
      pc: Number(raw.pc ?? 0),
      x: Number(raw.x ?? 0) >>> 0,
      y: Number(raw.y ?? 0) >>> 0,
      isr: Number(raw.isr ?? 0) >>> 0,
      osr: Number(raw.osr ?? 0) >>> 0,
      isr_count: Number(raw.isr_count ?? 0),
      osr_count: Number(raw.osr_count ?? 32),
      pins: Number(raw.pins ?? 0) >>> 0,
      pindirs: Number(raw.pindirs ?? 0) >>> 0,
      external_mask: Number(raw.external_mask ?? 0) >>> 0,
      external_values: Number(raw.external_values ?? 0) >>> 0,
      irq_flags: Number(raw.irq_flags ?? 0) & 0xff,
      tx_fifo: [...(raw.tx_fifo || [])].map((value) => Number(value) >>> 0),
      rx_fifo: [...(raw.rx_fifo || [])].map((value) => Number(value) >>> 0),
      delay_remaining: Number(raw.delay_remaining ?? 0),
      exec_latch: raw.exec_latch == null ? null : Number(raw.exec_latch) & 0xffff,
      pending_kind: raw.pending_kind == null ? null : String(raw.pending_kind),
      halted_reason: raw.halted_reason == null ? null : String(raw.halted_reason),
    };
  }

  function stateFromRecord(record, index) {
    const inferredPc = record.state_pc == null
      ? (records[index + 1] ? Number(records[index + 1].pc) : Number(record.pc))
      : Number(record.state_pc);
    return normaliseState({
      ...record,
      pc: inferredPc,
    });
  }

  function stateBeforeCycle(index) {
    return index <= 0 ? normaliseState(initialState) : stateFromRecord(records[index - 1], index - 1);
  }

  function sameValue(left, right) {
    if (Array.isArray(left) || Array.isArray(right)) return JSON.stringify(left || []) === JSON.stringify(right || []);
    return left === right;
  }

  function stateValue(name, value) {
    if (["x", "y", "isr", "osr", "pins", "pindirs", "external_mask", "external_values"].includes(name)) return hex(value);
    if (name === "irq_flags") return `0b${Number(value).toString(2).padStart(8, "0")}`;
    if (name === "exec_latch") return value == null ? "(empty)" : hex(value, 4);
    if (name === "pending_kind") return value == null ? "(none)" : String(value);
    if (name === "halted_reason") return value == null ? "(running)" : String(value);
    if (name === "tx_fifo" || name === "rx_fifo") return value.length ? value.map((word) => hex(word)).join(" · ") : "(empty)";
    return String(value);
  }

  function renderStateChanges(before, after) {
    const definitions = [
      ["PC", "pc"], ["X", "x"], ["Y", "y"], ["ISR", "isr"], ["ISR count", "isr_count"],
      ["OSR", "osr"], ["OSR count", "osr_count"], ["Pin output latch", "pins"],
      ["Pin directions", "pindirs"], ["External mask", "external_mask"], ["External values", "external_values"],
      ["IRQ flags", "irq_flags"], ["TX FIFO", "tx_fifo"], ["RX FIFO", "rx_fifo"],
      ["Delay remaining", "delay_remaining"], ["EXEC latch", "exec_latch"], ["Pending operation", "pending_kind"],
      ["Halt state", "halted_reason"],
    ];
    const changes = definitions.filter(([, key]) => !sameValue(before[key], after[key]));
    const list = document.getElementById("state-changes");
    if (!changes.length) {
      list.innerHTML = '<li class="unchanged">No tracked architectural state changed during this cycle.</li>';
      return;
    }
    list.innerHTML = changes.map(([label, key]) => `<li><span class="changed-key">${esc(label)}</span>: ${esc(stateValue(key, before[key]))} → ${esc(stateValue(key, after[key]))}</li>`).join("");
  }

  function logicClass(value) {
    if (value === 1) return "logic-high";
    if (value === 0) return "logic-low";
    if (value === "X") return "logic-x";
    return "logic-z";
  }

  function renderPinState(record) {
    const body = displayPins.map((pin) => {
      const resolved = pinLevel(record, pin);
      const external = externalLevel(record, pin);
      const output = record.pins & bit(pin) ? 1 : 0;
      const oe = record.pindirs & bit(pin) ? 1 : 0;
      const read = readPin(record, pin);
      return `<tr><td>GPIO${pin}</td><td class="${logicClass(resolved)}">${resolved}</td><td>${read}</td><td class="${logicClass(external)}">${external}</td><td>${oe}</td><td>${output}</td></tr>`;
    }).join("");
    document.getElementById("pin-state").innerHTML = `<table><thead><tr><th>Pin</th><th>Resolved</th><th>PIO read</th><th>External</th><th>OE</th><th>Output latch</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  function updateDebuggerControls() {
    const maximum = Math.max(0, records.length - 1);
    const cycleInput = document.getElementById("debug-cycle");
    cycleInput.max = maximum;
    cycleInput.value = Math.max(0, Math.min(maximum, selectedCycle));
    document.getElementById("step-back").disabled = !records.length || selectedCycle <= 0;
    document.getElementById("step-forward").disabled = !records.length || selectedCycle >= maximum;
    document.getElementById("continue-breakpoint").disabled = !records.length || breakpoints.size === 0 || selectedCycle >= maximum;
    document.getElementById("clear-breakpoints").disabled = breakpoints.size === 0;
  }

  function addText(x, y, text, className = "axis-label", anchor = "start") {
    wave.appendChild(svgElement("text", { x, y, class: className, "text-anchor": anchor }, text));
  }

  function digitalPath(values, x0, top, cycleWidth, rowHeight, className) {
    const high = top + 8;
    const low = top + rowHeight - 8;
    const middle = top + rowHeight / 2;
    const y = (value) => value === 1 ? high : value === 0 ? low : middle;
    let path = "";
    values.forEach((value, index) => {
      const x = x0 + index * cycleWidth;
      const currentY = y(value);
      if (index === 0) path += `M ${x} ${currentY}`;
      else path += ` L ${x} ${y(values[index - 1])} L ${x} ${currentY}`;
      path += ` L ${x + cycleWidth} ${currentY}`;
      if ((value === "Z" || value === "X") && (values.length <= 300 || index === 0 || value !== values[index - 1])) {
        addText(x + cycleWidth / 2, middle, value, "zx", "middle");
      }
    });
    wave.appendChild(svgElement("path", { d: path, class: className }));
  }

  function numericPath(values, maximum, x0, top, cycleWidth, rowHeight) {
    const y = (value) => top + rowHeight - 7 - (maximum ? Number(value) / maximum * (rowHeight - 14) : 0);
    let path = "";
    values.forEach((value, index) => {
      const x = x0 + index * cycleWidth;
      const currentY = y(value);
      if (index === 0) path += `M ${x} ${currentY}`;
      else path += ` L ${x} ${y(values[index - 1])} L ${x} ${currentY}`;
      path += ` L ${x + cycleWidth} ${currentY}`;
      if (index === 0 || value !== values[index - 1]) addText(x + 4, top + 11, String(value), "axis-label");
    });
    wave.appendChild(svgElement("path", { d: path, class: "fifo-line" }));
  }

  function fifoBoundaryPath(windowRecords, absoluteStart, fifo, maximum, x0, top, cycleWidth, rowHeight) {
    const y = (value) => top + rowHeight - 7 - (maximum ? Number(value) / maximum * (rowHeight - 14) : 0);
    let path = "";
    windowRecords.forEach((record, index) => {
      const boundary = fifoBoundaryState(record, absoluteStart + index);
      const levels = fifo === "tx"
        ? [boundary.startTx.length, boundary.hostTx.length, boundary.endTx.length]
        : [boundary.startRx.length, boundary.hostRx.length, boundary.endRx.length];
      const x = x0 + index * cycleWidth;
      const hostX = x + cycleWidth * 0.32;
      const endX = x + cycleWidth * 0.68;
      if (!path) path = `M ${x} ${y(levels[0])}`;
      else path += ` L ${x} ${y(levels[0])}`;
      path += ` L ${hostX} ${y(levels[0])}`;
      path += ` L ${hostX} ${y(levels[1])}`;
      path += ` L ${endX} ${y(levels[1])}`;
      path += ` L ${endX} ${y(levels[2])}`;
      path += ` L ${x + cycleWidth} ${y(levels[2])}`;

      const compact = compactLevelPath(levels);
      if (compact.length > 1) {
        addText(x + 3, top + 11, compact.join("→"), "axis-label");
      } else if (index === 0 || levels[2] !== (fifo === "tx"
        ? fifoBoundaryState(windowRecords[index - 1], absoluteStart + index - 1).endTx.length
        : fifoBoundaryState(windowRecords[index - 1], absoluteStart + index - 1).endRx.length)) {
        addText(x + 4, top + 11, String(levels[2]), "axis-label");
      }
    });
    wave.appendChild(svgElement("path", { d: path, class: "fifo-line" }));
  }

  function busRow(values, x0, top, cycleWidth, rowHeight, classResolver = null) {
    let index = 0;
    while (index < values.length) {
      let end = index + 1;
      while (end < values.length && values[end] === values[index]) end += 1;
      const x = x0 + index * cycleWidth;
      const width = (end - index) * cycleWidth;
      const value = String(values[index]);
      const className = classResolver ? classResolver(value) : "bus-box";
      wave.appendChild(svgElement("rect", { x, y: top + 4, width, height: rowHeight - 8, rx: 3, class: className }));
      if (width > 20 && value) addText(x + width / 2, top + rowHeight / 2, value.slice(0, Math.max(2, Math.floor(width / 7))), "bus-text", "middle");
      index = end;
    }
  }

  function resolveIrqIndex(rawIndex, relative) {
    const index = Number(rawIndex) & 0x7;
    if (!relative) return index;
    return (index & 0x4) | ((index + (Number(model.config.sm_id) & 0x3)) & 0x3);
  }

  function decodeWait(record) {
    if (record.instruction_word == null) return null;
    let instruction;
    try {
      instruction = simulator.decodeInstruction(Number(record.instruction_word), model.program, String(record.instruction).startsWith("EXEC:"));
    } catch (_error) {
      return null;
    }
    if (instruction.op !== "wait") return null;
    const [polarity, source, index, relative] = instruction.args;
    let pin = null;
    let irqIndex = null;
    if (source === "gpio") pin = Number(index) & 31;
    else if (source === "pin") pin = (Number(model.config.in_base) + Number(index)) & 31;
    else if (source === "irq") irqIndex = resolveIrqIndex(index, Boolean(relative));
    const met = !record.stalled && (record.events || []).some((event) => String(event).startsWith("WAIT condition met"));
    return {
      polarity: Number(polarity) & 1,
      source,
      index: Number(index),
      relative: Boolean(relative),
      pin,
      irqIndex,
      stalled: Boolean(record.stalled),
      met,
    };
  }

  function waitLabel(record) {
    const wait = decodeWait(record);
    if (!wait || (!wait.stalled && !wait.met)) return "";
    const target = wait.pin != null ? `GPIO${wait.pin}` : wait.irqIndex != null ? `IRQ${wait.irqIndex}` : `${wait.source.toUpperCase()}${wait.index}`;
    return `${wait.met ? "MET" : "WAIT"} ${target}=${wait.polarity}`;
  }

  function pinEvents(pin, start, end) {
    return stimuli.filter((event) => isPinEvent(event) && Number(event.pin) === Number(pin) && event.cycle >= start && event.cycle < end);
  }

  function drawEventMarkers(pin, start, end, left, top, cycleWidth, rowHeight) {
    for (const event of pinEvents(pin, start, end)) {
      const x = left + (event.cycle - start) * cycleWidth;
      const state = event.value == null || String(event.value).toUpperCase() === "Z" ? "Z" : Number(event.value) ? "1" : "0";
      wave.appendChild(svgElement("line", { x1: x, y1: top + 3, x2: x, y2: top + rowHeight - 3, class: "event-marker" }));
      addText(x + Math.min(cycleWidth / 2, 7), top + 11, state, "event-marker-label", "middle");
    }
  }

  function fifoEventsFor(fifo, start, end) {
    return orderedStimuli(stimuli.filter((event) => {
      if (event.cycle < start || event.cycle >= end) return false;
      return fifo === "tx" ? isTxPutEvent(event) : isRxPutEvent(event) || isRxGetEvent(event);
    }));
  }

  function drawFifoEventMarkers(fifo, start, end, left, top, cycleWidth, rowHeight) {
    for (const event of fifoEventsFor(fifo, start, end)) {
      const x = left + (event.cycle - start) * cycleWidth;
      const label = isTxPutEvent(event) ? "T+" : isRxPutEvent(event) ? "R+" : "R−";
      wave.appendChild(svgElement("line", { x1: x, y1: top + 3, x2: x, y2: top + rowHeight - 3, class: "event-marker" }));
      addText(x + Math.min(cycleWidth / 2, 9), top + 11, label, "event-marker-label", "middle");
    }
  }

  function irqEventsFor(index, start, end) {
    return orderedStimuli(stimuli.filter((event) => {
      if (!isIrqEvent(event) || event.cycle < start || event.cycle >= end) return false;
      return index == null || irqEventIndex(event) === Number(index);
    }));
  }

  function drawIrqEventMarkers(index, start, end, left, top, cycleWidth, rowHeight) {
    for (const event of irqEventsFor(index, start, end)) {
      const x = left + (event.cycle - start) * cycleWidth;
      const label = irqEventSets(event) ? "I+" : "I−";
      wave.appendChild(svgElement("line", { x1: x, y1: top + 3, x2: x, y2: top + rowHeight - 3, class: "irq-event-marker" }));
      addText(x + Math.min(cycleWidth / 2, 9), top + 11, label, "irq-event-marker-label", "middle");
    }
  }

  function usedIrqIndices() {
    const indices = new Set(stimuli.filter(isIrqEvent).map(irqEventIndex));
    for (const instruction of model.program.instructions || []) {
      if (instruction.op === "irq") {
        const [, index, relative] = instruction.args || [];
        indices.add(resolveIrqIndex(index, Boolean(relative)));
      } else if (instruction.op === "wait") {
        const [, source, index, relative] = instruction.args || [];
        if (source === "irq") indices.add(resolveIrqIndex(index, Boolean(relative)));
      }
    }
    return [...indices].filter((index) => Number.isInteger(index) && index >= 0 && index <= 7).sort((a, b) => a - b);
  }

  function drawWaitHighlights(pin, windowRecords, left, top, cycleWidth, rowHeight) {
    windowRecords.forEach((record, index) => {
      const wait = decodeWait(record);
      if (!wait || wait.pin !== pin || (!wait.stalled && !wait.met)) return;
      wave.appendChild(svgElement("rect", {
        x: left + index * cycleWidth,
        y: top + 1,
        width: cycleWidth,
        height: rowHeight - 2,
        class: wait.met ? "wait-met-highlight" : "wait-highlight",
      }));
    });
  }

  function drawIrqWaitHighlights(irqIndex, windowRecords, left, top, cycleWidth, rowHeight) {
    windowRecords.forEach((record, index) => {
      const wait = decodeWait(record);
      if (!wait || wait.irqIndex !== irqIndex || (!wait.stalled && !wait.met)) return;
      wave.appendChild(svgElement("rect", {
        x: left + index * cycleWidth,
        y: top + 1,
        width: cycleWidth,
        height: rowHeight - 2,
        class: wait.met ? "wait-met-highlight" : "wait-highlight",
      }));
    });
  }

  function draw() {
    const total = records.length;
    const requestedStart = Math.max(0, Math.floor(Number(document.getElementById("start").value) || 0));
    let start = total ? Math.min(total - 1, requestedStart) : 0;
    let end = Math.min(total, Math.floor(Number(document.getElementById("end").value) || total));
    if (end <= start) end = Math.min(total, start + 1);
    if (!total) { start = 0; end = 0; }
    document.getElementById("start").value = start;
    document.getElementById("end").value = end;

    const cycleWidth = Number(document.getElementById("scale").value);
    const left = 178;
    const rowHeight = 34;
    const top = 52;
    const windowRecords = records.slice(start, end);
    const channels = [];
    for (const pin of displayPins) {
      channels.push({ label: `GPIO${pin}`, kind: "pin", pin, editable: true });
      channels.push({ label: `GPIO${pin} external`, kind: "external", pin, editable: true });
      channels.push({ label: `GPIO${pin} OE`, kind: "dir", pin, editable: false });
    }
    for (const irqIndex of usedIrqIndices()) {
      channels.push({ label: `IRQ${irqIndex}`, kind: "irqbit", irqIndex, editable: false });
    }
    channels.push(
      { label: "IRQ flags", kind: "irqflags" },
      { label: "WAIT target", kind: "wait" },
      { label: "TX_FIFO level", kind: "tx" },
      { label: "TX_FIFO front · host", kind: "txhostfront" },
      { label: "TX_FIFO front · end", kind: "txfront" },
      { label: "RX_FIFO level", kind: "rx" },
      { label: "RX_FIFO front · host", kind: "rxhostfront" },
      { label: "RX_FIFO front · end", kind: "rxfront" },
      { label: "Instruction PC", kind: "instructionpc" },
      { label: "PC after cycle", kind: "statepc" },
      { label: "Phase", kind: "phase" },
      { label: "Instruction", kind: "instruction" },
    );

    const width = left + windowRecords.length * cycleWidth + 18;
    const height = top + channels.length * rowHeight + 38;
    wave.replaceChildren();
    wave.setAttribute("width", Math.max(width, left + 30));
    wave.setAttribute("height", height);
    wave.setAttribute("viewBox", `0 0 ${Math.max(width, left + 30)} ${height}`);

    // Choose a human-friendly tick interval from the actual pixel density so
    // time/cycle labels remain readable in both narrow and wide windows.
    const desiredStep = Math.max(1, 92 / cycleWidth);
    const magnitude = 10 ** Math.floor(Math.log10(desiredStep));
    const normalisedStep = desiredStep / magnitude;
    const niceFactor = normalisedStep <= 1 ? 1 : normalisedStep <= 2 ? 2 : normalisedStep <= 5 ? 5 : 10;
    const major = Math.max(1, niceFactor * magnitude);
    for (let index = 0; index <= windowRecords.length; index += 1) {
      const cycle = start + index;
      const x = left + index * cycleWidth;
      const isMajor = cycle % major === 0;
      if (windowRecords.length <= 250 || isMajor) wave.appendChild(svgElement("line", { x1: x, y1: top - 20, x2: x, y2: height - 24, class: isMajor ? "major-grid" : "grid" }));
      if (isMajor && index < windowRecords.length) {
        addText(x + 2, 17, fmtTime(cycle * Number(meta.period_s)), "axis-label");
        addText(x + 2, height - 8, String(cycle), "axis-label");
      }
    }
    addText(5, 17, "time", "axis-label");
    addText(5, height - 8, "cycle", "axis-label");

    channels.forEach((channel, rowIndex) => {
      const y = top + rowIndex * rowHeight;
      wave.appendChild(svgElement("line", { x1: 0, y1: y + rowHeight, x2: width, y2: y + rowHeight, class: "grid" }));
      addText(8, y + rowHeight / 2 + 4, channel.label, channel.editable ? "row-label editable-label" : "row-label");

      if (channel.kind === "pin") {
        drawWaitHighlights(channel.pin, windowRecords, left, y, cycleWidth, rowHeight);
        digitalPath(windowRecords.map((record) => pinLevel(record, channel.pin)), left, y, cycleWidth, rowHeight, "wave-line");
        drawEventMarkers(channel.pin, start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "external") {
        drawWaitHighlights(channel.pin, windowRecords, left, y, cycleWidth, rowHeight);
        digitalPath(windowRecords.map((record) => externalLevel(record, channel.pin)), left, y, cycleWidth, rowHeight, "external-line");
        drawEventMarkers(channel.pin, start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "dir") {
        digitalPath(windowRecords.map((record) => record.pindirs & bit(channel.pin) ? 1 : 0), left, y, cycleWidth, rowHeight, "dir-line");
      } else if (channel.kind === "irqbit") {
        drawIrqWaitHighlights(channel.irqIndex, windowRecords, left, y, cycleWidth, rowHeight);
        digitalPath(windowRecords.map((record) => record.irq_flags & (1 << channel.irqIndex) ? 1 : 0), left, y, cycleWidth, rowHeight, "irq-line");
        drawIrqEventMarkers(channel.irqIndex, start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "irqflags") {
        busRow(windowRecords.map((record) => formatIrqFlags(record.irq_flags)), left, y, cycleWidth, rowHeight);
        drawIrqEventMarkers(null, start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "wait") {
        busRow(windowRecords.map(waitLabel), left, y, cycleWidth, rowHeight, (value) => value.startsWith("MET") ? "wait-met-box" : value.startsWith("WAIT") ? "wait-box" : "bus-box");
      } else if (channel.kind === "tx") {
        fifoBoundaryPath(windowRecords, start, "tx", Number(meta.tx_capacity), left, y, cycleWidth, rowHeight);
        drawFifoEventMarkers("tx", start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "txhostfront") {
        busRow(windowRecords.map((record, index) => {
          const words = fifoBoundaryState(record, start + index).hostTx;
          return words.length ? hex(words[0]) : "empty";
        }), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "txfront") {
        busRow(windowRecords.map((record) => record.tx_fifo.length ? hex(record.tx_fifo[0]) : "empty"), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "rx") {
        fifoBoundaryPath(windowRecords, start, "rx", Number(meta.rx_capacity), left, y, cycleWidth, rowHeight);
        drawFifoEventMarkers("rx", start, end, left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "rxhostfront") {
        busRow(windowRecords.map((record, index) => {
          const words = fifoBoundaryState(record, start + index).hostRx;
          return words.length ? hex(words[0]) : "empty";
        }), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "rxfront") {
        busRow(windowRecords.map((record) => record.rx_fifo.length ? hex(record.rx_fifo[0]) : "empty"), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "instructionpc") {
        busRow(windowRecords.map((record) => record.instruction_pc == null ? "—" : record.instruction_pc), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "statepc") {
        busRow(windowRecords.map((record, index) => record.state_pc == null ? (windowRecords[index + 1] ? windowRecords[index + 1].pc : record.pc) : record.state_pc), left, y, cycleWidth, rowHeight);
      } else if (channel.kind === "phase") {
        busRow(windowRecords.map((record) => record.stalled ? `stall: ${record.stall_reason}` : record.phase), left, y, cycleWidth, rowHeight, (value) => value.startsWith("stall") ? "stall-box" : "bus-box");
      } else {
        busRow(windowRecords.map((record) => record.instruction), left, y, cycleWidth, rowHeight);
      }

      wave.appendChild(svgElement("rect", {
        x: left,
        y,
        width: Math.max(0, windowRecords.length * cycleWidth),
        height: rowHeight,
        class: channel.editable && activeTool !== "inspect" ? "hit-row" : "hit-row inspect",
        "data-row": rowIndex,
      }));
    });

    drawState = { start, end, cycleWidth, left, rowHeight, top, width, height, channels };
    updateCursor();
    renderDataTable(start, end);
  }

  function svgCoordinates(event) {
    const box = wave.getBoundingClientRect();
    const viewBox = wave.viewBox.baseVal;
    return {
      x: (event.clientX - box.left) * viewBox.width / box.width,
      y: (event.clientY - box.top) * viewBox.height / box.height,
    };
  }

  function locationFromPointer(event) {
    if (!drawState) return null;
    const point = svgCoordinates(event);
    if (point.x < drawState.left || point.y < drawState.top) return null;
    const rowIndex = Math.floor((point.y - drawState.top) / drawState.rowHeight);
    if (rowIndex < 0 || rowIndex >= drawState.channels.length) return null;
    const cycle = Math.floor((point.x - drawState.left) / drawState.cycleWidth) + drawState.start;
    if (cycle < drawState.start || cycle >= drawState.end) return null;
    return { point, rowIndex, channel: drawState.channels[rowIndex], cycle };
  }

  function handleWaveClick(event) {
    const location = locationFromPointer(event);
    if (!location) return;
    selectedCycle = location.cycle;
    inspect(location.cycle);
    if (location.channel.editable && activeTool !== "inspect") {
      applyPinEdit(location.cycle, location.channel.pin, activeTool);
    }
  }

  function handleWaveMove(event) {
    const location = locationFromPointer(event);
    if (!location || !records[location.cycle]) {
      tooltip.hidden = true;
      return;
    }
    const record = records[location.cycle];
    const channel = location.channel;
    const lines = [`Cycle ${location.cycle} · ${fmtTime(record.time_s)}`, channel.label];
    if (channel.pin != null) {
      lines.push(`line=${pinLevel(record, channel.pin)}, external=${externalLevel(record, channel.pin)}, read=${readPin(record, channel.pin)}, OE=${record.pindirs & bit(channel.pin) ? 1 : 0}`);
      if (channel.editable) lines.push(activeTool === "inspect" ? "Click to inspect" : `Click to apply ${toolDescription(activeTool)}`);
    } else if (["tx", "txhostfront", "txfront"].includes(channel.kind)) {
      const boundary = fifoBoundaryState(record, location.cycle);
      lines.push(`TX cycle start [${boundary.startTx.length}/${meta.tx_capacity}]: ${fifoWordsText(boundary.startTx)}`);
      lines.push(`TX after host events [${boundary.hostTx.length}/${meta.tx_capacity}]: ${fifoWordsText(boundary.hostTx)}`);
      lines.push(`TX end of cycle [${boundary.endTx.length}/${meta.tx_capacity}]: ${fifoWordsText(boundary.endTx)}`);
      const events = fifoEventsFor("tx", location.cycle, location.cycle + 1);
      if (events.length) lines.push(`${events.length} TX put event${events.length === 1 ? "" : "s"} scheduled before this cycle`);
    } else if (["rx", "rxhostfront", "rxfront"].includes(channel.kind)) {
      const boundary = fifoBoundaryState(record, location.cycle);
      lines.push(`RX cycle start [${boundary.startRx.length}/${meta.rx_capacity}]: ${fifoWordsText(boundary.startRx)}`);
      lines.push(`RX after host events [${boundary.hostRx.length}/${meta.rx_capacity}]: ${fifoWordsText(boundary.hostRx)}`);
      lines.push(`RX end of cycle [${boundary.endRx.length}/${meta.rx_capacity}]: ${fifoWordsText(boundary.endRx)}`);
      const events = fifoEventsFor("rx", location.cycle, location.cycle + 1);
      if (events.length) lines.push(`${events.length} RX event${events.length === 1 ? "" : "s"} scheduled before this cycle`);
    } else if (channel.kind === "irqbit") {
      lines.push(`IRQ${channel.irqIndex}=${record.irq_flags & (1 << channel.irqIndex) ? 1 : 0}; all flags ${formatIrqFlags(record.irq_flags)}`);
      const events = irqEventsFor(channel.irqIndex, location.cycle, location.cycle + 1);
      if (events.length) lines.push(`${events.length} IRQ event${events.length === 1 ? "" : "s"} scheduled before this cycle`);
    } else if (channel.kind === "irqflags") {
      lines.push(`flags=${formatIrqFlags(record.irq_flags)} (${hex(record.irq_flags, 2)})`);
      const events = irqEventsFor(null, location.cycle, location.cycle + 1);
      if (events.length) lines.push(`${events.length} IRQ event${events.length === 1 ? "" : "s"} scheduled before this cycle`);
    }
    const wait = decodeWait(record);
    if (wait && (wait.stalled || wait.met)) lines.push(wait.met ? "WAIT condition met" : waitLabel(record));
    tooltip.textContent = lines.join("\n");
    tooltip.style.whiteSpace = "pre-line";
    tooltip.style.left = `${Math.min(window.innerWidth - 360, event.clientX + 14)}px`;
    tooltip.style.top = `${Math.min(window.innerHeight - 120, event.clientY + 14)}px`;
    tooltip.hidden = false;
  }

  function updateCursor() {
    wave.querySelectorAll(".cursor-line,.cursor-start-line,.selected-cycle-band").forEach((node) => node.remove());
    if (!drawState || selectedCycle < drawState.start || selectedCycle >= drawState.end) return;
    const xStart = drawState.left + (selectedCycle - drawState.start) * drawState.cycleWidth;
    const xEnd = xStart + drawState.cycleWidth;
    wave.appendChild(svgElement("rect", {
      x: xStart,
      y: drawState.top - 20,
      width: drawState.cycleWidth,
      height: drawState.height - drawState.top - 4,
      class: "selected-cycle-band",
    }));
    wave.appendChild(svgElement("line", { x1: xStart, y1: drawState.top - 20, x2: xStart, y2: drawState.height - 24, class: "cursor-start-line" }));
    wave.appendChild(svgElement("line", { x1: xEnd, y1: drawState.top - 20, x2: xEnd, y2: drawState.height - 24, class: "cursor-line" }));
  }

  function ensureCursorVisible() {
    if (!drawState || selectedCycle < drawState.start || selectedCycle >= drawState.end) return;
    const wrap = document.getElementById("wave-wrap");
    const cursorCenter = drawState.left + (selectedCycle - drawState.start + 0.5) * drawState.cycleWidth;
    const margin = Math.min(80, Math.max(18, wrap.clientWidth / 8));
    const visibleLeft = wrap.scrollLeft + margin;
    const visibleRight = wrap.scrollLeft + wrap.clientWidth - margin;
    if (cursorCenter < visibleLeft || cursorCenter > visibleRight) {
      wrap.scrollLeft = Math.max(0, cursorCenter - wrap.clientWidth / 2);
    }
  }

  function inspect(rawIndex) {
    if (!records.length) {
      updateFifoUsage(null);
      updateIrqUsage(null);
      highlightDisassembly(null);
      updateDebuggerControls();
      return;
    }
    const index = Math.max(0, Math.min(records.length - 1, Math.floor(Number(rawIndex) || 0)));
    selectedCycle = index;
    document.getElementById("cycle").value = index;
    document.getElementById("debug-cycle").value = index;
    if (document.getElementById("fifo-follow-selected").checked) document.getElementById("fifo-cycle").value = index;
    if (document.getElementById("irq-follow-selected").checked) document.getElementById("irq-cycle").value = index;
    const record = records[index];
    updateFifoUsage(record);
    updateIrqUsage(record);
    const before = stateBeforeCycle(index);
    const after = stateFromRecord(record, index);
    const startTime = Number(record.time_s);
    const endTime = (index + 1) * Number(meta.period_s);

    document.getElementById("inspect-title").textContent = `Cycle ${index} result · ${fmtTime(endTime)}`;
    document.getElementById("debugger-moment").textContent = `Cycle ${index} spans ${fmtTime(startTime)} → ${fmtTime(endTime)}. The values below are sampled at the end of the highlighted cycle.`;
    document.getElementById("state-sampling-note").textContent = "External/host events scheduled for this cycle are applied first, then the PIO instruction executes or stalls. Registers, GPIOs, and IRQ flags are end-of-cycle values. The FIFO panel additionally shows the cycle-start and post-host-event boundaries so same-cycle writes and reads remain visible.";

    const changedHtml = (current, previous, formatter = (value) => String(value)) => {
      const changed = !sameValue(current, previous);
      const prior = changed ? `<span class="previous-value">before cycle: ${esc(formatter(previous))}</span>` : "";
      return { changed, html: `${esc(formatter(current))}${prior}` };
    };
    const rows = [];
    const addRow = (label, current, previous, formatter) => {
      const rendered = previous === undefined
        ? { changed: false, html: esc(formatter ? formatter(current) : String(current)) }
        : changedHtml(current, previous, formatter);
      rows.push(`<dt>${esc(label)}</dt><dd${rendered.changed ? ' class="changed"' : ""}>${rendered.html}</dd>`);
    };

    addRow("Instruction PC", record.instruction_pc == null ? "—" : record.instruction_pc, undefined);
    addRow("PC after cycle", after.pc, before.pc);
    addRow("Phase", record.stalled ? `STALL — ${record.stall_reason}` : record.phase, undefined);
    addRow("X", after.x, before.x, (value) => hex(value));
    addRow("Y", after.y, before.y, (value) => hex(value));
    addRow("ISR / shifted bits", `${hex(after.isr)} / ${after.isr_count}`, `${hex(before.isr)} / ${before.isr_count}`);
    addRow("OSR / shifted bits", `${hex(after.osr)} / ${after.osr_count}`, `${hex(before.osr)} / ${before.osr_count}`);
    addRow("IRQ flags", after.irq_flags, before.irq_flags, (value) => `0b${Number(value).toString(2).padStart(8, "0")}`);
    addRow("Pin output latch", after.pins, before.pins, (value) => hex(value));
    addRow("Pin directions", after.pindirs, before.pindirs, (value) => hex(value));
    addRow("External mask", after.external_mask, before.external_mask, (value) => hex(value));
    addRow("External values", after.external_values, before.external_values, (value) => hex(value));
    addRow("Delay cycles remaining", after.delay_remaining, before.delay_remaining);
    addRow("Forced EXEC latch", after.exec_latch, before.exec_latch, (value) => value == null ? "(empty)" : hex(value, 4));
    addRow("Pending operation", after.pending_kind, before.pending_kind, (value) => value == null ? "(none)" : String(value));
    addRow("Halt state", after.halted_reason, before.halted_reason, (value) => value == null ? "(running)" : String(value));
    document.getElementById("state").innerHTML = rows.join("");

    document.getElementById("instruction").textContent = `${record.instruction_word == null ? "" : `${hex(record.instruction_word, 4)}  `}${record.instruction}`;
    document.getElementById("events").innerHTML = ((record.events || []).length ? record.events : ["No additional event"]).map((text) => `<li>${esc(text)}</li>`).join("");
    const fifoBoundary = fifoBoundaryState(record, index);
    const fifoPhase = (label, tx, rx, changed) => `<div class="fifo-state-phase${changed ? " changed" : ""}">
      <span class="fifo-state-phase-label">${esc(label)}</span>
      <span>TX [${tx.length}/${meta.tx_capacity}]: ${esc(fifoWordsText(tx))}<br>RX [${rx.length}/${meta.rx_capacity}]: ${esc(fifoWordsText(rx))}</span>
    </div>`;
    document.getElementById("fifos").innerHTML = [
      fifoPhase("Cycle start", fifoBoundary.startTx, fifoBoundary.startRx, false),
      fifoPhase(
        "After host events",
        fifoBoundary.hostTx,
        fifoBoundary.hostRx,
        !sameValue(fifoBoundary.startTx, fifoBoundary.hostTx) || !sameValue(fifoBoundary.startRx, fifoBoundary.hostRx),
      ),
      fifoPhase(
        "End of cycle",
        fifoBoundary.endTx,
        fifoBoundary.endRx,
        !sameValue(fifoBoundary.hostTx, fifoBoundary.endTx) || !sameValue(fifoBoundary.hostRx, fifoBoundary.endRx),
      ),
    ].join("");
    renderStateChanges(before, after);
    renderPinState(record);

    const waitDetail = document.getElementById("wait-detail");
    const wait = decodeWait(record);
    if (wait && (wait.stalled || wait.met)) {
      const target = wait.pin != null ? `GPIO${wait.pin}` : wait.irqIndex != null ? `IRQ${wait.irqIndex}` : `${wait.source.toUpperCase()} ${wait.index}`;
      const current = wait.pin != null
        ? ` Current sampled value: ${readPin(record, wait.pin)}.`
        : wait.irqIndex != null
          ? ` Current flag value: ${record.irq_flags & (1 << wait.irqIndex) ? 1 : 0}.`
          : "";
      const consume = wait.source === "irq" && wait.polarity && wait.met ? " The matching IRQ flag was cleared by WAIT." : "";
      waitDetail.textContent = wait.met
        ? `WAIT condition met during cycle ${index}: ${target} is ${wait.polarity}.${current}${consume}`
        : `PIO remains stalled after cycle ${index}; ${target} must become ${wait.polarity}.${current}`;
      waitDetail.className = wait.met ? "wait-detail met" : "wait-detail";
      waitDetail.hidden = false;
    } else {
      waitDetail.hidden = true;
    }

    highlightDisassembly(record);
    highlightSource(record, index);
    updateDebuggerControls();
    updateCursor();
    ensureCursorVisible();
  }

  function renderDataTable(start, end) {
    const body = document.getElementById("rows");
    const limit = Math.min(end, start + 1000);
    body.innerHTML = records.slice(start, limit).map((record, offset) => {
      const absoluteIndex = start + offset;
      const statePc = record.state_pc == null ? (records[absoluteIndex + 1] ? records[absoluteIndex + 1].pc : record.pc) : record.state_pc;
      const fifoBoundary = fifoBoundaryState(record, absoluteIndex);
      const txPath = levelPathText([fifoBoundary.startTx.length, fifoBoundary.hostTx.length, fifoBoundary.endTx.length]);
      const rxPath = levelPathText([fifoBoundary.startRx.length, fifoBoundary.hostRx.length, fifoBoundary.endRx.length]);
      const txTitle = `cycle start: ${fifoWordsText(fifoBoundary.startTx)}; after host events: ${fifoWordsText(fifoBoundary.hostTx)}; end of cycle: ${fifoWordsText(fifoBoundary.endTx)}`;
      const rxTitle = `cycle start: ${fifoWordsText(fifoBoundary.startRx)}; after host events: ${fifoWordsText(fifoBoundary.hostRx)}; end of cycle: ${fifoWordsText(fifoBoundary.endRx)}`;
      return `<tr>
        <td>${record.cycle}</td><td>${fmtTime(record.time_s)}</td><td>${record.instruction_pc == null ? "—" : record.instruction_pc}</td><td>${statePc}</td>
        <td>${esc(record.stalled ? "STALL" : record.phase)}</td><td class="mono">${esc(record.instruction)}</td>
        <td class="mono">${formatIrqFlags(record.irq_flags)}</td>
        <td title="${esc(txTitle)}">${esc(txPath)} · ${esc(fifoWordsText(fifoBoundary.endTx))}</td>
        <td title="${esc(rxTitle)}">${esc(rxPath)} · ${esc(fifoWordsText(fifoBoundary.endRx))}</td>
        <td class="mono">${hex(record.x)}</td><td class="mono">${hex(record.y)}</td>
        <td class="mono">${hex(record.isr)}/${record.isr_count}</td><td class="mono">${hex(record.osr)}/${record.osr_count}</td>
        <td>${esc((record.events || []).join("; "))}</td></tr>`;
    }).join("") + (end - start > 1000 ? '<tr><td colspan="14">Table limited to the first 1000 cycles in this window.</td></tr>' : "");
  }

  function eventKind(event) {
    return String(event && (event.kind ?? event.type) || "").toLowerCase().replaceAll("-", "_");
  }

  function isPinEvent(event) {
    return ["pin", "gpio", "pin_drive"].includes(eventKind(event));
  }

  function isTxPutEvent(event) {
    return ["tx", "tx_put", "put"].includes(eventKind(event));
  }

  function isRxPutEvent(event) {
    return ["rx_put", "rx_fill", "rx_inject", "inject_rx"].includes(eventKind(event));
  }

  function isRxGetEvent(event) {
    return ["rx", "rx_get", "get"].includes(eventKind(event));
  }

  function isIrqEvent(event) {
    return ["irq", "irq_set", "irq_clear"].includes(eventKind(event));
  }

  function irqEventIndex(event) {
    const raw = event.index != null ? event.index : event.value;
    return Number(raw || 0) & 0x7;
  }

  function irqEventSets(event) {
    const kind = eventKind(event);
    if (kind === "irq_clear") return false;
    if (kind === "irq_set") return true;
    if (event.index != null && event.value != null) return Boolean(Number(event.value));
    return true;
  }

  function isFifoEvent(event) {
    return isTxPutEvent(event) || isRxPutEvent(event) || isRxGetEvent(event);
  }

  function orderedStimuli(events = stimuli) {
    return events.slice().sort((a, b) => Number(a.cycle) - Number(b.cycle) || Number(a._order || 0) - Number(b._order || 0));
  }

  function normaliseEvent(raw, order) {
    const event = {
      cycle: Math.max(0, Math.floor(Number(raw.cycle || 0))),
      kind: String(raw.kind ?? raw.type ?? ""),
      value: raw.value ?? null,
      pin: raw.pin == null ? null : Number(raw.pin) & 31,
      index: raw.index == null ? null : Number(raw.index),
      shift: Number(raw.shift || 0),
      note: String(raw.note || ""),
      _order: raw._order == null ? order : Number(raw._order),
    };
    if (isPinEvent(event) && typeof event.value === "string" && ["Z", "RELEASE", "NONE"].includes(event.value.toUpperCase())) event.value = null;
    return event;
  }

  function normaliseStimuli(rawStimuli) {
    return rawStimuli.map((event, index) => normaliseEvent(event, index));
  }

  function parseStimulusDocument(data) {
    let rawEvents = [];
    if (Array.isArray(data)) rawEvents = data;
    else if (data && typeof data === "object") {
      rawEvents = [...(data.events || [])];
      for (const item of data.pins || []) rawEvents.push({ type: "pin", ...item });
      for (const item of data.tx || []) rawEvents.push({ type: "tx_put", ...item });
      for (const item of data.rx_put || []) rawEvents.push({ type: "rx_put", ...item });
      for (const item of data.rx_fill || []) rawEvents.push({ type: "rx_put", ...item });
      for (const original of data.rx_get || []) {
        const item = typeof original === "number" ? { cycle: original } : original;
        rawEvents.push({ type: "rx_get", ...item });
      }
      for (const item of data.irq || []) rawEvents.push({ type: "irq", ...item });
      for (const item of data.irq_set || []) rawEvents.push({ type: "irq_set", ...item });
      for (const item of data.irq_clear || []) rawEvents.push({ type: "irq_clear", ...item });
    } else throw new Error("Stimulus JSON must be an array or object.");
    return rawEvents.map((event, index) => {
      if (!event || typeof event !== "object") throw new Error(`Stimulus event ${index} is not an object.`);
      if (!(event.kind || event.type)) throw new Error(`Stimulus event ${index} has no type or kind.`);
      return normaliseEvent(event, index);
    }).sort((a, b) => a.cycle - b.cycle || a._order - b._order);
  }

  function cleanEvent(event) {
    const output = { cycle: Number(event.cycle), type: String(event.kind) };
    if (isPinEvent(event)) {
      output.pin = Number(event.pin);
      output.value = event.value == null ? "Z" : Number(event.value) ? 1 : 0;
    } else if (event.value != null) output.value = event.value;
    if (event.index != null) output.index = Number(event.index);
    if (event.shift) output.shift = Number(event.shift);
    if (event.note) output.note = String(event.note);
    return output;
  }

  function parseWordToken(rawToken) {
    let token = String(rawToken).trim().replaceAll("_", "");
    if (!token) throw new Error("empty FIFO word");
    let sign = 1n;
    if (token.startsWith("+") || token.startsWith("-")) {
      if (token[0] === "-") sign = -1n;
      token = token.slice(1);
    }
    if (!token) throw new Error(`invalid FIFO word ${JSON.stringify(rawToken)}`);
    let magnitude;
    if (/^0x[0-9a-f]+$/i.test(token) || /^0b[01]+$/i.test(token) || /^0o[0-7]+$/i.test(token) || /^[0-9]+$/.test(token)) {
      magnitude = BigInt(token);
    } else {
      throw new Error(`invalid FIFO word ${JSON.stringify(rawToken)}; use hex, binary, octal, or decimal`);
    }
    return Number(BigInt.asUintN(32, sign * magnitude));
  }

  function parseWordList(text) {
    const tokens = String(text || "").trim().split(/[\s,;]+/).filter(Boolean);
    if (!tokens.length) throw new Error("enter at least one FIFO word");
    return tokens.map(parseWordToken);
  }

  function eventWord(event) {
    try {
      const value = BigInt(parseWordToken(event.value));
      const shift = BigInt(Math.max(0, Math.floor(Number(event.shift || 0))));
      return Number(BigInt.asUintN(32, value << shift));
    } catch (_error) {
      return null;
    }
  }

  function renderFifoEvents() {
    const body = document.getElementById("fifo-events");
    const events = orderedStimuli(stimuli.filter(isFifoEvent));
    if (!events.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No manual FIFO events. Select a cycle, enter one or more words, and use a FIFO button above.</td></tr>';
      return;
    }
    body.innerHTML = events.map((event) => {
      let operation;
      let operationClass;
      let value;
      if (isTxPutEvent(event)) {
        operation = "TX put";
        operationClass = "fifo-event-tx";
        const word = eventWord(event);
        value = word == null ? String(event.value ?? "") : hex(word);
      } else if (isRxPutEvent(event)) {
        operation = "RX inject";
        operationClass = "fifo-event-rx-put";
        const word = eventWord(event);
        value = word == null ? String(event.value ?? "") : hex(word);
      } else {
        operation = "RX host read";
        operationClass = "fifo-event-rx-get";
        value = Number(event.shift || 0) ? `front >> ${Number(event.shift)}` : "front word";
      }
      return `<tr data-order="${event._order}">
        <td><button type="button" class="jump-event" data-cycle="${event.cycle}">${event.cycle}</button></td>
        <td>${fmtTime(event.cycle * Number(meta.period_s))}</td>
        <td class="${operationClass}">${operation}</td><td class="mono">${esc(value)}</td><td>${esc(event.note || "")}</td>
        <td><button type="button" class="delete-event" data-order="${event._order}" title="Delete this FIFO event">Delete</button></td>
      </tr>`;
    }).join("");
    body.querySelectorAll(".jump-event").forEach((button) => button.addEventListener("click", () => jumpToCycle(Number(button.dataset.cycle))));
    body.querySelectorAll(".delete-event").forEach((button) => button.addEventListener("click", () => deleteEventByOrder(Number(button.dataset.order))));
  }

  function renderIrqEvents() {
    const body = document.getElementById("irq-events");
    const events = orderedStimuli(stimuli.filter(isIrqEvent));
    if (!events.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No manual IRQ events. Choose a cycle and flag, then set or clear it above.</td></tr>';
      return;
    }
    body.innerHTML = events.map((event) => {
      const sets = irqEventSets(event);
      return `<tr data-order="${event._order}">
        <td><button type="button" class="jump-event" data-cycle="${event.cycle}">${event.cycle}</button></td>
        <td>${fmtTime(event.cycle * Number(meta.period_s))}</td>
        <td class="mono">IRQ${irqEventIndex(event)}</td>
        <td class="${sets ? "irq-event-set" : "irq-event-clear"}">${sets ? "Set / trigger" : "Clear"}</td>
        <td>${esc(event.note || "")}</td>
        <td><button type="button" class="delete-event" data-order="${event._order}" title="Delete this IRQ event">Delete</button></td>
      </tr>`;
    }).join("");
    body.querySelectorAll(".jump-event").forEach((button) => button.addEventListener("click", () => jumpToCycle(Number(button.dataset.cycle))));
    body.querySelectorAll(".delete-event").forEach((button) => button.addEventListener("click", () => deleteEventByOrder(Number(button.dataset.order))));
  }

  function renderStimulusTables() {
    renderFifoEvents();
    renderIrqEvents();
    renderInputEvents();
  }

  function eventCycle(fieldId, label) {
    const cycle = Number(document.getElementById(fieldId).value);
    const simulationCycles = Math.floor(Number(document.getElementById("simulation-cycles").value));
    if (!Number.isInteger(cycle) || cycle < 0) throw new Error(`${label} event cycle must be a non-negative integer`);
    if (Number.isInteger(simulationCycles) && simulationCycles > 0 && cycle >= simulationCycles) {
      throw new Error(`${label} event cycle must be between 0 and ${simulationCycles - 1}`);
    }
    return cycle;
  }

  function fifoEventCycle() {
    return eventCycle("fifo-cycle", "FIFO");
  }

  function irqEventCycle() {
    return eventCycle("irq-cycle", "IRQ");
  }

  function addFifoEvents(kind, values, cycle, note) {
    cycle = Math.max(0, Math.floor(Number(cycle)));
    const words = values == null ? [] : values;
    if (kind !== "rx_get" && !words.length) throw new Error("enter at least one FIFO word");
    snapshotForUndo();
    if (kind === "rx_get") {
      const order = nextOrder++;
      stimuli.push(normaliseEvent({ cycle, kind, note, _order: order }, order));
    } else {
      for (const value of words) {
        const order = nextOrder++;
        stimuli.push(normaliseEvent({ cycle, kind, value: Number(value) >>> 0, note, _order: order }, order));
      }
    }
    renderStimulusTables();
    const count = kind === "rx_get" ? 1 : words.length;
    const label = kind === "tx_put" ? "TX put" : kind === "rx_put" ? "RX injection" : "RX host read";
    if (document.getElementById("auto-rerun").checked) {
      return runSimulation(`${count} ${label} event${count === 1 ? "" : "s"} at cycle ${cycle}`, cycle);
    }
    draw();
    inspect(Math.min(cycle, Math.max(0, records.length - 1)));
    setStatus(`${count} ${label} event${count === 1 ? "" : "s"} pending at cycle ${cycle}; run simulation to apply.`, "running");
    return Promise.resolve(true);
  }

  function addFifoWords(kind) {
    const capacity = kind === "tx_put" ? Number(meta.tx_capacity) : Number(meta.rx_capacity);
    const label = kind === "tx_put" ? "TX" : "RX";
    if (capacity <= 0) throw new Error(`${label} FIFO is disabled by the selected FIFO join mode`);
    const cycle = fifoEventCycle();
    const values = parseWordList(document.getElementById("fifo-values").value);
    const note = kind === "tx_put" ? "interactive TX FIFO editor" : "interactive RX FIFO editor (debug injection)";
    return addFifoEvents(kind, values, cycle, note);
  }

  function addRxGetEvent() {
    if (Number(meta.rx_capacity) <= 0) throw new Error("RX FIFO is disabled by the selected FIFO join mode");
    return addFifoEvents("rx_get", null, fifoEventCycle(), "interactive RX host read");
  }

  function addIrqEvent(kind, cycle = irqEventCycle(), index = Number(document.getElementById("irq-index").value)) {
    cycle = Math.max(0, Math.floor(Number(cycle)));
    index = Number(index);
    if (!Number.isInteger(index) || index < 0 || index > 7) throw new Error("IRQ flag must be between 0 and 7");
    if (!["irq_set", "irq_clear"].includes(kind)) throw new Error(`unsupported IRQ editor action ${kind}`);
    snapshotForUndo();
    const order = nextOrder++;
    stimuli.push(normaliseEvent({
      cycle,
      kind,
      index,
      note: kind === "irq_set" ? "interactive IRQ trigger" : "interactive IRQ clear",
      _order: order,
    }, order));
    renderStimulusTables();
    const label = kind === "irq_set" ? "set" : "clear";
    if (document.getElementById("auto-rerun").checked) return runSimulation(`IRQ${index} ${label} event at cycle ${cycle}`, cycle);
    draw();
    inspect(Math.min(cycle, Math.max(0, records.length - 1)));
    setStatus(`IRQ${index} ${label} pending at cycle ${cycle}; run simulation to apply.`, "running");
    return Promise.resolve(true);
  }

  function renderInputEvents() {
    const body = document.getElementById("input-events");
    const events = stimuli.filter(isPinEvent).sort((a, b) => a.cycle - b.cycle || a._order - b._order);
    if (!events.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No external GPIO transitions. Choose a state and click a GPIO waveform.</td></tr>';
      return;
    }
    body.innerHTML = events.map((event) => {
      const state = event.value == null ? "Z / release" : Number(event.value) ? "1 / high" : "0 / low";
      const stateClass = event.value == null ? "release" : Number(event.value) ? "high" : "low";
      return `<tr data-order="${event._order}">
        <td><button type="button" class="jump-event" data-cycle="${event.cycle}">${event.cycle}</button></td>
        <td>${fmtTime(event.cycle * Number(meta.period_s))}</td><td>GPIO${event.pin}</td>
        <td class="event-state ${stateClass}">${state}</td><td>${esc(event.note || "")}</td>
        <td><button type="button" class="delete-event" data-order="${event._order}" title="Delete this transition">Delete</button></td>
      </tr>`;
    }).join("");
    body.querySelectorAll(".jump-event").forEach((button) => button.addEventListener("click", () => jumpToCycle(Number(button.dataset.cycle))));
    body.querySelectorAll(".delete-event").forEach((button) => button.addEventListener("click", () => deleteEventByOrder(Number(button.dataset.order))));
  }

  function snapshotForUndo() {
    undoStack.push(deepClone(stimuli));
    if (undoStack.length > 100) undoStack.shift();
    redoStack = [];
    updateHistoryButtons();
  }

  function updateHistoryButtons() {
    document.getElementById("undo").disabled = !undoStack.length;
    document.getElementById("redo").disabled = !redoStack.length;
  }

  function applyPinEdit(cycle, pin, tool) {
    cycle = Math.max(0, Math.floor(Number(cycle)));
    pin = Number(pin) & 31;
    snapshotForUndo();
    const matching = stimuli
      .map((event, index) => ({ event, index }))
      .filter(({ event }) => isPinEvent(event) && event.cycle === cycle && Number(event.pin) === pin);
    const existingIndex = matching.length ? matching[0].index : -1;
    if (tool === "erase") {
      if (matching.length) {
        for (const { index } of [...matching].reverse()) stimuli.splice(index, 1);
      } else {
        undoStack.pop();
        updateHistoryButtons();
        setStatus(`No GPIO${pin} transition exists at cycle ${cycle}.`, "ready");
        return Promise.resolve(false);
      }
    } else {
      const value = tool === "Z" ? null : Number(tool) ? 1 : 0;
      const order = matching.length ? matching[0].event._order : nextOrder++;
      const replacement = normaliseEvent({ cycle, kind: "pin", value, pin, note: "interactive editor", _order: order }, order);
      if (matching.length) {
        for (const { index } of [...matching].reverse()) stimuli.splice(index, 1);
        stimuli.splice(existingIndex, 0, replacement);
      } else stimuli.push(replacement);
    }
    renderStimulusTables();
    draw();
    inspect(Math.min(cycle, Math.max(0, records.length - 1)));
    if (document.getElementById("auto-rerun").checked) return runSimulation(`GPIO${pin} edit at cycle ${cycle}`);
    setStatus(`Edit pending: ${toolDescription(tool)} on GPIO${pin} at cycle ${cycle}.`, "running");
    return Promise.resolve(true);
  }

  function deleteEventByOrder(order) {
    const index = stimuli.findIndex((event) => event._order === order);
    if (index < 0) return;
    snapshotForUndo();
    stimuli.splice(index, 1);
    renderStimulusTables();
    draw();
    if (document.getElementById("auto-rerun").checked) runSimulation("transition deleted");
    else setStatus("Transition deleted; rerun pending.", "running");
  }

  function undo() {
    if (!undoStack.length) return;
    redoStack.push(deepClone(stimuli));
    stimuli = undoStack.pop();
    updateHistoryButtons();
    renderStimulusTables();
    runSimulation("undo");
  }

  function redo() {
    if (!redoStack.length) return;
    undoStack.push(deepClone(stimuli));
    stimuli = redoStack.pop();
    updateHistoryButtons();
    renderStimulusTables();
    runSimulation("redo");
  }

  function resetEdits() {
    snapshotForUndo();
    stimuli = deepClone(baselineStimuli);
    nextOrder = Math.max(0, ...stimuli.map((event) => Number(event._order || 0))) + 1;
    renderStimulusTables();
    runSimulation("embedded events restored");
  }

  function setStatus(text, kind = "ready") {
    const element = document.getElementById("simulation-status");
    element.textContent = text;
    element.className = `status ${kind}`;
  }

  function runSimulation(reason = "manual", focusCycle = null) {
    const cyclesInput = document.getElementById("simulation-cycles");
    const cycles = Math.floor(Number(cyclesInput.value));
    if (!Number.isInteger(cycles) || cycles < 1) {
      setStatus("Simulation cycles must be at least 1.", "error");
      return Promise.reject(new Error("invalid simulation length"));
    }
    const token = ++simulationToken;
    setStatus(`Simulating ${cycles.toLocaleString()} cycles…`, "running");
    return new Promise((resolve, reject) => {
      window.setTimeout(() => {
        if (token !== simulationToken) { resolve(false); return; }
        const started = performance.now();
        try {
          const ordered = orderedStimuli();
          const result = simulator.simulate(model, cycles, ordered.map(cleanEvent));
          records = result.records;
          initialState = result.initial_state;
          runtimeWarnings = result.warnings || [];
          hostRxValues = result.host_rx_values || [];
          meta.cycles = cycles;
          meta.duration_s = cycles * Number(meta.period_s);
          embedded.records = records;
          embedded.initial_state = initialState;
          embedded.stimuli = ordered.map(cleanEvent);
          document.getElementById("card-cycles").textContent = cycles.toLocaleString();
          document.getElementById("card-duration").textContent = fmtTime(meta.duration_s);
          const slider = document.getElementById("cycle");
          slider.max = Math.max(0, cycles - 1);
          document.getElementById("debug-cycle").max = Math.max(0, cycles - 1);
          const requestedCycle = focusCycle == null ? selectedCycle : Math.floor(Number(focusCycle) || 0);
          selectedCycle = Math.max(0, Math.min(requestedCycle, cycles - 1));
          const endInput = document.getElementById("end");
          if (Number(endInput.value) > cycles || Number(endInput.value) <= Number(document.getElementById("start").value)) endInput.value = Math.min(cycles, Number(document.getElementById("start").value) + 200);
          renderWarnings();
          draw();
          inspect(selectedCycle);
          const elapsed = performance.now() - started;
          const pinCount = stimuli.filter(isPinEvent).length;
          const fifoCount = stimuli.filter(isFifoEvent).length;
          const irqCount = stimuli.filter(isIrqEvent).length;
          setStatus(`${cycles.toLocaleString()} cycles rerun in ${elapsed.toFixed(1)} ms · ${pinCount} GPIO transition${pinCount === 1 ? "" : "s"} · ${fifoCount} FIFO event${fifoCount === 1 ? "" : "s"} · ${irqCount} IRQ event${irqCount === 1 ? "" : "s"}`, "ready");
          resolve(true);
        } catch (error) {
          setStatus(`Simulation error: ${error.message}`, "error");
          console.error(error);
          reject(error);
        }
      }, 0);
    });
  }

  function renderWarnings() {
    const warnings = [...new Set([...baseWarnings, ...runtimeWarnings])];
    document.getElementById("warnings").innerHTML = warnings.length ? warnings.map((warning) => `<li>${esc(warning)}</li>`).join("") : "<li>None</li>";
  }

  function selectTool(tool) {
    activeTool = String(tool);
    document.querySelectorAll(".tool").forEach((button) => button.classList.toggle("active", button.dataset.tool === activeTool));
    if (drawState) draw();
    setStatus(`Click action: ${toolDescription(activeTool)}.`, "ready");
  }

  function toolDescription(tool) {
    if (tool === "1") return "drive high (1)";
    if (tool === "0") return "drive low (0)";
    if (tool === "Z") return "release to Z";
    if (tool === "erase") return "delete transition";
    return "inspect only";
  }

  function jumpToCycle(cycle, flash = true) {
    if (!records.length) return;
    cycle = Math.max(0, Math.min(records.length - 1, Math.floor(Number(cycle) || 0)));
    const span = Math.max(1, Number(document.getElementById("end").value) - Number(document.getElementById("start").value));
    if (!drawState || cycle < drawState.start || cycle >= drawState.end) {
      let start = Math.max(0, cycle - Math.floor(span / 2));
      if (start + span > records.length) start = Math.max(0, records.length - span);
      document.getElementById("start").value = start;
      document.getElementById("end").value = Math.min(records.length, start + span);
      draw();
    }
    inspect(cycle);
    if (flash) {
      document.getElementById("wave-wrap").classList.remove("flash");
      void document.getElementById("wave-wrap").offsetWidth;
      document.getElementById("wave-wrap").classList.add("flash");
    }
  }

  function stepCycle(delta) {
    if (!records.length) return;
    jumpToCycle(selectedCycle + Number(delta), false);
  }

  function saveStimulus() {
    const payload = JSON.stringify({ events: orderedStimuli().map(cleanEvent) }, null, 2);
    const blob = new Blob([payload], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${String(meta.program || "pio").replace(/[^a-z0-9_.-]+/gi, "_")}_stimulus.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
    setStatus("Stimulus JSON saved.", "ready");
  }

  async function loadStimulusFile(file) {
    const text = await file.text();
    const parsed = parseStimulusDocument(JSON.parse(text));
    snapshotForUndo();
    stimuli = parsed;
    nextOrder = Math.max(0, ...stimuli.map((event) => Number(event._order || 0))) + 1;
    renderStimulusTables();
    await runSimulation(`loaded ${file.name}`);
  }

  function bindControls() {
    const runFifoAction = (action) => {
      try {
        Promise.resolve(action()).catch((error) => setStatus(`FIFO editor error: ${error.message}`, "error"));
      } catch (error) {
        setStatus(`FIFO editor error: ${error.message}`, "error");
      }
    };
    const runIrqAction = (action) => {
      try {
        Promise.resolve(action()).catch((error) => setStatus(`IRQ editor error: ${error.message}`, "error"));
      } catch (error) {
        setStatus(`IRQ editor error: ${error.message}`, "error");
      }
    };
    const txButton = document.getElementById("fifo-add-tx");
    const rxButton = document.getElementById("fifo-add-rx");
    const rxGetButton = document.getElementById("fifo-rx-get");
    txButton.addEventListener("click", () => runFifoAction(() => addFifoWords("tx_put")));
    rxButton.addEventListener("click", () => runFifoAction(() => addFifoWords("rx_put")));
    rxGetButton.addEventListener("click", () => runFifoAction(addRxGetEvent));
    document.getElementById("fifo-follow-selected").addEventListener("change", (event) => {
      if (event.target.checked) document.getElementById("fifo-cycle").value = selectedCycle;
    });
    document.getElementById("fifo-cycle").addEventListener("input", (event) => {
      if (Number(event.target.value) !== selectedCycle) document.getElementById("fifo-follow-selected").checked = false;
    });
    document.getElementById("irq-follow-selected").addEventListener("change", (event) => {
      if (event.target.checked) document.getElementById("irq-cycle").value = selectedCycle;
    });
    document.getElementById("irq-cycle").addEventListener("input", (event) => {
      if (Number(event.target.value) !== selectedCycle) document.getElementById("irq-follow-selected").checked = false;
    });
    document.getElementById("irq-set").addEventListener("click", () => runIrqAction(() => addIrqEvent("irq_set")));
    document.getElementById("irq-clear").addEventListener("click", () => runIrqAction(() => addIrqEvent("irq_clear")));
    document.getElementById("fifo-values").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        runFifoAction(() => addFifoWords("tx_put"));
      }
    });
    document.querySelectorAll(".tool").forEach((button) => button.addEventListener("click", () => selectTool(button.dataset.tool)));
    document.getElementById("program-select").addEventListener("change", (event) => selectProgram(event.target.value));
    document.getElementById("draw").addEventListener("click", draw);
    document.getElementById("rerun").addEventListener("click", () => runSimulation("manual"));
    document.getElementById("cycle").addEventListener("input", (event) => inspect(Number(event.target.value)));
    document.getElementById("continue-breakpoint").addEventListener("click", continueToBreakpoint);
    document.getElementById("step-back").addEventListener("click", () => stepCycle(-1));
    document.getElementById("step-forward").addEventListener("click", () => stepCycle(1));
    document.getElementById("clear-breakpoints").addEventListener("click", clearAllBreakpoints);
    document.getElementById("debug-cycle").addEventListener("change", (event) => jumpToCycle(Number(event.target.value), false));
    document.getElementById("follow-source").addEventListener("change", () => {
      if (!records.length) return;
      highlightDisassembly(records[selectedCycle]);
      highlightSource(records[selectedCycle], selectedCycle);
    });
    document.getElementById("undo").addEventListener("click", undo);
    document.getElementById("redo").addEventListener("click", redo);
    document.getElementById("reset-edits").addEventListener("click", resetEdits);
    document.getElementById("save-stimulus").addEventListener("click", saveStimulus);
    document.getElementById("load-stimulus").addEventListener("click", () => document.getElementById("stimulus-file").click());
    document.getElementById("stimulus-file").addEventListener("change", async (event) => {
      const [file] = event.target.files;
      if (!file) return;
      try { await loadStimulusFile(file); }
      catch (error) { setStatus(`Cannot load stimulus: ${error.message}`, "error"); }
      event.target.value = "";
    });
    wave.addEventListener("click", handleWaveClick);
    wave.addEventListener("mousemove", handleWaveMove);
    wave.addEventListener("mouseleave", () => { tooltip.hidden = true; });
    document.addEventListener("keydown", (event) => {
      if (event.key === "F5") { event.preventDefault(); continueToBreakpoint(); return; }
      const tag = document.activeElement && document.activeElement.tagName;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); event.shiftKey ? redo() : undo(); return; }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "y") { event.preventDefault(); redo(); return; }
      if (event.key === "ArrowLeft") { event.preventDefault(); stepCycle(-1); return; }
      if (event.key === "ArrowRight") { event.preventDefault(); stepCycle(1); return; }
      if (event.key === "Home") { event.preventDefault(); jumpToCycle(0, false); return; }
      if (event.key === "End") { event.preventDefault(); jumpToCycle(records.length - 1, false); return; }
      if (event.key === "1") selectTool("1");
      else if (event.key === "0") selectTool("0");
      else if (event.key.toLowerCase() === "z") selectTool("Z");
      else if (event.key.toLowerCase() === "i") selectTool("inspect");
      else if (event.key === "Delete" || event.key === "Backspace") selectTool("erase");
    });
  }

  window.__PIO_TRACE_APP__ = {
    version: window.__PIO_TRACE_VERSION__,
    getRecords: () => records,
    getStimuli: () => deepClone(stimuli.map(cleanEvent)),
    getInitialState: () => deepClone(initialState),
    getHostRxValues: () => [...hostRxValues],
    getActiveTool: () => activeTool,
    getSelectedCycle: () => selectedCycle,
    getPrograms: () => traceOptions.map((option) => ({ key: String(option.key), label: String(option.label || option.program || option.key), program: option.program, sm_id: option.sm_id })),
    getSelectedProgram: () => ({ key: activeTraceKey, program: meta.program, sm_id: meta.sm_id }),
    selectProgram,
    getSourceLine: () => records.length ? sourceLineForRecord(records[selectedCycle], selectedCycle) : null,
    getBreakpoints: () => [...breakpoints].sort((left, right) => left - right),
    setBreakpoint: (pc, enabled = true) => toggleBreakpoint(pc, enabled),
    toggleBreakpoint,
    clearBreakpoints: clearAllBreakpoints,
    continueToBreakpoint,
    findNextBreakpointCycle,
    setPin: (cycle, pin, value) => applyPinEdit(
      cycle,
      pin,
      value == null || ["Z", "RELEASE", "NONE"].includes(String(value).toUpperCase()) ? "Z" : String(Number(value) ? 1 : 0),
    ),
    erasePinEvent: (cycle, pin) => applyPinEdit(cycle, pin, "erase"),
    addTxWords: (cycle, values) => addFifoEvents("tx_put", (Array.isArray(values) ? values : [values]).map((value) => parseWordToken(value)), cycle, "browser API TX put"),
    injectRxWords: (cycle, values) => addFifoEvents("rx_put", (Array.isArray(values) ? values : [values]).map((value) => parseWordToken(value)), cycle, "browser API RX injection"),
    readRxWord: (cycle) => addFifoEvents("rx_get", null, cycle, "browser API RX host read"),
    setIrq: (cycle, index) => addIrqEvent("irq_set", cycle, index),
    clearIrq: (cycle, index) => addIrqEvent("irq_clear", cycle, index),
    rerun: runSimulation,
    draw,
    inspect,
    selectTool,
    jumpToCycle,
    stepBackward: () => stepCycle(-1),
    stepForward: () => stepCycle(1),
  };

  initialiseProgramSelector();
  bindControls();
  updateTraceChrome();
  updateFifoControlAvailability();
  renderSourceCode();
  renderDisassembly();
  renderWarnings();
  renderStimulusTables();
  draw();
  inspect(0);
  setStatus(`Ready · ${records.length.toLocaleString()} embedded cycles · click the PIO disassembly gutter to set breakpoints, then Continue/F5`, "ready");
  window.__PIO_TRACE_APP_READY__ = true;
})();
