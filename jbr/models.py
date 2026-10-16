"""Resolve `"model": "auto:<family>"` engine entries to the best model the CLI lists right now.

agy retires model ids without notice (gemini-3.1-pro-high vanished from `agy models` on 2026-10-16), so an
agy engine can name a family instead of a pinned id: `auto:gemini`, `auto:claude-opus`, `auto:claude-sonnet`.
The family must end right before the version number (`auto:claude` is ambiguous: opus or sonnet?).

Choice, in order: the highest leading version (`gemini-4-argon-high` beats `gemini-3.8-flash-high`: 4 > 3.8;
digits later in the id, like `-8b`, are ignored), then the effort suffix equal to the engine's `effort`, then
the highest effort suffix, then the id itself (deterministic regardless of listing order).

`agy models` is cached for CACHE_TTL_S next to the engine state. A failed or empty listing falls back to a
stale cache; with no cache at all it raises LookupError, which `jbr probe` reports as UNAVAILABLE. Pinned ids
pass through unchanged; `auto:` on a non-agy engine is a configuration error (codex has no model list).
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
LIST_TIMEOUT_S = 60
EFFORT_RANK = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
_LINE_RE = re.compile(r"^([a-z0-9][a-z0-9.\-]*)(?:\t| {2,})\S")  # `<id>\t<label>` as `agy models` prints
_VERSION_RE = re.compile(r"\d+(?:[.-]\d+)*")


def cache_path() -> pathlib.Path:
    base = os.environ.get("JBR_STATE")
    root = pathlib.Path(base).parent if base else pathlib.Path.home() / ".jbr"
    return root / "agy_models.json"


def parse_agy_models(text: str) -> list[str]:
    """Model ids from `agy models` stdout (lines `<id>\\t<label>`; banners and prose are skipped)."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if m and "-" in m.group(1):
            out.append(m.group(1))
    return out


def _rank(model_id: str, family: str, effort: str | None) -> tuple:
    rest = model_id[len(family):].lstrip("-")
    m = _VERSION_RE.match(rest)
    version = tuple(int(n) for n in re.split(r"[.-]", m.group(0))) if m else ()
    suffix = rest.rsplit("-", 1)[-1]
    return version, int(effort is not None and suffix == effort), EFFORT_RANK.get(suffix, 0), model_id


def pick(models: list[str], family: str, effort: str | None = None) -> str | None:
    """Best id in `models` for `family` (see module doc); None when agy lists none of that family.
    Raises LookupError when the family does not end right before a version (e.g. `claude`)."""
    fam = family.strip().lower()
    cands = [m for m in models if m == fam or m.startswith(fam + "-")]
    versioned = [m for m in cands if _VERSION_RE.match(m[len(fam):].lstrip("-"))]
    if cands and not versioned:
        raise LookupError(f"auto:{fam} is ambiguous ({', '.join(sorted(cands)[:4])}...): name the family up to "
                          "the version, e.g. auto:claude-opus")
    return max(versioned, key=lambda m: _rank(m, fam, effort)) if versioned else None


def _read_cache(p: pathlib.Path) -> tuple[float, list[str]] | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    at, ids = data.get("at"), data.get("models")
    if not isinstance(at, (int, float)) or not isinstance(ids, list) or not ids:
        return None
    if not all(isinstance(i, str) for i in ids):
        return None
    return float(at), ids


def _write_cache(p: pathlib.Path, ids: list[str], at: float) -> None:
    """Atomic and best-effort: a cache that cannot be written never fails a listing that worked."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"at": at, "models": ids}, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def list_agy_models(*, run: Callable[..., Any] | None = None, path: pathlib.Path | None = None,
                    now: Callable[[], float] = time.time, agy_bin: str = "agy", offline: bool = False) -> list[str]:
    """`agy models` ids: the cache when younger than CACHE_TTL_S (any age when offline), else a live listing.
    A failed or empty live listing falls back to a stale cache, else raises LookupError (offline: [])."""
    p = path or cache_path()
    cached = _read_cache(p)
    if cached and (offline or now() - cached[0] < CACHE_TTL_S):
        return list(cached[1])
    if offline:
        return []
    try:
        res = (run or subprocess.run)([agy_bin, "models"], capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=LIST_TIMEOUT_S, stdin=subprocess.DEVNULL)
        ids = parse_agy_models(res.stdout or "") if res.returncode == 0 else []
        detail = f"exit {res.returncode}: {((res.stderr or '') + (res.stdout or '')).strip()[:200]}"
    except (OSError, subprocess.SubprocessError) as e:  # binary missing, timeout
        ids, detail = [], f"cannot run {agy_bin} models: {e}"
    if ids:
        _write_cache(p, ids, now())
        return ids
    if cached:
        return list(cached[1])  # stale beats nothing
    raise LookupError(f"agy lists no models ({detail})")


def resolve(engine: dict, *, offline: bool = False, **kw: Any) -> dict:
    """The engine with an `auto:<family>` model replaced by the current best id; `model_spec` keeps the
    original. offline=True only reads the cache and returns the engine unresolved when there is none.
    Raises LookupError when agy lists no model of the family, ValueError for `auto:` on a non-agy engine."""
    spec = str(engine.get("model", ""))
    if not spec.startswith(AUTO):
        return engine
    if engine.get("runner", "agy") != "agy":
        raise ValueError(f"{spec!r}: auto: models resolve from `agy models` only; pin the id for "
                         f"runner {engine.get('runner')!r}")
    family = spec[len(AUTO):]
    ids = list_agy_models(offline=offline, **kw)
    chosen = pick(ids, family, engine.get("effort")) if ids else None
    if not chosen:
        if offline:
            return engine
        raise LookupError(f"agy lists no model of family {family!r} (have: {', '.join(ids) or 'none'})")
    return {**engine, "model": chosen, "model_spec": spec}
