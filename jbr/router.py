"""Ask Jev (TypeSafe System One) which engine implements each work package.

One batch request: one `choice` question per package, criteria = the available
engine keys. Result written to <project_root>/ops/routing.json as
{"model": ..., "state": ..., "routing": {"WP04": {"engine", "confidence", "probabilities"}, ...}}.

Package attributes shown to Jev come from each package's optional `routing_hints`
dict (numerical_difficulty, sign_convention_critical, in_flight_on, ...); `depth`
is taken from routing_hints when present, otherwise computed from depends_on.
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.request
from typing import Any, Callable

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
ROUTING_FILE = pathlib.Path("ops") / "routing.json"

DEFAULT_GOAL = (
    "Finish, review and audit the build fastest without sacrificing correctness. Every package is "
    "reviewed by fresh Claude reviewers with adversarial refutation and up to 2 fix rounds regardless of engine."
)
SCHEDULING_NOTE = (
    "depth = position in the dependency chain; depth 1 starts now, each further level starts roughly "
    "20-40 minutes later. Packages already in flight on an engine (in_flight_on) should only be re-routed "
    "if clearly better."
)


# ----------------------------------------------------------------------------- engines


def load_engines(path: str | os.PathLike, overrides: dict[str, bool] | None = None) -> dict[str, dict]:
    """Load engines.json; `overrides` maps engine key -> available flag (from --available)."""
    engines = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if "engines" in engines and isinstance(engines["engines"], dict):
        engines = engines["engines"]
    for key, flag in (overrides or {}).items():
        if key not in engines:
            raise KeyError(f"unknown engine {key!r}; known: {sorted(engines)}")
        engines[key]["available"] = bool(flag)
    return engines


def parse_available(values: list[str] | None) -> dict[str, bool]:
    """Parse repeated `--available engine=yes|no` values."""
    out: dict[str, bool] = {}
    for v in values or []:
        if "=" not in v:
            raise ValueError(f"--available expects engine=yes|no, got {v!r}")
        key, flag = v.split("=", 1)
        flag = flag.strip().lower()
        if flag not in {"yes", "no", "true", "false", "1", "0"}:
            raise ValueError(f"--available {key}: flag must be yes|no, got {flag!r}")
        out[key.strip()] = flag in {"yes", "true", "1"}
    return out


def available_engines(engines: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in engines.items() if v.get("available", True)}


def engine_description(key: str, eng: dict) -> str:
    bits = [eng.get("description", key)]
    bits.append(f"runner={eng.get('runner', '?')} model={eng.get('model', '?')} effort={eng.get('effort', '?')}")
    if eng.get("parallel_limit"):
        bits.append(f"up to {eng['parallel_limit']} packages in parallel")
    bits.append("currently available: " + ("yes" if eng.get("available", True) else "no"))
    return ". ".join(bits)


# ----------------------------------------------------------------------------- packages


def compute_depths(packages: list[dict]) -> dict[str, int]:
    """depth = 1 for roots, 1 + max(depth of deps) otherwise. Unknown deps count as depth 0."""
    by_id = {p["id"]: p for p in packages}
    memo: dict[str, int] = {}

    def depth(pid: str, stack: tuple[str, ...] = ()) -> int:
        if pid in memo:
            return memo[pid]
        if pid in stack:
            raise ValueError(f"dependency cycle: {' -> '.join(stack + (pid,))}")
        deps = [d for d in by_id.get(pid, {}).get("depends_on", []) if d in by_id]
        memo[pid] = 1 + max((depth(d, stack + (pid,)) for d in deps), default=0)
        return memo[pid]

    return {p["id"]: depth(p["id"]) for p in packages}


def package_attrs(packages: list[dict], done: set[str] | None = None) -> dict[str, dict]:
    """What Jev sees per package: title, depth, depends_on plus routing_hints."""
    done = done or set()
    depths = compute_depths(packages)
    out: dict[str, dict] = {}
    for p in packages:
        if p["id"] in done:
            continue
        attrs = {"title": p.get("title", p["id"]), "depth": depths[p["id"]], "depends_on": list(p.get("depends_on", []))}
        hints = p.get("routing_hints") or {}
        attrs.update(hints)
        out[p["id"]] = attrs
    return out


# ----------------------------------------------------------------------------- request


def build_request(packages: list[dict], engines: dict[str, dict], extra_state: dict | None = None,
                  done: set[str] | None = None, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Compose the System One body: state + one choice question per package."""
    avail = available_engines(engines)
    if not avail:
        raise ValueError("no engine is available; use --available <engine>=yes")
    state: dict[str, Any] = {
        "goal": DEFAULT_GOAL,
        "engines": {k: engine_description(k, e) for k, e in engines.items()},
        "packages": package_attrs(packages, done),
        "scheduling_note": SCHEDULING_NOTE,
    }
    state.update(extra_state or {})
    questions = {}
    for wp in state["packages"]:
        questions[f"engine_{wp}"] = {
            "type": "choice",
            "instructions": (
                f"Which engine in `engines` should implement package {wp} (`packages.{wp}`), given its numerical "
                "difficulty, dependency depth (start time), sign-convention criticality, the engines' availability "
                "and observed quality/speed, and the goal? Only engines marked available may be chosen."
            ),
            "criteria": {k: None for k in avail},
        }
    return {"state": state, "model": model, "questions": questions}


def _post(body: dict, api_key: str, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        TYPESAFE_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def route(project_root: str | os.PathLike, packages: list[dict], engines: dict[str, dict],
          extra_state: dict | None = None, *, done: set[str] | None = None, api_key: str | None = None,
          dry_run: bool = False, post: Callable[[dict, str], dict] | None = None,
          model: str = DEFAULT_MODEL, write: bool = True) -> dict[str, Any]:
    """Route every not-done package to an engine. Returns the routing document.

    dry_run: return {"dry_run": True, "request": body} without calling the API.
    Single available engine: no API call, everything routed there with p=1.
    """
    body = build_request(packages, engines, extra_state, done, model)
    if dry_run:
        return {"dry_run": True, "request": body}

    avail = list(available_engines(engines))
    routing: dict[str, dict] = {}
    if len(avail) == 1:
        only = avail[0]
        for wp in body["state"]["packages"]:
            routing[wp] = {"engine": only, "confidence": 1.0, "probabilities": {only: 1.0}}
        model_used = "none (single available engine)"
    else:
        key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY not set (or pass api_key=)")
        out = (post or _post)(body, key)
        for wp in body["state"]["packages"]:
            ans = out["answers"][f"engine_{wp}"]
            routing[wp] = {"engine": ans["choice"], "confidence": ans.get("confidence"),
                           "probabilities": ans.get("probabilities", {})}
        model_used = out.get("model", model)

    doc = {"model": model_used, "state": body["state"], "routing": routing}
    if write:
        path = pathlib.Path(project_root) / ROUTING_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def load_routing(project_root: str | os.PathLike) -> dict[str, str]:
    """{WP: engine} from ops/routing.json, or {} when absent."""
    path = pathlib.Path(project_root) / ROUTING_FILE
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {wp: r["engine"] for wp, r in doc.get("routing", {}).items()}


def format_routing(doc: dict) -> str:
    lines = []
    for wp, r in doc["routing"].items():
        p = r.get("probabilities", {}).get(r["engine"])
        conf = r.get("confidence")
        ptxt = f"p={p:.2f}" if isinstance(p, (int, float)) else "p=?"
        ctxt = f"conf={conf:.2f}" if isinstance(conf, (int, float)) else "conf=?"
        lines.append(f"{wp}: {r['engine']} ({ptxt}, {ctxt})")
    return "\n".join(lines)
