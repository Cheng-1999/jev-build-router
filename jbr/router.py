"""Ask Jev (TypeSafe System One) which engine implements each work package.

One batch request: one `choice` question per package, criteria = the available
engine keys. Result written to <project_root>/ops/routing.json as
{"model": ..., "state": ..., "routing": {"WP04": {"engine", "confidence", "probabilities"}, ...}}.

Package attributes shown to Jev come from each package's optional `routing_hints`
dict (numerical_difficulty, sign_convention_critical, in_flight_on, ...); `depth`
is taken from routing_hints when present, otherwise computed from depends_on.

Engine chain (2026-10-02): each engine has a `priority` (1 = preferred). When `availability`
(live `jbr probe` results, see jbr/state.py) is passed, Jev sees every engine's priority and live
status, may only choose a usable one, and is asked to prefer the highest-priority usable engine
unless the package's hints clearly favour another. Each package also gets a fallback chain
(Jev's pick, then the other usable engines by priority, ending in claude_subagent), written to
routing.json as routing[wp].chain and as the top-level {"chains": {wp: [...]}} that
workflows/build_dag.js takes as its `routing` arg.
"""
from __future__ import annotations

import json
import os
import pathlib
import urllib.request
import urllib.parse
from typing import Any, Callable

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
# Any System One-compatible endpoint: a local Clef server (clef_mlx.py serve -> http://127.0.0.1:8000/v1/systemone,
# no key needed) or Cloudflare Workers AI (.../ai/run/@cf/cloudflare/clef, answers wrapped in `result`).
DECISION_URL_ENV = "JBR_DECISION_URL"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_MODEL = "jev-latest"
ROUTING_FILE = pathlib.Path("ops") / "routing.json"

DEFAULT_GOAL = (
    "Finish, review and audit the build fastest without sacrificing correctness. Every package is "
    "reviewed by fresh Claude reviewers with adversarial refutation and up to 2 fix rounds regardless of engine."
)
CHAIN_POLICY = (
    "Engine chain: engines are ranked by `priority` (1 = most preferred; the user's default order is agy Gemini, "
    "agy Claude Opus, Codex, Claude subagent as last resort). Prefer the highest-priority engine that is available "
    "now, unless this package's routing hints (numerical difficulty, state consistency / sign-convention "
    "criticality, size) clearly favour another available engine. Never choose an engine whose live_status is "
    "EXHAUSTED or UNAVAILABLE: it is out of quota or broken right now and is not offered as an answer."
)
LAST_RESORT = "claude_subagent"
USABLE_STATUSES = {"AVAILABLE", "UNKNOWN"}  # UNKNOWN = not probed (offline / dry-run)
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


def by_priority(engines: dict[str, dict]) -> list[str]:
    """Engine keys by priority ascending (missing priority last), engines.json order on ties."""
    keys = list(engines)
    return sorted(keys, key=lambda k: (engines[k].get("priority", 99), keys.index(k)))


def usable_engines(engines: dict[str, dict], availability: dict[str, dict] | None = None) -> dict[str, dict]:
    """Available engines (engines.json / --available) minus those whose live probe is not usable."""
    avail = available_engines(engines)
    if availability is None:
        return avail
    return {k: v for k, v in avail.items() if availability.get(k, {}).get("status", "UNKNOWN") in USABLE_STATUSES}


def engine_description(key: str, eng: dict, live: dict | None = None) -> str:
    bits = [eng.get("description", key)]
    bits.append(f"runner={eng.get('runner', '?')} model={eng.get('model', '?')} effort={eng.get('effort', '?')}")
    if eng.get("parallel_limit"):
        bits.append(f"up to {eng['parallel_limit']} packages in parallel")
    if eng.get("priority") is not None:
        bits.append(f"chain priority {eng['priority']} (1 = most preferred)")
    bits.append("currently available: " + ("yes" if eng.get("available", True) else "no"))
    if live is not None:
        bits.append(f"live_status: {live.get('status', 'UNKNOWN')}" + (f" until {live['until']}" if live.get("until") else ""))
    return ". ".join(bits)


def chain_for(pick: str, engines: dict[str, dict], availability: dict[str, dict] | None = None) -> list[str]:
    """Jev's pick first, then the other usable engines by priority, ending in claude_subagent."""
    if pick == LAST_RESORT:  # never quota-limited: nothing after it would ever run
        return [LAST_RESORT]
    usable = usable_engines(engines, availability)
    chain = [pick] + [k for k in by_priority(engines) if k in usable and k not in (pick, LAST_RESORT)]
    if LAST_RESORT in engines and LAST_RESORT not in chain:
        chain.append(LAST_RESORT)
    return chain


def live_status(key: str, engines: dict[str, dict], availability: dict[str, dict] | None) -> str:
    if not engines[key].get("available", True):
        return "UNAVAILABLE"
    if availability is None:
        return "AVAILABLE"
    return availability.get(key, {}).get("status", "UNKNOWN")


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
                  done: set[str] | None = None, model: str = DEFAULT_MODEL,
                  availability: dict[str, dict] | None = None) -> dict[str, Any]:
    """Compose the System One body: state + one choice question per package.

    availability: {engine: {"status": AVAILABLE|EXHAUSTED|UNAVAILABLE|UNKNOWN, "until", ...}} from
    `jbr probe`; engines that are not usable are described to Jev but never offered as answers."""
    avail = usable_engines(engines, availability)
    if not avail:
        raise ValueError("no engine is available; use --available <engine>=yes")
    state: dict[str, Any] = {
        "goal": DEFAULT_GOAL,
        "engines": {k: engine_description(k, e, None if availability is None else availability.get(k, {}))
                    for k, e in engines.items()},
        "packages": package_attrs(packages, done),
        "scheduling_note": SCHEDULING_NOTE,
    }
    if any("priority" in e for e in engines.values()):
        state["routing_policy"] = CHAIN_POLICY
        state["engine_chain"] = [{"engine": k, "priority": engines[k].get("priority"),
                                  "live_status": live_status(k, engines, availability)} for k in by_priority(engines)]
    state.update(extra_state or {})
    questions = {}
    for wp in state["packages"]:
        questions[f"engine_{wp}"] = {
            "type": "choice",
            "instructions": (
                f"Which engine in `engines` should implement package {wp} (`packages.{wp}`), given its numerical "
                "difficulty, dependency depth (start time), sign-convention criticality, the engines' availability "
                "and observed quality/speed, and the goal? Only engines marked available may be chosen."
                + (" Follow `routing_policy`: prefer the highest-priority available engine in `engine_chain` unless "
                   "this package's hints clearly favour another available one." if "routing_policy" in state else "")
            ),
            "criteria": {k: None for k in avail},
        }
    return {"state": state, "model": model, "questions": questions}


def decision_url() -> str:
    return os.environ.get(DECISION_URL_ENV) or TYPESAFE_URL


def is_local(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS
            and parsed.username is None and parsed.password is None)


def decision_token(url: str) -> str:
    """Select credentials for the destination; never forward a Jev key to an override URL."""
    if is_local(url):
        return "local"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("JBR_DECISION_URL must be loopback HTTP or HTTPS without URL credentials")
    if url == TYPESAFE_URL:
        return os.environ.get("TYPESAFE_API_KEY", "")
    if (parsed.hostname == "api.cloudflare.com"
            and parsed.path.startswith("/client/v4/accounts/")
            and "/ai/run/@cf/cloudflare/" in parsed.path):
        return os.environ.get("CLOUDFLARE_AI_TOKEN", "")
    return os.environ.get("JBR_DECISION_TOKEN", "")


def _post(body: dict, api_key: str, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        decision_url(), data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    # Cloudflare's REST envelope: {"result": {...answers...}, "success": true, ...}
    if "answers" not in out and isinstance(out.get("result"), dict):
        out = out["result"]
    return out


def assign(project_root: str | os.PathLike, assignments: dict[str, str], engines: dict[str, dict],
           reasons: dict[str, str] | None = None, *, availability: dict[str, dict] | None = None,
           write: bool = True, second_opinion: dict | None = None) -> dict[str, Any]:
    """The commander's routing: `assignments` maps package -> engine, chosen by the main session from
    the specs, the code and `jbr scorecard`. Same ops/routing.json shape as route(), so build_dag.js
    and `run`/`spawn` read it unchanged. An engine that is unknown or not usable right now is refused
    (the commander re-assigns; a silent substitution would hide the decision). `second_opinion` is an
    optional route() document kept beside the decision for the record, never applied."""
    reasons = reasons or {}
    usable = usable_engines(engines, availability)
    routing: dict[str, dict] = {}
    for wp, eng in assignments.items():
        if eng not in engines:
            raise ValueError(f"{wp}: unknown engine {eng!r}; known: {sorted(engines)}")
        if eng not in usable and eng != LAST_RESORT:
            live = (availability or {}).get(eng, {})
            raise ValueError(f"{wp}: engine {eng} is not usable now ({live.get('status', 'disabled')}"
                             f"{' until ' + live['until'] if live.get('until') else ''}); assign another")
        routing[wp] = {"engine": eng, "assigned_by": "commander", "reason": reasons.get(wp, ""),
                       "chain": chain_for(eng, engines, availability)}
        if second_opinion:
            other = second_opinion.get("routing", {}).get(wp)
            if other:
                routing[wp]["second_opinion"] = {"engine": other["engine"], "confidence": other.get("confidence"),
                                                 "model": second_opinion.get("model")}
    doc = {"model": "commander", "routing": routing, "chains": {wp: r["chain"] for wp, r in routing.items()}}
    if availability is not None:
        doc["availability"] = availability
    if write:
        path = pathlib.Path(project_root) / ROUTING_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return doc


def route(project_root: str | os.PathLike, packages: list[dict], engines: dict[str, dict],
          extra_state: dict | None = None, *, done: set[str] | None = None, api_key: str | None = None,
          dry_run: bool = False, post: Callable[[dict, str], dict] | None = None,
          model: str = DEFAULT_MODEL, write: bool = True,
          availability: dict[str, dict] | None = None) -> dict[str, Any]:
    """Route every not-done package to an engine. Returns the routing document.

    dry_run: return {"dry_run": True, "request": body} without calling the API.
    Single available engine: no API call, everything routed there with p=1.
    availability: live probe results (jbr/state.py); unusable engines are never routed to.
    """
    body = build_request(packages, engines, extra_state, done, model, availability)
    if dry_run:
        return {"dry_run": True, "request": body}

    avail = list(usable_engines(engines, availability))
    routing: dict[str, dict] = {}
    if len(avail) == 1:
        only = avail[0]
        for wp in body["state"]["packages"]:
            routing[wp] = {"engine": only, "confidence": 1.0, "probabilities": {only: 1.0}}
        model_used = "none (single available engine)"
    else:
        key = api_key or decision_token(decision_url())
        if not key:
            raise RuntimeError("decision endpoint token not set (TYPESAFE_API_KEY, CLOUDFLARE_AI_TOKEN, or JBR_DECISION_TOKEN)")
        out = (post or _post)(body, key)
        for wp in body["state"]["packages"]:
            ans = out["answers"][f"engine_{wp}"]
            routing[wp] = {"engine": ans["choice"], "confidence": ans.get("confidence"),
                           "probabilities": ans.get("probabilities", {})}
            if ans["choice"] not in avail:  # never route to an exhausted/unavailable engine, whatever came back
                routing[wp]["engine"] = next(k for k in by_priority(engines) if k in avail)
                routing[wp]["overridden"] = f"Jev chose unusable engine {ans['choice']!r}"
        model_used = out.get("model", model)

    for r in routing.values():
        r["chain"] = chain_for(r["engine"], engines, availability)
    doc = {"model": model_used, "state": body["state"], "routing": routing,
           "chains": {wp: r["chain"] for wp, r in routing.items()}}
    if availability is not None:
        doc["availability"] = availability
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


def load_chains(project_root: str | os.PathLike) -> dict[str, list[str]]:
    """{WP: [engine, ...]} fallback chains from ops/routing.json (pre-chain files: [engine])."""
    path = pathlib.Path(project_root) / ROUTING_FILE
    if not path.exists():
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {wp: list(r.get("chain") or [r["engine"]]) for wp, r in doc.get("routing", {}).items()}


def format_routing(doc: dict) -> str:
    lines = []
    for wp, r in doc["routing"].items():
        p = r.get("probabilities", {}).get(r["engine"])
        conf = r.get("confidence")
        ptxt = f"p={p:.2f}" if isinstance(p, (int, float)) else "p=?"
        ctxt = f"conf={conf:.2f}" if isinstance(conf, (int, float)) else "conf=?"
        chain = f" chain: {' > '.join(r['chain'])}" if r.get("chain") else ""
        lines.append(f"{wp}: {r['engine']} ({ptxt}, {ctxt}){chain}")
    return "\n".join(lines)
