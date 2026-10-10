"""Backlog queue: work packages that are not urgent (or are easy) worked off by one engine, unattended.

Built for the local Qwen engine (`ollama_qwen`): free and never quota-limited, but slow, and the Mac
serves one Ollama request at a time (measured 2026-10-10: two concurrent jobs took 72 s, the same as
running them back to back), so the worker runs ONE package at a time and a lock keeps a second worker
from starting. Any engine key works (`--engine`).

Each package runs in its own git worktree on a new branch `jbr/<engine>/<WP>-<stamp>` cut from
`base`, so unattended edits never touch the checkout people are working in. Untracked essentials
the tests need (`.venv`, `node_modules`) are symlinked in. The worker never commits, never pushes,
never reviews: it runs the engine, runs the package's test command, records the result and moves on.
Review happens later, in a batch, by the commander (review_level.js on each worktree); `fix` puts a
package back in the queue with the reviewer's fix file, in the same worktree.

Everything lives in the project's runs dir (jbr/paths.py): queue.json, worker.lock, worker.log and
worktrees/. Nothing is written into the project's own tree; `git worktree add` records the worktree
in the project's .git, which is git's own bookkeeping.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

from jbr import paths, runner, state

DEFAULT_ENGINE = "ollama_qwen"
DEFAULT_LINKS = (".venv", "node_modules")
PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
TEST_TAIL_LINES = 15


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def queue_path(project: str | os.PathLike) -> pathlib.Path:
    return paths.project_dir(project) / "queue.json"


def worker_lock_path(project: str | os.PathLike) -> pathlib.Path:
    return paths.project_dir(project) / "worker.lock"


def load(project: str | os.PathLike) -> list[dict]:
    p = queue_path(project)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _save(project: str | os.PathLike, items: list[dict]) -> None:
    p = queue_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _update(project: str | os.PathLike, fn: Callable[[list[dict]], Any]) -> Any:
    lock = paths.project_dir(project) / "queue.lock"
    with state._lock(lock):
        items = load(project)
        out = fn(items)
        _save(project, items)
        return out


def _abs(project: pathlib.Path, p: str | os.PathLike) -> str:
    """Plan paths are resolved against the MAIN checkout: an untracked plan does not exist in a worktree."""
    q = pathlib.Path(p)
    return str(q if q.is_absolute() else (project / q).resolve())


# ----------------------------------------------------------------------------- add / fix / drop


def add(project: str | os.PathLike, wp_ids: list[str], *, packages: str, prompts: str,
        engine: str = DEFAULT_ENGINE, base: str = "HEAD", test_cmd: str = "") -> list[dict]:
    """Queue packages (validated against the plan). Already pending/running ids are skipped."""
    root = pathlib.Path(project).resolve()
    pk_path, pr_path = _abs(root, packages), _abs(root, prompts)
    known = {w["id"]: w for w in runner.load_packages(pk_path)[0]}
    missing = [w for w in wp_ids if w not in known]
    if missing:
        raise ValueError(f"not in {pk_path}: {missing}")
    base_sha = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()

    def fn(items: list[dict]) -> list[dict]:
        active = {i["wp"] for i in items if i["status"] in (PENDING, RUNNING)}
        added = []
        for wp in wp_ids:
            if wp in active:
                continue
            tests = test_cmd or (known[wp].get("tests") if isinstance(known[wp].get("tests"), str) else "")
            item = {"wp": wp, "engine": engine, "packages": pk_path, "prompts": pr_path, "base": base_sha,
                    "test_cmd": tests, "status": PENDING, "added": _now(), "round": 0, "extra_file": "",
                    "worktree": "", "branch": ""}
            items.append(item)
            added.append(item)
        return added
    return _update(root, fn)


def fix(project: str | os.PathLike, wp: str, extra_file: str) -> dict:
    """Back to the queue with a reviewer's fix file, in the SAME worktree (the code is already there)."""
    root = pathlib.Path(project).resolve()
    extra = str(pathlib.Path(extra_file).resolve())
    if not pathlib.Path(extra).is_file():
        raise ValueError(f"fix file not found: {extra}")

    def fn(items: list[dict]) -> dict:
        it = _latest(items, wp)
        if it["status"] not in (DONE, FAILED) or not it.get("worktree"):
            raise ValueError(f"{wp} is {it['status']}: only a finished package with a worktree can take a fix round")
        it.update(status=PENDING, extra_file=extra, round=it.get("round", 0) + 1, requeued=_now())
        return it
    return _update(root, fn)


def drop(project: str | os.PathLike, wp: str) -> dict:
    root = pathlib.Path(project).resolve()

    def fn(items: list[dict]) -> dict:
        it = _latest(items, wp)
        if it["status"] != PENDING:
            raise ValueError(f"{wp} is {it['status']}: only a pending package can be dropped")
        items.remove(it)
        return it
    return _update(root, fn)


def _latest(items: list[dict], wp: str) -> dict:
    for it in reversed(items):
        if it["wp"] == wp:
            return it
    raise ValueError(f"{wp} is not in the queue")


# ----------------------------------------------------------------------------- the worker


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def _make_worktree(root: pathlib.Path, it: dict, links: tuple[str, ...]) -> tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"jbr/{it['engine']}/{it['wp']}-{stamp}"
    wt = paths.project_dir(root) / "worktrees" / f"{it['wp']}-{stamp}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", branch, str(wt), it["base"])
    for name in links:
        src, dst = root / name, wt / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src, target_is_directory=src.is_dir())
    return str(wt), branch


def _changed(wt: str, links: tuple[str, ...] = DEFAULT_LINKS) -> list[str]:
    """Paths the engine touched. The symlinks we placed are not work: a `.venv/` ignore rule matches a
    directory, not a symlink, so git would list them."""
    out = subprocess.run(["git", "-C", wt, "status", "--porcelain"], capture_output=True, text=True).stdout
    return [ln[3:] for ln in out.splitlines() if ln[3:].rstrip("/") not in links]


def _run_tests(wt: str, cmd: str, timeout_s: int) -> dict:
    if not cmd:
        return {"cmd": "", "exit": None, "tail": "no test command"}
    try:
        p = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=timeout_s,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-TEST_TAIL_LINES:])
        return {"cmd": cmd, "exit": p.returncode, "tail": tail}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "exit": 124, "tail": f"timed out after {timeout_s}s"}


def run_one(project: str | os.PathLike, engines: dict[str, dict], *, links: tuple[str, ...] = DEFAULT_LINKS,
            test_timeout_s: int = 3600, probe: Callable[..., Any] | None = None,
            run_engine: Callable[..., dict] | None = None) -> dict | None:
    """Take the first pending package and see it through. None when nothing is pending.
    An engine that is not usable right now leaves the package pending and returns {"stopped": line}."""
    root = pathlib.Path(project).resolve()
    picked = _update(root, lambda items: next((dict(i) for i in items if i["status"] == PENDING), None))
    if picked is None:
        return None
    pr = (probe or state.probe)(picked["engine"], engines)
    if not pr.usable:
        return {"wp": picked["wp"], "stopped": f"{picked['engine']}: {pr.line()}"}

    def claim(items: list[dict]) -> dict | None:
        it = next((i for i in items if i["wp"] == picked["wp"] and i["status"] == PENDING), None)
        if it:
            it.update(status=RUNNING, started=_now())
        return it
    it = _update(root, claim)
    if it is None:  # dropped meanwhile
        return {"wp": picked["wp"], "skipped": "no longer pending"}
    result: dict[str, Any] = {}
    try:
        if not it.get("worktree") or not pathlib.Path(it["worktree"]).exists():
            it["worktree"], it["branch"] = _make_worktree(root, it, links)
            # saved now, not at the end: a worker killed mid-run must not orphan the worktree
            _update(root, lambda items: next(i for i in items if i["wp"] == it["wp"] and i["status"] == RUNNING)
                    .update(worktree=it["worktree"], branch=it["branch"]))
        tag = f"q{it.get('round', 0)}"
        extra = pathlib.Path(it["extra_file"]).read_text(encoding="utf-8") if it.get("extra_file") else ""
        res = (run_engine or runner.run)(it["worktree"], it["wp"], it["engine"], engines, it["packages"],
                                         it["prompts"], extra=extra, tag=tag,
                                         reports_dir=str(paths.reports_dir(root)))
        tests = _run_tests(it["worktree"], it["test_cmd"], test_timeout_s) if res.get("status") == "ok" else \
            {"cmd": it["test_cmd"], "exit": None, "tail": f"not run: engine {res.get('status')}"}
        changed = _changed(it["worktree"], links)
        result = {"status": DONE if res.get("status") == "ok" else FAILED, "engine_status": res.get("status"),
                  "exit": res.get("exit"), "minutes": res.get("minutes"), "tests": tests,
                  "changed": changed, "finished": _now()}
    except Exception as e:  # a broken package must not stop the backlog
        result = {"status": FAILED, "error": f"{type(e).__name__}: {e}", "finished": _now()}

    def finish(items: list[dict]) -> dict:
        cur = next(i for i in items if i["wp"] == it["wp"] and i["status"] == RUNNING)
        cur.update(worktree=it.get("worktree", ""), branch=it.get("branch", ""), **result)
        return dict(cur)
    return _update(root, finish)


def work(project: str | os.PathLike, engines: dict[str, dict], *, max_items: int = 0,
         log: Callable[[str], None] = print, **kw: Any) -> list[dict]:
    """Drain the queue one package at a time under the worker lock. Stops when nothing is pending,
    after max_items, or when the next package's engine is not usable."""
    root = pathlib.Path(project).resolve()
    lock = worker_lock_path(root)
    if not _acquire_worker(lock):
        raise RuntimeError(f"a worker is already running for {root} (pid in {lock})")
    done: list[dict] = []
    try:
        orphans = _update(root, _requeue_orphans)
        if orphans:
            log(f"re-queued {orphans}: left running by a worker that died")
        while not max_items or len(done) < max_items:
            r = run_one(root, engines, **kw)
            if r is None:
                log("queue empty")
                break
            if "stopped" in r:
                log(f"stopped before {r['wp']}: {r['stopped']}")
                break
            done.append(r)
            t = r.get("tests") or {}
            log(f"{r['wp']}: {r['status']} engine={r.get('engine_status')} {r.get('minutes')}min "
                f"tests exit={t.get('exit')} changed={len(r.get('changed') or [])} {r.get('worktree', '')}")
    finally:
        lock.unlink(missing_ok=True)
    return done


def _requeue_orphans(items: list[dict]) -> list[str]:
    """Called with the worker lock held, so nothing is really running: a RUNNING item was left by a
    worker that died (killed, reboot). Back to pending; its worktree is kept and reused."""
    out = []
    for i in items:
        if i["status"] == RUNNING:
            i.update(status=PENDING, requeued=_now())
            out.append(i["wp"])
    return out


def _acquire_worker(lock: pathlib.Path) -> bool:
    """One worker per project: the pid in the lock file must be dead before another may start."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid = int(lock.read_text().strip() or 0)
        except (ValueError, OSError):
            pid = 0
        if pid and _alive(pid):
            return False
        lock.unlink(missing_ok=True)
        return _acquire_worker(lock)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def spawn_worker(project: str | os.PathLike, global_args: list[str], work_args: list[str]) -> dict:
    """`jbr queue work` detached (survives the terminal), output in the runs dir's worker.log."""
    root = pathlib.Path(project).resolve()
    logf = paths.project_dir(root) / "worker.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    jbr_root = str(pathlib.Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = jbr_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, "-m", "jbr", *global_args, "--project", str(root), "queue", "work", *work_args]
    kw: dict[str, Any] = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
                          if os.name == "nt" else {"start_new_session": True})
    with open(logf, "a", encoding="utf-8") as out:
        out.write(f"\n# worker started {_now()}\n")
        out.flush()
        p = subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, **kw)
    return {"pid": p.pid, "log": str(logf)}


# ----------------------------------------------------------------------------- review hand-off / clean


def review_args(project: str | os.PathLike, item: dict, test_cmd: str = "") -> dict:
    """Workflow args for review_level.js on one finished package's worktree."""
    return {"root": item["worktree"], "ids": [item["wp"]], "promptsDir": item["prompts"],
            "reportsDir": str(paths.reports_dir(pathlib.Path(project).resolve())),
            "logTag": f"q{item.get('round', 0)}",
            "testCmd": test_cmd or item.get("test_cmd") or "python -m pytest -q"}


def clean(project: str | os.PathLike, wp: str, *, force: bool = False) -> dict:
    """Remove a finished package's worktree and branch (after its work was merged or rejected)."""
    root = pathlib.Path(project).resolve()

    def fn(items: list[dict]) -> dict:
        it = _latest(items, wp)
        if it["status"] in (PENDING, RUNNING):
            raise ValueError(f"{wp} is {it['status']}")
        wt = it.get("worktree")
        if wt and pathlib.Path(wt).exists():
            if not force:
                # Uncommitted work is lost on removal whatever the branch says (a branch with no commits
                # counts as "merged"), and committed work must already be in the project's HEAD.
                if _changed(wt):
                    raise ValueError(f"{wp}: worktree has uncommitted engine work ({wt}); review it, or --force")
                unmerged = it.get("branch") and subprocess.run(
                    ["git", "-C", str(root), "log", "--oneline", f"HEAD..{it['branch']}"],
                    capture_output=True, text=True).stdout.strip()
                if unmerged:
                    raise ValueError(f"{wp}: branch {it['branch']} has commits not in HEAD; merge them, or --force")
            _git(root, "worktree", "remove", "--force", wt)
        if it.get("branch"):
            subprocess.run(["git", "-C", str(root), "branch", "-D", it["branch"]], capture_output=True)
        items.remove(it)
        return it
    return _update(root, fn)


def format_status(items: list[dict]) -> str:
    if not items:
        return "queue is empty"
    lines = [f"{'wp':<8} {'status':<8} {'engine':<14} {'rnd':>3} {'min':>6} {'tests':>5}  worktree"]
    for i in items:
        t = (i.get("tests") or {}).get("exit")
        mins = "-" if i.get("minutes") is None else f"{i['minutes']:.1f}"
        lines.append(f"{i['wp']:<8} {i['status']:<8} {i['engine']:<14} {i.get('round', 0):>3} {mins:>6} "
                     f"{'-' if t is None else t:>5}  {i.get('worktree') or ''}")
    return "\n".join(lines)
