import json
import subprocess
import sys

import pytest

from jbr import cli, router
from tests.conftest import REPO


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for sub in ("route", "run", "spawn", "wait", "compose-fix", "status"):
        assert sub in out


def test_module_entrypoint_help():
    p = subprocess.run([sys.executable, "-m", "jbr", "--help"], cwd=REPO, capture_output=True, text=True)
    assert p.returncode == 0 and "Jev build router" in p.stdout


def test_route_dry_run_prints_questions_without_api(project, capsys, monkeypatch):
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API")))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rc = cli.main(["--project", str(project), "--available", "codex_astra=yes", "route", "--dry-run", "--done", "WP01", "--now", "04:15"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run] would POST" in out
    assert "'codex_astra'" in out and "'agy_claude_opus'" not in out
    assert "engine_WP02" in out and "engine_WP03" in out and "engine_WP01" not in out
    assert not (project / "ops" / "routing.json").exists()


def test_run_dry_run_uses_routing_json_when_no_engine(project, capsys, monkeypatch):
    routing = {"model": "x", "state": {}, "routing": {"WP01": {"engine": "codex_astra", "confidence": 1, "probabilities": {}}}}
    (project / "ops" / "routing.json").write_text(json.dumps(routing), encoding="utf-8")
    rc = cli.main(["--project", str(project), "run", "WP01", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "engine=codex_astra" in out and '"codex"' in out
    assert (project / "ops" / "reports" / "run-WP01.prompt.md").stat().st_size > 0


def test_run_without_routing_fails_clearly(project):
    with pytest.raises(SystemExit, match="ops/routing.json"):
        cli.main(["--project", str(project), "run", "WP01", "--dry-run"])


def test_spawn_dry_run_and_claude_subagent_skip(project, capsys):
    rc = cli.main(["--project", str(project), "spawn", "WP01", "WP02", "--engine", "agy_gemini_pro", "--tag", "fix1", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("[dry-run]") == 2 and "run-WP01-fix1.done" in out
    rc = cli.main(["--project", str(project), "spawn", "WP01", "--engine", "claude_subagent", "--dry-run"])
    assert "hand it to workflows/build_dag.js" in capsys.readouterr().out


def test_hoist_globals_moves_options_after_subcommand():
    argv = ["run", "WP01", "--project", "/p", "--engine", "x", "--engines", "e.json"]
    assert cli._hoist_globals(argv) == ["--project", "/p", "--engines", "e.json", "run", "WP01", "--engine", "x"]


def test_status_and_compose_fix_via_cli(project, capsys):
    (project / "ops" / "reports").mkdir(parents=True)
    src = project / "r.json"
    src.write_text(json.dumps([{"wp": "WP03", "verdict": "fix", "confirmed_failures": [
        {"criterion": "c", "severity": "medium", "evidence": "e", "fix": "f"}], "low_findings": []}]), encoding="utf-8")
    assert cli.main(["--project", str(project), "compose-fix", str(src), "2"]) == 0
    assert "WP03 fix 1 0" in capsys.readouterr().out
    assert (project / "ops" / "reports" / "fix-WP03-round2.md").exists()
    assert cli.main(["--project", str(project), "status"]) == 0
    assert "no runs found" in capsys.readouterr().out
