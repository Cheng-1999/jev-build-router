import json
import pathlib
from datetime import datetime, timezone

import pytest

from jbr import cli, ledger, paths, router
from tests.conftest import reports

REPO = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)

# The shape build_dag.js returns (trimmed from the 2026-10-06 clef build).
BUILD = [
    {"wp": "WP01", "engine": "agy_gemini_pro", "final": "accept", "rounds": 2,
     "history": [{"stage": "impl", "engines": [{"engine": "agy_gemini_pro", "status": "ok"}]},
                 {"stage": "review1", "verdict": "fix", "confirmed": 5, "low": 4},
                 {"stage": "fix1", "engines": [{"engine": "agy_gemini_pro", "status": "ok"}]},
                 {"stage": "review2", "verdict": "accept", "confirmed": 0, "low": 3}],
     "unresolved": [], "low_open": [{}, {}, {}]},
    {"wp": "WP03", "engine": "agy_claude_opus", "final": "accept", "rounds": 1,
     "history": [{"stage": "impl", "engines": [{"engine": "agy_claude_opus", "status": "quota"},
                                               {"engine": "codex_astra", "status": "ok"}]},
                 {"stage": "review1", "verdict": "accept", "confirmed": 0}],
     "unresolved": [], "low_open": []},
    {"wp": "WP10", "engine": "claude_subagent", "final": "blocked", "rounds": 0, "history": []},
]


@pytest.fixture
def project(tmp_path):
    rep = reports(tmp_path)
    rep.mkdir(parents=True)
    (rep / "run-WP01.done").write_text("0 6.2min OK\n")
    (rep / "run-WP01-fix1.done").write_text("0 3.1min OK\n")
    (rep / "run-WP10.done").write_text("0 99.0min OK\n")   # must not leak into WP1-anything
    return tmp_path


def test_entries_take_the_engine_that_actually_implemented(project):
    es = {e["wp"]: e for e in ledger.entries_from_build(BUILD, project=project, build="b", now=NOW,
                                                        hints={"WP01": {"size": "medium"}})}
    assert es["WP01"]["engine"] == "agy_gemini_pro"
    assert es["WP03"]["engine"] == "codex_astra"          # opus was quota-dead; codex did the work
    assert es["WP03"]["quota_hits"] == 1
    assert es["WP01"]["fix_rounds"] == 1 and es["WP01"]["confirmed"] == 5 and es["WP01"]["low_open"] == 3
    assert es["WP01"]["minutes"] == 9.3                   # impl + fix1 markers
    assert es["WP03"]["minutes"] is None                  # no markers: unknown, not zero
    assert es["WP10"]["final"] == "blocked"
    assert es["WP01"]["hints"] == {"size": "medium"} and es["WP01"]["project"] == project.name


def test_minutes_never_mix_prefix_ids(project):
    (reports(project) / "run-WP1.done").write_text("0 1.0min OK\n")
    e = ledger.entries_from_build([{"wp": "WP1", "final": "accept", "history": []}], project=project, now=NOW)[0]
    assert e["minutes"] == 1.0                            # not 1.0 + WP10's 99.0


def test_scorecard_aggregates_per_engine(project, tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger.append(ledger.entries_from_build(BUILD, project=project, now=NOW), path)
    with open(path, "a") as fh:
        fh.write('{"torn line\n')                         # a crashed append must not hide history
    card = ledger.scorecard(ledger.load(path))
    assert card["agy_gemini_pro"] == {"packages": 1, "accepted": 1, "failed": 0, "mean_fix_rounds": 1.0,
                                      "mean_confirmed_defects": 5.0, "mean_minutes": 9.3, "quota_hits": 0,
                                      "last": "2026-10-10T01:00:00Z", "kinds": []}
    assert card["codex_astra"]["quota_hits"] == 1
    assert card["claude_subagent"]["failed"] == 1
    assert "agy_gemini_pro" in ledger.format_scorecard(card)
    assert "empty" in ledger.format_scorecard({})


def _engines():
    return router.load_engines(REPO / "engines.json", {"ollama_qwen": False, "lan_qwen": False})


def test_assign_writes_routing_build_dag_can_read(tmp_path):
    avail = {"agy_gemini_pro": {"status": "AVAILABLE"}, "agy_claude_opus": {"status": "EXHAUSTED", "until": "x"},
             "codex_astra": {"status": "AVAILABLE"}, "claude_subagent": {"status": "AVAILABLE"}}
    doc = router.assign(tmp_path, {"WP01": "codex_astra", "WP02": "claude_subagent"}, _engines(),
                        {"WP01": "different family from the Claude reviewers"}, availability=avail)
    assert doc["model"] == "commander"
    assert doc["routing"]["WP01"]["chain"] == ["codex_astra", "agy_gemini_pro", "claude_subagent"]  # opus dead
    assert doc["routing"]["WP01"]["reason"].startswith("different family")
    assert doc["routing"]["WP02"]["chain"] == ["claude_subagent"]
    assert router.load_chains(tmp_path)["WP01"][0] == "codex_astra"
    assert router.load_routing(tmp_path) == {"WP01": "codex_astra", "WP02": "claude_subagent"}


def test_assign_refuses_dead_or_unknown_engines(tmp_path):
    avail = {"agy_claude_opus": {"status": "EXHAUSTED", "until": "2026-10-10T03:00:00Z"}}
    with pytest.raises(ValueError, match="not usable now .*EXHAUSTED until"):
        router.assign(tmp_path, {"WP01": "agy_claude_opus"}, _engines(), availability=avail)
    with pytest.raises(ValueError, match="not usable"):
        router.assign(tmp_path, {"WP01": "ollama_qwen"}, _engines())       # available=false, opt-in only
    with pytest.raises(ValueError, match="unknown engine"):
        router.assign(tmp_path, {"WP01": "gpt9"}, _engines())
    assert not paths.routing_path(tmp_path).exists()


def test_assign_records_second_opinion_without_applying_it(tmp_path):
    opinion = {"model": "clef-4bit", "routing": {"WP01": {"engine": "agy_gemini_pro", "confidence": 0.8}}}
    doc = router.assign(tmp_path, {"WP01": "codex_astra"}, _engines(), second_opinion=opinion, write=False)
    r = doc["routing"]["WP01"]
    assert r["engine"] == "codex_astra"
    assert r["second_opinion"] == {"engine": "agy_gemini_pro", "confidence": 0.8, "model": "clef-4bit"}


def test_cli_record_then_scorecard(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JBR_LEDGER", str(tmp_path / "l.jsonl"))
    res = tmp_path / "result.json"
    res.write_text(json.dumps(BUILD))
    assert cli.main(["--project", str(project), "record", str(res), "--build", "clef"]) == 0
    assert "appended 3" in capsys.readouterr().out
    assert cli.main(["--project", str(project), "scorecard", "--json"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["agy_gemini_pro"]["mean_minutes"] == 9.3


def test_cli_assign_rejects_packages_not_in_the_plan(tmp_path, capsys):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "work_packages.json").write_text(json.dumps({"work_packages": [{"id": "WP01", "title": "t"}]}))
    rc = cli.main(["--project", str(tmp_path), "assign", "WP09=codex_astra", "--no-probe", "--no-write"])
    assert rc == 2 and "WP09" in capsys.readouterr().err
