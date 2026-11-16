"""Behavioural tests of the Workflow scripts under node with stubbed `agent`/`parallel`/`pipeline`."""
import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")
HARNESS = REPO / "tests" / "workflow_harness.js"
BUILD_DAG = REPO / "workflows" / "build_dag.js"
REVIEW_LEVEL = REPO / "workflows" / "review_level.js"


def run_workflow(script, args, stubs=None):
    p = subprocess.run([NODE, str(HARNESS), str(script), json.dumps(args), json.dumps(stubs or {})],
                       capture_output=True, text=True, cwd=REPO)
    assert p.stdout.strip(), p.stderr
    return p.returncode, json.loads(p.stdout.strip().splitlines()[-1])


def test_node_check_both_workflows():
    for f in (BUILD_DAG, REVIEW_LEVEL):
        p = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr


def test_build_dag_rejects_unknown_dependency_at_startup():
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": [{"id": "WP02", "depends_on": ["WP01"]}]})
    assert rc == 1 and "unknown dependency WP01 of WP02" in out["error"]
    assert out["calls"] == []  # nothing started
    # same dependency listed as done: accepted
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": [{"id": "WP02", "depends_on": ["WP01"]}], "done": ["WP01"]})
    assert rc == 0 and [r["wp"] for r in out["result"]] == ["WP02"]


def test_build_dag_orders_by_dependencies_and_accepts():
    args = {"root": "C:\\p", "packages": [{"id": "WP01", "depends_on": []}, {"id": "WP02", "depends_on": ["WP01"]},
                                          {"id": "WP03", "depends_on": ["WP01", "WP02"]}], "maxFixRounds": 2}
    rc, out = run_workflow(BUILD_DAG, args)
    assert rc == 0
    calls = out["calls"]
    assert calls.index("review:WP01:r1") < calls.index("impl:WP02")
    assert calls.index("review:WP02:r1") < calls.index("impl:WP03")
    assert [(r["wp"], r["final"], r["rounds"]) for r in out["result"]] == [("WP01", "accept", 1), ("WP02", "accept", 1), ("WP03", "accept", 1)]
    assert not any(c.startswith("fix:") for c in calls)


def test_build_dag_fix_verdict_without_failures_stays_fix():
    rev = {"wp": "WP01", "pytest_summary": "2 failed, 3 passed", "test_count": 5, "passed_criteria": [], "failures": [],
           "mutation_check": "", "ownership_violations": [], "verdict": "fix"}
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": [{"id": "WP01", "depends_on": []}], "maxFixRounds": 1},
                           {"reviews": {"WP01": rev}})
    assert rc == 0
    row = out["result"][0]
    assert row["final"] == "fix" and "fix:WP01:r1" in out["calls"]
    assert row["unresolved"][0]["criterion"] == "reviewer verdict fix without failure entries"
    assert row["unresolved"][0]["evidence"] == "2 failed, 3 passed"
    # a refuted finding with verdict fix is still accepted (the refutation stands)
    rev2 = dict(rev, failures=[{"criterion": "c", "severity": "high", "evidence": "e", "fix": "f"}])
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": [{"id": "WP01", "depends_on": []}]},
                           {"reviews": {"WP01": rev2}, "refuted": True})
    assert rc == 0 and out["result"][0]["final"] == "accept"


def test_review_level_fix_verdict_without_failures_stays_fix():
    rev = {"wp": "WP01", "pytest_summary": "1 failed", "test_count": 1, "passed_criteria": [], "failures": [],
           "mutation_check": "", "ownership_violations": [], "verdict": "fix"}
    rc, out = run_workflow(REVIEW_LEVEL, {"root": "C:\\p", "ids": ["WP01", "WP02"]}, {"reviews": {"WP01": rev}})
    assert rc == 0
    rows = {r["wp"]: r for r in out["result"]}
    assert rows["WP01"]["verdict"] == "fix" and len(rows["WP01"]["confirmed_failures"]) == 1
    assert rows["WP01"]["confirmed_failures"][0]["evidence"] == "1 failed"
    assert rows["WP02"]["verdict"] == "accept" and rows["WP02"]["confirmed_failures"] == []


def test_review_level_requires_root_and_ids():
    rc, out = run_workflow(REVIEW_LEVEL, ["WP01"])
    assert rc == 1 and "args.root" in out["error"]
    rc, out = run_workflow(REVIEW_LEVEL, {"root": "C:\\p"})
    assert rc == 1 and "args.ids" in out["error"]


def _independent(n):
    return [{"id": f"WP{i:02d}", "depends_on": []} for i in range(1, n + 1)]


EXT = {"root": "/p", "jbr": "/jbr", "reportsDir": "/r"}


def test_build_dag_default_limits_match_engines_json():
    import re
    src = BUILD_DAG.read_text(encoding="utf-8")
    m = re.search(r"const DEFAULT_LIMITS = (\{[^}]*\})", src)
    assert m, "DEFAULT_LIMITS literal not found in build_dag.js"
    js = json.loads(re.sub(r"(\w+):", r'"\1":', m.group(1)))
    engines = json.loads((REPO / "engines.json").read_text(encoding="utf-8"))
    want = {k: v["parallel_limit"] for k, v in engines.items()
            if isinstance(v, dict) and v.get("parallel_limit") and v.get("runner") != "manual"}
    assert js == want


def test_build_dag_caps_runs_per_engine():
    # 5 independent packages pinned to agy_claude_opus (limit 1) and 5 to agy_gemini_flash (limit 5)
    pk = _independent(10)
    routing = {p["id"]: ("agy_claude_opus" if i < 5 else "agy_gemini_flash") for i, p in enumerate(pk)}
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing}, {"delayMs": 30})
    assert rc == 0, out
    assert out["peak"]["agy_claude_opus"] == 1
    assert out["peak"]["agy_gemini_flash"] == 5
    assert all(r["final"] == "accept" for r in out["result"])
    assert any("agy_claude_opus already has 1 run(s) going" in m for m in out["logs"])


def test_build_dag_engine_limits_arg_overrides_and_zero_is_unlimited():
    pk = _independent(4)
    routing = {p["id"]: "agy_gemini_pro" for p in pk}
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing, "engineLimits": {"agy_gemini_pro": 2}}, {"delayMs": 30})
    assert rc == 0 and out["peak"]["agy_gemini_pro"] == 2
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing, "engineLimits": {"agy_gemini_pro": 0}}, {"delayMs": 30})
    assert rc == 0 and out["peak"]["agy_gemini_pro"] == 4


def test_build_dag_quota_run_frees_its_slot_for_the_next_engine():
    # opus (limit 1) exhausted for WP01: its slot is released and WP02 still gets opus; WP01 falls back
    pk = _independent(2)
    routing = {p["id"]: "agy_claude_opus" for p in pk}
    quota = {"wp": "WP01", "engine_status": "quota", "reset_hint": "1h", "probe": "EXHAUSTED x", "pytest_summary": "",
             "files_created_or_changed": [], "unsatisfied": [], "notes": ""}
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing},
                           {"delayMs": 10, "runs": {"impl:WP01:agy_claude_opus": quota}})
    assert rc == 0
    assert "impl:WP02:agy_claude_opus" in out["calls"]
    assert out["peak"]["agy_claude_opus"] == 1
    assert [r["final"] for r in out["result"]] == ["accept", "accept"]


def test_build_dag_slot_waiters_start_in_arrival_order():
    pk = _independent(3)
    routing = {p["id"]: "agy_claude_opus" for p in pk}
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing}, {"delayMs": 20})
    assert rc == 0
    impls = [c for c in out["calls"] if c.startswith("impl:")]
    assert impls == ["impl:WP01:agy_claude_opus", "impl:WP02:agy_claude_opus", "impl:WP03:agy_claude_opus"]


def test_build_dag_fix_rounds_share_the_engine_cap():
    # WP01's review asks for a fix while WP02/WP03 still queue on opus (limit 1): never 2 opus runs at once
    pk = _independent(3)
    routing = {p["id"]: "agy_claude_opus" for p in pk}
    fail = {"criterion": "c", "severity": "high", "evidence": "e", "fix": "f"}
    rev_fix = {"wp": "WP01", "pytest_summary": "1 failed", "test_count": 5, "passed_criteria": [], "failures": [fail],
               "mutation_check": "", "ownership_violations": [], "verdict": "fix"}
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": pk, "routing": routing, "maxFixRounds": 1},
                           {"delayMs": 20, "reviews": {"WP01": [rev_fix]}, "refuted": False})
    assert rc == 0
    assert "fix:WP01:r1:agy_claude_opus" in out["calls"]
    assert out["peak"]["agy_claude_opus"] == 1


def test_build_dag_logs_engines_without_a_limit():
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": _independent(1), "routing": {"WP01": ["custom_eng", "claude_subagent"]}})
    assert rc == 0
    assert any("no parallel limit for custom_eng" in m for m in out["logs"])
    rc, out = run_workflow(BUILD_DAG, {**EXT, "packages": _independent(1), "routing": {"WP01": "agy_gemini_pro"}})
    assert not any("no parallel limit" in m for m in out["logs"])
