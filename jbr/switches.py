"""Saved engine on/off switches: what `jbr enable` / `jbr disable` / `jbr dashboard` change.

One small JSON file next to the engine state file:

    ~/.jbr/engine_switches.json      ($JBR_SWITCHES, else beside $JBR_STATE)
    {"codex_astra": {"available": false, "at": "2026-11-16T09:00:00Z", "note": "subscription ended"}}

`router.load_engines` applies it to every engines.json it loads, so every jbr process on the machine
(every session, every build_dag runner agent, every probe) obeys the same switch. Precedence, lowest
first: engines.json `available` < saved switch < `--available` on one command line. A switch for an
engine the loaded engines.json does not have is ignored (a custom --engines file need not list them all).

Writes take a lock and go through a temp file + os.replace (state._write), so concurrent writers never lose an
update and a reader never sees a half-written file.
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone

FILE_NAME = "engine_switches.json"


def switches_path() -> pathlib.Path:
    env = os.environ.get("JBR_SWITCHES")
    if env:
        return pathlib.Path(env)
    state = os.environ.get("JBR_STATE")  # tests isolate JBR_STATE per test: the switches follow it
    return (pathlib.Path(state).parent if state else pathlib.Path.home() / ".jbr") / FILE_NAME


def load(path: pathlib.Path | None = None) -> dict[str, dict]:
    p = path or switches_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}  # unreadable or half-edited by hand: no switches rather than a crash in every jbr command
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("available"), bool)}


def set_switch(engine_key: str, available: bool | None, note: str = "", path: pathlib.Path | None = None) -> dict[str, dict]:
    """Save `engine_key` on (True) or off (False); None removes the switch (back to engines.json)."""
    from jbr import state  # lazy: state imports router, which imports this module
    p = path or switches_path()
    # read-modify-write under a lock (the dashboard serves requests on threads, the CLI may run meanwhile);
    # state._write is the same temp file + os.replace, with the Windows retry while a reader has the file open
    with state._lock(p.with_name(f"{p.name}.lock")):
        data = load(p)
        if available is None:
            data.pop(engine_key, None)
        else:
            rec = {"available": available, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            if note:
                rec["note"] = note
            data[engine_key] = rec
        state._write(p, data)
    return data


def apply(engines: dict[str, dict], data: dict[str, dict] | None = None) -> dict[str, dict]:
    """Set `available` from the saved switches (in place); returns engines."""
    for key, rec in (load() if data is None else data).items():
        if key in engines:
            engines[key]["available"] = rec["available"]
    return engines
