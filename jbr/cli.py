"""python -m jbr route|run|spawn|wait|compose-fix|status

Global options (before the subcommand):
  --project <root>          project root (default: cwd)
  --packages <path>         work packages json, relative to project (default ops/work_packages.json)
  --prompts <dir>           per-package spec dir, relative to project (default ops/prompts)
  --engines <path>          engines.json (default: the one shipped with jbr)
  --available eng=yes|no    override an engine's availability (repeatable)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from jbr import router, runner

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ENGINES = REPO_ROOT / "engines.json"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m jbr", description="Jev build router: route work packages to engines and drive them.")
    ap.add_argument("--project", default=".", help="project root (default cwd)")
    ap.add_argument("--packages", default="ops/work_packages.json", help="work packages json (relative to project)")
    ap.add_argument("--prompts", default="ops/prompts", help="per-package spec markdown dir (relative to project)")
    ap.add_argument("--engines", default=str(DEFAULT_ENGINES), help="engines.json path")
    ap.add_argument("--available", action="append", default=[], metavar="ENGINE=yes|no", help="override availability (repeatable)")
    ap.add_argument("--reports", default="ops/reports", help="reports dir (relative to project)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("route", help="ask Jev which engine implements each package; writes ops/routing.json")
    r.add_argument("--dry-run", action="store_true", help="print the request instead of calling TypeSafe")
    r.add_argument("--done", nargs="*", default=[], help="package ids already finished (excluded)")
    r.add_argument("--now", default=None, help="local time hint passed to Jev")
    r.add_argument("--goal", default=None, help="override the goal sentence")
    r.add_argument("--state-json", default=None, help="path to extra state merged into the request")
    r.add_argument("--model", default=router.DEFAULT_MODEL)
    r.add_argument("--no-write", action="store_true", help="do not write ops/routing.json")

    x = sub.add_parser("run", help="run one package on an engine synchronously (agy|codex)")
    x.add_argument("wp_id")
    x.add_argument("--engine", default=None, help="engine key (default: from ops/routing.json)")
    x.add_argument("--extra", default="", help="additional manager instructions")
    x.add_argument("--extra-file", default=None, help="file with additional instructions (e.g. fix-WP-round1.md)")
    x.add_argument("--tag", default="")
    x.add_argument("--timeout", default=runner.DEFAULT_TIMEOUT)
    x.add_argument("--dry-run", action="store_true", help="compose prompt and print the command only")

    s = sub.add_parser("spawn", help="launch `run` detached for one or more packages")
    s.add_argument("wp_ids", nargs="+")
    s.add_argument("--engine", default=None)
    s.add_argument("--extra", default="")
    s.add_argument("--extra-file", default=None)
    s.add_argument("--tag", default="")
    s.add_argument("--timeout", default=runner.DEFAULT_TIMEOUT)
    s.add_argument("--dry-run", action="store_true", help="print the argv without launching")

    w = sub.add_parser("wait", help="block until the .done markers exist")
    w.add_argument("wp_ids", nargs="+")
    w.add_argument("--tag", default="")
    w.add_argument("--timeout-min", type=float, default=70)
    w.add_argument("--poll", type=float, default=30)

    c = sub.add_parser("compose-fix", help="review workflow output -> fix-<WP>-round<N>.md")
    c.add_argument("review_output")
    c.add_argument("round_no", type=int)

    sub.add_parser("status", help="list runs and their .done markers")
    return ap


def _engines(a) -> dict:
    return router.load_engines(a.engines, router.parse_available(a.available))


def _engine_for(a, wp_id: str, project: pathlib.Path) -> str:
    if a.engine:
        return a.engine
    routing = router.load_routing(project)
    if wp_id not in routing:
        sys.exit(f"no --engine given and {wp_id} not in ops/routing.json (run `python -m jbr route` first)")
    return routing[wp_id]


def _extra(a) -> str:
    extra = a.extra or ""
    if a.extra_file:
        extra = (extra + "\n\n" if extra else "") + pathlib.Path(a.extra_file).read_text(encoding="utf-8")
    return extra


def cmd_route(a) -> int:
    project = pathlib.Path(a.project).resolve()
    packages, meta = runner.load_packages(project / a.packages)
    engines = _engines(a)
    extra_state = {}
    if a.state_json:
        extra_state.update(json.loads(pathlib.Path(a.state_json).read_text(encoding="utf-8")))
    if a.now:
        extra_state["now_local_time"] = a.now
    if a.goal:
        extra_state["goal"] = a.goal
    elif meta.get("goal"):
        extra_state["goal"] = meta["goal"]
    doc = router.route(project, packages, engines, extra_state, done=set(a.done), dry_run=a.dry_run,
                       model=a.model, write=not a.no_write)
    if doc.get("dry_run"):
        req = doc["request"]
        print(f"[dry-run] would POST to {router.TYPESAFE_URL} model={req['model']}")
        print(f"[dry-run] criteria (available engines): {list(router.available_engines(engines))}")
        print(json.dumps(req["questions"], indent=2))
        return 0
    print(router.format_routing(doc))
    if not a.no_write:
        print(f"wrote {project / router.ROUTING_FILE}")
    return 0


def cmd_run(a) -> int:
    project = pathlib.Path(a.project).resolve()
    engines = _engines(a)
    engine_key = _engine_for(a, a.wp_id, project)
    res = runner.run(project, a.wp_id, engine_key, engines, project / a.packages, a.prompts,
                     extra=_extra(a), tag=a.tag, timeout=a.timeout, dry_run=a.dry_run, reports_dir=a.reports)
    if res["exit"] == "workflow":
        print(f"[jbr run] {a.wp_id} engine={engine_key}: {res['note']}")
        return 0
    if a.dry_run:
        print(f"[jbr run] {a.wp_id} engine={engine_key} prompt={res['paths']['prompt']}")
        print("[dry-run] argv:")
        print(json.dumps(res["cmd"], indent=2))
        return 0
    log = pathlib.Path(res["paths"]["log"])
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-25:] if log.exists() else []
    print(f"[jbr run] {a.wp_id} engine={engine_key} exit={res['exit']} elapsed={res['minutes']}min log={log}")
    print("\n".join(tail))
    return int(res["exit"] or 0)


def cmd_spawn(a) -> int:
    project = pathlib.Path(a.project).resolve()
    engines = _engines(a)
    rc = 0
    for wp in a.wp_ids:
        engine_key = _engine_for(a, wp, project)
        if engine_key not in engines:
            # validate here: the detached child would only leave a traceback in .stdout
            print(f"{wp}: unknown engine {engine_key!r}; known: {sorted(engines)}", file=sys.stderr)
            return 2
        if engines[engine_key].get("runner") == "claude_subagent":
            print(f"{wp}: engine {engine_key} is a Claude Code subagent; hand it to workflows/build_dag.js")
            continue
        # the child runs with cwd=project: paths given relative to THIS cwd must be made absolute
        run_args = ["--engine", engine_key, "--engines", str(pathlib.Path(a.engines).resolve()),
                    "--packages", a.packages, "--prompts", a.prompts, "--reports", a.reports, "--timeout", a.timeout]
        for ov in a.available:
            run_args += ["--available", ov]
        if a.extra:
            run_args += ["--extra", a.extra]
        if a.extra_file:
            run_args += ["--extra-file", str(pathlib.Path(a.extra_file).resolve())]
        # global options land after `run`; main() hoists them in the child via _hoist_globals
        res = runner.spawn(project, wp, run_args, tag=a.tag, reports_dir=a.reports, dry_run=a.dry_run)
        if a.dry_run:
            print(f"{wp}: [dry-run] {json.dumps(res['argv'])} marker={res['marker']}")
        else:
            print(f"{wp}: spawned pid={res['pid']} marker={res['marker']}")
    return rc


GLOBAL_OPTS = {"--project", "--packages", "--prompts", "--engines", "--available", "--reports"}
# sub-options that take one value: the token after them is a VALUE, never a global option
VALUE_OPTS = {"--engine", "--extra", "--extra-file", "--tag", "--timeout", "--now", "--goal", "--state-json",
              "--model", "--timeout-min", "--poll"}


def _hoist_globals(argv: list[str]) -> list[str]:
    """Allow global options after the subcommand (spawn emits them that way)."""
    if not argv:
        return argv
    sub_idx = next((i for i, t in enumerate(argv) if not t.startswith("-") and t in
                    {"route", "run", "spawn", "wait", "compose-fix", "status"}), None)
    if sub_idx is None:
        return argv
    head, tail = argv[:sub_idx], argv[sub_idx:]
    hoisted, rest = [], []
    i = 0
    while i < len(tail):
        t = tail[i]
        if t in VALUE_OPTS and i + 1 < len(tail):
            rest += [t, tail[i + 1]]
            i += 2
        elif t in GLOBAL_OPTS and i + 1 < len(tail):
            hoisted += [t, tail[i + 1]]
            i += 2
        elif any(t.startswith(g + "=") for g in GLOBAL_OPTS):
            hoisted.append(t)
            i += 1
        else:
            rest.append(t)
            i += 1
    return head + hoisted + rest


def cmd_wait(a) -> int:
    project = pathlib.Path(a.project).resolve()
    state = runner.wait(project, a.wp_ids, a.tag, a.timeout_min * 60, a.poll, a.reports)
    rc = 0
    for wp, d in state.items():
        if d is None:
            print(f"{wp}: STILL-RUNNING")
            rc = 124
        else:
            print(f"{wp}: exit={d['exit']} {d['minutes']}min")
            rc = rc or (d["exit"] if d["exit"] else 0)
    return rc


def cmd_compose_fix(a) -> int:
    project = pathlib.Path(a.project).resolve()
    for row in runner.compose_fix(project, a.review_output, a.round_no, a.reports):
        print(f"{row['wp']} {row['verdict']} {row['confirmed']} {row['low']}" + (f" {row['fix_file']}" if row["fix_file"] else ""))
    return 0


def cmd_status(a) -> int:
    project = pathlib.Path(a.project).resolve()
    routing = router.load_routing(project)
    pk_path = project / a.packages
    known_ids = [w["id"] for w in runner.load_packages(pk_path)[0]] if pk_path.exists() else []
    rows = runner.status(project, a.reports, known_ids)
    if routing:
        print("routing: " + ", ".join(f"{k}={v}" for k, v in routing.items()))
    if not rows:
        print("no runs found")
    for r in rows:
        d = r["done"]
        state = "running" if d is None else f"exit={d['exit']} {d['minutes']}min"
        print(f"{r['wp']}{('-' + r['tag']) if r['tag'] else ''}: {state} log={r['log_bytes']}B")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = _hoist_globals(list(sys.argv[1:] if argv is None else argv))
    a = _parser().parse_args(argv)
    return {"route": cmd_route, "run": cmd_run, "spawn": cmd_spawn, "wait": cmd_wait,
            "compose-fix": cmd_compose_fix, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
