"""Resolve `"model": "auto:<family>"` engine entries to the best model the CLI lists right now.

agy retires model ids without notice (gemini-3.1-pro-high vanished from `agy models` on 2026-10-16), so an
engine can name a family instead of a pinned id: `auto:gemini`, `auto:claude-opus`, `auto:claude-sonnet`.
The choice is the listed id with that prefix and the highest version, then the highest effort suffix
(`gemini-3.8-flash-high` beats `gemini-3.8-flash-medium`; `gemini-4-argon-high` would beat both).
`agy models` is cached for CACHE_TTL_S in the jbr state dir, so probes and runs stay cheap. Pinned ids
(no `auto:` prefix) pass through unchanged, and codex engines are never resolved (codex has no model list).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
from typing import Any, Callable

AUTO = "auto:"
CACHE_TTL_S = 3600
EFFORT_RANK = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
_ID_RE = re.compile(r"^([a-z0-9][a-z0-9.\-]*)\s")


def cache_path() -> pathlib.Path:
    base = os.environ.get("JBR_STATE")
    root = pathlib.Path(base).parent if base else pathlib.Path.home() / ".jbr"
    return root / "agy_models.json"


def parse_agy_models(text: str) -> list[str]:
    """Model ids from `agy models` output (lines `<id>\\t<label>`; the 'Fetching ...' banner is skipped)."""
    out = []
    for line in (text or "").splitlines():
        m = _ID_RE.match(line.strip() + " ")
        if m and "-" in m.group(1):
            out.append(m.group(1))
    return out


def _rank(model_id: str, family: str) -> tuple[tuple[int, ...], int]:
    rest = model_id[len(family):].lstrip("-")
    parts = rest.split("-")
    effort = EFFORT_RANK.get(parts[-1], 0) if parts else 0
    version = tuple(int(n) for n in re.findall(r"\d+", rest))
    return version, effort


def pick(models: list[str], family: str) -> str | None:
    """Best id in `models` whose name is `family` or starts with `family-`; None when there is none."""
    fam = family.strip().lower()
    cands = [m for m in models if m == fam or m.startswith(fam + "-")]
    return max(cands, key=lambda m: _rank(m, fam)) if cands else None


def list_agy_models(*, run: Callable[..., Any] | None = None, path: pathlib.Path | None = None,
                    now: Callable[[], float] = time.time, agy_bin: str = "agy") -> list[str]:
    """`agy models` ids, from the cache when it is younger than CACHE_TTL_S."""
    p = path or cache_path()
    try:
        cached = json.loads(p.read_text(encoding="utf-8"))
        if now() - float(cached.get("at", 0)) < CACHE_TTL_S and cached.get("models"):
            return list(cached["models"])
    except (OSError, ValueError):
        pass
    res = (run or subprocess.run)([agy_bin, "models"], capture_output=True, text=True, timeout=120)
    models = parse_agy_models(f"{res.stdout or ''}\n{res.stderr or ''}")
    if models:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"at": now(), "models": models}, indent=2), encoding="utf-8")
    return models


def resolve(engine: dict, **kw: Any) -> dict:
    """The engine with an `auto:<family>` model replaced by the current best id (`model_spec` keeps the
    original). Raises LookupError when agy lists no model of that family."""
    spec = str(engine.get("model", ""))
    if not spec.startswith(AUTO) or engine.get("runner", "agy") != "agy":
        return engine
    family = spec[len(AUTO):]
    models = list_agy_models(**kw)
    chosen = pick(models, family)
    if not chosen:
        raise LookupError(f"agy lists no model of family {family!r} (have: {', '.join(models) or 'none'})")
    return {**engine, "model": chosen, "model_spec": spec}
