"""Named teams and roles (`~/.jbr/teams.json`), each role with its own ordered engine list.

File location:
    ~/.jbr/teams.json      ($JBR_TEAMS overrides; else beside $JBR_STATE when set)

Schema on disk:
    {"teams": {
      "marketing": {
        "description": "Marketing copy and campaign docs",
        "kind": "doc",
        "roles": {
          "drafter": {"engines": ["agy_gemini_flash", "hermes_qwen", "claude_subagent"], "instructions": "..."},
          "editor":  {"engines": ["agy_claude_opus", "claude_subagent"], "instructions": "..."}
        }
      }
    }}

Role order in the JSON is preserved (sort_keys=False): the first role of a team is its default role.
Writes take a lock file and use atomic temp-file + os.replace with Windows retry (reusing state._lock and state._write).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from typing import Any, Iterable

from jbr import router, runner, state

FILE_NAME = "teams.json"
DEFAULT_ENGINES = pathlib.Path(__file__).resolve().parents[1] / "engines.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
KINDS = ("code", "doc")


def teams_path() -> pathlib.Path:
    env = os.environ.get("JBR_TEAMS")
    if env:
        return pathlib.Path(env)
    st = os.environ.get("JBR_STATE")  # tests isolate JBR_STATE per test: teams follow it like switches
    return (pathlib.Path(st).parent if st else pathlib.Path.home() / ".jbr") / FILE_NAME


def _default_engines() -> dict[str, dict]:
    return router.load_engines(DEFAULT_ENGINES, use_switches=False)


def allowed_engines(engines: dict[str, dict] | None = None) -> list[str]:
    """Engine keys allowed in a team role's engine list (all loaded engines except manual ones)."""
    engs = _default_engines() if engines is None else engines
    return [k for k in router.by_priority(engs) if k != "agy_manual" and not runner.is_manual(engs.get(k))]


def _unwrap(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """The {team_name: {...}} mapping. Callers always pass that inner mapping; only the file on disk
    carries the {"teams": ...} wrapper, which load() strips. No guessing, so any valid name (even
    "teams" or "roles") round-trips."""
    if not isinstance(data, dict):
        return None, "teams data must be a JSON object"
    return data, None


def load(path: pathlib.Path | None = None, *, strict: bool = False) -> dict[str, dict]:
    """Load teams from disk. Missing file -> {}; malformed or non-dict -> {} plus a one-line stderr warning;
    non-dict team/role entries are dropped.

    strict=True raises ValueError instead of returning {} for an unreadable or malformed file. Every
    read-modify-write uses it, so a hand-edit typo is never "fixed" by overwriting the whole roster."""
    p = pathlib.Path(path) if path else teams_path()

    def bad(msg: str) -> dict[str, dict]:
        if strict:
            raise ValueError(f"{msg}; fix or remove {p} first (nothing was written)")
        print(f"[jbr teams] warning: {msg}", file=sys.stderr)
        return {}

    try:
        raw = state._retry(lambda: p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as e:
        return bad(f"could not read {p}: {e}")
    try:
        data = json.loads(raw)
    except ValueError as e:
        return bad(f"malformed JSON in {p}: {e}")
    teams_map = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(teams_map, dict):
        return bad(f"invalid teams file {p}: expected {{\"teams\": {{...}}}}")
    out: dict[str, dict] = {}
    for tname, tobj in teams_map.items():
        if not isinstance(tobj, dict):
            continue
        t = dict(tobj)
        t.setdefault("description", "")
        t.setdefault("kind", "code")
        roles = t.get("roles")
        if isinstance(roles, dict):
            clean_roles: dict[str, dict] = {}
            for rname, robj in roles.items():
                if isinstance(robj, dict):
                    r = dict(robj)
                    r.setdefault("instructions", "")
                    clean_roles[rname] = r
            t["roles"] = clean_roles
        else:
            t["roles"] = {}
        out[tname] = t
    return out


def validate(data: Any, engines: dict[str, dict] | None = None) -> list[str]:
    """Errors (empty when valid) for a {team_name: {...}} mapping, the inner part of the file."""
    engs = _default_engines() if engines is None else engines
    teams_map, err = _unwrap(data)
    if teams_map is None:
        return [err or "teams data must be a JSON object"]
    errors: list[str] = []
    for tname, tobj in teams_map.items():
        if not isinstance(tname, str) or not NAME_RE.match(tname):
            errors.append(f"team {tname!r}: invalid name (must match [a-z0-9][a-z0-9_-]{{0,39}})")
        if not isinstance(tobj, dict):
            errors.append(f"team {tname!r}: must be a JSON object")
            continue
        kind = tobj.get("kind", "code")
        if kind not in KINDS:
            errors.append(f"team {tname!r}: kind must be 'code' or 'doc', got {kind!r}")
        desc = tobj.get("description", "")
        if desc is not None and not isinstance(desc, str):
            errors.append(f"team {tname!r}: description must be a string")
        roles = tobj.get("roles")
        if not isinstance(roles, dict) or not roles:
            errors.append(f"team {tname!r}: must have at least one role in 'roles'")
            continue
        for rname, robj in roles.items():
            if not isinstance(rname, str) or not NAME_RE.match(rname):
                errors.append(f"team {tname!r} role {rname!r}: invalid name (must match [a-z0-9][a-z0-9_-]{{0,39}})")
            if not isinstance(robj, dict):
                errors.append(f"team {tname!r} role {rname!r}: must be a JSON object")
                continue
            inst = robj.get("instructions", "")
            if inst is not None and not isinstance(inst, str):
                errors.append(f"team {tname!r} role {rname!r}: instructions must be a string")
            r_engines = robj.get("engines")
            if not isinstance(r_engines, list) or not r_engines:
                errors.append(f"team {tname!r} role {rname!r}: 'engines' must be a non-empty list")
                continue
            seen: set[str] = set()
            dups: set[str] = set()
            for ek in r_engines:
                if not isinstance(ek, str) or ek not in engs:
                    errors.append(f"team {tname!r} role {rname!r}: unknown engine {ek!r}")
                elif ek == "agy_manual" or runner.is_manual(engs.get(ek)):
                    errors.append(f"team {tname!r} role {rname!r}: manual engine {ek!r} is not allowed in a role list")
                if isinstance(ek, str):
                    if ek in seen and ek not in dups:
                        errors.append(f"team {tname!r} role {rname!r}: duplicate engine {ek!r}")
                        dups.add(ek)
                    seen.add(ek)
    return errors


def _normalize(teams_map: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for tname, tobj in teams_map.items():
        roles: dict[str, dict] = {}
        for rname, robj in (tobj.get("roles") or {}).items():
            roles[rname] = {
                "engines": list(robj.get("engines") or []),
                "instructions": str(robj.get("instructions") or ""),
            }
        out[tname] = {
            "description": str(tobj.get("description") or ""),
            "kind": tobj.get("kind") or "code",
            "roles": roles,
        }
    return out


def save(data: Any, path: pathlib.Path | None = None, *, engines: dict[str, dict] | None = None) -> dict[str, dict]:
    """Validate and atomically write `data` to the teams file under a lock. Returns the saved teams dict."""
    p = pathlib.Path(path) if path else teams_path()
    errs = validate(data, engines)
    if errs:
        raise ValueError("; ".join(errs))
    teams_map, _ = _unwrap(data)
    assert teams_map is not None
    clean = _normalize(teams_map)
    with state._lock(p.with_name(f"{p.name}.lock")):
        load(p, strict=True)  # a malformed file on disk is the user's to fix, never silently replaced
        state._write(p, {"teams": clean}, sort_keys=False)
    return clean


def set_role(
    team: str,
    role: str,
    engines: list[str] | str,
    instructions: str | None = None,
    kind: str | None = None,
    description: str | None = None,
    *,
    path: pathlib.Path | None = None,
    known_engines: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Create or update one role (and team metadata) under a single lock."""
    p = pathlib.Path(path) if path else teams_path()
    eng_list = [e.strip() for e in engines.split(",") if e.strip()] if isinstance(engines, str) else list(engines)
    with state._lock(p.with_name(f"{p.name}.lock")):
        data = load(p, strict=True)
        tobj = dict(data.get(team) or {"description": "", "kind": "code", "roles": {}})
        if description is not None:
            tobj["description"] = description
        else:
            tobj.setdefault("description", "")
        if kind is not None:
            tobj["kind"] = kind
        else:
            tobj.setdefault("kind", "code")
        roles = dict(tobj.get("roles") or {})
        prev_role = roles.get(role) or {}
        roles[role] = {
            "engines": eng_list,
            "instructions": instructions if instructions is not None else str(prev_role.get("instructions") or ""),
        }
        tobj["roles"] = roles
        data[team] = tobj
        errs = validate(data, known_engines)
        if errs:
            raise ValueError("; ".join(errs))
        clean = _normalize(data)
        state._write(p, {"teams": clean}, sort_keys=False)
    return clean


def remove(team: str, role: str | None = None, *, path: pathlib.Path | None = None) -> dict[str, dict]:
    """Remove a role from `team`, or the whole `team` when `role` is None, under a single lock."""
    p = pathlib.Path(path) if path else teams_path()
    with state._lock(p.with_name(f"{p.name}.lock")):
        data = load(p, strict=True)
        if team not in data:
            raise KeyError(f"unknown team {team!r}; known: {sorted(data)}")
        if role is None:
            del data[team]
        else:
            roles = dict(data[team].get("roles") or {})
            if role not in roles:
                raise KeyError(f"unknown role {role!r} in team {team!r}; known: {list(roles)}")
            del roles[role]
            if roles:
                data[team]["roles"] = roles
            else:
                del data[team]
        clean = _normalize(data)
        state._write(p, {"teams": clean}, sort_keys=False)
    return clean


def pick_for_role(
    team: str,
    role: str | None,
    engines: dict[str, dict],
    exclude: Iterable[str] | None = None,
    live: bool = True,
    *,
    path: pathlib.Path | None = None,
    **kw: Any,
) -> tuple[str, list[state.Probe]]:
    """Walk the role's engine list in order (ignoring priority), skipping excluded and switched-off engines
     while allowing assign_only engines. Return (engine_key, seen_probes). `claude_subagent` returns immediately
    when reached, and is the fallback if the list is exhausted."""
    excl = set(exclude or ())
    data = load(path)
    if team not in data:
        raise KeyError(f"unknown team {team!r}; known: {sorted(data)}")
    roles = data[team].get("roles") or {}
    if not role:
        role = next(iter(roles), "")
    if role not in roles:
        raise KeyError(f"unknown role {role!r} in team {team!r}; known: {list(roles)}")
    seen: list[state.Probe] = []
    for k in roles[role].get("engines") or []:
        if k in excl:
            continue
        if k == "claude_subagent" or (k in engines and engines[k].get("runner") == "claude_subagent"):
            return "claude_subagent", seen
        if k not in engines or not engines[k].get("available", True) or runner.is_manual(engines[k]):
            continue
        r = state.probe(k, engines, live=live, **kw)
        seen.append(r)
        if r.usable:
            return k, seen
    return "claude_subagent", seen


def example() -> dict[str, dict]:
    """Starter teams dict: `marketing` (doc: drafter, editor) and `engineering` (code: builder, reviewer)."""
    return {
        "marketing": {
            "description": "Marketing copy and campaign docs",
            "kind": "doc",
            "roles": {
                "drafter": {
                    "engines": ["agy_gemini_flash", "hermes_qwen", "claude_subagent"],
                    "instructions": "Write the first draft for the target audience with clear structure and concrete claims.",
                },
                "editor": {
                    "engines": ["agy_claude_opus", "claude_subagent"],
                    "instructions": "Tighten prose, verify every claim against the source material, and remove filler or placeholders.",
                },
            },
        },
        "engineering": {
            "description": "Code implementation and review",
            "kind": "code",
            "roles": {
                "builder": {
                    "engines": ["agy_gemini_pro", "codex_astra", "hermes_qwen", "claude_subagent"],
                    "instructions": "Implement the requested changes cleanly, keep existing behaviour intact, and add focused tests.",
                },
                "reviewer": {
                    "engines": ["agy_claude_opus", "codex_astra", "claude_subagent"],
                    "instructions": "Audit the implementation against every acceptance criterion, edge case, and test assertion.",
                },
            },
        },
    }
