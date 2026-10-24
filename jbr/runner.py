"""Drive one work package through an external engine (agy or codex), headless.

Artifacts under paths.reports_dir(project) (~/.jbr/runs/<project key>/reports/ unless --reports says otherwise):
  run-<WP>[-tag].prompt.md   the self-contained prompt (engine reads it via a file pointer)
  run-<WP>[-tag].log         engine transcript
  run-<WP>[-tag].done        "<exit code> <minutes>min <STATUS> [reset-hint]" once finished;
                             STATUS = OK | QUOTA | CRASH (classify_run). A jbr crash writes
                             "-1 <minutes>min" (no status token; engine_status() reads it as crash)
  run-<WP>[-tag].stdout      stdout of a detached (spawned) run
  run-<WP>[-tag].last.md     codex only: final message (-o)
  run-<WP>[-tag].handoff     manual engines only: JSON {engine, started, pointer} written by handoff()

Engine `claude_subagent` is never executed here: build_command returns None and
run() reports that the package belongs to the Claude Code workflow.

Manual engines (runner "manual", e.g. agy_manual) are never executed either: Google's Antigravity
terms (FAQ, 2026-10) do not support wrapping `agy` inside third-party coding agents on consumer plans,
so jbr only writes the handoff packet (handoff()) and the user runs agy themselves, then records the
outcome (mark_done()). The .done marker has the usual format, so `jbr wait`/`status` and
review_level.js treat the package like any other run.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time
import traceback
from typing import Any

from jbr import models, paths

RUN_PREFIX = "run"
MANUAL = "manual"  # runner of engines the user drives by hand (agy_manual); jbr never executes them
MANUAL_REFUSAL = ("manual engine: jbr never runs it. Write the packet with `python -m jbr handoff <WP> --engine <engine>`, "
                  "run the engine yourself, then record the outcome with `python -m jbr done <WP>`")
IS_WINDOWS = os.name == "nt"  # module constant so tests can flip it
DEFAULT_TIMEOUT = "55m"
DEFAULT_RULES = (
    "Rules: do NOT modify files owned by other work packages except to append exports. Do not commit to git. "
    "Do not create files outside the repo. When done, run the full test suite and make sure it is green; "
    "report the final test summary line, the list of files you created/changed, and any acceptance item you "
    "could not satisfy."
)


# ----------------------------------------------------------------------------- packages


def load_packages(path: str | os.PathLike) -> tuple[list[dict], dict]:
    """Return (packages, top-level metadata). Accepts {"work_packages": [...]} or a bare list."""
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, list):
        return doc, {}
    pkgs = doc.get("work_packages") or doc.get("packages") or []
    meta = {k: v for k, v in doc.items() if k not in {"work_packages", "packages"}}
    return pkgs, meta


def find_package(packages: list[dict], wp_id: str) -> dict:
    wp = next((w for w in packages if w["id"] == wp_id), None)
    if wp is None:
        raise KeyError(f"unknown work package {wp_id}; known: {[w['id'] for w in packages]}")
    return wp


def report_paths(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str | None = None) -> dict[str, pathlib.Path]:
    rep = paths.reports_dir(project_root, reports_dir)
    stem = f"{RUN_PREFIX}-{wp_id}" + (f"-{tag}" if tag else "")
    return {
        "dir": rep,
        "prompt": rep / f"{stem}.prompt.md",
        "log": rep / f"{stem}.log",
        "done": rep / f"{stem}.done",
        "stdout": rep / f"{stem}.stdout",
        "last": rep / f"{stem}.last.md",
        "handoff": rep / f"{stem}.handoff",
    }


# ----------------------------------------------------------------------------- prompt


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def rules_line(rules: str | None) -> str:
    """Project rules are a PREFIX to DEFAULT_RULES, never a replacement: the ownership /
    no-commit / no-files-outside-repo rules are always emitted."""
    rules = (rules or "").strip()
    return f"{rules} {DEFAULT_RULES}" if rules else DEFAULT_RULES


def compose_prompt(project_root: str | os.PathLike, wp: dict, prompts_dir: str | os.PathLike = "ops/prompts",
                   extra: str = "", rules: str | None = None) -> str:
    root = pathlib.Path(project_root)
    lines = [
        f"You are the software engineer on work package {wp['id']}: {wp.get('title', '')}.",
        f"Repository root (already exists): {root}",
        rules_line(rules or wp.get("rules")),
        "",
        "GOAL",
        wp.get("goal", ""),
        "",
        "FILES TO CREATE / OWN",
        *[f"- {f}" for f in _as_list(wp.get("files"))],
        "",
        "ACCEPTANCE CRITERIA (a fresh reviewer will check each one)",
        *[f"- {x}" for x in _as_list(wp.get("acceptance"))],
        "",
        "TESTS REQUIRED",
        *[f"- {t}" for t in _as_list(wp.get("tests"))],
        "",
        "SPEC REFERENCES",
        *[f"- {s}" for s in _as_list(wp.get("spec_refs"))],
        "",
        "DETAILED INSTRUCTIONS",
        wp.get("implementer_prompt", ""),
        "",
    ]
    spec_file = root / prompts_dir / f"{wp['id']}.md"
    if spec_file.exists():
        lines += [
            f"FULL PACKAGE SPEC (verbatim copy of {spec_file}; this is part of your instructions)",
            spec_file.read_text(encoding="utf-8"),
            "",
        ]
    if extra:
        lines += ["ADDITIONAL INSTRUCTIONS FROM THE MANAGER", extra, ""]
    return "\n".join(lines)


def pointer_text(prompt_file: pathlib.Path, project_root: pathlib.Path) -> str:
    # Windows command lines cap at 32K chars; the engine gets a pointer to the prompt file instead.
    return (
        f"Your complete instructions are in the file {prompt_file}. Read that file in full with your "
        f"file-read tool before doing anything else, then carry out every instruction in it. "
        f"Work only inside {project_root}."
    )


# ----------------------------------------------------------------------------- command


def build_command(engine: dict, prompt_file: pathlib.Path, project_root: pathlib.Path,
                  last_file: pathlib.Path, timeout: str = DEFAULT_TIMEOUT) -> list[str] | None:
    """argv for the engine's CLI, or None when the engine is a Claude Code subagent. Pure: an
    `auto:<family>` model must be resolved first (run() does; models.resolve)."""
    runner = engine.get("runner", "agy")
    model = engine.get("model", "")
    effort = engine.get("effort", "high")
    pointer = pointer_text(prompt_file, project_root)
    if runner == "claude_subagent":
        return None
    if runner == MANUAL:
        raise ValueError(MANUAL_REFUSAL)
    if runner == "codex":
        # OpenAI Codex CLI. workspace-write sandbox keeps it inside the repo.
        # `windows_sandbox`: on Windows the workspace-write shell can fail with "CryptUnprotectData
        # failed" (DPAPI); engines.json may name the sandbox to use there instead.
        sandbox = engine.get("windows_sandbox", "workspace-write") if IS_WINDOWS else "workspace-write"
        cmd = ["codex", "exec", "-s", sandbox, "--skip-git-repo-check", "-m", model]
        if effort:  # empty effort: provider without reasoning effort (e.g. a LAN OpenAI-compatible server)
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd += ["-C", str(project_root), "-o", str(last_file), pointer]
        if engine.get("local_provider"):
            # local model (ollama | lmstudio) through codex's own OSS provider: same sandbox, no quota
            cmd[2:2] = ["--oss", "--local-provider", engine["local_provider"]]
        # a LAN OpenAI-compatible server is configured in its own slim CODEX_HOME instead (engines.json `env`)
        return cmd
    if runner == "agy":
        cmd = ["agy", "-p", pointer, "--model", model, "--dangerously-skip-permissions", "--print-timeout", timeout]
        if not model.removeprefix(models.AUTO).startswith("claude"):
            # agy rejects --effort for Claude thinking models (their thinking is the boost)
            cmd += ["--effort", effort]
        return cmd
    raise ValueError(f"unknown runner {runner!r} (expected agy|codex|claude_subagent|manual)")


# ----------------------------------------------------------------------------- classification

# Quota / rate-limit signatures. agy: `RESOURCE_EXHAUSTED (code 429): Individual quota reached ... Resets in 2h9m31s.`
# codex: usage-limit refusal ("You've hit your usage limit ... try again at 3:05 PM").
QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|\"error_code\"\s*:\s*429|\bcode 429\b|quota (?:reached|exceeded|exhausted)"
                      r"|usage limit|rate[- ]limit(?:ed)? exceeded", re.IGNORECASE)
RESET_RE = re.compile(r"resets? in ([0-9][0-9hms ]*[hms])|try again (?:at|in) ([^.\n\"]+)", re.IGNORECASE)
FAST_EXIT_MIN = 1.0   # an engine that finishes this fast...
NO_WORK_CHARS = 1000  # ...with less transcript than this did no work (quota stall / instant refusal)


def classify_run(exit_code: int, minutes: float, log_text: str) -> tuple[str, str]:
    """(status, reset_hint) for a finished engine run. status: OK | QUOTA | CRASH.

    QUOTA: a quota/429 signature in the transcript, a fast exit (< FAST_EXIT_MIN) with no work, or
           exit 124 (agy --print-timeout) with an empty transcript.
    CRASH: jbr itself crashed (exit -1), or a non-zero exit with an empty transcript.
    OK: the engine ran and produced output (even with a non-zero exit: the review judges it).
    """
    body = "\n".join(ln for ln in log_text.splitlines() if not ln.startswith("# ")).strip()
    if exit_code == -1:
        return "CRASH", ""
    stalled = exit_code == 124 and not body  # agy --print-timeout after a silent quota stall (observed ~90 min)
    if QUOTA_RE.search(log_text) or stalled or (minutes < FAST_EXIT_MIN and len(body) < NO_WORK_CHARS):
        m = RESET_RE.search(log_text)
        hint = (m.group(1) or m.group(2)).strip().replace(" ", "") if m else ""
        return "QUOTA", hint
    if exit_code != 0 and not body:
        return "CRASH", ""
    return "OK", ""


def engine_status(done: dict | None) -> str | None:
    """'ok' | 'quota' | 'crash' from a read_done() dict; None while the run is still going.
    Markers written before status tokens existed: exit -1 -> crash, anything else -> ok."""
    if done is None:
        return None
    if "status" in done:
        return done["status"].lower()
    return "crash" if done["exit"] == -1 else "ok"


def _env(engine: dict | None = None) -> dict[str, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.update(engine_env(engine))
    return env


def is_manual(engine: dict | None) -> bool:
    return (engine or {}).get("runner") == MANUAL


def engine_env(engine: dict | None) -> dict[str, str]:
    """engines.json `env`: per-engine variables (`~` expanded), e.g. CODEX_HOME for lan_qwen's slim codex home."""
    return {k: os.path.expanduser(str(v)) for k, v in ((engine or {}).get("env") or {}).items()}


# ----------------------------------------------------------------------------- run / spawn / wait


def run(project_root: str | os.PathLike, wp_id: str, engine_key: str, engines: dict[str, dict],
        packages_path: str | os.PathLike, prompts_dir: str | os.PathLike = "ops/prompts", *,
        extra: str = "", tag: str = "", timeout: str = DEFAULT_TIMEOUT, dry_run: bool = False,
        reports_dir: str | None = None, rules: str | None = None) -> dict[str, Any]:
    """Compose the prompt, run the engine synchronously, write the .done marker.

    Returns {"wp", "engine", "cmd", "exit", "minutes", "paths"}; exit is None on dry-run
    and "workflow" when the engine is claude_subagent.

    Any exception (unknown engine/package, bad packages file, engine launch failure) is
    re-raised AFTER writing the .done marker as "-1 <minutes>min" and the traceback into the
    .log, so a poller waiting on the marker never hangs (dry-run: no marker is written).
    """
    if is_manual(engines.get(engine_key)):
        raise ValueError(f"{engine_key}: {MANUAL_REFUSAL}")  # before any marker: a manual package's .done is the user's
    root = pathlib.Path(project_root).resolve()
    paths = report_paths(root, wp_id, tag, reports_dir)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    if paths["done"].exists():
        paths["done"].unlink()
    paths["handoff"].unlink(missing_ok=True)  # an automatic run supersedes an earlier manual handoff
    result: dict[str, Any] = {"wp": wp_id, "engine": engine_key, "cmd": None, "exit": None, "minutes": 0.0,
                              "paths": {k: str(v) for k, v in paths.items()}}
    t0 = time.time()
    try:
        if engine_key not in engines:
            raise KeyError(f"unknown engine {engine_key!r}; known: {sorted(engines)}")
        from jbr import state  # lazy: state imports runner
        # "auto:<family>" -> the best id agy lists now; a dry-run only reads the cache (stays offline)
        engine = models.resolve(engines[engine_key], offline=dry_run, agy_bin=state._bin("agy"))
        result["model"] = engine.get("model")
        packages, meta = load_packages(packages_path)
        wp = find_package(packages, wp_id)
        prompt = compose_prompt(root, wp, prompts_dir, extra, rules or meta.get("rules"))
        paths["prompt"].write_text(prompt, encoding="utf-8")

        cmd = build_command(engine, paths["prompt"], root, paths["last"], timeout)
        result["cmd"] = cmd
        if cmd is None:
            result["exit"] = "workflow"
            result["note"] = "engine is a Claude Code subagent: hand this package to workflows/build_dag.js"
            return result
        if dry_run:
            result["note"] = "dry-run: command not executed"
            return result

        with open(paths["log"], "w", encoding="utf-8") as fh:
            spec = f" ({engine['model_spec']})" if engine.get("model_spec") else ""
            fh.write(f"# {engine_key} run {wp_id} model={engine.get('model')}{spec} effort={engine.get('effort')}\n\n")
            fh.flush()
            # stdin closed: `codex exec` reads stdin when it is not a TTY and would wait forever on an
            # inherited pipe (2026-10-10, a foreground `jbr queue work`)
            p = subprocess.run(cmd, cwd=root, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, env=_env(engine))
        dt = (time.time() - t0) / 60
        status, hint = classify_run(p.returncode, dt, paths["log"].read_text(encoding="utf-8", errors="replace"))
        paths["done"].write_text(f"{p.returncode} {dt:.1f}min {status}{' ' + hint if hint else ''}\n", encoding="utf-8")
        result.update(exit=p.returncode, minutes=round(dt, 1), status=status.lower(), reset_hint=hint)
        _record_engine_state(engine_key, engine, status, hint, paths["log"], result)
        return result
    except Exception:
        if dry_run:
            raise
        dt = (time.time() - t0) / 60
        tb = traceback.format_exc()
        with open(paths["log"], "a", encoding="utf-8") as fh:
            fh.write(f"\n# jbr run {wp_id} engine={engine_key} CRASHED after {dt:.1f}min\n{tb}")
        paths["done"].write_text(f"-1 {dt:.1f}min\n", encoding="utf-8")
        raise


def _record_engine_state(engine_key: str, engine: dict, status: str, hint: str, log: pathlib.Path,
                         result: dict[str, Any]) -> None:
    """Feed the run's outcome into the shared engine state (jbr/state.py) so `jbr probe` / `pick`
    and build_dag.js skip a quota-exhausted engine until its reset time. QUOTA -> exhausted_until
    from the reset hint (unparseable -> +30 min); OK -> AVAILABLE. Never fails the run."""
    if status not in {"QUOTA", "OK"}:
        return
    try:
        from jbr import state
        if status == "QUOTA":
            result["exhausted_until"] = state.iso(state.mark_exhausted(engine_key, engine, hint))
        else:
            state.record(engine_key, engine, state.AVAILABLE)
    except Exception as e:  # state file trouble must not turn a finished run into a crash
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"\n# jbr: could not update engine state: {e!r}\n")


def spawn(project_root: str | os.PathLike, wp_id: str, run_args: list[str], tag: str = "",
          reports_dir: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Launch `python -m jbr run <wp_id> <run_args...>` detached so it survives the caller's timeout."""
    root = pathlib.Path(project_root).resolve()
    paths = report_paths(root, wp_id, tag, reports_dir)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    if paths["done"].exists():
        paths["done"].unlink()
    paths["handoff"].unlink(missing_ok=True)  # `jbr done` must never fake this run's marker
    argv = [sys.executable, "-m", "jbr", "run", wp_id, "--project", str(root), *run_args]
    if tag and "--tag" not in run_args:
        argv += ["--tag", tag]
    if dry_run:
        return {"pid": None, "argv": argv, "marker": str(paths["done"])}
    return {"pid": launch_detached(argv, root, paths["stdout"]), "argv": argv, "marker": str(paths["done"])}


def launch_detached(argv: list[str], cwd: pathlib.Path, stdout_path: pathlib.Path) -> int:
    """Start a `python -m jbr ...` child that survives the caller (and its timeout); returns the pid."""
    env = _env()
    # make the jbr package importable from the child no matter where the project lives
    jbr_root = str(pathlib.Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = jbr_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    out = open(stdout_path, "w", encoding="utf-8")
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, **kw)
    return p.pid


# ----------------------------------------------------------------------------- manual engines


def handoff(project_root: str | os.PathLike, wp_id: str, engine_key: str, engines: dict[str, dict],
            packages_path: str | os.PathLike, prompts_dir: str | os.PathLike = "ops/prompts", *,
            extra: str = "", tag: str = "", reports_dir: str | None = None, rules: str | None = None,
            now: float | None = None) -> dict[str, Any]:
    """Write the packet for a package the user runs by hand on a manual engine. Executes nothing.

    Writes run-<WP>[-tag].prompt.md (same prompt `jbr run` would give the engine), a log header, and
    the .handoff record; clears a stale .done so `jbr wait` / build_dag.js see the package as pending
    until mark_done(). Returns {"wp", "engine", "pointer", "paths"}; pointer = the line to paste."""
    if engine_key not in engines:
        raise KeyError(f"unknown engine {engine_key!r}; known: {sorted(engines)}")
    engine = engines[engine_key]
    if not is_manual(engine):
        raise ValueError(f"engine {engine_key} is not manual (runner {engine.get('runner')!r}); use `jbr run`/`spawn`")
    root = pathlib.Path(project_root).resolve()
    rp = report_paths(root, wp_id, tag, reports_dir)
    packages, meta = load_packages(packages_path)
    wp = find_package(packages, wp_id)  # before touching any file: an unknown package leaves nothing behind
    rp["dir"].mkdir(parents=True, exist_ok=True)
    if rp["done"].exists():
        rp["done"].unlink()
    rp["prompt"].write_text(compose_prompt(root, wp, prompts_dir, extra, rules or meta.get("rules")), encoding="utf-8")
    pointer = pointer_text(rp["prompt"], root)
    started = time.time() if now is None else now
    rp["log"].write_text(f"# {engine_key} handoff {wp_id}{' tag=' + tag if tag else ''} model={engine.get('model', '')} "
                         f"(run by the user, not by jbr)\n\n", encoding="utf-8")
    rp["handoff"].write_text(json.dumps({"wp": wp_id, "tag": tag, "engine": engine_key, "started": started,
                                         "pointer": pointer}, indent=2), encoding="utf-8")
    return {"wp": wp_id, "engine": engine_key, "pointer": pointer, "paths": {k: str(v) for k, v in rp.items()}}


def read_handoff(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str | None = None) -> dict | None:
    f = report_paths(project_root, wp_id, tag, reports_dir)["handoff"]
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def mark_done(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str | None = None, *,
              status: str = "OK", exit_code: int | None = None, minutes: float | None = None, notes: str = "",
              force: bool = False, now: float | None = None) -> dict[str, Any]:
    """Record the outcome of a handed-off package: append `notes` (the engine's final report, pasted by
    the user) to the log, write the .done marker. minutes default: time since the handoff.
    status OK | CRASH | QUOTA (CRASH/QUOTA = the engine did no usable work: not reviewed).
    An existing .done marker is kept unless force=True (a second `done` would rewrite a recorded outcome)."""
    status = status.upper()
    if status not in {"OK", "CRASH", "QUOTA"}:
        raise ValueError(f"status must be OK, CRASH or QUOTA, got {status!r}")
    if minutes is not None and minutes < 0:
        raise ValueError(f"minutes must be >= 0, got {minutes}")
    h = read_handoff(project_root, wp_id, tag, reports_dir)
    if h is None:
        stem = f"{wp_id}{'-' + tag if tag else ''}"
        raise ValueError(f"no handoff for {stem}: run `python -m jbr handoff {wp_id}{' --tag ' + tag if tag else ''}` first")
    rp = report_paths(project_root, wp_id, tag, reports_dir)
    if rp["done"].exists() and not force:
        raise ValueError(f"{rp['done'].name} already exists ({rp['done'].read_text(encoding='utf-8').strip()}); "
                         "pass --force to overwrite it")
    if minutes is None:
        minutes = max(0.0, ((time.time() if now is None else now) - float(h.get("started") or 0)) / 60)
    if exit_code is None:
        exit_code = 0 if status == "OK" else 1
    with open(rp["log"], "a", encoding="utf-8") as fh:
        fh.write(f"# recorded by the user: {status} exit={exit_code} {minutes:.1f}min\n")
        if notes.strip():
            fh.write("\n" + notes.strip() + "\n")
    rp["done"].write_text(f"{exit_code} {minutes:.1f}min {status}\n", encoding="utf-8")
    return {"wp": wp_id, "tag": tag, "engine": h.get("engine"), "status": status, "exit": exit_code,
            "minutes": round(minutes, 1), "marker": str(rp["done"]), "log": str(rp["log"])}


PENDING_FILE = "handoff-pending.md"
REPORT_RE = re.compile(r"^=+ *REPORT +([^\s=]+) *=+[ \t]*$(.*?)^=+ *END +\1 *=+[ \t]*$", re.MULTILINE | re.DOTALL)
REPORT_STATUS_RE = re.compile(r"^\s*status\s*:\s*[*_`\"']*([A-Za-z]+)", re.IGNORECASE | re.MULTILINE)  # **done** too


def _topo(packages: list[dict]) -> dict[str, int]:
    """Package id -> build order (deps first, plan order on ties)."""
    order: dict[str, int] = {}
    deps = {w["id"]: list(w.get("depends_on") or []) for w in packages}

    def visit(pid: str, stack: tuple[str, ...] = ()) -> None:
        if pid in order or pid in stack or pid not in deps:
            return
        for d in deps[pid]:
            visit(d, stack + (pid,))
        order[pid] = len(order)

    for w in packages:
        visit(w["id"])
    return order


def has_ok_run(project_root: str | os.PathLike, wp_id: str, known_ids: list[str], reports_dir: str | None = None) -> bool:
    """True when any run of the package (any tag) ended OK: its code exists on disk."""
    rep = paths.reports_dir(project_root, reports_dir)
    for f in rep.glob(f"{RUN_PREFIX}-{wp_id}*.done") if rep.exists() else []:
        wp, tag = split_stem(f.name[len(RUN_PREFIX) + 1:-len(".done")], known_ids)
        d = read_done(project_root, wp, tag, reports_dir) if wp == wp_id else None
        if d and engine_status(d) == "ok":
            return True
    return False


def pending_manual(project_root: str | os.PathLike, packages: list[dict], routing: dict[str, str],
                   engines: dict[str, dict], reports_dir: str | None = None, done: set[str] | None = None) -> list[dict]:
    """Manual work the user still has to run, in dependency order: every .handoff without a .done (any tag,
    e.g. fix rounds), plus every package routed to a manual engine that was never handed off nor finished.
    [{"wp", "tag", "engine", "existing": bool, "blocked_on": [dep, ...]}]. An item is blocked when a dependency
    is neither earlier on the list (unblocked), nor has an OK run, nor is in `done` (built elsewhere, e.g. by a
    Claude subagent, which leaves no run marker); blocking is transitive. Fix rounds (tagged) are never blocked."""
    rep = paths.reports_dir(project_root, reports_dir)
    ids = [w["id"] for w in packages]
    out: dict[tuple[str, str], dict] = {}
    for f in sorted(rep.glob(f"{RUN_PREFIX}-*.handoff")) if rep.exists() else []:
        wp, tag = split_stem(f.name[len(RUN_PREFIX) + 1:-len(".handoff")], ids)
        h = read_handoff(project_root, wp, tag, reports_dir)
        if h and wp in ids and read_done(project_root, wp, tag, reports_dir) is None:
            out[(wp, tag)] = {"wp": wp, "tag": tag, "engine": h.get("engine"), "existing": True}
    for wp in ids:
        eng = routing.get(wp)
        if (eng in engines and is_manual(engines[eng]) and not any(k[0] == wp for k in out)  # an open fix round counts
                and read_handoff(project_root, wp, "", reports_dir) is None
                and read_done(project_root, wp, "", reports_dir) is None):
            out[(wp, "")] = {"wp": wp, "tag": "", "engine": eng, "existing": False}
    order = _topo(packages)
    items = sorted(out.values(), key=lambda i: (order.get(i["wp"], 1 << 30), i["tag"]))
    deps = {w["id"]: list(w.get("depends_on") or []) for w in packages}
    done = set(done or ())
    ready: set[str] = set()
    for it in items:
        # a fix round works on code that exists already: its dependencies were met in round 0
        it["blocked_on"] = [] if it["tag"] else [
            d for d in deps.get(it["wp"], []) if d in deps and d not in ready and d not in done
            and not has_ok_run(project_root, d, ids, reports_dir)]
        if not it["blocked_on"]:
            ready.add(it["wp"])
    return items


def _label(wp: str, tag: str) -> str:
    return f"{wp}-{tag}" if tag else wp


def handoff_pending(project_root: str | os.PathLike, engines: dict[str, dict], packages_path: str | os.PathLike,
                    prompts_dir: str | os.PathLike = "ops/prompts", routing: dict[str, str] | None = None, *,
                    reports_dir: str | None = None, inline: bool = False, done: set[str] | None = None,
                    now: float | None = None) -> dict[str, Any]:
    """One checklist for every pending manual package, so a single engine session the user starts works
    through them in order. New packages get their packet (handoff()); packets already handed off are reused
    as they are (their start time and any fix instructions kept). Writes <reports>/handoff-pending.md.
    inline=True puts each packet's text in the checklist (for engines that cannot read ~/.jbr). Blocked items
    (an unbuilt dependency, see pending_manual) are listed as "do not start" and get no packet yet.
    Returns {"items", "blocked", "path", "pointer", "text"}; nothing ready -> no file written."""
    root = pathlib.Path(project_root).resolve()
    packages, meta = load_packages(packages_path)
    found = pending_manual(root, packages, routing or {}, engines, reports_dir, done)
    items = [i for i in found if not i["blocked_on"]]
    blocked = [i for i in found if i["blocked_on"]]
    rep = paths.reports_dir(root, reports_dir)
    batch = rep / PENDING_FILE
    if not items:
        batch.unlink(missing_ok=True)
        return {"items": [], "blocked": blocked, "path": None, "pointer": "", "text": ""}
    for it in items:
        if not it["existing"]:
            handoff(root, it["wp"], it["engine"], engines, packages_path, prompts_dir, reports_dir=reports_dir,
                    rules=meta.get("rules"), now=now)
        it["prompt"] = str(report_paths(root, it["wp"], it["tag"], reports_dir)["prompt"])
    titles = {w["id"]: w.get("title", "") for w in packages}
    deps = {w["id"]: list(w.get("depends_on") or []) for w in packages}
    lines = [
        f"You have {len(items)} work package{'s' if len(items) != 1 else ''} to do, ONE AT A TIME, in the order below.",
        f"Repository root: {root}. Work only inside it.",
        "",
        "For each package, in order:",
        "1. Read its packet in full" + (" (below)" if inline else " (the file named next to it)")
        + " and carry out every instruction in it.",
        "2. Finish it completely, with its tests green, before you start the next one.",
        "3. Then print exactly this block, with <id> = the package's REPORT ID shown below (fill in the values),",
        "   and only then go on:",
        "   === REPORT <id> ===",
        "   status: done        (or: failed, when you could not do it)",
        "   tests: <the final test summary line>",
        "   files: <files you created or changed>",
        "   unsatisfied: <acceptance items you could not meet, or none>",
        "   === END <id> ===",
        "If a package fails, report it and skip every later package that depends on it.",
        "",
        "PACKAGES",
    ]
    for n, it in enumerate(items, 1):
        lab = _label(it["wp"], it["tag"])
        dep = [d for d in deps.get(it["wp"], [])]
        lines.append(f"{n}. {lab}: {titles.get(it['wp'], '')}" + (f" (depends on {', '.join(dep)})" if dep else "")
                     + f"\n   REPORT ID: {lab}" + ("" if inline else f"\n   packet: {it['prompt']}"))
    if blocked:
        lines += ["", "NOT YET (do not start these: a package they depend on is not built)"]
        lines += [f"- {_label(i['wp'], i['tag'])}: waits on {', '.join(i['blocked_on'])}" for i in blocked]
    if inline:
        for it in items:
            lab = _label(it["wp"], it["tag"])
            lines += ["", f"######## PACKET {lab} ########", pathlib.Path(it["prompt"]).read_text(encoding="utf-8"),
                      f"######## END PACKET {lab} ########"]
    text = "\n".join(lines) + "\n"
    batch.write_text(text, encoding="utf-8")
    return {"items": items, "blocked": blocked, "path": str(batch), "pointer": pointer_text(batch, root), "text": text}


_ROOT_LINE_RE = re.compile(r"^Repository root: (.+?)\. Work only inside it\.$", re.MULTILINE)
_ITEM_LINE_RE = re.compile(r"^\d+\. (\S+?): ", re.MULTILINE)


def inbox(runs_root: pathlib.Path | None = None) -> list[dict]:
    """Every project's agy checklist (handoff-pending.md) across the runs dir, for the user's one look at what
    is waiting for them. Per checklist: {"project", "checklist", "items", "open", "pointer"}; `open` = the
    REPORT ids with no .done marker yet (a checklist whose items are all recorded is stale: open == []).
    Reads files only."""
    root = runs_root or paths.runs_root()
    out = []
    for f in sorted(root.glob(f"*/reports/{PENDING_FILE}")) if root.exists() else []:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        m = _ROOT_LINE_RE.search(text)
        if not m:
            continue
        project = pathlib.Path(m.group(1))
        labels = _ITEM_LINE_RE.findall(text.split("NOT YET")[0])
        still = [lab for lab in labels if not (f.parent / f"{RUN_PREFIX}-{lab}.done").exists()]
        out.append({"project": str(project), "checklist": str(f), "items": labels, "open": still,
                    "pointer": pointer_text(f, project)})
    return out


def resolve_report(project_root: str | os.PathLike, label: str, known_ids: list[str],
                   reports_dir: str | None = None) -> tuple[str, str] | None:
    """The (wp, tag) a REPORT label belongs to, or None when it is ambiguous / nothing is open. An exact
    label (WP03-fix1) wins. A bare id whose round 0 is already recorded maps to its one open tagged handoff
    (engines often write REPORT WP03 for a fix round, since the packet says "work package WP03")."""
    wp, tag = split_stem(label, known_ids)
    if tag or read_done(project_root, wp, "", reports_dir) is None:
        return wp, tag
    rep = paths.reports_dir(project_root, reports_dir)
    open_tags = []
    for f in rep.glob(f"{RUN_PREFIX}-{wp}-*.handoff") if rep.exists() else []:
        w, t = split_stem(f.name[len(RUN_PREFIX) + 1:-len(".handoff")], known_ids)
        if w == wp and t and read_done(project_root, w, t, reports_dir) is None:
            open_tags.append(t)
    return (wp, open_tags[0]) if len(open_tags) == 1 else None


def parse_reports(text: str) -> dict[str, dict[str, str]]:
    """=== REPORT <id> === ... === END <id> === blocks -> {id: {"status": OK|CRASH, "notes": block}}.
    status `done` (or ok) -> OK; anything else (failed, blocked, missing) -> CRASH (not reviewed)."""
    out = {}
    for m in REPORT_RE.finditer(text or ""):
        body = m.group(2).strip()
        st = REPORT_STATUS_RE.search(body)
        word = st.group(1).lower() if st else ""
        out[m.group(1)] = {"status": "OK" if word in {"done", "ok"} else "CRASH", "notes": body}
    return out


def read_done(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str | None = None) -> dict | None:
    """Parse a .done marker: {"exit": int, "minutes": float} or None if still running.
    Markers carrying a status token add {"status": "OK|QUOTA|CRASH", "reset_hint": str}."""
    marker = report_paths(project_root, wp_id, tag, reports_dir)["done"]
    if not marker.exists():
        return None
    txt = marker.read_text(encoding="utf-8").strip()
    parts = txt.split()
    try:
        d: dict[str, Any] = {"exit": int(parts[0]), "minutes": float(parts[1].rstrip("min")) if len(parts) > 1 else 0.0}
    except (ValueError, IndexError):
        return {"exit": -1, "minutes": 0.0, "raw": txt}
    if len(parts) > 2:
        d["status"] = parts[2].upper()
        d["reset_hint"] = " ".join(parts[3:])
    return d


def wait(project_root: str | os.PathLike, wp_ids: list[str], tag: str = "", timeout_s: float = 70 * 60,
         poll_s: float = 30, reports_dir: str | None = None, sleep=time.sleep) -> dict[str, dict | None]:
    """Block until every marker exists or timeout; returns {wp: done-dict|None}."""
    t0 = time.time()
    while True:
        state = {wp: read_done(project_root, wp, tag, reports_dir) for wp in wp_ids}
        if all(v is not None for v in state.values()) or time.time() - t0 >= timeout_s:
            return state
        sleep(poll_s)


def split_stem(stem: str, known_ids: list[str] | None = None) -> tuple[str, str]:
    """`<wp>[-<tag>]` -> (wp, tag). Hyphenated package ids are resolved against known_ids
    (longest match wins); without known_ids the id is everything before the first `-`."""
    for wp in sorted(known_ids or [], key=len, reverse=True):
        if stem == wp:
            return wp, ""
        if stem.startswith(wp + "-"):
            return wp, stem[len(wp) + 1:]
    wp, _, tag = stem.partition("-")
    return wp, tag


def status(project_root: str | os.PathLike, reports_dir: str | None = None,
           known_ids: list[str] | None = None) -> list[dict]:
    """One row per run-* prompt file: wp, tag, done marker, log size."""
    rep = paths.reports_dir(project_root, reports_dir)
    rows = []
    for prompt in sorted(rep.glob(f"{RUN_PREFIX}-*.prompt.md")) if rep.exists() else []:
        stem = prompt.name[: -len(".prompt.md")][len(RUN_PREFIX) + 1:]
        wp, tag = split_stem(stem, known_ids)
        done = read_done(project_root, wp, tag, reports_dir)
        log = rep / f"{RUN_PREFIX}-{stem}.log"
        rows.append({"wp": wp, "tag": tag, "done": done, "log_bytes": log.stat().st_size if log.exists() else 0})
    return rows


# ----------------------------------------------------------------------------- compose-fix


def compose_fix(project_root: str | os.PathLike, review_output: str | os.PathLike, round_no: int,
                reports_dir: str | None = None) -> list[dict]:
    """Turn review-workflow output into per-package fix instructions.

    Writes <reports>/review-<WP>-round<N>.json for every package and fix-<WP>-round<N>.md
    for packages with verdict 'fix'. Returns [{"wp", "verdict", "confirmed", "low", "fix_file"}].
    """
    rep = paths.reports_dir(project_root, reports_dir)
    rep.mkdir(parents=True, exist_ok=True)
    out = json.loads(pathlib.Path(review_output).read_text(encoding="utf-8"))
    results = out["result"] if isinstance(out, dict) and "result" in out else out
    if isinstance(results, dict):
        results = [results]
    summary = []
    for r in results:
        wp = r["wp"]
        (rep / f"review-{wp}-round{round_no}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
        confirmed = r.get("confirmed_failures", [])
        low = r.get("low_findings", [])
        row = {"wp": wp, "verdict": r["verdict"], "confirmed": len(confirmed), "low": len(low), "fix_file": None}
        if r["verdict"] == "fix":
            lines = [
                f"FIX ROUND {round_no}. The code for this package already exists on disk (you wrote it in a previous run; "
                "read it, do not start over). A fresh reviewer confirmed the defects below. Fix every item, keep "
                "everything else, re-run the full suite and report per item what you changed (file:line).",
                "",
            ]
            for i, f in enumerate(confirmed + low, 1):
                lines += [
                    f"{i}. [{f.get('severity', '?')}] {f.get('criterion', '')}",
                    f"   Evidence: {f.get('evidence', '')}",
                    f"   Required fix: {f.get('fix', '')}",
                    "",
                ]
            if r.get("ownership_violations"):
                lines += ["Ownership violations to undo (restore those files to their committed state unless the change is a pure export append):"]
                lines += [f"- {v}" for v in r["ownership_violations"]]
                lines.append("")
            if r.get("mutation_check"):
                lines += ["Mutation-check notes from the reviewer (any 'NOT caught' mutation needs a test that catches it):",
                          r["mutation_check"], ""]
            fix_file = rep / f"fix-{wp}-round{round_no}.md"
            fix_file.write_text("\n".join(lines), encoding="utf-8")
            row["fix_file"] = str(fix_file)
        summary.append(row)
    return summary
