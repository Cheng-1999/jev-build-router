"""Manual engines (agy_manual): jbr writes the packet and records the outcome, never runs the engine.
No engine is executed anywhere here: a fake subprocess.run fails the test if anything is spawned."""
import json
import subprocess

import pytest

from jbr import cli, ledger, router, runner, state
from tests.conftest import REPO, reports
from tests.test_workflows import BUILD_DAG, NODE, run_workflow

needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@pytest.fixture
def all_engines():
    return router.load_engines(REPO / "engines.json")


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch, request):
    if "build_dag" in request.node.name:  # those run the workflow script under node themselves
        return
    def boom(*a, **k):
        raise AssertionError(f"an engine was executed: {a[0] if a else k}")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


def _pk(project):
    return project / "ops" / "work_packages.json"


# ----------------------------------------------------------------------------- handoff / done


def test_handoff_writes_packet_outside_project_and_runs_nothing(project, all_engines, monkeypatch):
    monkeypatch.setenv("JBR_RUNS", str(project.parent / (project.name + "-runs")))  # the fixture's runs dir is under tmp_path
    before = set(project.rglob("*"))
    res = runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), now=1000.0)
    assert set(project.rglob("*")) == before
    prompt = reports(project) / "run-WP01.prompt.md"
    assert res["paths"]["prompt"] == str(prompt)
    assert "work package WP01" in prompt.read_text(encoding="utf-8")
    assert "Locked decision" in prompt.read_text(encoding="utf-8")  # spec file inlined, as for `jbr run`
    assert str(prompt) in res["pointer"]
    h = runner.read_handoff(project, "WP01")
    assert h["engine"] == "agy_manual" and h["started"] == 1000.0
    assert runner.read_done(project, "WP01") is None  # pending until the user records it


def test_handoff_clears_a_stale_marker(project, all_engines):
    reports(project).mkdir(parents=True)
    (reports(project) / "run-WP01.done").write_text("0 3.0min OK\n", encoding="utf-8")
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    assert runner.read_done(project, "WP01") is None


def test_handoff_refuses_automatic_engine_and_unknown_package(project, all_engines):
    with pytest.raises(ValueError, match="not manual"):
        runner.handoff(project, "WP01", "agy_gemini_pro", all_engines, _pk(project))
    with pytest.raises(KeyError):
        runner.handoff(project, "WP99", "agy_manual", all_engines, _pk(project))
    assert not reports(project).exists() or not list(reports(project).glob("run-WP99*"))


def test_done_writes_marker_minutes_and_notes(project, all_engines):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), now=1000.0)
    res = runner.mark_done(project, "WP01", notes="12 passed in 0.4s\nfiles: src/demo/core.py", now=1000.0 + 25 * 60)
    assert res["minutes"] == 25.0 and res["status"] == "OK" and res["exit"] == 0
    assert runner.read_done(project, "WP01") == {"exit": 0, "minutes": 25.0, "status": "OK", "reset_hint": ""}
    assert runner.engine_status(runner.read_done(project, "WP01")) == "ok"
    log = (reports(project) / "run-WP01.log").read_text(encoding="utf-8")
    assert "agy_manual handoff WP01" in log and "12 passed in 0.4s" in log


def test_done_crash_status_and_tagged_fix_round(project, all_engines):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), extra="FIX ROUND 1: x", tag="fix1")
    assert "FIX ROUND 1: x" in (reports(project) / "run-WP01-fix1.prompt.md").read_text(encoding="utf-8")
    runner.mark_done(project, "WP01", "fix1", status="crash", minutes=3)
    d = runner.read_done(project, "WP01", "fix1")
    assert d["exit"] == 1 and d["status"] == "CRASH"


def test_done_without_handoff_is_refused(project):
    with pytest.raises(ValueError, match="no handoff for WP01"):
        runner.mark_done(project, "WP01")
    with pytest.raises(ValueError, match="status must be"):
        runner.mark_done(project, "WP01", status="maybe")


# ----------------------------------------------------------------------------- never automatic


def test_run_and_build_command_refuse_manual(project, all_engines):
    with pytest.raises(ValueError, match="manual engine"):
        runner.run(project, "WP01", "agy_manual", all_engines, _pk(project))
    assert runner.read_done(project, "WP01") is None  # no CRASH marker for a refused manual run
    with pytest.raises(ValueError, match="manual engine"):
        runner.build_command(all_engines["agy_manual"], project / "p.md", project, project / "l.md")


def test_probe_pick_and_route_never_choose_manual(project, all_engines):
    p = state.probe("agy_manual", all_engines)  # live=True, yet nothing runs (subprocess is booby-trapped)
    assert p.status == state.UNAVAILABLE and "manual" in p.detail
    only_manual = {k: dict(v, available=(k == "agy_manual")) for k, v in all_engines.items()}
    assert state.pick(only_manual, live=False)[0] == "claude_subagent"
    avail = state.availability(all_engines, live=False)
    assert "agy_manual" not in router.usable_engines(all_engines, avail)
    assert "agy_manual" not in router.usable_engines(all_engines, None)
    for pick in ("agy_gemini_pro", "codex_astra"):
        assert "agy_manual" not in router.chain_for(pick, all_engines, avail)
    body = router.build_request([{"id": "WP01", "depends_on": []}], all_engines, availability=avail)
    assert "agy_manual" not in body["questions"]["engine_WP01"]["criteria"]


def test_assign_accepts_manual_with_no_fallback(project, all_engines):
    avail = state.availability(all_engines, live=False)
    doc = router.assign(project, {"WP01": "agy_manual"}, all_engines, availability=avail, write=False)
    assert doc["chains"]["WP01"] == ["agy_manual"]
    off = router.load_engines(REPO / "engines.json", {"agy_manual": False})
    with pytest.raises(ValueError, match="disabled"):
        router.assign(project, {"WP01": "agy_manual"}, off, availability=avail, write=False)


# ----------------------------------------------------------------------------- CLI


def test_cli_handoff_done_status_round_trip(project, capsys):
    base = ["--project", str(project)]
    assert cli.main(base + ["handoff", "WP01"]) == 0  # default engine: agy_manual
    out = capsys.readouterr().out
    assert "agy_manual" in out and "Read that file in full" in out and "done WP01" in out
    assert cli.main(base + ["status"]) == 0
    assert "WP01: handed off to agy_manual: waiting for `jbr done`" in capsys.readouterr().out
    notes = project.parent / "agy-report.txt"
    notes.write_text("5 passed", encoding="utf-8")
    assert cli.main(base + ["done", "WP01", "--minutes", "7", "--notes-file", str(notes)]) == 0
    assert "OK exit=0 7.0min" in capsys.readouterr().out
    assert cli.main(base + ["wait", "WP01", "--timeout-min", "0"]) == 0
    assert "WP01: exit=0 7.0min OK" in capsys.readouterr().out


def test_cli_handoff_print_emits_the_packet(project, capsys):
    assert cli.main(["--project", str(project), "handoff", "WP02", "--print"]) == 0
    assert capsys.readouterr().out.startswith("You are the software engineer on work package WP02")


def test_cli_refuses_manual_in_run_spawn_queue(project, capsys):
    base = ["--project", str(project)]
    assert cli.main(base + ["run", "WP01", "--engine", "agy_manual"]) == 2
    assert cli.main(base + ["spawn", "WP01", "--engine", "agy_manual"]) == 2
    assert cli.main(base + ["queue", "add", "WP01", "--engine", "agy_manual"]) == 2
    assert capsys.readouterr().err.count("jbr never runs it") == 3
    assert cli.main(base + ["done", "WP03"]) == 2
    assert cli.main(base + ["handoff", "WP01", "--engine", "codex_astra"]) == 2


def test_hoist_keeps_done_options_as_values():
    argv = cli._hoist_globals(["done", "WP01", "--status", "crash", "--project", "/p", "--notes", "--project"])
    assert argv == ["--project", "/p", "done", "WP01", "--status", "crash", "--notes", "--project"]


# ----------------------------------------------------------------------------- ledger / build_dag


def test_ledger_skips_pending_manual_rows():
    rows = [{"wp": "WP01", "engine": "agy_manual", "final": "manual", "history": []},
            {"wp": "WP02", "engine": "codex_astra", "final": "waiting", "history": []},
            {"wp": "WP03", "engine": "codex_astra", "final": "accept", "history": []}]
    assert [e["wp"] for e in ledger.entries_from_build(rows)] == ["WP03"]


@needs_node
def test_build_dag_stops_at_manual_handoff_and_dependents_wait():
    args = {"root": "/p", "packages": [{"id": "WP01", "depends_on": []}, {"id": "WP02", "depends_on": ["WP01"]},
                                       {"id": "WP03", "depends_on": []}, {"id": "WP04", "depends_on": ["WP02"]}],
            "routing": {"WP01": "agy_manual", "WP03": "claude_subagent"}}  # no args.jbr: manual needs none
    rc, out = run_workflow(BUILD_DAG, args)
    assert rc == 0, out
    rows = {r["wp"]: r for r in out["result"]}
    assert rows["WP01"]["final"] == "manual" and rows["WP01"]["chain"] == ["agy_manual"]
    assert "handoff WP01 --engine agy_manual" in rows["WP01"]["handoff"]
    assert '--prompts "ops/prompts"' in rows["WP01"]["handoff"]
    assert rows["WP02"]["final"] == "waiting" and rows["WP02"]["waiting_on"] == ["WP01"]
    assert rows["WP04"]["final"] == "waiting" and rows["WP04"]["waiting_on"] == ["WP02"]
    assert rows["WP03"]["final"] == "accept"
    assert not any(":WP01" in c or ":WP02" in c or ":WP04" in c for c in out["calls"])  # nothing ran for them


@needs_node
def test_build_dag_drops_manual_from_automatic_chains():
    args = {"root": "/p", "jbr": "/jbr", "reportsDir": "/r", "packages": [{"id": "WP01", "depends_on": []}],
            "engineChain": ["agy_manual", "codex_astra", "claude_subagent"],
            "routing": {"WP01": ["codex_astra", "agy_manual"]}}
    rc, out = run_workflow(BUILD_DAG, args, {"runs": {"impl:WP01:codex_astra": {
        "wp": "WP01", "pytest_summary": "1 passed", "files_created_or_changed": [], "unsatisfied": [], "notes": "",
        "engine_status": "ok"}}})
    assert rc == 0, out
    assert out["result"][0]["chain"] == ["codex_astra", "claude_subagent"]
    rc, out = run_workflow(BUILD_DAG, dict(args, routing={}))
    assert out["result"][0]["chain"] == ["codex_astra", "claude_subagent"]


def test_done_twice_is_refused_unless_forced(project, all_engines):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    runner.mark_done(project, "WP01", minutes=1, notes="first")
    with pytest.raises(ValueError, match="already exists"):
        runner.mark_done(project, "WP01", minutes=2, notes="second")
    runner.mark_done(project, "WP01", status="CRASH", minutes=2, force=True)
    assert runner.read_done(project, "WP01")["status"] == "CRASH"
    with pytest.raises(ValueError, match=">= 0"):
        runner.mark_done(project, "WP01", minutes=-5, force=True)


def test_automatic_run_supersedes_a_handoff(project, all_engines):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    runner.spawn(project, "WP01", ["--engine", "codex_astra"], dry_run=True)
    assert runner.read_handoff(project, "WP01") is None
    with pytest.raises(ValueError, match="no handoff"):
        runner.mark_done(project, "WP01")


def test_cli_done_missing_notes_file_exits_cleanly(project, capsys):
    base = ["--project", str(project)]
    assert cli.main(base + ["handoff", "WP01"]) == 0
    assert cli.main(base + ["done", "WP01", "--notes-file", str(project / "nope.txt")]) == 2
    assert "[jbr done]" in capsys.readouterr().err
    assert runner.read_done(project, "WP01") is None


@needs_node
def test_build_dag_default_chain_has_no_automated_agy():
    args = {"root": "/p", "jbr": "/jbr", "reportsDir": "/r", "packages": [{"id": "WP01", "depends_on": []}]}
    ok = {"wp": "WP01", "pytest_summary": "1 passed", "files_created_or_changed": [], "unsatisfied": [], "notes": "", "engine_status": "ok"}
    rc, out = run_workflow(BUILD_DAG, args, {"runs": {"impl:WP01:codex_astra": ok}})
    assert rc == 0, out
    assert out["result"][0]["chain"] == ["codex_astra", "ollama_qwen", "lan_qwen", "claude_subagent"]
    assert not any("agy" in c for c in out["calls"])


def test_shipped_engines_keep_automated_agy_off(all_engines):
    assert not all_engines["agy_gemini_pro"]["available"] and not all_engines["agy_claude_opus"]["available"]
    assert state.probe("agy_gemini_pro", all_engines).line() == "UNAVAILABLE disabled (available=false / --available no)"
    assert not any(k.startswith("agy") for k in router.usable_engines(all_engines))


# ----------------------------------------------------------------------------- handoff --pending / done --from

AGY_OUTPUT = """agy> working on WP01 ...
=== REPORT WP01 ===
status: done
tests: 4 passed in 0.2s
files: src/demo/core.py, tests/test_core.py
unsatisfied: none
=== END WP01 ===
some chatter
=== REPORT WP02 ===
status: failed
tests: 1 failed
files: src/demo/ledger.py
unsatisfied: signature check
=== END WP02 ===
"""


def _route(project, mapping):
    from jbr import paths
    paths.routing_path(project).parent.mkdir(parents=True, exist_ok=True)
    paths.routing_path(project).write_text(json.dumps({"routing": {k: {"engine": v} for k, v in mapping.items()}}),
                                           encoding="utf-8")


def test_pending_collects_manual_packages_in_dependency_order(project, all_engines):
    _route(project, {"WP03": "agy_manual", "WP01": "agy_manual", "WP02": "agy_manual"})
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project), now=500.0)
    assert [(i["wp"], i["existing"]) for i in res["items"]] == [("WP01", False), ("WP02", False), ("WP03", False)]
    text = (reports(project) / "handoff-pending.md").read_text(encoding="utf-8")
    assert text.startswith("You have 3 work packages to do, ONE AT A TIME")
    assert text.index("1. WP01") < text.index("2. WP02") < text.index("3. WP03") and "(depends on WP01, WP02)" in text
    assert "REPORT ID: WP03" in text and str(reports(project) / "run-WP01.prompt.md") in text
    assert str(reports(project) / "handoff-pending.md") in res["pointer"]
    assert runner.read_handoff(project, "WP03")["started"] == 500.0


def test_pending_blocks_packages_whose_dependency_is_not_built(project, all_engines):
    _route(project, {"WP01": "agy_manual", "WP02": "codex_astra", "WP03": "agy_manual"})
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project))
    assert [i["wp"] for i in res["items"]] == ["WP01"]
    assert [(i["wp"], i["blocked_on"]) for i in res["blocked"]] == [("WP03", ["WP02"])]
    assert "NOT YET" in res["text"] and "- WP03: waits on WP02" in res["text"]
    assert runner.read_handoff(project, "WP03") is None  # no packet for a blocked package
    # WP02 built by codex (OK marker) or declared done -> WP03 is ready
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project), done={"WP02"})
    assert [i["wp"] for i in res["items"]] == ["WP01", "WP03"]
    reports(project).joinpath("run-WP02.done").write_text("0 4.0min OK\n", encoding="utf-8")
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project))
    assert [i["wp"] for i in res["items"]] == ["WP01", "WP03"] and not res["blocked"]


def test_pending_blocks_transitively_after_a_crash(project, all_engines):
    _route(project, {"WP01": "agy_manual", "WP02": "agy_manual", "WP03": "agy_manual"})
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    runner.mark_done(project, "WP01", status="CRASH", minutes=1)
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project))
    assert res["items"] == [] and [(i["wp"], i["blocked_on"]) for i in res["blocked"]] == [("WP02", ["WP01"]), ("WP03", ["WP01", "WP02"])]


def test_pending_reuses_open_handoffs_and_drops_finished_ones(project, all_engines):
    _route(project, {"WP01": "agy_manual", "WP03": "agy_manual"})
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), now=100.0)
    runner.handoff(project, "WP03", "agy_manual", all_engines, _pk(project), extra="FIX ROUND 1: x", tag="fix1", now=100.0)
    runner.mark_done(project, "WP01", minutes=1)
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project), now=900.0)
    # WP01 finished; WP03's open fix-round handoff kept as it is (no fresh round-0 packet while a fix is open)
    assert [(i["wp"], i["tag"], i["existing"]) for i in res["items"]] == [("WP03", "fix1", True)]
    assert res["blocked"] == []  # WP03 depends on unbuilt WP02, but a fix round works on code that exists
    assert runner.read_handoff(project, "WP03", "fix1")["started"] == 100.0
    assert "FIX ROUND 1: x" in (reports(project) / "run-WP03-fix1.prompt.md").read_text(encoding="utf-8")
    assert "1. WP03-fix1" in res["text"]


def test_pending_with_nothing_to_do_removes_the_checklist(project, all_engines):
    _route(project, {"WP01": "agy_manual"})
    runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project))
    runner.mark_done(project, "WP01", minutes=1)
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project))
    assert res["items"] == [] and not (reports(project) / "handoff-pending.md").exists()


def test_pending_inline_contains_every_packet(project, all_engines):
    _route(project, {"WP01": "agy_manual", "WP02": "agy_manual"})
    res = runner.handoff_pending(project, all_engines, _pk(project), routing=router.load_routing(project), inline=True)
    assert "######## PACKET WP01 ########" in res["text"] and "######## END PACKET WP02 ########" in res["text"]
    assert "work package WP02" in res["text"] and "packet: " not in res["text"]


def test_parse_reports_maps_status():
    r = runner.parse_reports(AGY_OUTPUT)
    assert r["WP01"]["status"] == "OK" and "4 passed" in r["WP01"]["notes"]
    assert r["WP02"]["status"] == "CRASH"
    assert runner.parse_reports("=== REPORT WP09 ===\nno status line\n=== END WP09 ===")["WP09"]["status"] == "CRASH"
    assert runner.parse_reports("=== REPORT WP09 ===\nstatus: done\n(no end marker)") == {}


def test_cli_pending_then_done_from_round_trip(project, capsys):
    base = ["--project", str(project)]
    assert cli.main(base + ["assign", "WP01=agy_manual", "WP02=agy_manual", "--no-probe"]) == 0
    capsys.readouterr()
    assert cli.main(base + ["handoff", "--pending"]) == 0
    out = capsys.readouterr().out
    assert "2 pending" in out and "1. WP01 on agy_manual  (new packet)" in out and "done --from" in out
    agy = project.parent / "agy-session.txt"
    agy.write_text(AGY_OUTPUT, encoding="utf-8")
    assert cli.main(base + ["done", "--from", str(agy), "--minutes", "12"]) == 0
    out = capsys.readouterr().out
    assert "WP01: agy_manual OK exit=0 12.0min" in out and "WP02: agy_manual CRASH exit=1 12.0min" in out
    assert "4 passed in 0.2s" in (reports(project) / "run-WP01.log").read_text(encoding="utf-8")
    assert cli.main(base + ["handoff", "--pending"]) == 0
    assert "nothing ready" in capsys.readouterr().out  # WP02 has an outcome (CRASH): re-hand it off explicitly


def test_cli_pending_and_done_argument_errors(project, capsys):
    base = ["--project", str(project)]
    assert cli.main(base + ["handoff"]) == 2
    assert cli.main(base + ["handoff", "WP01", "--pending"]) == 2
    assert cli.main(base + ["done"]) == 2
    assert cli.main(base + ["done", "WP01", "WP02", "--notes", "x"]) == 2
    empty = project.parent / "empty.txt"
    empty.write_text("no blocks here", encoding="utf-8")
    assert cli.main(base + ["done", "--from", str(empty)]) == 2
    assert cli.main(base + ["done", "--from", str(project / "missing.txt")]) == 2


def test_report_parsing_tolerates_markdown_and_tight_markers():
    r = runner.parse_reports("=== REPORT WP01===\nstatus: **done**\n=== END WP01===\n"
                             "== REPORT WP02 ==\nstatus: `done`\n== END WP02 ==")
    assert r["WP01"]["status"] == "OK" and r["WP02"]["status"] == "OK"


def test_done_from_maps_a_bare_id_to_its_open_fix_round(project, all_engines, capsys):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    runner.mark_done(project, "WP01", minutes=1)
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), tag="fix1", extra="fix it")
    out = project.parent / "agy.txt"
    out.write_text("=== REPORT WP01 ===\nstatus: done\ntests: 5 passed\n=== END WP01 ===\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "done", "--from", str(out), "--minutes", "3"]) == 0
    o = capsys.readouterr().out
    assert "WP01-fix1: agy_manual OK" in o and "args.logTag: 'fix1'" in o
    assert runner.read_done(project, "WP01")["minutes"] == 1.0  # round 0 untouched
    # nothing open any more: the same report is refused, never --force-suggested
    assert cli.main(["--project", str(project), "done", "--from", str(out)]) == 2
    err = capsys.readouterr().err
    assert "--force" not in err


def test_done_from_exact_tagged_id_and_filter(project, all_engines, capsys):
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project))
    runner.mark_done(project, "WP01", minutes=1)
    runner.handoff(project, "WP01", "agy_manual", all_engines, _pk(project), tag="fix1")
    out = project.parent / "agy.txt"
    out.write_text("=== REPORT WP01-fix1 ===\nstatus: done\n=== END WP01-fix1 ===\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "done", "--from", str(out), "WP01-fix1"]) == 0
    assert runner.read_done(project, "WP01", "fix1")["status"] == "OK"


def test_handoff_missing_packages_file_exits_cleanly(tmp_path, capsys):
    assert cli.main(["--project", str(tmp_path), "handoff", "--pending"]) == 2
    assert cli.main(["--project", str(tmp_path), "handoff", "WP01"]) == 2


def test_inbox_lists_open_checklists_across_projects(tmp_path, all_engines, capsys):
    import shutil
    from tests.conftest import PACKAGES
    projects = []
    for name in ("app-a", "app-b"):
        p = tmp_path / name
        (p / "ops" / "prompts").mkdir(parents=True)
        (p / "ops" / "work_packages.json").write_text(json.dumps(PACKAGES), encoding="utf-8")
        _route(p, {"WP01": "agy_manual"})
        runner.handoff_pending(p, all_engines, _pk(p), routing=router.load_routing(p))
        projects.append(p)
    runner.mark_done(projects[1], "WP01", minutes=1)  # app-b's checklist is finished: stale
    rows = {pathlib_name(r["project"]): r for r in runner.inbox()}
    assert rows["app-a"]["open"] == ["WP01"] and rows["app-b"]["open"] == []
    assert "handoff-pending.md" in rows["app-a"]["pointer"]
    assert cli.main(["inbox"]) == 0
    out = capsys.readouterr().out
    assert "app-a: 1 open (WP01)" in out and "app-b" not in out and "1 finished checklist not shown" in out
    shutil.rmtree(tmp_path / "jbr-runs")
    assert cli.main(["inbox"]) == 0 and "inbox empty" in capsys.readouterr().out


def pathlib_name(p):
    import pathlib
    return pathlib.Path(p).name
