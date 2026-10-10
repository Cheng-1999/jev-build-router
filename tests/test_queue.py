import json
import os
import pathlib
import subprocess

import pytest

from jbr import cli, paths, queue, router
from tests.conftest import REPO, reports


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git project with a plan; runs dir OUTSIDE it (a sibling), as on a real machine."""
    root = tmp_path / "app"
    root.mkdir()
    monkeypatch.setenv("JBR_RUNS", str(tmp_path / "runs"))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("x = 1\n")
    (root / ".gitignore").write_text(".venv/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    (root / ".venv").mkdir()                       # untracked, needed by tests: gets symlinked in
    (root / ".venv" / "marker").write_text("venv")
    plan = tmp_path / "plan"
    (plan / "prompts").mkdir(parents=True)
    (plan / "work_packages.json").write_text(json.dumps({"work_packages": [
        {"id": "WP01", "title": "one", "tests": "ls .venv/marker && echo 3 passed"},
        {"id": "WP02", "title": "two"}]}))
    (plan / "prompts" / "WP01.md").write_text("do one")
    return root, plan


class _Probe:
    def __init__(self, ok=True):
        self.usable = ok

    def line(self):
        return "AVAILABLE" if self.usable else "UNAVAILABLE ollama not running"


def _engine(calls):
    def run(project_root, wp, engine_key, engines, packages, prompts, *, extra="", tag="", reports_dir=None):
        calls.append({"root": project_root, "wp": wp, "engine": engine_key, "extra": extra, "tag": tag,
                      "reports_dir": reports_dir, "packages": packages})
        (pathlib.Path(project_root) / f"{wp.lower()}.py").write_text(f"# {wp} {tag}\n")
        return {"status": "ok", "exit": 0, "minutes": 0.5}
    return run


ENGINES = router.load_engines(REPO / "engines.json")


def test_add_validates_against_the_plan_and_skips_duplicates(repo):
    root, plan = repo
    added = queue.add(root, ["WP01", "WP02"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    assert [i["wp"] for i in added] == ["WP01", "WP02"]
    assert added[0]["test_cmd"].startswith("ls .venv/marker") and added[1]["test_cmd"] == ""
    assert added[0]["base"] == _git(root, "rev-parse", "HEAD").strip()   # pinned, not a moving ref
    assert queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts")) == []
    with pytest.raises(ValueError, match="WP09"):
        queue.add(root, ["WP09"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    assert queue.queue_path(root).parent == paths.project_dir(root)


def test_worker_runs_each_package_in_its_own_worktree_and_never_touches_the_checkout(repo):
    root, plan = repo
    queue.add(root, ["WP01", "WP02"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    before = _git(root, "status", "--porcelain")
    calls = []
    done = queue.work(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine(calls), log=lambda m: None)
    assert [d["wp"] for d in done] == ["WP01", "WP02"] and all(d["status"] == "done" for d in done)
    assert _git(root, "status", "--porcelain") == before and not (root / "wp01.py").exists()
    one = done[0]
    wt = pathlib.Path(one["worktree"])
    assert wt.is_relative_to(paths.project_dir(root)) and (wt / "wp01.py").exists()
    assert one["branch"].startswith("jbr/ollama_qwen/WP01-") and one["branch"] in _git(root, "branch", "--list", "jbr/*")
    assert one["changed"] == ["wp01.py"]                                 # the .venv symlink is not engine work
    assert (wt / ".venv").is_symlink() and one["tests"]["exit"] == 0 and "3 passed" in one["tests"]["tail"]
    assert done[1]["tests"]["tail"] == "no test command"
    assert calls[0]["reports_dir"] == str(reports(root)) and calls[0]["tag"] == "q0"
    assert calls[0]["engine"] == "ollama_qwen" and calls[0]["root"] == one["worktree"]
    assert not queue.worker_lock_path(root).exists()                    # released


def test_unusable_engine_stops_the_worker_and_leaves_the_package_pending(repo):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    msgs, calls = [], []
    assert queue.work(root, ENGINES, probe=lambda *a, **k: _Probe(False), run_engine=_engine(calls), log=msgs.append) == []
    assert calls == [] and "UNAVAILABLE" in msgs[0]
    assert queue.load(root)[0]["status"] == "pending" and not queue.load(root)[0]["worktree"]


def test_engine_failure_is_recorded_and_the_backlog_continues(repo):
    root, plan = repo
    queue.add(root, ["WP01", "WP02"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    good = _engine([])

    def flaky(project_root, wp, *a, **k):
        if wp == "WP01":
            raise RuntimeError("codex died")
        return good(project_root, wp, *a, **k)
    done = queue.work(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=flaky, log=lambda m: None)
    assert [(d["wp"], d["status"]) for d in done] == [("WP01", "failed"), ("WP02", "done")]
    assert "codex died" in done[0]["error"]


def test_quota_or_crash_status_fails_without_running_tests(repo):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    r = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(),
                      run_engine=lambda *a, **k: {"status": "crash", "exit": 1, "minutes": 0.1})
    assert r["status"] == "failed" and r["tests"]["exit"] is None and "not run" in r["tests"]["tail"]


def test_one_worker_per_project(repo):
    root, plan = repo
    lock = queue.worker_lock_path(root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))                                   # a live worker holds it
    with pytest.raises(RuntimeError, match="already running"):
        queue.work(root, ENGINES, log=lambda m: None)
    lock.write_text("999999999")                                        # dead pid: lock is stale
    assert queue.work(root, ENGINES, log=lambda m: None) == []


def test_fix_round_reuses_the_worktree_with_the_fix_file(repo, tmp_path):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    calls = []
    first = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine(calls))
    fixf = tmp_path / "fix-WP01-round1.md"
    fixf.write_text("rename x to y")
    with pytest.raises(ValueError, match="not found"):
        queue.fix(root, "WP01", str(tmp_path / "nope.md"))
    it = queue.fix(root, "WP01", str(fixf))
    assert it["status"] == "pending" and it["round"] == 1
    second = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine(calls))
    assert second["worktree"] == first["worktree"] and calls[1]["extra"] == "rename x to y" and calls[1]["tag"] == "q1"
    args = queue.review_args(root, second)
    assert args["root"] == first["worktree"] and args["ids"] == ["WP01"] and args["logTag"] == "q1"
    assert args["reportsDir"] == str(reports(root)) and args["promptsDir"] == str(plan / "prompts")


def test_clean_refuses_unreviewed_work_unless_forced(repo):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    r = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine([]))
    with pytest.raises(ValueError, match="uncommitted engine work"):
        queue.clean(root, "WP01")
    _git(r["worktree"], "add", "-A")
    _git(r["worktree"], "commit", "-qm", "qwen work")                   # committed but not merged: still refused
    with pytest.raises(ValueError, match="commits not in HEAD"):
        queue.clean(root, "WP01")
    _git(root, "merge", "-q", r["branch"])                               # merged: clean goes through without --force
    queue.clean(root, "WP01")
    assert not pathlib.Path(r["worktree"]).exists() and "jbr/" not in _git(root, "branch", "--list", "jbr/*")
    assert queue.load(root) == []
    assert (root / "wp01.py").exists()                                   # the merged work stays


def test_clean_force_discards(repo):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    r = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine([]))
    queue.clean(root, "WP01", force=True)
    assert not pathlib.Path(r["worktree"]).exists() and queue.load(root) == []


def test_drop_only_pending(repo):
    root, plan = repo
    queue.add(root, ["WP01", "WP02"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    assert queue.drop(root, "WP02")["wp"] == "WP02"
    queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine([]))
    with pytest.raises(ValueError, match="only a pending"):
        queue.drop(root, "WP01")


def test_cli_queue_add_status_and_review_args(repo, capsys):
    root, plan = repo
    g = ["--project", str(root), "--packages", str(plan / "work_packages.json"), "--prompts", str(plan / "prompts")]
    assert cli.main(g + ["queue", "add", "WP01", "--test-cmd", "echo ok"]) == 0
    assert "queued ['WP01'] on ollama_qwen" in capsys.readouterr().out
    assert cli.main(g + ["queue", "add", "WP01", "--engine", "gpt9"]) == 2
    capsys.readouterr()
    assert cli.main(["--project", str(root), "queue", "status"]) == 0
    assert "WP01" in capsys.readouterr().out and "pending" in capsys.readouterr().out + "pending"
    assert cli.main(["--project", str(root), "queue", "review-args"]) == 0
    assert json.loads(capsys.readouterr().out) == []                     # nothing finished yet


def test_a_dead_workers_running_package_is_requeued_with_its_worktree(repo):
    root, plan = repo
    queue.add(root, ["WP01"], packages=str(plan / "work_packages.json"), prompts=str(plan / "prompts"))
    first = queue.run_one(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine([]))
    items = queue.load(root)
    items[0]["status"] = "running"                                       # worker killed mid-run
    queue._save(root, items)
    msgs, calls = [], []
    done = queue.work(root, ENGINES, probe=lambda *a, **k: _Probe(), run_engine=_engine(calls), log=msgs.append)
    assert "re-queued ['WP01']" in msgs[0]
    assert done[0]["status"] == "done" and done[0]["worktree"] == first["worktree"]


def test_engine_subprocess_never_inherits_stdin(tmp_path, monkeypatch):
    """codex exec reads stdin when it is not a TTY: an inherited pipe hangs it forever."""
    from jbr import runner
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    (tmp_path / "ops" / "prompts").mkdir(parents=True)
    (tmp_path / "ops" / "work_packages.json").write_text(json.dumps({"work_packages": [{"id": "WP01", "title": "t"}]}))
    runner.run(tmp_path, "WP01", "ollama_qwen", ENGINES, tmp_path / "ops" / "work_packages.json")
    assert seen["stdin"] is subprocess.DEVNULL
