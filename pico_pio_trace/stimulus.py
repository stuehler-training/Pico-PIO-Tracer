from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .model import StimulusEvent, parse_int


def load_stimulus(path: str | Path) -> list[StimulusEvent]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_stimulus(data)


def parse_stimulus(data: Any) -> list[StimulusEvent]:
    if isinstance(data, list):
        raw_events = data
    elif isinstance(data, dict):
        raw_events = list(data.get("events", []))
        for item in data.get("pins", []):
            raw_events.append({"type": "pin", **item})
        for item in data.get("tx", []):
            raw_events.append({"type": "tx_put", **item})
        for item in data.get("rx_put", []):
            raw_events.append({"type": "rx_put", **item})
        for item in data.get("rx_fill", []):
            raw_events.append({"type": "rx_put", **item})
        for item in data.get("rx_get", []):
            if isinstance(item, int):
                item = {"cycle": item}
            raw_events.append({"type": "rx_get", **item})
        for item in data.get("irq", []):
            raw_events.append({"type": "irq", **item})
        for item in data.get("irq_set", []):
            raw_events.append({"type": "irq_set", **item})
        for item in data.get("irq_clear", []):
            raw_events.append({"type": "irq_clear", **item})
    else:
        raise ValueError("stimulus JSON must be an array of events or an object")

    events: list[StimulusEvent] = []
    for item in raw_events:
        if not isinstance(item, dict):
            raise ValueError(f"stimulus event must be an object, got {item!r}")
        kind = str(item.get("type", item.get("kind", "")))
        if not kind:
            raise ValueError(f"stimulus event has no type/kind: {item!r}")
        value = item.get("value")
        if isinstance(value, str) and value.upper() not in {"Z", "RELEASE", "NONE"}:
            value = parse_int(value)
        pin = item.get("pin")
        index = item.get("index")
        events.append(
            StimulusEvent(
                cycle=int(item.get("cycle", 0)),
                kind=kind,
                value=value,
                pin=None if pin is None else int(pin),
                index=None if index is None else int(index),
                shift=int(item.get("shift", 0)),
                note=str(item.get("note", "")),
            )
        )
    return sorted(events, key=lambda event: event.cycle)
