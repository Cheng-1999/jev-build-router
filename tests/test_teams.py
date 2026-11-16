"""Teams (`jbr/teams.py`), CLI (`teams`, `team-set`, `team-rm`, `quick --team`), and doc-mode review."""
from __future__ import annotations

import json

import pytest

from jbr import cli, router, runner, state, switches, teams
from tests.conftest import REPO
from tests.test_workflows import NODE, REVIEW_LEVEL, run_workflow

ENGINES_PATH = REPO / "engines.json"


@pytest.fixture
def all_engines():
    return router.load_engines(ENGINES_PATH, use_switches=False)


def test_teams_path_and_load_missing_malformed_nondict(tmp_path, monkeypatch, capsys):
    assert teams.teams_path().parent == state.state_path().parent
    assert teams.load() == {}
    assert capsys.readouterr().err == ""

    p = teams.teams_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    assert teams.load() == {}
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "warning" in err

    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert teams.load() == {}
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "warning" in err

    p.write_text(json.dumps({"teams": ["not", "a", "dict"]}), encoding="utf-8")
    assert teams.load() == {}
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "warning" in err

    # Non-dict entries inside teams or roles are dropped without crashing
    p.write_text(json.dumps({
        "teams": {
            "marketing": {
                "kind": "doc",
                "roles": {
                    "drafter": {"engines": ["claude_subagent"], "instructions": "draft"},
                    "junk_role": "not-a-dict",
                },
            },
            "junk_team": 42,
        }
    }), encoding="utf-8")
    loaded = teams.load()
    assert set(loaded) == {"marketing"}
    assert set(loaded["marketing"]["roles"]) == {"drafter"}

    custom = tmp_path / "custom_teams.json"
    monkeypatch.setenv("JBR_TEAMS", str(custom))
    assert teams.teams_path() == custom


def test_validate_catches_all_error_cases(all_engines):
    assert teams.validate(teams.example(), all_engines) == []

    # 1. Unknown engine
    errs = teams.validate({"t1": {"kind": "code", "roles": {"r1": {"engines": ["no_such_engine"]}}}}, all_engines)
    assert any("unknown engine 'no_such_engine'" in e for e in errs)

    # 2. Bad team or role name
    for bad in ("BadName", "-bad", "bad space", "a" * 41, ""):
        errs = teams.validate({bad: {"kind": "code", "roles": {"r1": {"engines": ["codex_astra"]}}}}, all_engines)
        assert any("invalid name" in e for e in errs)
        errs = teams.validate({"t1": {"kind": "code", "roles": {bad: {"engines": ["codex_astra"]}}}}, all_engines)
        assert any("invalid name" in e for e in errs)

    # 3. Empty engines list
    errs = teams.validate({"t1": {"kind": "code", "roles": {"r1": {"engines": []}}}}, all_engines)
    assert any("non-empty list" in e for e in errs)

    # 4. agy_manual and any is_manual engine refused
    errs = teams.validate({"t1": {"kind": "code", "roles": {"r1": {"engines": ["agy_manual"]}}}}, all_engines)
    assert any("manual engine 'agy_manual'" in e for e in errs)
    custom_engines = dict(all_engines, custom_manual={"runner": "manual", "available": True})
    errs = teams.validate({"t1": {"kind": "code", "roles": {"r1": {"engines": ["custom_manual"]}}}}, custom_engines)
    assert any("manual engine 'custom_manual'" in e for e in errs)

    # 5. Duplicate engine in a role list
    errs = teams.validate({"t1": {"kind": "code", "roles": {"r1": {"engines": ["codex_astra", "codex_astra"]}}}}, all_engines)
    assert any("duplicate engine 'codex_astra'" in e for e in errs)

    # 6. Bad kind
    errs = teams.validate({"t1": {"kind": "prose", "roles": {"r1": {"engines": ["codex_astra"]}}}}, all_engines)
    assert any("kind must be 'code' or 'doc'" in e for e in errs)


def test_set_role_and_remove_round_trip_preserves_role_order(tmp_path, capsys):
    # Role order: "writer" added first, then "editor" (alphabetically "editor" < "writer", but "writer" must stay first)
    teams.set_role("docs", "writer", ["agy_gemini_flash", "claude_subagent"], instructions="Write first", kind="doc", description="Docs team")
    teams.set_role("docs", "editor", ["agy_claude_opus", "claude_subagent"], instructions="Edit second")
    loaded = teams.load()
    assert list(loaded["docs"]["roles"]) == ["writer", "editor"]
    assert loaded["docs"]["kind"] == "doc" and loaded["docs"]["description"] == "Docs team"
    assert loaded["docs"]["roles"]["writer"]["instructions"] == "Write first"

    # Update writer engines without losing instructions or role order
    teams.set_role("docs", "writer", ["hermes_qwen", "claude_subagent"])
    loaded = teams.load()
    assert list(loaded["docs"]["roles"]) == ["writer", "editor"]
    assert loaded["docs"]["roles"]["writer"]["engines"] == ["hermes_qwen", "claude_subagent"]
    assert loaded["docs"]["roles"]["writer"]["instructions"] == "Write first"

    # CLI team-set with --instructions-file and validation failure
    inst_file = tmp_path / "inst.txt"
    inst_file.write_text("From file instructions", encoding="utf-8")
    assert cli.main(["team-set", "docs", "reviewer", "--engines", "codex_astra,claude_subagent", "--instructions-file", str(inst_file)]) == 0
    assert teams.load()["docs"]["roles"]["reviewer"]["instructions"] == "From file instructions"

    before_raw = teams.teams_path().read_text(encoding="utf-8")
    assert cli.main(["team-set", "docs", "bad_role", "--engines", "agy_manual"]) == 2
    assert teams.teams_path().read_text(encoding="utf-8") == before_raw  # nothing written on validation error

    # CLI teams & teams --json
    capsys.readouterr()
    assert cli.main(["teams"]) == 0
    out = capsys.readouterr().out
    assert "docs [doc] - Docs team" in out and "writer (default): pick=" in out
    assert cli.main(["teams", "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["docs"]["kind"] == "doc" and list(j["docs"]["roles"]) == ["writer", "editor", "reviewer"]

    # Remove single role, then whole team
    assert cli.main(["team-rm", "docs", "editor"]) == 0
    assert list(teams.load()["docs"]["roles"]) == ["writer", "reviewer"]
    assert cli.main(["team-rm", "docs"]) == 0
    assert teams.load() == {}
    assert cli.main(["team-rm", "docs"]) == 2


def test_teams_init_writes_example_and_refuses_to_overwrite(capsys):
    assert not teams.teams_path().exists()
    assert cli.main(["teams", "init"]) == 0
    assert teams.teams_path().exists()
    loaded = teams.load()
    assert set(loaded) == {"marketing", "engineering"}
    assert loaded["marketing"]["kind"] == "doc" and list(loaded["marketing"]["roles"]) == ["drafter", "editor"]
    assert loaded["engineering"]["kind"] == "code" and list(loaded["engineering"]["roles"]) == ["builder", "reviewer"]

    # Refuses to overwrite existing file
    teams.set_role("custom", "only", ["codex_astra"])
    capsys.readouterr()
    assert cli.main(["teams", "init"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert "custom" in teams.load()


def test_pick_for_role_order_switches_exclude_assign_only_and_subagent(monkeypatch):
    # Role list has hermes_qwen (prio 4) before agy_gemini_pro (prio 1), plus muse_spark (assign_only),
    # then claude_subagent, then codex_astra after claude_subagent.
    teams.set_role("eng", "custom", ["hermes_qwen", "agy_gemini_pro", "muse_spark", "claude_subagent", "codex_astra"])
    probed = []
    statuses = {"hermes_qwen": state.AVAILABLE, "agy_gemini_pro": state.AVAILABLE, "muse_spark": state.AVAILABLE, "codex_astra": state.AVAILABLE}

    def fake_probe(k, engs, **kw):
        probed.append(k)
        return state.Probe(k, statuses[k])

    monkeypatch.setattr(state, "probe", fake_probe)

    # 1. Role order beats priority: picks hermes_qwen first
    engs = router.load_engines(ENGINES_PATH)
    pick, seen = teams.pick_for_role("eng", "custom", engs, live=True)
    assert pick == "hermes_qwen" and probed == ["hermes_qwen"]

    # 2. Switch off hermes_qwen and exclude agy_gemini_pro -> picks muse_spark (assign_only is allowed!)
    probed.clear()
    switches.set_switch("hermes_qwen", False)
    engs = router.load_engines(ENGINES_PATH)
    pick, seen = teams.pick_for_role("eng", "custom", engs, exclude={"agy_gemini_pro"}, live=True)
    assert pick == "muse_spark" and probed == ["muse_spark"]

    # 3. muse_spark exhausted -> reaches claude_subagent and stops BEFORE codex_astra
    probed.clear()
    statuses["muse_spark"] = state.EXHAUSTED
    pick, seen = teams.pick_for_role("eng", "custom", engs, exclude={"agy_gemini_pro"}, live=True)
    assert pick == "claude_subagent"
    assert probed == ["muse_spark"] and "codex_astra" not in probed

    # 4. List without claude_subagent falls back to claude_subagent when exhausted
    teams.set_role("eng", "ext_only", ["codex_astra"])
    statuses["codex_astra"] = state.EXHAUSTED
    probed.clear()
    pick, seen = teams.pick_for_role("eng", "ext_only", engs, live=True)
    assert pick == "claude_subagent" and [p.engine for p in seen] == ["codex_astra"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_review_level_doc_mode_vs_code_mode():
    base_args = {"root": "/repo", "ids": ["Q1"], "promptsDir": "/runs/quick/Q1/prompts", "reportsDir": "/runs/reports"}
    rc1, out_default = run_workflow(REVIEW_LEVEL, base_args)
    rc2, out_code = run_workflow(REVIEW_LEVEL, dict(base_args, mode="code"))
    assert rc1 == 0 and rc2 == 0
    # Code mode output prompt must be byte-identical to default
    assert out_code["prompts"]["review:Q1"] == out_default["prompts"]["review:Q1"]

    # Doc mode: no test run, no mutation check, prose quality checks, ownership stays
    rc3, out_doc = run_workflow(REVIEW_LEVEL, dict(base_args, mode="doc"))
    assert rc3 == 0
    doc_prompt = out_doc["prompts"]["review:Q1"]
    assert 'set pytest_summary to "n/a (doc)", test_count to 0, and mutation_check to "n/a (doc)"' in doc_prompt
    assert "role instructions" in doc_prompt
    assert "factual claims are not invented" in doc_prompt
    assert "placeholder text" in doc_prompt
    assert "Ownership:" in doc_prompt
    assert "Mutation spot-check" not in doc_prompt
    row = out_doc["result"][0]
    assert row["pytest_summary"] == "n/a (doc)" and row["test_count"] == 0 and row["mutation_check"] == "n/a (doc)"


def test_teams_named_teams_and_roles_round_trip(all_engines):
    data = {"roles": {"kind": "code", "roles": {"r": {"engines": ["codex_astra"]}}},
            "teams": {"kind": "doc", "roles": {"roles": {"engines": ["agy_claude_opus"]}}}}
    assert teams.validate(data, all_engines) == []
    teams.save(data, engines=all_engines)
    back = teams.load()
    assert set(back) == {"roles", "teams"}
    assert back["roles"]["roles"]["r"]["engines"] == ["codex_astra"]
    assert back["teams"]["kind"] == "doc" and list(back["teams"]["roles"]) == ["roles"]
    teams.set_role("roles", "r2", "agy_gemini_pro", known_engines=all_engines)
    assert list(teams.load()["roles"]["roles"]) == ["r", "r2"]


def test_edits_refuse_a_malformed_teams_file_instead_of_wiping_it(all_engines, capsys):
    teams.save(teams.example(), engines=all_engines)
    p = teams.teams_path()
    broken = p.read_text(encoding="utf-8").rstrip().rstrip("}") + ",}}"  # trailing comma
    p.write_text(broken, encoding="utf-8")
    for call in (lambda: teams.set_role("sales", "writer", "codex_astra", known_engines=all_engines),
                 lambda: teams.remove("marketing"),
                 lambda: teams.save({"x": {"roles": {"r": {"engines": ["codex_astra"]}}}}, engines=all_engines)):
        with pytest.raises(ValueError, match="nothing was written"):
            call()
        assert p.read_text(encoding="utf-8") == broken
    assert cli.main(["team-set", "sales", "writer", "--engines", "codex_astra"]) == 2
    assert "nothing was written" in capsys.readouterr().err
    assert p.read_text(encoding="utf-8") == broken
    assert teams.load() == {}  # plain readers still degrade to {} with a warning


def test_quick_id_rerun_keeps_the_stored_kind(tmp_path, monkeypatch, capsys, all_engines):
    teams.save(teams.example(), engines=all_engines)
    monkeypatch.setattr(state, "probe", lambda k, engs, **kw: state.Probe(k, state.AVAILABLE))
    proj = tmp_path / "proj"
    proj.mkdir()
    assert cli.main(["--project", str(proj), "quick", "--team", "engineering", "add a flag", "--dry-run"]) == 0
    capsys.readouterr()
    assert cli.main(["--project", str(proj), "quick", "--id", "Q1", "--team", "marketing", "--dry-run"]) == 0
    out = capsys.readouterr().out
    review = json.loads(out.split("args=", 1)[1].splitlines()[0])
    assert "mode" not in review  # stored plan is code: no doc review even though --team names a doc team
