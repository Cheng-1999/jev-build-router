"""Drive one work package through an external engine (agy or codex), headless.

Artifacts under <project_root>/ops/reports/:
  run-<WP>[-tag].prompt.md   the self-contained prompt (engine reads it via a file pointer)
  run-<WP>[-tag].log         engine transcript
  run-<WP>[-tag].done        "<exit code> <minutes>" once finished
  run-<WP>[-tag].stdout      stdout of a detached (spawned) run
  run-<WP>[-tag].last.md     codex only: final message (-o)

Engine `claude_subagent` is never executed here: build_command returns None and
run() reports that the package belongs to the Claude Code workflow.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

RUN_PREFIX = "run"
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


def report_paths(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str = "ops/reports") -> dict[str, pathlib.Path]:
    rep = pathlib.Path(project_root) / reports_dir
    stem = f"{RUN_PREFIX}-{wp_id}" + (f"-{tag}" if tag else "")
    return {
        "dir": rep,
        "prompt": rep / f"{stem}.prompt.md",
        "log": rep / f"{stem}.log",
        "done": rep / f"{stem}.done",
        "stdout": rep / f"{stem}.stdout",
        "last": rep / f"{stem}.last.md",
    }


# ----------------------------------------------------------------------------- prompt


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def compose_prompt(project_root: str | os.PathLike, wp: dict, prompts_dir: str | os.PathLike = "ops/prompts",
                   extra: str = "", rules: str | None = None) -> str:
    root = pathlib.Path(project_root)
    lines = [
        f"You are the software engineer on work package {wp['id']}: {wp.get('title', '')}.",
        f"Repository root (already exists): {root}",
        rules or wp.get("rules") or DEFAULT_RULES,
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
    """argv for the engine's CLI, or None when the engine is a Claude Code subagent."""
    runner = engine.get("runner", "agy")
    model = engine.get("model", "")
    effort = engine.get("effort", "high")
    pointer = pointer_text(prompt_file, project_root)
    if runner == "claude_subagent":
        return None
    if runner == "codex":
        # OpenAI Codex CLI. workspace-write sandbox keeps it inside the repo.
        return ["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check", "-m", model,
                "-c", f"model_reasoning_effort={effort}", "-C", str(project_root),
                "-o", str(last_file), pointer]
    if runner == "agy":
        cmd = ["agy", "-p", pointer, "--model", model, "--dangerously-skip-permissions", "--print-timeout", timeout]
        if not model.startswith("claude"):
            # agy rejects --effort for Claude thinking models (their thinking is the boost)
            cmd += ["--effort", effort]
        return cmd
    raise ValueError(f"unknown runner {runner!r} (expected agy|codex|claude_subagent)")


def _env() -> dict[str, str]:
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


# ----------------------------------------------------------------------------- run / spawn / wait


def run(project_root: str | os.PathLike, wp_id: str, engine_key: str, engines: dict[str, dict],
        packages_path: str | os.PathLike, prompts_dir: str | os.PathLike = "ops/prompts", *,
        extra: str = "", tag: str = "", timeout: str = DEFAULT_TIMEOUT, dry_run: bool = False,
        reports_dir: str = "ops/reports", rules: str | None = None) -> dict[str, Any]:
    """Compose the prompt, run the engine synchronously, write the .done marker.

    Returns {"wp", "engine", "cmd", "exit", "minutes", "paths"}; exit is None on dry-run
    and "workflow" when the engine is claude_subagent.
    """
    root = pathlib.Path(project_root).resolve()
    if engine_key not in engines:
        raise KeyError(f"unknown engine {engine_key!r}; known: {sorted(engines)}")
    engine = engines[engine_key]
    packages, meta = load_packages(packages_path)
    wp = find_package(packages, wp_id)
    paths = report_paths(root, wp_id, tag, reports_dir)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    prompt = compose_prompt(root, wp, prompts_dir, extra, rules or meta.get("rules"))
    paths["prompt"].write_text(prompt, encoding="utf-8")
    if paths["done"].exists():
        paths["done"].unlink()

    cmd = build_command(engine, paths["prompt"], root, paths["last"], timeout)
    result: dict[str, Any] = {"wp": wp_id, "engine": engine_key, "cmd": cmd, "exit": None, "minutes": 0.0,
                              "paths": {k: str(v) for k, v in paths.items()}}
    if cmd is None:
        result["exit"] = "workflow"
        result["note"] = "engine is a Claude Code subagent: hand this package to workflows/build_dag.js"
        return result
    if dry_run:
        result["note"] = "dry-run: command not executed"
        return result

    t0 = time.time()
    with open(paths["log"], "w", encoding="utf-8") as fh:
        fh.write(f"# {engine_key} run {wp_id} model={engine.get('model')} effort={engine.get('effort')}\n\n")
        fh.flush()
        p = subprocess.run(cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT, env=_env())
    dt = (time.time() - t0) / 60
    paths["done"].write_text(f"{p.returncode} {dt:.1f}min\n", encoding="utf-8")
    result.update(exit=p.returncode, minutes=round(dt, 1))
    return result


def spawn(project_root: str | os.PathLike, wp_id: str, run_args: list[str], tag: str = "",
          reports_dir: str = "ops/reports", dry_run: bool = False) -> dict[str, Any]:
    """Launch `python -m jbr run <wp_id> <run_args...>` detached so it survives the caller's timeout."""
    root = pathlib.Path(project_root).resolve()
    paths = report_paths(root, wp_id, tag, reports_dir)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    if paths["done"].exists():
        paths["done"].unlink()
    argv = [sys.executable, "-m", "jbr", "run", wp_id, "--project", str(root), *run_args]
    if tag and "--tag" not in run_args:
        argv += ["--tag", tag]
    if dry_run:
        return {"pid": None, "argv": argv, "marker": str(paths["done"])}
    env = _env()
    # make the jbr package importable from the child no matter where the project lives
    jbr_root = str(pathlib.Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = jbr_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kw: dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kw["start_new_session"] = True
    out = open(paths["stdout"], "w", encoding="utf-8")
    p = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, **kw)
    return {"pid": p.pid, "argv": argv, "marker": str(paths["done"])}


def read_done(project_root: str | os.PathLike, wp_id: str, tag: str = "", reports_dir: str = "ops/reports") -> dict | None:
    """Parse a .done marker: {"exit": int, "minutes": float} or None if still running."""
    marker = report_paths(project_root, wp_id, tag, reports_dir)["done"]
    if not marker.exists():
        return None
    txt = marker.read_text(encoding="utf-8").strip()
    parts = txt.split()
    try:
        return {"exit": int(parts[0]), "minutes": float(parts[1].rstrip("min")) if len(parts) > 1 else 0.0}
    except (ValueError, IndexError):
        return {"exit": -1, "minutes": 0.0, "raw": txt}


def wait(project_root: str | os.PathLike, wp_ids: list[str], tag: str = "", timeout_s: float = 70 * 60,
         poll_s: float = 30, reports_dir: str = "ops/reports", sleep=time.sleep) -> dict[str, dict | None]:
    """Block until every marker exists or timeout; returns {wp: done-dict|None}."""
    t0 = time.time()
    while True:
        state = {wp: read_done(project_root, wp, tag, reports_dir) for wp in wp_ids}
        if all(v is not None for v in state.values()) or time.time() - t0 >= timeout_s:
            return state
        sleep(poll_s)


def status(project_root: str | os.PathLike, reports_dir: str = "ops/reports") -> list[dict]:
    """One row per run-* prompt file: wp, tag, done marker, log size."""
    rep = pathlib.Path(project_root) / reports_dir
    rows = []
    for prompt in sorted(rep.glob(f"{RUN_PREFIX}-*.prompt.md")) if rep.exists() else []:
        stem = prompt.name[: -len(".prompt.md")][len(RUN_PREFIX) + 1:]
        wp, _, tag = stem.partition("-")
        done = read_done(project_root, wp, tag, reports_dir)
        log = rep / f"{RUN_PREFIX}-{stem}.log"
        rows.append({"wp": wp, "tag": tag, "done": done, "log_bytes": log.stat().st_size if log.exists() else 0})
    return rows


# ----------------------------------------------------------------------------- compose-fix


def compose_fix(project_root: str | os.PathLike, review_output: str | os.PathLike, round_no: int,
                reports_dir: str = "ops/reports") -> list[dict]:
    """Turn review-workflow output into per-package fix instructions.

    Writes ops/reports/review-<WP>-round<N>.json for every package and fix-<WP>-round<N>.md
    for packages with verdict 'fix'. Returns [{"wp", "verdict", "confirmed", "low", "fix_file"}].
    """
    rep = pathlib.Path(project_root) / reports_dir
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
