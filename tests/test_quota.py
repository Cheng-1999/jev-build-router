"""Quota / crash / ok classification of engine runs and the workflows' fallback behaviour.
Engine output is mocked (fake subprocess.run, stubbed workflow agents); nothing calls a real API."""
import json
import pathlib

import pytest

from jbr import cli, runner
from tests.test_workflows import BUILD_DAG, NODE, REVIEW_LEVEL, run_workflow

# verbatim shape of ops/reports/run-WP27.log from 2026-10-01 (agy, Gemini quota)
AGY_QUOTA_LOG = (
    "# agy_gemini_pro run WP27 model=gemini-3.1-pro-high effort=high\n\n"
    "error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h9m31s.\n"
    'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 2h9m31s.",'
    '"status":"RESOURCE_EXHAUSTED","error_code":429,"code_kind":"http","retryable":true}\n'
)
WORK_LOG = "# codex_astra run WP01 model=m effort=high\n\n" + "edited src/demo/core.py\n" * 80 + "12 passed in 0.4s\n"


# ----------------------------------------------------------------------------- classify_run


@pytest.mark.parametrize("exit_code,minutes,log,expected", [
    (3, 0.1, AGY_QUOTA_LOG, ("QUOTA", "2h9m31s")),
    (3, 12.0, AGY_QUOTA_LOG, ("QUOTA", "2h9m31s")),  # signature wins regardless of duration
    (1, 0.2, "# h\n\nYou've hit your usage limit. Please try again at 3:05 PM.\n", ("QUOTA", "3:05PM")),
    (0, 0.3, "# h\n\nok\n", ("QUOTA", "")),            # fast exit, no work
    (124, 55.0, "# h\n\n", ("QUOTA", "")),              # silent stall until --print-timeout
    (0, 6.0, WORK_LOG, ("OK", "")),
    (1, 0.4, WORK_LOG, ("OK", "")),                     # failed but did work: the review judges it
    (124, 55.0, WORK_LOG, ("OK", "")),                  # timed out mid-work: partial work is reviewed
    (2, 5.0, "# h\n\n", ("CRASH", "")),                 # non-zero, no output, not fast
    (-1, 0.0, AGY_QUOTA_LOG, ("CRASH", "")),            # jbr itself crashed
])
def test_classify_run(exit_code, minutes, log, expected):
    assert runner.classify_run(exit_code, minutes, log) == expected


def test_engine_status_reads_new_and_legacy_markers():
    assert runner.engine_status(None) is None
    assert runner.engine_status({"exit": 0, "minutes": 1.0}) == "ok"
    assert runner.engine_status({"exit": -1, "minutes": 0.0}) == "crash"
    assert runner.engine_status({"exit": 3, "minutes": 0.0, "status": "QUOTA", "reset_hint": "2h"}) == "quota"


# ----------------------------------------------------------------------------- run / wait / cli


def _fake_engine(monkeypatch, text, returncode):
    class P:
        pass

    def fake_run(cmd, cwd, stdout, stderr, env):
        stdout.write(text)
        p = P()
        p.returncode = returncode
        return p

    monkeypatch.setattr(runner.subprocess, "run", fake_run)


def test_run_quota_writes_quota_marker_with_reset_hint(project, engines, monkeypatch):
    _fake_engine(monkeypatch, AGY_QUOTA_LOG.split("\n\n", 1)[1], 3)
    res = runner.run(project, "WP01", "agy_gemini_pro", engines, project / "ops" / "work_packages.json")
    assert res["exit"] == 3 and res["status"] == "quota" and res["reset_hint"] == "2h9m31s"
    paths = runner.report_paths(project, "WP01")
    assert paths["done"].read_text(encoding="utf-8").split() == ["3", "0.0min", "QUOTA", "2h9m31s"]
    done = runner.read_done(project, "WP01")
    assert done == {"exit": 3, "minutes": 0.0, "status": "QUOTA", "reset_hint": "2h9m31s"}
    assert runner.engine_status(done) == "quota"


def test_run_with_real_work_writes_ok_marker(project, engines, monkeypatch):
    _fake_engine(monkeypatch, WORK_LOG, 0)
    res = runner.run(project, "WP01", "codex_astra", engines, project / "ops" / "work_packages.json", tag="fix1")
    assert res["status"] == "ok" and res["reset_hint"] == ""
    assert runner.report_paths(project, "WP01", "fix1")["done"].read_text(encoding="utf-8").split()[2:] == ["OK"]
    assert runner.engine_status(runner.read_done(project, "WP01", "fix1")) == "ok"


def test_run_engine_crash_without_output_writes_crash_marker(project, engines, monkeypatch):
    _fake_engine(monkeypatch, "", 2)
    clock = iter([0.0, 300.0])  # t0, then 5 minutes later: not a fast exit
    monkeypatch.setattr(runner.time, "time", lambda: next(clock))
    res = runner.run(project, "WP01", "codex_astra", engines, project / "ops" / "work_packages.json")
    assert res["status"] == "crash"
    assert runner.read_done(project, "WP01")["status"] == "CRASH"


def test_wait_and_cli_report_quota_status(project, capsys):
    rep = project / "ops" / "reports"
    rep.mkdir(parents=True)
    (rep / "run-WP01.done").write_text("3 0.0min QUOTA 2h9m31s\n", encoding="utf-8")
    (rep / "run-WP02.done").write_text("0 6.0min OK\n", encoding="utf-8")
    (rep / "run-WP03.done").write_text("-1 0.0min\n", encoding="utf-8")
    state = runner.wait(project, ["WP01", "WP02", "WP03"], timeout_s=0, poll_s=0, sleep=lambda s: None)
    assert {wp: runner.engine_status(d) for wp, d in state.items()} == {"WP01": "quota", "WP02": "ok", "WP03": "crash"}
    rc = cli.main(["--project", str(project), "wait", "WP01", "WP02", "WP03", "--timeout-min", "0"])
    out = capsys.readouterr().out
    assert rc == 3
    assert "WP01: exit=3 0.0min QUOTA resets=2h9m31s" in out
    assert "WP02: exit=0 6.0min OK" in out and "WP03: exit=-1 0.0min CRASH" in out


def test_compose_fix_writes_no_fix_file_for_blocked_rows(project):
    src = project / "review.json"
    src.write_text(json.dumps([{"wp": "WP02", "verdict": "blocked", "blocked_reason": "engine quota",
                                "confirmed_failures": [], "low_findings": []}]), encoding="utf-8")
    rows = runner.compose_fix(project, src, 1)
    assert rows == [{"wp": "WP02", "verdict": "blocked", "confirmed": 0, "low": 0, "fix_file": None}]
    assert not (project / "ops" / "reports" / "fix-WP02-round1.md").exists()


# ----------------------------------------------------------------------------- workflows

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")
EXT = {"root": "C:\\p", "jbr": "/c/jbr", "routing": {"WP01": "agy_gemini_pro"}}
FIX_REVIEW = {"wp": "WP01", "pytest_summary": "1 failed, 4 passed", "test_count": 5, "passed_criteria": [],
              "failures": [{"criterion": "sign()", "severity": "high", "evidence": "returns 0", "fix": "+1/-1"}],
              "mutation_check": "", "ownership_violations": [], "verdict": "fix"}
ACCEPT_REVIEW = dict(FIX_REVIEW, pytest_summary="5 passed", failures=[], verdict="accept")


def run_stub(wp, status, hint=""):
    return {"wp": wp, "pytest_summary": f"no engine work: {status}", "files_created_or_changed": [], "unsatisfied": [],
            "notes": "exit 3", "engine_status": status, "reset_hint": hint}


def reviews_of(calls, wp="WP01"):
    return [c for c in calls if c.startswith(f"review:{wp}:")]


@needs_node
def test_build_dag_ok_engine_run_uses_runner_schema_and_no_fallback():
    rc, out = run_workflow(BUILD_DAG, dict(EXT, packages=[{"id": "WP01", "depends_on": []}]),
                           {"runs": {"impl:WP01:agy_gemini_pro": run_stub("WP01", "ok")}})
    assert rc == 0
    assert "engine_status" in out["required"]["impl:WP01:agy_gemini_pro"]
    assert not any("fallback" in c for c in out["calls"])
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("accept", 1)
    assert row["history"][0]["engines"] == [{"engine": "agy_gemini_pro", "status": "ok", "reset_hint": ""}]


@needs_node
def test_build_dag_impl_quota_falls_back_before_any_review():
    rc, out = run_workflow(BUILD_DAG, dict(EXT, packages=[{"id": "WP01", "depends_on": []}]),
                           {"runs": {"impl:WP01:agy_gemini_pro": run_stub("WP01", "quota", "2h9m31s")}})
    assert rc == 0
    calls = out["calls"]
    assert calls[:3] == ["impl:WP01:agy_gemini_pro", "impl:WP01:fallback", "review:WP01:r1"]
    assert "engine_status" not in out["required"]["impl:WP01:fallback"]  # claude subagent: plain impl schema
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("accept", 1)
    assert [t["status"] for t in row["history"][0]["engines"]] == ["quota", "ok"]
    assert any("agy_gemini_pro -> quota (resets in 2h9m31s)" in m for m in out["logs"])
    assert any("falling back to claude_subagent" in m for m in out["logs"])


@needs_node
def test_build_dag_fix_quota_does_not_consume_the_fix_round():
    # maxFixRounds 1: before the fix, the dead agy run burned the only round and WP01 ended UNRESOLVED
    args = dict(EXT, packages=[{"id": "WP01", "depends_on": []}], maxFixRounds=1)
    rc, out = run_workflow(BUILD_DAG, args, {
        "reviews": {"WP01": [FIX_REVIEW, ACCEPT_REVIEW]},
        "runs": {"fix:WP01:r1:agy_gemini_pro": run_stub("WP01", "quota", "2h")}})
    assert rc == 0
    calls = out["calls"]
    i_dead, i_fb = calls.index("fix:WP01:r1:agy_gemini_pro"), calls.index("fix:WP01:r1:fallback")
    assert reviews_of(calls) == ["review:WP01:r1", "review:WP01:r2"]
    assert calls.index("review:WP01:r1") < i_dead < i_fb < calls.index("review:WP01:r2")  # no review of the dead run
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("accept", 2)
    assert [t["status"] for t in row["history"][2]["engines"]] == ["quota", "ok"]


@needs_node
def test_build_dag_quota_is_sticky_across_packages_but_crash_is_not():
    pk = [{"id": "WP01", "depends_on": []}, {"id": "WP02", "depends_on": ["WP01"]}]
    routing = {"WP01": "agy_gemini_pro", "WP02": "agy_gemini_pro"}
    rc, out = run_workflow(BUILD_DAG, dict(EXT, packages=pk, routing=routing),
                           {"runs": {"impl:WP01:agy_gemini_pro": run_stub("WP01", "quota")}})
    assert rc == 0
    assert "impl:WP02:agy_gemini_pro" not in out["calls"] and "impl:WP02:fallback" in out["calls"]
    assert any("WP02: implement: skipping agy_gemini_pro (quota exhausted" in m for m in out["logs"])
    assert [r["final"] for r in out["result"]] == ["accept", "accept"]
    # crash is package-local: WP02 still tries agy first
    rc, out = run_workflow(BUILD_DAG, dict(EXT, packages=pk, routing=routing),
                           {"runs": {"impl:WP01:agy_gemini_pro": run_stub("WP01", "crash")}})
    assert rc == 0
    assert "impl:WP01:fallback" in out["calls"] and "impl:WP02:agy_gemini_pro" in out["calls"]
    assert "impl:WP02:fallback" not in out["calls"]


@needs_node
def test_build_dag_blocked_when_fallback_engine_is_dead_too():
    args = dict(EXT, packages=[{"id": "WP01", "depends_on": []}], fallbackEngine="codex_astra")
    rc, out = run_workflow(BUILD_DAG, args, {"runs": {
        "impl:WP01:agy_gemini_pro": run_stub("WP01", "quota"),
        "impl:WP01:codex_astra:fallback": run_stub("WP01", "quota")}})
    assert rc == 0
    assert "engine_status" in out["required"]["impl:WP01:codex_astra:fallback"]  # external fallback: runner schema
    assert reviews_of(out["calls"]) == []
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("blocked", 0)
    assert row["blocked"]["stage"] == "impl"
    assert [(t["engine"], t["status"]) for t in row["blocked"]["tried"]] == [("agy_gemini_pro", "quota"), ("codex_astra", "quota")]


@needs_node
def test_build_dag_fix_round_blocked_keeps_last_review_and_stops():
    args = dict(EXT, packages=[{"id": "WP01", "depends_on": []}], fallbackEngine="codex_astra", maxFixRounds=2)
    rc, out = run_workflow(BUILD_DAG, args, {"reviews": {"WP01": [FIX_REVIEW]}, "runs": {
        "fix:WP01:r1:agy_gemini_pro": run_stub("WP01", "quota"),
        "fix:WP01:r1:codex_astra:fallback": run_stub("WP01", "crash")}})
    assert rc == 0
    assert reviews_of(out["calls"]) == ["review:WP01:r1"]  # neither dead run was reviewed
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("blocked", 1)
    assert row["unresolved"][0]["criterion"] == "sign()"  # last real review's findings stay open
    assert row["blocked"]["stage"] == "fix1"


@needs_node
def test_review_level_skips_packages_whose_engine_did_no_work():
    rc, out = run_workflow(REVIEW_LEVEL, {"root": "C:\\p", "ids": ["WP01", "WP02", "WP03"],
                                          "engineStatus": {"WP02": "QUOTA", "WP03": "ok"}})
    assert rc == 0
    assert "review:WP02" not in out["calls"] and {"review:WP01", "review:WP03"} <= set(out["calls"])
    rows = {r["wp"]: r for r in out["result"]}
    assert rows["WP02"]["verdict"] == "blocked" and rows["WP02"]["blocked_reason"] == "engine quota"
    assert rows["WP01"]["verdict"] == rows["WP03"]["verdict"] == "accept"
