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
