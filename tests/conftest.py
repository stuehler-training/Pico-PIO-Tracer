from __future__ import annotations

from dataclasses import replace

import pytest

from pico_pio_trace.emulator import PIOEmulator
from pico_pio_trace.parser import parse_source


@pytest.fixture
def run_source():
    def run(source: str, cycles: int, *, program: str | None = None, **changes):
        parsed = parse_source(source)
        config = parsed.choose(program_name=program)
        if changes:
            changes.setdefault("actual_freq_hz", None if "requested_freq_hz" in changes or "system_clock_hz" in changes else config.actual_freq_hz)
            config = replace(config, **changes)
        emulator = PIOEmulator(config)
        return emulator, emulator.run(cycles)

    return run
