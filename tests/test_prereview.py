"""jbr prereview: open-code-review (`ocr`) as a cheap first-pass reviewer on local Ollama. ocr is never
executed: every git/ocr call goes through a fake `run`, the backend check (`ready`) is a stub."""
import io
import json
import os
import pathlib
import subprocess
import time

import pytest

from jbr import cli, prereview, router
from tests.conftest import REPO

FAKE_BIN = "/fake/bin/opencodereview"
OCR_RESULT = {
    "status": "success",
    "llm": {"provider": "jbr-local", "model": "qwen3.8:27b-mlx"},
    "summary": {"files_reviewed": 1, "comments": 2, "total_tokens": 900, "input_tokens": 800, "output_tokens": 100, "elapsed": "1m2s"},
    "comments": [
        {"path": "src/demo/core.py", "content": "sign() returns 0 for zero input; spec says +1/-1", "start_line": 3, "end_line": 4,
         "existing_code": "return 0", "suggestion_code": "return 1", "thinking": "long chain of thought"},
        {"path": "src/demo/core.py", "content": "unused import", "start_line": 1, "end_line": 1},
    ],
}


class FakeRun:
    """subprocess.run stand-in: answers git from `changed`, plays ocr from `ocr` (result dict | Exception | rc)."""

    def __init__(self, changed=("src/demo/core.py",), untracked=(), ocr=OCR_RESULT, rc=0, git_config=None):
        self.changed, self.untracked, self.ocr, self.rc = list(changed), list(untracked), ocr, rc
        self.git_config = git_config or {}  # key -> [values] answered by `git config`
        self.calls, self.kw = [], []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kw.append(kw)
        if cmd[0] == "git":
            sub = [t for t in cmd[1:] if not t.startswith("-") and "=" not in t][0]
            if sub == "config":
                vals = self.git_config.get(cmd[-1], [])
                return subprocess.CompletedProcess(cmd, 0 if vals else 1, "".join(v + "\n" for v in vals), "")
            out = self.untracked if sub == "ls-files" else self.changed
            return subprocess.CompletedProcess(cmd, 0, "".join(p + "\0" for p in out), "")
        if isinstance(self.ocr, BaseException):
            raise self.ocr
        if self.ocr is not None:
            pathlib.Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(self.ocr), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.rc)

    def ocr_calls(self):
        return [(c, k) for c, k in zip(self.calls, self.kw) if c[0] != "git"]


def ready(url, model):
    return ""


@pytest.fixture
def engines():
    """engines.json as shipped (ollama_qwen on: the prereview backend)."""
    return router.load_engines(REPO / "engines.json")


@pytest.fixture
def ocr_bin(monkeypatch):
    monkeypatch.setenv("JBR_OCR_BIN", FAKE_BIN)
    return FAKE_BIN


def go(project, engines, run, **kw):
    kw.setdefault("ready", ready)
    return prereview.prereview(project, kw.pop("wp", "WP01"), engines, project / "ops" / "work_packages.json",
                               "ops/prompts", run=run, **kw)


# ----------------------------------------------------------------------------- scope


def test_expand_braces_and_globs():
    assert prereview.expand_braces("src/{a,b}/{x,y}.ts") == ["src/a/x.ts", "src/a/y.ts", "src/b/x.ts", "src/b/y.ts"]
    owned = prereview.owned_matcher(["pkg/core/{alpha,beta}.ts", "src/rules/**", "tests/test_*.py", "./.env.example",
                                     ".github/workflows/*.yml"])
    assert owned("pkg/core/alpha.ts") and not owned("pkg/core/gamma.ts")
    assert owned(".env.example") and owned(".github/workflows/ci.yml")  # dot files: only a `./` prefix is stripped
    assert owned("src/rules/deep/er/file.ts") and owned("src/rules/x.ts")
    assert owned("tests/test_a.py") and not owned("tests/sub/test_a.py")  # * never crosses a directory
    assert prereview.owned_matcher(["a/**/b.ts"])("a/b.ts") and prereview.owned_matcher(["a/**/b.ts"])("a/x/y/b.ts")


def test_exclude_patterns_are_full_paths_escaped_and_collapsed():
    inside = ["src/pkg/README.md", "src/pkg/a.py"]
    pats = prereview.exclude_patterns(["README.md", "src/other.py", "app/[slug]/page.tsx", "docs/a.md", "docs/b.md", "src/pkg/b*.py"], inside)
    # ocr matches each pattern against the whole path (`README.md` = the root file only); a dir with no owned
    # change collapses to `dir/**`; glob characters become one-char classes
    assert pats == ["README.md", "app/**", "docs/**", "src/other.py", "src/pkg/b[*].py"]
    assert prereview.exclude_patterns(["x/[a]{b}?/f.ts"], ["x/keep.ts"]) == ["x/[[]a]?b??/**"]
    assert prereview.exclude_patterns(["docs/a,b.md"], ["docs/keep.md"]) == ["docs/a?b.md"]  # a comma would split the list
    # a wildcard that would also catch an owned file goes one level deeper, else to the file itself
    assert prereview.exclude_patterns(["app/{id}/x.ts"], ["app/[id]/y.ts", "app/z.ts"]) == ["app/?id?/x.ts"]
    assert prereview.exclude_patterns(["a{b"], ["a[b"]) == []  # no safe pattern: reviewed rather than an owned file lost


def test_split_scope_excludes_other_packages_work():
    inside, out = prereview.split_scope(["src/demo/core.py", "src/demo/ledger.py", "README.md"], ["src/demo/core.py"])
    assert inside == ["src/demo/core.py"] and out == ["src/demo/ledger.py", "README.md"]
    assert prereview.split_scope(["a", "b"], []) == (["a", "b"], [])  # no owned list: review everything changed


# ----------------------------------------------------------------------------- run


def test_ok_run_isolates_ocr_and_normalizes_findings(project, engines, ocr_bin, monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OCR_LLM_URL", "OCR_LLM_TOKEN"):
        monkeypatch.setenv(k, "must-not-leak")
    run = FakeRun(changed=["src/demo/core.py", "src/demo/ledger.py"], untracked=["notes.txt"],
                  git_config={"core.excludesFile": [str(tmp_path / "global-ignore")], "safe.directory": ["/mnt/a", "/mnt/b"]})
    res = go(project, engines, run, tag="r1")
    assert res["status"] == "OK" and res["scope"] == ["src/demo/core.py"]
    assert res["findings"][0] == {"path": "src/demo/core.py", "start_line": 3, "end_line": 4, "content": OCR_RESULT["comments"][0]["content"],
                                  "existing_code": "return 0", "suggestion_code": "return 1"}  # no `thinking`
    [(cmd, kw)] = run.ocr_calls()
    assert cmd[:2] == [FAKE_BIN, "review"]
    assert cmd[cmd.index("--provider") + 1] == "jbr-local" and cmd[cmd.index("--model") + 1] == "qwen3.8:27b-mlx"
    assert cmd[cmd.index("--format") + 1] == "json" and cmd[cmd.index("--audience") + 1] == "agent"
    assert cmd[cmd.index("--exclude") + 1] == "notes.txt,src/demo/ledger.py"  # another package's work stays out
    assert cmd[cmd.index("--background-file") + 1] == str(project / "ops" / "prompts" / "WP01.md")  # the spec, as context
    assert "--commit" not in cmd and kw["cwd"] == project.resolve() and kw["stdin"] is subprocess.DEVNULL
    env, home = kw["env"], prereview.ocr_home()
    assert env["HOME"] == str(home) and env["OCR_NO_UPDATE"] == "1"
    assert not [k for k in env if k.startswith(("ANTHROPIC", "OPENAI", "OCR_LLM_URL", "OCR_LLM_TOKEN"))]
    # ocr's git keeps the user's global ignore file and safe.directory list although HOME is jbr's
    assert env["GIT_CONFIG_COUNT"] == "3" and env["GIT_CONFIG_KEY_0"] == "core.excludesFile"
    assert env["GIT_CONFIG_VALUE_0"] == str(tmp_path / "global-ignore") and env["GIT_CONFIG_VALUE_2"] == "/mnt/b"
    cfg = json.loads((home / ".opencodereview" / "config.json").read_text(encoding="utf-8"))
    assert cfg["provider"] == "jbr-local"
    assert cfg["custom_providers"]["jbr-local"]["url"] == "http://127.0.0.1:11434/v1"
    out = json.loads(pathlib.Path(res["paths"]["json"]).read_text(encoding="utf-8"))
    assert out["status"] == "OK" and len(out["findings"]) == 2 and out["summary"]["comments"] == 2
    assert pathlib.Path(res["paths"]["done"]).read_text(encoding="utf-8").split()[::2] == ["0", "OK"]
    assert pathlib.Path(res["paths"]["done"]).name == "prereview-WP01-r1.done"
    assert not (project / ".opencodereview").exists()  # nothing written into the project


def test_non_local_endpoint_refused_unless_allowed(project, engines, ocr_bin):
    def no_run(*a, **k):
        raise AssertionError("nothing may run for a refused endpoint")
    res = go(project, engines, no_run, url="https://api.example.com/v1")
    assert res["status"] == "SKIPPED" and "non-local" in res["reason"]
    assert go(project, engines, FakeRun(), url="https://api.example.com/v1", allow_remote=True)["status"] == "OK"


def test_missing_binary_skips(project, engines, monkeypatch):
    monkeypatch.setattr(prereview.shutil, "which", lambda name: None)
    res = go(project, engines, FakeRun())
    assert res["status"] == "SKIPPED" and "not installed" in res["reason"]
    assert pathlib.Path(res["paths"]["done"]).read_text(encoding="utf-8").split()[2] == "SKIPPED"


def test_backend_not_ready_or_disabled_skips_without_running_ocr(project, engines, ocr_bin, monkeypatch):
    run = FakeRun()
    res = go(project, engines, run, ready=lambda url, model: f"{url} not reachable")
    assert res["status"] == "SKIPPED" and "backend not ready" in res["reason"] and run.ocr_calls() == []
    engines["ollama_qwen"]["available"] = False  # --available ollama_qwen=no
    res = go(project, engines, run)
    assert res["status"] == "SKIPPED" and "disabled" in res["reason"]
    # the real check is one GET of /models: nothing recorded in the engine state, no model loaded
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    handlers = []

    class Opener:
        def open(self, u, timeout):
            seen.append(u)
            return Resp(json.dumps({"data": [{"id": "qwen3.8:27b-mlx"}, {"id": "llama3:latest"}]}).encode())
    monkeypatch.setattr(prereview.urllib.request, "build_opener", lambda *h: handlers.append(h) or Opener())
    assert prereview.backend_ready("http://127.0.0.1:11434/v1", "qwen3.8:27b-mlx") == ""
    assert prereview.backend_ready("http://127.0.0.1:11434/v1", "llama3") == ""  # Ollama's implicit :latest
    assert "does not serve" in prereview.backend_ready("http://127.0.0.1:11434/v1/", "other")
    assert seen == ["http://127.0.0.1:11434/v1/models"] * 3
    assert all(isinstance(h[0], prereview.urllib.request.ProxyHandler) for h in handlers)  # loopback: no proxy


def test_no_owned_changes_or_no_files_list_skips(project, engines, ocr_bin):
    run = FakeRun(changed=["src/demo/ledger.py"])
    res = go(project, engines, run)
    assert res["status"] == "SKIPPED" and "no changed files" in res["reason"] and run.ocr_calls() == []
    pk = project / "ops" / "work_packages.json"
    doc = json.loads(pk.read_text())
    doc["work_packages"][0]["files"] = []
    pk.write_text(json.dumps(doc))
    res = go(project, engines, run)  # workspace mode: it would review every package's work in the checkout
    assert res["status"] == "SKIPPED" and "no `files`" in res["reason"]


def test_partial_result_at_the_budget_is_used(project, engines, ocr_bin):
    class Partial(FakeRun):  # ocr wrote its JSON on SIGTERM, then the budget expired
        def __call__(self, cmd, **kw):
            if cmd[0] != "git":
                pathlib.Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(dict(OCR_RESULT, status="partial")))
                raise subprocess.TimeoutExpired(cmd, 1)
            return super().__call__(cmd, **kw)
    res = go(project, engines, Partial())
    assert res["status"] == "OK" and "partial" in res["reason"] and len(res["findings"]) == 2


def test_refuses_the_real_home_as_ocr_home(project, engines, ocr_bin, monkeypatch):
    monkeypatch.setenv("JBR_OCR_HOME", str(pathlib.Path.home()))
    run = FakeRun()
    res = go(project, engines, run)
    assert res["status"] == "SKIPPED" and "your home" in res["reason"] and run.ocr_calls() == []


def test_timeout_and_failures_are_statuses_not_exceptions(project, engines, ocr_bin):
    run = FakeRun(ocr=subprocess.TimeoutExpired(["ocr"], 1))
    res = go(project, engines, run, budget_min=2, effort="medium")
    assert res["status"] == "TIMEOUT"
    cmd, kw = run.ocr_calls()[0]
    assert kw["timeout"] <= 120 and cmd[cmd.index("--timeout") + 1] == "1"  # per file group, divided by rounds
    assert pathlib.Path(res["paths"]["done"]).read_text(encoding="utf-8").split()[2] == "TIMEOUT"
    assert go(project, engines, FakeRun(ocr=None, rc=1))["status"] == "FAILED"  # exit 1, no JSON
    res = go(project, engines, FakeRun(ocr=dict(OCR_RESULT, status="completed_with_errors"), rc=1))
    assert res["status"] == "FAILED" and len(res["findings"]) == 2  # kept for the log, never handed to the reviewer
    skipped = {"status": "skipped", "message": "No supported files changed.", "comments": []}
    assert go(project, engines, FakeRun(ocr=skipped))["status"] == "SKIPPED"

    def git_breaks(cmd, **kw):
        raise OSError("git: not found")
    res = go(project, engines, git_breaks)
    assert res["status"] == "FAILED" and "crashed" in res["reason"]
    assert "Traceback" in pathlib.Path(res["paths"]["log"]).read_text(encoding="utf-8")
    assert pathlib.Path(res["paths"]["done"]).read_text(encoding="utf-8").split()[2] == "FAILED"


def test_commit_mode_reads_the_commit_and_passes_it(project, engines, ocr_bin):
    run = FakeRun()
    res = go(project, engines, run, commit="abc123", spec=False)
    assert res["status"] == "OK"
    assert "diff-tree" in run.calls[0] and "-z" in run.calls[0] and run.calls[0][-1] == "abc123"
    assert run.calls[0][1:3] == ["-c", "core.quotePath=false"]
    cmd = run.ocr_calls()[0][0]
    assert cmd[cmd.index("--commit") + 1] == "abc123" and "--background-file" not in cmd


def test_machine_lock_busy_dead_and_expired_holders(project, engines, ocr_bin, monkeypatch):
    monkeypatch.setattr(prereview, "LOCK_POLL_S", 0.01)
    monkeypatch.setattr(prereview, "LOCK_WAIT_MAX_S", 0.05)
    lock = prereview.ocr_home() / "prereview.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    live = json.dumps({"pid": os.getpid(), "deadline": time.time() + 3600})
    lock.write_text(live)
    run = FakeRun()
    res = go(project, engines, run)
    assert res["status"] == "SKIPPED" and res["reason"].startswith("busy") and run.ocr_calls() == []
    assert lock.read_text() == live  # someone else's live lock is left alone
    dead = subprocess.Popen(["/bin/sh", "-c", "exit 0"] if os.name != "nt" else ["cmd", "/c", "exit 0"])
    dead.wait()
    for held in ({"pid": dead.pid, "deadline": time.time() + 3600},    # holder died
                 {"pid": os.getpid(), "deadline": time.time() - 200}):  # holder overran its deadline (+120 s margin)
        lock.write_text(json.dumps(held))
        assert go(project, engines, FakeRun())["status"] == "OK"
        assert not lock.exists()  # ours, released


def test_dry_run_runs_nothing_and_writes_no_marker(project, engines, ocr_bin):
    run = FakeRun()
    res = go(project, engines, run, dry_run=True)
    assert res["status"] == "OK" and res["argv"][0] == FAKE_BIN and run.ocr_calls() == []
    assert "HOME" in res["env"] and not pathlib.Path(res["paths"]["done"]).exists()


# ----------------------------------------------------------------------------- cli


def test_cli_prereview_always_exits_zero(project, capsys, monkeypatch):
    monkeypatch.setattr(prereview.shutil, "which", lambda name: None)
    assert cli.main(["--project", str(project), "prereview", "WP01", "--tag", "r1"]) == 0
    assert "WP01 SKIPPED: ocr not installed" in capsys.readouterr().out


def test_cli_prereview_detach_spawns_a_child_with_globals(project, capsys):
    rc = cli.main(["--project", str(project), "prereview", "WP01", "--tag", "r1", "--effort", "medium", "--detach", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    argv = json.loads(out[out.index("[\""): out.rindex("]") + 1])
    assert argv[1:5] == ["-m", "jbr", "prereview", "WP01"]
    assert argv[argv.index("--effort") + 1] == "medium" and argv[argv.index("--tag") + 1] == "r1"
    assert "--detach" not in argv and "--engines" in argv
    assert "prereview-WP01-r1.done" in out


# ----------------------------------------------------------------------------- workflows (stub agents under node)

from tests.test_workflows import BUILD_DAG, NODE, REVIEW_LEVEL, run_workflow  # noqa: E402

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
DAG = {"root": "/p", "jbr": "/jbr", "reportsDir": "/runs/p/reports", "packages": [{"id": "WP01", "depends_on": []}],
       "routing": {"WP01": "claude_subagent"}}


def review(failures, verdict="fix"):
    return {"wp": "WP01", "pytest_summary": "5 passed", "test_count": 5, "passed_criteria": ["a"], "failures": failures,
            "mutation_check": "caught", "ownership_violations": [], "verdict": verdict}


@needs_node
def test_build_dag_without_prereview_is_unchanged():
    rc, out = run_workflow(BUILD_DAG, DAG)
    assert rc == 0 and not [c for c in out["calls"] if c.startswith("prereview:")]
    assert "Pre-review hints" not in out["prompts"]["review:WP01:r1"]
    assert "prereview" not in out["result"][0]["history"][1]


@needs_node
def test_build_dag_prereview_findings_reach_the_reviewer_first():
    pre_finding = {"criterion": "pre-review: wrapper fakes the collection size", "severity": "high", "evidence": "core.py:3", "fix": "drop it"}
    stubs = {"runs": {"prereview:WP01:r1": {"status": "ok", "reason": "2 finding(s)", "findings": 2}},
             "reviews": {"WP01": [review([pre_finding]), review([], "accept")]}}
    rc, out = run_workflow(BUILD_DAG, dict(DAG, preReview=True), stubs)
    assert rc == 0
    calls = out["calls"]
    assert calls.index("prereview:WP01:r1") < calls.index("review:WP01:r1")
    assert "prereview:WP01:r2" not in calls  # default rounds: 'first'
    assert out["required"]["prereview:WP01:r1"] == ["status", "findings"]
    pre_prompt = out["prompts"]["prereview:WP01:r1"]
    assert 'python -m jbr --prompts "ops/prompts" --reports "/runs/p/reports" --project "/p" prereview WP01 --tag r1 --effort low --budget-min 45 --detach' in pre_prompt
    assert 'If it does not print "spawned pid=", STOP' in pre_prompt  # a failed launch never waits 40 min
    hint = out["prompts"]["review:WP01:r1"]
    assert "Pre-review hints" in hint and "/runs/p/reports/prereview-WP01-r1.json" in hint and "UNVERIFIED" in hint
    assert "Pre-review hints" not in out["prompts"]["review:WP01:r2"]
    row = out["result"][0]
    assert row["final"] == "accept" and row["history"][1]["prereview"] == {"status": "ok", "findings": 2, "confirmed": 1}
    assert row["history"][1]["confirmed"] == 1  # it went through refutation like any other failure


@pytest.mark.parametrize("stub", [{"status": "timeout", "reason": "budget", "findings": 0}, None, "ok",
                                  {"status": "ok", "reason": "0 finding(s)", "findings": 0}])
@needs_node
def test_build_dag_prereview_failure_degrades_to_the_plain_review(stub):
    rc, out = run_workflow(BUILD_DAG, dict(DAG, preReview={"budgetMin": 12}), {"runs": {"prereview:WP01:r1": stub}})
    assert rc == 0 and out["result"][0]["final"] == "accept"
    assert "Pre-review hints" not in out["prompts"]["review:WP01:r1"]
    assert "--budget-min 12" in out["prompts"]["prereview:WP01:r1"]
    if not (isinstance(stub, dict) and stub["status"] == "ok"):
        assert any("reviewing without it" in m for m in out["logs"])


@needs_node
def test_build_dag_prereview_every_round_and_needs_jbr():
    stubs = {"runs": {"prereview:WP01:r1": {"status": "ok", "findings": 1}, "prereview:WP01:r2": {"status": "ok", "findings": 1}},
             "reviews": {"WP01": [review([{"criterion": "c", "severity": "high", "evidence": "e", "fix": "f"}]), review([], "accept")]}}
    rc, out = run_workflow(BUILD_DAG, dict(DAG, preReview={"rounds": "all"}), stubs)
    assert rc == 0 and "prereview:WP01:r2" in out["calls"] and "Pre-review hints" in out["prompts"]["review:WP01:r2"]
    rc, out = run_workflow(BUILD_DAG, {k: v for k, v in dict(DAG, preReview=True).items() if k != "jbr"})
    assert rc == 1 and "args.preReview needs args.jbr" in out["error"]


@needs_node
def test_review_level_prereview():
    args = {"root": "/p", "ids": ["WP01"], "reportsDir": "/runs/p/reports", "jbr": "/jbr", "preReview": True, "logTag": "q1"}
    rc, out = run_workflow(REVIEW_LEVEL, args, {"runs": {"prereview:WP01": {"status": "ok", "findings": 3}}})
    assert rc == 0 and out["calls"].index("prereview:WP01") < out["calls"].index("review:WP01")
    assert "prereview WP01 --tag pre-q1" in out["prompts"]["prereview:WP01"]
    assert "/runs/p/reports/prereview-WP01-pre-q1.json" in out["prompts"]["review:WP01"]
    assert out["result"][0]["prereview"] == {"status": "ok", "findings": 3, "confirmed": 0}
    rc, out = run_workflow(REVIEW_LEVEL, dict(args, preReview=False))
    assert rc == 0 and "prereview" not in out["result"][0] and "prereview:WP01" not in out["calls"]
    rc, out = run_workflow(REVIEW_LEVEL, {k: v for k, v in args.items() if k != "reportsDir"})
    assert rc == 1 and "args.reportsDir" in out["error"]


@pytest.mark.skipif(prereview.os.name == "nt", reason="POSIX process groups")
def test_run_group_kills_the_whole_tree_on_timeout(tmp_path):
    # a parent shell whose child outlives it unless the group is killed (the npm launcher -> Go binary case)
    marker = tmp_path / "child-alive"
    script = f"(sleep 1; touch '{marker}') & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        prereview.run_group(["/bin/sh", "-c", script], timeout=0.2, stdin=subprocess.DEVNULL)
    prereview.time.sleep(1.5)
    assert not marker.exists()
    assert prereview.run_group(["/bin/sh", "-c", "exit 3"], timeout=10).returncode == 3


@pytest.mark.skipif(prereview.os.name == "nt", reason="POSIX signals")
def test_run_group_sends_sigterm_first_so_ocr_can_publish(tmp_path):
    marker = tmp_path / "published"
    script = f"trap \"touch '{marker}'; exit 0\" TERM; sleep 5 & wait"
    with pytest.raises(subprocess.TimeoutExpired):
        prereview.run_group(["/bin/sh", "-c", script], timeout=0.3, grace=3, stdin=subprocess.DEVNULL)
    assert marker.exists()
