"use strict";
const fs = require("fs");
const simulatorPath = process.argv[2];
if (!simulatorPath) throw new Error("simulator path argument missing");
const api = require(simulatorPath);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
const results = cases.map((item) => {
  const emulator = new api.PIOEmulator(item.model);
  const result = emulator.run(item.cycles, item.stimuli);
  return {
    initial_state: result.initial_state,
    records: result.records,
    warnings: result.warnings,
    host_rx_values: result.host_rx_values,
  };
});
process.stdout.write(JSON.stringify(results));
