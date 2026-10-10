"""Engine ledger: what each engine actually delivered, build after build.

The commander (the main Claude Code session) assigns packages itself (`jbr assign`); this ledger is
the evidence it assigns from. After a build, `jbr record <result.json>` appends one line per
package from the build_dag.js result (the list the Workflow returns): who implemented it, the
final verdict, review/fix rounds, confirmed defects, quota hits, and the engine minutes read from
the project's run-*.done markers (paths.reports_dir). `jbr scorecard` aggregates it per engine.

Ledger: ~/.jbr/ledger.jsonl (override JBR_LEDGER), one JSON object per line, append-only, shared
by every project on this machine (the same scope as the engine state file).
"""
from __future__ import annotations

import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Any

from jbr import paths, runner

LAST_RESORT = "claude_subagent"


def ledger_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("JBR_LEDGER") or pathlib.Path.home() / ".jbr" / "ledger.jsonl")


def _implementer(row: dict) -> str:
    """The engine whose implement run succeeded (the chain may have skipped quota-dead engines)."""
    for h in row.get("history", []):
        if h.get("stage") == "impl":
            ok = [e["engine"] for e in h.get("engines", []) if e.get("status") == "ok"]
            if ok:
                return ok[-1]
    return row.get("engine") or "unknown"


def _minutes(project: pathlib.Path | None, wp: str, reports_dir: str | None) -> float | None:
    """Sum of engine minutes over this package's run markers (impl + fix tags); None if none found."""
    if project is None:
        return None
    total, found = 0.0, False
    for f in paths.reports_dir(project, reports_dir).glob(f"{runner.RUN_PREFIX}-{wp}*.done"):
        stem_wp, tag = runner.split_stem(f.name[len(runner.RUN_PREFIX) + 1:-len(".done")], [wp])
        if stem_wp != wp:
            continue  # WP1 must not pick up WP10's markers
        done = runner.read_done(project, wp, tag, reports_dir=reports_dir)
        if done and done.get("minutes") is not None:
            total += float(done["minutes"])
            found = True
    return round(total, 1) if found else None


def entries_from_build(results: list[dict], *, project: str | os.PathLike | None = None, build: str = "",
                       reports_dir: str | None = None, hints: dict[str, dict] | None = None,
                       now: datetime | None = None) -> list[dict]:
    """One ledger entry per package of a build_dag.js result list."""
    root = pathlib.Path(project).resolve() if project else None
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for row in results:
        wp = row["wp"]
        hist = row.get("history", [])
        reviews = [h for h in hist if str(h.get("stage", "")).startswith("review")]
        out.append({
            "ts": ts, "project": root.name if root else "", "build": build, "wp": wp,
            "engine": _implementer(row),
            "final": row.get("final", "unknown"),
            "fix_rounds": sum(1 for h in hist if str(h.get("stage", "")).startswith("fix")),
            "confirmed": sum(int(h.get("confirmed") or 0) for h in reviews),
            "low_open": len(row.get("low_open") or []),
            "unresolved": len(row.get("unresolved") or []),
            "quota_hits": sum(1 for h in hist for e in h.get("engines", []) if e.get("status") == "quota"),
            "minutes": _minutes(root, wp, reports_dir),
            "hints": (hints or {}).get(wp, {}),
        })
    return out


def append(entries: list[dict], path: pathlib.Path | None = None) -> pathlib.Path:
    p = path or ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return p


def load(path: pathlib.Path | None = None) -> list[dict]:
    p = path or ledger_path()
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue  # a torn last line must not hide the rest of the history
    return out


def scorecard(entries: list[dict]) -> dict[str, dict[str, Any]]:
    """Per engine: packages, accepted, failed, mean fix rounds / confirmed defects / minutes, quota hits.

    `failed` = any final other than accept (blocked, unresolved after the last round, manual)."""
    by: dict[str, list[dict]] = {}
    for e in entries:
        by.setdefault(e.get("engine", "unknown"), []).append(e)
    card = {}
    for eng, rows in sorted(by.items()):
        mins = [r["minutes"] for r in rows if r.get("minutes") is not None]
        n = len(rows)
        card[eng] = {
            "packages": n,
            "accepted": sum(1 for r in rows if r.get("final") == "accept"),
            "failed": sum(1 for r in rows if r.get("final") != "accept"),
            "mean_fix_rounds": round(sum(r.get("fix_rounds", 0) for r in rows) / n, 2),
            "mean_confirmed_defects": round(sum(r.get("confirmed", 0) for r in rows) / n, 2),
            "mean_minutes": round(sum(mins) / len(mins), 1) if mins else None,
            "quota_hits": sum(r.get("quota_hits", 0) for r in rows),
            "last": max(r.get("ts", "") for r in rows),
            "kinds": sorted({str(v) for r in rows for k, v in (r.get("hints") or {}).items() if k in ("kind", "size")}),
        }
    return card


def format_scorecard(card: dict[str, dict]) -> str:
    if not card:
        return "ledger is empty: record a build with `python -m jbr record <result.json>`"
    head = f"{'engine':<18} {'pkgs':>4} {'ok':>3} {'fail':>4} {'fix/pkg':>7} {'defects/pkg':>11} {'min/pkg':>7} {'quota':>5}  last"
    lines = [head]
    for eng, c in card.items():
        mins = "-" if c["mean_minutes"] is None else f"{c['mean_minutes']:.1f}"
        lines.append(f"{eng:<18} {c['packages']:>4} {c['accepted']:>3} {c['failed']:>4} {c['mean_fix_rounds']:>7.2f} "
                     f"{c['mean_confirmed_defects']:>11.2f} {mins:>7} {c['quota_hits']:>5}  {c['last'][:10]}")
    return "\n".join(lines)
