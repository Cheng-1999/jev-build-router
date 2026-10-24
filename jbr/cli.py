"""python -m jbr route|run|spawn|wait|compose-fix|status|probe|pick|prereview|handoff|done|inbox

Global options (before the subcommand):
  --project <root>          project root (default: cwd)
  --packages <path>         work packages json, relative to project (default ops/work_packages.json)
  --prompts <dir>           per-package spec dir, relative to project (default ops/prompts)
  --engines <path>          engines.json (default: the one shipped with jbr)
  --available eng=yes|no    override an engine's availability (repeatable)

Engine chain: `probe <engine>` prints AVAILABLE (exit 0) | EXHAUSTED <until> (exit 3) |
UNAVAILABLE <reason> (exit 4) from the shared state file ($JBR_STATE, default ~/.jbr/engine_state.json)
or one cheap live probe; `pick [--exclude a,b]` prints the first available engine by priority.

Pre-review: `prereview <WP>` runs alibaba/open-code-review on the package's changed owned files against
local Ollama (jbr/prereview.py) and writes prereview-<WP>[-tag].json; it always exits 0 (the status is
in the json and the .done marker), so a missing tool or a slow model never stops a build.

Manual engines (runner "manual", e.g. agy_manual): jbr never runs them. `handoff <WP>` writes the
packet and prints the line to paste into the engine you run yourself; `done <WP>` records the outcome
(.done marker), after which review_level.js reviews it like any other run.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from jbr import paths, prereview, router, runner, state

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_ENGINES = REPO_ROOT / "engines.json"


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m jbr", description="Jev build router: route work packages to engines and drive them.")
    ap.add_argument("--project", default=".", help="project root (default cwd)")
    ap.add_argument("--packages", default="ops/work_packages.json", help="work packages json (relative to project)")
    ap.add_argument("--prompts", default="ops/prompts", help="per-package spec markdown dir (relative to project)")
    ap.add_argument("--engines", default=str(DEFAULT_ENGINES), help="engines.json path")
    ap.add_argument("--available", action="append", default=[], metavar="ENGINE=yes|no", help="override availability (repeatable)")
    ap.add_argument("--reports", default=None,
                    help="run-artifact dir (default ~/.jbr/runs/<project key>/reports; relative = inside the project)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("route", help="ask Jev which engine implements each package; writes ~/.jbr/runs/<key>/routing.json")
    r.add_argument("--dry-run", action="store_true", help="print the request instead of calling TypeSafe")
    r.add_argument("--done", nargs="*", default=[], help="package ids already finished (excluded)")
    r.add_argument("--now", default=None, help="local time hint passed to Jev")
    r.add_argument("--goal", default=None, help="override the goal sentence")
    r.add_argument("--state-json", default=None, help="path to extra state merged into the request")
    r.add_argument("--model", default=router.DEFAULT_MODEL)
    r.add_argument("--no-write", action="store_true", help="do not write routing.json")
    r.add_argument("--no-probe", action="store_true",
                   help="use only the engine state file for availability (no live probes; implied by --dry-run)")

    x = sub.add_parser("run", help="run one package on an engine synchronously (agy|codex)")
    x.add_argument("wp_id")
    x.add_argument("--engine", default=None, help="engine key (default: from routing.json)")
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

    pr = sub.add_parser("probe", help="engine availability: AVAILABLE (0) | EXHAUSTED <until> (3) | UNAVAILABLE <reason> (4)")
    pr.add_argument("engine")
    pr.add_argument("--no-live", action="store_true", help="state file only; never call the engine (UNKNOWN -> exit 0)")

    asg = sub.add_parser("assign", help="the commander's routing: WP=engine pairs -> routing.json (live-checked chains)")
    asg.add_argument("pairs", nargs="+", metavar="WP=ENGINE", help="e.g. WP01=agy_gemini_pro WP03=codex_astra")
    asg.add_argument("--reason", action="append", default=[], metavar="WP=TEXT", help="one-line rationale per package (repeatable)")
    asg.add_argument("--no-probe", action="store_true", help="state file only for availability (no live probes)")
    asg.add_argument("--second-opinion", action="store_true",
                     help="also ask the decision model (JBR_DECISION_URL, e.g. local Clef) and record its pick beside yours; never applied")
    asg.add_argument("--model", default=router.DEFAULT_MODEL, help="decision model name for --second-opinion")
    asg.add_argument("--no-write", action="store_true")

    rec = sub.add_parser("record", help="append a build_dag.js result (JSON list) to the engine ledger")
    rec.add_argument("result_json", help="file with the Workflow's returned list [{wp, engine, final, history, ...}]")
    rec.add_argument("--build", default="", help="build name for the ledger (e.g. clef-backend)")

    sc = sub.add_parser("scorecard", help="per-engine track record from the ledger (what the commander assigns from)")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--project-only", action="store_true", help="only this project's entries")

    q = sub.add_parser("queue", help="backlog worked off by one engine (local Qwen by default), one package at a time, in worktrees")
    qs = q.add_subparsers(dest="qcmd", required=True)
    qa = qs.add_parser("add", help="queue packages from the plan (--packages/--prompts) for unattended work")
    qa.add_argument("wp_ids", nargs="+")
    qa.add_argument("--engine", default="ollama_qwen")
    qa.add_argument("--base", default="HEAD", help="git ref the worktree branches from (default HEAD of the project)")
    qa.add_argument("--test-cmd", default="", help="run in the worktree after the engine (default: the package's `tests`)")
    qw = qs.add_parser("work", help="drain the queue (one package at a time; stops when the engine is not usable)")
    qw.add_argument("--max", type=int, default=0, help="stop after N packages")
    qw.add_argument("--detach", action="store_true", help="run in the background; log in the runs dir's worker.log")
    qw.add_argument("--test-timeout-min", type=float, default=60)
    qs.add_parser("status", help="every queued package: status, round, minutes, test exit, worktree")
    qf = qs.add_parser("fix", help="put a finished package back with a reviewer's fix file (same worktree)")
    qf.add_argument("wp_id")
    qf.add_argument("fix_file")
    qd = qs.add_parser("drop", help="remove a pending package")
    qd.add_argument("wp_id")
    qr = qs.add_parser("review-args", help="Workflow args for review_level.js, one object per finished package")
    qr.add_argument("wp_ids", nargs="*", help="default: every done package")
    qc = qs.add_parser("clean", help="remove a finished package's worktree and branch")
    qc.add_argument("wp_id")
    qc.add_argument("--force", action="store_true", help="even with unmerged engine work in it")

    sub.add_parser("paths", help="print where jbr keeps this project's files (pass reportsDir to the Workflow scripts)")

    pv = sub.add_parser("prereview", help="cheap first-pass review: open-code-review on local Ollama; always exits 0")
    pv.add_argument("wp_id")
    pv.add_argument("--tag", default="", help="artifact tag, e.g. r1 for review round 1")
    pv.add_argument("--commit", default="", help="review this commit instead of the uncommitted workspace")
    pv.add_argument("--url", default=prereview.DEFAULT_URL, help="OpenAI-compatible endpoint (default: local Ollama)")
    pv.add_argument("--model", default="", help="default: the backend engine's model")
    pv.add_argument("--backend-engine", default=prereview.BACKEND_ENGINE,
                    help="engines.json key whose model is the default (disabled engine = SKIPPED)")
    pv.add_argument("--effort", choices=["low", "medium", "high"], default=prereview.DEFAULT_EFFORT)
    pv.add_argument("--budget-min", type=float, default=prereview.DEFAULT_BUDGET_MIN,
                    help="whole run; waiting for another prereview (max 5 min, then SKIPPED busy) included; then TIMEOUT")
    pv.add_argument("--no-spec", action="store_true", help="do not pass ops/prompts/<WP>.md as review background")
    pv.add_argument("--allow-remote", action="store_true",
                    help="permit a non-loopback --url (repository content then leaves this machine)")
    pv.add_argument("--detach", action="store_true", help="run in the background; poll the .done marker")
    pv.add_argument("--dry-run", action="store_true", help="print scope and argv; never run ocr")

    ho = sub.add_parser("handoff", help="manual engine: write the packet for a package YOU run (e.g. in agy); executes nothing")
    ho.add_argument("wp_id", nargs="?", help="the package (omit with --pending)")
    ho.add_argument("--pending", action="store_true",
                    help="every package assigned to a manual engine and not finished, plus open fix handoffs: one checklist")
    ho.add_argument("--done", nargs="*", default=[], metavar="WP",
                    help="with --pending: dependencies already built elsewhere (e.g. by a Claude subagent: no run marker)")
    ho.add_argument("--engine", default=None, help="manual engine key (default: from routing.json, else agy_manual)")
    ho.add_argument("--extra", default="", help="additional manager instructions")
    ho.add_argument("--extra-file", default=None, help="file with additional instructions (e.g. fix-WP-round1.md)")
    ho.add_argument("--tag", default="", help="e.g. fix1 for fix round 1 (keeps round 0's packet and log)")
    ho.add_argument("--print", dest="print_prompt", action="store_true",
                    help="print the whole packet (with --pending: the checklist with every packet inlined) instead of the pointer line")

    dn = sub.add_parser("done", help="manual engine: record the outcome of a handed-off package (.done marker)")
    dn.add_argument("wp_ids", nargs="*", help="packages to record (with --from: only these; default every report found)")
    dn.add_argument("--from", dest="from_file", default=None,
                    help="file with the engine's output: each === REPORT <id> === block marks <id> done (status: done -> OK, else CRASH)")
    dn.add_argument("--tag", default="")
    dn.add_argument("--status", default="OK", type=str.upper, choices=["OK", "CRASH", "QUOTA"],
                    help="OK = the engine did the work (gets reviewed); CRASH/QUOTA = it did nothing usable")
    dn.add_argument("--exit", dest="exit_code", type=int, default=None, help="default 0 for OK, else 1")
    dn.add_argument("--minutes", type=float, default=None, help="default: time since `jbr handoff`")
    dn.add_argument("--notes", default="", help="the engine's final report (test summary, files changed)")
    dn.add_argument("--notes-file", default=None, help="file holding that report")
    dn.add_argument("--force", action="store_true", help="overwrite an outcome already recorded")

    sub.add_parser("inbox", help="every project's agy checklist still waiting for you (handoff-pending.md), with its paste line")

    pk = sub.add_parser("pick", help="print the first available engine by priority (claude_subagent if none)")
    pk.add_argument("--exclude", default="", help="comma-separated engine keys to skip")
    pk.add_argument("--no-live", action="store_true", help="state file only; never call an engine")
    return ap


def _engines(a) -> dict:
    return router.load_engines(a.engines, router.parse_available(a.available))


def _engine_for(a, wp_id: str, project: pathlib.Path) -> str:
    if a.engine:
        return a.engine
    routing = router.load_routing(project)
    if wp_id not in routing:
        sys.exit(f"no --engine given and {wp_id} not in routing.json (run `python -m jbr assign` first)")
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
    # live availability for the chain: never route to an engine that probes EXHAUSTED/UNAVAILABLE
    avail = state.availability(engines, live=not (a.dry_run or a.no_probe))
    for k, v in avail.items():
        if v["status"] not in router.USABLE_STATUSES:
            print(f"[jbr route] {k}: {v['status']}" + (f" until {v['until']}" if v.get("until") else "")
                  + (f" ({v['detail']})" if v.get("detail") else ""), file=sys.stderr)
    doc = router.route(project, packages, engines, extra_state, done=set(a.done), dry_run=a.dry_run,
                       model=a.model, write=not a.no_write, availability=avail)
    if doc.get("dry_run"):
        req = doc["request"]
        print(f"[dry-run] would POST to {router.decision_url()} model={req['model']}")
        print(f"[dry-run] criteria (available engines): {list(router.usable_engines(engines, avail))}")
        print(json.dumps(req["questions"], indent=2))
        return 0
    print(router.format_routing(doc))
    if not a.no_write:
        print(f"wrote {paths.routing_path(project)}")
    return 0


def _refuse_manual(engines: dict, engine_key: str, wp: str) -> bool:
    if runner.is_manual(engines.get(engine_key)):
        print(f"{wp}: {engine_key} is a {runner.MANUAL_REFUSAL}", file=sys.stderr)
        return True
    return False


def cmd_run(a) -> int:
    project = pathlib.Path(a.project).resolve()
    engines = _engines(a)
    engine_key = _engine_for(a, a.wp_id, project)
    if _refuse_manual(engines, engine_key, a.wp_id):
        return 2
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
        if _refuse_manual(engines, engine_key, wp):
            return 2
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


def cmd_probe(a) -> int:
    r = state.probe(a.engine, _engines(a), live=not a.no_live)
    print(r.line())
    return r.exit_code


def cmd_pick(a) -> int:
    exclude = {e.strip() for e in a.exclude.split(",") if e.strip()}
    key, seen = state.pick(_engines(a), exclude, live=not a.no_live)
    for r in seen:
        if r.engine != key:
            print(f"[jbr pick] {r.engine}: {r.line()}", file=sys.stderr)
    print(key)
    return 0


GLOBAL_OPTS = {"--project", "--packages", "--prompts", "--engines", "--available", "--reports"}
# sub-options that take one value: the token after them is a VALUE, never a global option
VALUE_OPTS = {"--from", "--status", "--exit", "--minutes", "--notes", "--notes-file", "--reason", "--build", "--base", "--test-cmd", "--max", "--test-timeout-min", "--engine", "--extra", "--extra-file", "--tag", "--timeout", "--now", "--goal", "--state-json",
              "--model", "--timeout-min", "--poll", "--exclude", "--commit", "--url", "--backend-engine", "--effort", "--budget-min"}


def _hoist_globals(argv: list[str]) -> list[str]:
    """Allow global options after the subcommand (spawn emits them that way)."""
    if not argv:
        return argv
    sub_idx = next((i for i, t in enumerate(argv) if not t.startswith("-") and t in
                    {"route", "run", "spawn", "wait", "compose-fix", "status", "probe", "pick", "assign", "record", "scorecard", "paths", "queue",
                     "prereview", "handoff", "done", "inbox"}), None)
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


def _status_suffix(d: dict) -> str:
    """' QUOTA resets=2h9m' / ' CRASH' / ' OK'; empty for markers without a status token."""
    if "status" not in d:
        return " CRASH" if d["exit"] == -1 else ""
    return f" {d['status']}" + (f" resets={d['reset_hint']}" if d.get("reset_hint") else "")


def cmd_wait(a) -> int:
    project = pathlib.Path(a.project).resolve()
    state = runner.wait(project, a.wp_ids, a.tag, a.timeout_min * 60, a.poll, a.reports)
    rc = 0
    for wp, d in state.items():
        if d is None:
            print(f"{wp}: STILL-RUNNING")
            rc = 124
        else:
            print(f"{wp}: exit={d['exit']} {d['minutes']}min{_status_suffix(d)}")
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
        if d is None:
            h = runner.read_handoff(project, r["wp"], r["tag"], a.reports)
            state = f"handed off to {h.get('engine')}: waiting for `jbr done`" if h else "running"
        else:
            state = f"exit={d['exit']} {d['minutes']}min{_status_suffix(d)}"
        print(f"{r['wp']}{('-' + r['tag']) if r['tag'] else ''}: {state} log={r['log_bytes']}B")
    return 0


def _pairs(values: list[str], what: str) -> dict[str, str]:
    out = {}
    for v in values:
        k, sep, val = v.partition("=")
        if not sep or not k.strip() or not val.strip():
            raise SystemExit(f"{what} expects WP=VALUE, got {v!r}")
        out[k.strip()] = val.strip()
    return out


def cmd_assign(a) -> int:
    project = pathlib.Path(a.project).resolve()
    engines = _engines(a)
    assignments = _pairs(a.pairs, "assign")
    reasons = _pairs(a.reason, "--reason")
    known = {w["id"] for w in runner.load_packages(project / a.packages)[0]} if (project / a.packages).exists() else set()
    unknown = sorted(set(assignments) - known) if known else []
    if unknown:
        print(f"[jbr assign] not in {a.packages}: {unknown}", file=sys.stderr)
        return 2
    avail = state.availability(engines, live=not a.no_probe)
    opinion = None
    if a.second_opinion:
        packages, meta = runner.load_packages(project / a.packages)
        packages = [p for p in packages if p["id"] in assignments]
        opinion = router.route(project, packages, engines, {"goal": meta["goal"]} if meta.get("goal") else {},
                               model=a.model, write=False, availability=avail)
    try:
        doc = router.assign(project, assignments, engines, reasons, availability=avail,
                            write=not a.no_write, second_opinion=opinion)
    except ValueError as e:
        print(f"[jbr assign] {e}", file=sys.stderr)
        return 2
    for wp, r in doc["routing"].items():
        so = r.get("second_opinion")
        flag = f"  [2nd opinion: {so['engine']} p={so['confidence']}{'' if so['engine'] == r['engine'] else '  <- disagrees'}]" if so else ""
        print(f"{wp}: {r['engine']} chain: {' > '.join(r['chain'])}{flag}" + (f"\n      why: {r['reason']}" if r["reason"] else ""))
    if not a.no_write:
        print(f"wrote {paths.routing_path(project)}")
    return 0


def cmd_record(a) -> int:
    from jbr import ledger
    project = pathlib.Path(a.project).resolve()
    results = json.loads(pathlib.Path(a.result_json).read_text(encoding="utf-8"))
    if isinstance(results, dict) and "results" in results:
        results = results["results"]
    hints = {}
    if (project / a.packages).exists():
        hints = {w["id"]: w.get("routing_hints") or {} for w in runner.load_packages(project / a.packages)[0]}
    entries = ledger.entries_from_build(results, project=project, build=a.build, reports_dir=a.reports, hints=hints)
    path = ledger.append(entries)
    for e in entries:
        print(f"{e['wp']}: {e['engine']} {e['final']} fix_rounds={e['fix_rounds']} defects={e['confirmed']} "
              f"minutes={e['minutes']}")
    print(f"appended {len(entries)} to {path}")
    return 0


def cmd_scorecard(a) -> int:
    from jbr import ledger
    entries = ledger.load()
    if a.project_only:
        name = pathlib.Path(a.project).resolve().name
        entries = [e for e in entries if e.get("project") == name]
    card = ledger.scorecard(entries)
    print(json.dumps(card, indent=2) if a.json else ledger.format_scorecard(card))
    return 0


def cmd_queue(a) -> int:
    from jbr import queue
    project = pathlib.Path(a.project).resolve()
    try:
        if a.qcmd == "add":
            engines = _engines(a)
            if a.engine not in engines:
                print(f"unknown engine {a.engine!r}; known: {sorted(engines)}", file=sys.stderr)
                return 2
            if _refuse_manual(engines, a.engine, ",".join(a.wp_ids)):
                return 2
            added = queue.add(project, a.wp_ids, packages=a.packages, prompts=a.prompts, engine=a.engine,
                              base=a.base, test_cmd=a.test_cmd)
            skipped = sorted(set(a.wp_ids) - {i["wp"] for i in added})
            print(f"queued {[i['wp'] for i in added]} on {a.engine} from {a.base}"
                  + (f"; already queued: {skipped}" if skipped else ""))
        elif a.qcmd == "work":
            if a.detach:
                glob = ["--engines", str(pathlib.Path(a.engines).resolve())] + [x for ov in a.available for x in ("--available", ov)]
                work = (["--max", str(a.max)] if a.max else []) + ["--test-timeout-min", str(a.test_timeout_min)]
                r = queue.spawn_worker(project, glob, work)
                print(f"worker pid={r['pid']} log={r['log']}")
            else:
                queue.work(project, _engines(a), max_items=a.max, test_timeout_s=int(a.test_timeout_min * 60),
                           log=lambda m: print(m, flush=True))
        elif a.qcmd == "status":
            print(queue.format_status(queue.load(project)))
        elif a.qcmd == "fix":
            it = queue.fix(project, a.wp_id, a.fix_file)
            print(f"{it['wp']}: back in the queue, round {it['round']}, worktree {it['worktree']}")
        elif a.qcmd == "drop":
            print(f"dropped {queue.drop(project, a.wp_id)['wp']}")
        elif a.qcmd == "review-args":
            items = [i for i in queue.load(project) if i["status"] == queue.DONE and (not a.wp_ids or i["wp"] in a.wp_ids)]
            print(json.dumps([queue.review_args(project, i) for i in items], indent=2, ensure_ascii=False))
        elif a.qcmd == "clean":
            it = queue.clean(project, a.wp_id, force=a.force)
            print(f"removed {it['wp']}: worktree {it.get('worktree') or '-'}, branch {it.get('branch') or '-'}")
    except (ValueError, RuntimeError) as e:
        print(f"[jbr queue] {e}", file=sys.stderr)
        return 2
    return 0


def cmd_prereview(a) -> int:
    project = pathlib.Path(a.project).resolve()
    if a.detach:
        args = ["--engines", str(pathlib.Path(a.engines).resolve()), "--packages", a.packages, "--prompts", a.prompts,
                "--url", a.url, "--backend-engine", a.backend_engine, "--effort", a.effort, "--budget-min", str(a.budget_min)]
        if a.reports:
            args += ["--reports", a.reports]
        for ov in a.available:
            args += ["--available", ov]
        args += (["--commit", a.commit] if a.commit else []) + (["--model", a.model] if a.model else [])
        args += (["--no-spec"] if a.no_spec else []) + (["--allow-remote"] if a.allow_remote else [])
        res = prereview.spawn(project, a.wp_id, args, tag=a.tag, reports_dir=a.reports, dry_run=a.dry_run)
        print(f"{a.wp_id}: {'[dry-run] ' + json.dumps(res['argv']) if a.dry_run else 'spawned pid=' + str(res['pid'])} marker={res['marker']}")
        return 0
    res = prereview.prereview(project, a.wp_id, _engines(a), project / a.packages, a.prompts, tag=a.tag,
                              reports_dir=a.reports, url=a.url, model=a.model, backend_engine=a.backend_engine,
                              effort=a.effort, budget_min=a.budget_min, commit=a.commit, spec=not a.no_spec,
                              allow_remote=a.allow_remote, dry_run=a.dry_run)
    print(f"[jbr prereview] {a.wp_id} {res['status']}: {res['reason']} ({res['seconds']}s) -> {res['paths']['json']}")
    if a.dry_run and res.get("argv"):
        print(json.dumps({"scope": res.get("scope"), "argv": res["argv"], "env": res.get("env")}, indent=2))
    return 0


DEFAULT_MANUAL = "agy_manual"


def cmd_handoff(a) -> int:
    project = pathlib.Path(a.project).resolve()
    engines = _engines(a)
    if a.pending:
        if a.wp_id or a.tag or a.extra or a.extra_file or a.engine:
            print("[jbr handoff] --pending takes no package, --tag, --engine or --extra: it collects them itself", file=sys.stderr)
            return 2
        return _handoff_pending(a, project, engines)
    if not a.wp_id:
        print("[jbr handoff] give a package id, or --pending for every outstanding one", file=sys.stderr)
        return 2
    engine_key = a.engine or router.load_routing(project).get(a.wp_id) or DEFAULT_MANUAL
    try:
        res = runner.handoff(project, a.wp_id, engine_key, engines, project / a.packages, a.prompts,
                             extra=_extra(a), tag=a.tag, reports_dir=a.reports)
    except (KeyError, ValueError, OSError) as e:
        print(f"[jbr handoff] {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    if a.print_prompt:
        print(pathlib.Path(res["paths"]["prompt"]).read_text(encoding="utf-8"))
        return 0
    tag = f" --tag {a.tag}" if a.tag else ""
    print(f"[jbr handoff] {a.wp_id}{('-' + a.tag) if a.tag else ''} -> {engine_key} (you run it; jbr executes nothing)")
    print(f"packet: {res['paths']['prompt']}")
    print("Paste into the engine (or use --print to paste the whole packet):\n")
    print(res["pointer"])
    print(f"\nWhen it finishes: python -m jbr --project \"{project}\" done {a.wp_id}{tag} --notes-file <its final report>")
    return 0


def _handoff_pending(a, project: pathlib.Path, engines: dict) -> int:
    try:
        res = runner.handoff_pending(project, engines, project / a.packages, a.prompts, router.load_routing(project),
                                     reports_dir=a.reports, inline=a.print_prompt, done=set(a.done))
    except (KeyError, ValueError, OSError) as e:
        print(f"[jbr handoff] {e.args[0] if e.args else e}", file=sys.stderr)
        return 2
    for it in res["blocked"]:
        print(f"[jbr handoff] {runner._label(it['wp'], it['tag'])} not yet: waits on {', '.join(it['blocked_on'])} "
              f"(already built elsewhere? add --done {' '.join(it['blocked_on'])})", file=sys.stderr)
    if not res["items"]:
        print("[jbr handoff] nothing ready: no package is assigned to a manual engine without a recorded outcome"
              + (" and with its dependencies built" if res["blocked"] else ""))
        return 0
    if a.print_prompt:
        print(res["text"])
        return 0
    print(f"[jbr handoff] {len(res['items'])} pending (you run them; jbr executes nothing):")
    for n, it in enumerate(res["items"], 1):
        print(f"  {n}. {runner._label(it['wp'], it['tag'])} on {it['engine']}" + ("" if it["existing"] else "  (new packet)"))
    print(f"checklist: {res['path']}")
    print("Paste into ONE agy session you start (or use --print to paste everything inline):\n")
    print(res["pointer"])
    print(f"\nWhen it finishes, save its output to a file and run:\n"
          f"  python -m jbr --project \"{project}\" done --from <that file>")
    return 0


def _done_one(a, project: pathlib.Path, wp: str, tag: str, status: str, notes: str) -> dict | None:
    """mark_done + one output line; the result, or None (error printed)."""
    try:
        res = runner.mark_done(project, wp, tag, a.reports, status=status, exit_code=a.exit_code,
                               minutes=a.minutes, notes=notes, force=a.force)
    except ValueError as e:
        print(f"[jbr done] {e}", file=sys.stderr)
        return None
    print(f"{runner._label(wp, tag)}: {res['engine']} {res['status']} exit={res['exit']} {res['minutes']}min marker={res['marker']}")
    return res


def cmd_done(a) -> int:
    project = pathlib.Path(a.project).resolve()
    if a.from_file:
        if a.notes or a.notes_file or a.tag:
            print("[jbr done] --from reads status and notes from the report blocks; drop --notes/--notes-file/--tag", file=sys.stderr)
            return 2
        try:
            reports = runner.parse_reports(pathlib.Path(a.from_file).read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            print(f"[jbr done] {e}", file=sys.stderr)
            return 2
        pk = project / a.packages
        ids = [w["id"] for w in runner.load_packages(pk)[0]] if pk.exists() else []
        wanted = set(a.wp_ids)
        todo = [(label, r) for label, r in reports.items()
                if not wanted or label in wanted or runner.split_stem(label, ids)[0] in wanted]
        if not todo:
            print(f"[jbr done] no === REPORT <id> === block" + (f" for {sorted(wanted)}" if wanted else "") + f" in {a.from_file}",
                  file=sys.stderr)
            return 2
        rc, recorded = 0, []
        for label, r in todo:
            key = runner.resolve_report(project, label, ids, a.reports)
            if key is None:
                print(f"[jbr done] REPORT {label}: its round 0 is already recorded and it does not name one open fix round; "
                      f"not recorded (re-run with the exact id, e.g. `done --from {a.from_file} {label}-fix1`, after "
                      f"fixing the block's id)", file=sys.stderr)
                rc = 2
                continue
            if runner.read_done(project, *key, a.reports) is not None and not a.force:
                print(f"[jbr done] {runner._label(*key)} already recorded; not overwritten", file=sys.stderr)
                rc = 2
                continue
            res = _done_one(a, project, key[0], key[1], a.status if a.status != "OK" else r["status"], r["notes"])
            if res:
                recorded.append(res)
            else:
                rc = 2
        seen = {runner.split_stem(label, ids)[0] for label in reports} | set(reports)
        for wp in sorted(wanted - seen):
            print(f"{wp}: no report block in {a.from_file}; not recorded", file=sys.stderr)
            rc = 2
        _next_hint(recorded)
        return rc
    if not a.wp_ids:
        print("[jbr done] give the package id(s), or --from <engine output file>", file=sys.stderr)
        return 2
    if len(a.wp_ids) > 1 and (a.notes or a.notes_file):
        print("[jbr done] --notes/--notes-file belong to one package; record several with --from", file=sys.stderr)
        return 2
    notes = a.notes or ""
    try:
        if a.notes_file:
            notes = (notes + "\n\n" if notes else "") + pathlib.Path(a.notes_file).read_text(encoding="utf-8")
    except OSError as e:
        print(f"[jbr done] {e}", file=sys.stderr)
        return 2
    rc, recorded = 0, []
    for wp in a.wp_ids:
        res = _done_one(a, project, wp, a.tag, a.status, notes)
        if res:
            recorded.append(res)
        else:
            rc = 2
    _next_hint(recorded)
    return rc


def _next_hint(recorded: list[dict]) -> None:
    """What to run next for the outcomes actually written (OK -> review with its logTag)."""
    ok = [r for r in recorded if r["status"] == "OK"]
    for r in ok:
        print(f"next: review {r['wp']} with workflows/review_level.js"
              + (f" (args.logTag: '{r['tag']}')" if r["tag"] else "") + ", then `jbr compose-fix` for a fix packet")
    dead = [runner._label(r["wp"], r["tag"]) for r in recorded if r["status"] != "OK"]
    if dead:
        print(f"next: {', '.join(dead)}: no engine work to review; hand off again (same round) or pass engineStatus "
              "'crash' for them to review_level.js")


def cmd_inbox(a) -> int:
    rows = runner.inbox()
    live = [r for r in rows if r["open"]]
    if not live:
        print("inbox empty: no project has an agy checklist waiting (a project session writes one with `jbr handoff --pending`)")
        return 0
    for n, r in enumerate(live, 1):
        done = len(r["items"]) - len(r["open"])
        print(f"{n}. {r['project']}: {len(r['open'])} open ({', '.join(r['open'])})" + (f", {done} already recorded" if done else ""))
        print(f"   paste into an agy session you start in that project:\n   {r['pointer']}")
        print(f"   afterwards: python -m jbr --project \"{r['project']}\" done --from <saved agy output>\n")
    stale = len(rows) - len(live)
    if stale:
        print(f"({stale} finished checklist{'s' if stale != 1 else ''} not shown)")
    return 0


def cmd_paths(a) -> int:
    project = pathlib.Path(a.project).resolve()
    from jbr import ledger
    print(json.dumps({"project": str(project), "runs_dir": str(paths.project_dir(project)),
                      "reports_dir": str(paths.reports_dir(project, a.reports)),
                      "routing": str(paths.routing_path(project)),
                      "ledger": str(ledger.ledger_path()), "engine_state": str(state.state_path())}, indent=2))
    return 0


def _tolerant_console() -> None:
    """Engine logs are UTF-8 (accents, emoji); a Windows console in a legacy code page (cp950, cp1252)
    raised UnicodeEncodeError while printing a log tail. Replace unencodable characters instead."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _tolerant_console()
    argv = _hoist_globals(list(sys.argv[1:] if argv is None else argv))
    a = _parser().parse_args(argv)
    return {"route": cmd_route, "run": cmd_run, "spawn": cmd_spawn, "wait": cmd_wait,
            "compose-fix": cmd_compose_fix, "status": cmd_status, "probe": cmd_probe, "pick": cmd_pick,
            "assign": cmd_assign, "record": cmd_record, "scorecard": cmd_scorecard, "paths": cmd_paths, "queue": cmd_queue,
            "prereview": cmd_prereview, "handoff": cmd_handoff, "done": cmd_done, "inbox": cmd_inbox}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
