import json
import os
import pathlib
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
    assert rc == 0
    assert "hand it to workflows/build_dag.js" in capsys.readouterr().out


def test_spawn_unknown_engine_exits_2_before_launching(project, capsys, monkeypatch):
    monkeypatch.setattr(cli.runner, "spawn", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")))
    rc = cli.main(["--project", str(project), "spawn", "WP01", "--engine", "bogus_engine"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown engine 'bogus_engine'" in err and "agy_gemini_pro" in err and "codex_astra" in err
    assert not (project / "ops" / "reports").exists()


def test_spawn_resolves_relative_engines_and_extra_file_to_absolute(project, capsys, monkeypatch):
    monkeypatch.chdir(project)
    (project / "ops" / "fix.md").write_text("fix it", encoding="utf-8")
    (project / "eng.json").write_text((REPO / "engines.json").read_text(encoding="utf-8"), encoding="utf-8")
    rc = cli.main(["--engines", "eng.json", "spawn", "WP01", "--engine", "codex_astra", "--extra-file", "ops/fix.md", "--dry-run"])
    assert rc == 0
    argv = json.loads(capsys.readouterr().out.split("[dry-run] ", 1)[1].split(" marker=")[0])
    assert argv[argv.index("--engines") + 1] == str((project / "eng.json").resolve())
    assert argv[argv.index("--extra-file") + 1] == str((project / "ops" / "fix.md").resolve())


def test_spawn_non_dry_launches_detached_child_with_pythonpath(project, capsys, monkeypatch):
    seen = {}

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            kw["stdout"].close()

    monkeypatch.setattr(cli.runner.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    rc = cli.main(["--project", str(project), "spawn", "WP01", "--engine", "agy_gemini_pro", "--tag", "t1"])
    assert rc == 0
    assert "WP01: spawned pid=4242" in capsys.readouterr().out
    argv, kw = seen["argv"], seen["kw"]
    assert argv[:5] == [sys.executable, "-m", "jbr", "run", "WP01"]
    assert argv[argv.index("--engine") + 1] == "agy_gemini_pro" and argv[argv.index("--tag") + 1] == "t1"
    env = kw["env"]
    assert env["PYTHONPATH"].split(os.pathsep) == [str(REPO), "/somewhere/else"]
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1"
    assert kw["cwd"] == project.resolve() and kw["stdin"] is subprocess.DEVNULL and kw["stderr"] is subprocess.STDOUT
    if os.name == "nt":
        assert kw["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        assert kw["start_new_session"] is True
    assert (project / "ops" / "reports" / "run-WP01-t1.stdout").exists()
    # the child's argv (globals after `run`) parses in the child exactly as spawn intended
    hoisted = cli._hoist_globals(argv[3:])
    a = cli._parser().parse_args(hoisted)
    assert a.cmd == "run" and a.wp_id == "WP01" and a.engine == "agy_gemini_pro" and a.tag == "t1"
    assert pathlib.Path(a.project) == project.resolve()


def test_hoist_globals_moves_options_after_subcommand():
    argv = ["run", "WP01", "--project", "/p", "--engine", "x", "--engines", "e.json"]
    assert cli._hoist_globals(argv) == ["--project", "/p", "--engines", "e.json", "run", "WP01", "--engine", "x"]
    # --available after the subcommand is hoisted too (spawn forwards overrides this way)
    assert cli._hoist_globals(["spawn", "WP01", "--available", "codex_astra=yes", "--engine", "x"]) == \
        ["--available", "codex_astra=yes", "spawn", "WP01", "--engine", "x"]


def test_hoist_globals_keeps_values_that_look_like_global_options():
    argv = ["run", "WP01", "--extra", "--project", "--engine", "x"]
    assert cli._hoist_globals(argv) == argv
    argv = ["run", "WP01", "--extra-file", "--reports", "--tag", "--packages", "--project", "/p"]
    assert cli._hoist_globals(argv) == ["--project", "/p", "run", "WP01", "--extra-file", "--reports", "--tag", "--packages"]


def test_available_after_subcommand_reaches_route(project, capsys, monkeypatch):
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API")))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    rc = cli.main(["route", "--dry-run", "--project", str(project), "--available", "codex_astra=yes", "--available", "agy_gemini_pro=no"])
    assert rc == 0
    criteria = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("[dry-run] criteria"))
    assert "'codex_astra'" in criteria and "'agy_gemini_pro'" not in criteria


def test_wait_returns_124_while_a_marker_is_missing(project, capsys):
    rep = project / "ops" / "reports"
    rep.mkdir(parents=True)
    (rep / "run-WP01.done").write_text("0 1.0min\n", encoding="utf-8")
    rc = cli.main(["--project", str(project), "wait", "WP01", "WP02", "--timeout-min", "0", "--poll", "0"])
    assert rc == 124
    out = capsys.readouterr().out
    assert "WP01: exit=0 1.0min" in out and "WP02: STILL-RUNNING" in out
    (rep / "run-WP02.done").write_text("3 2.5min\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "wait", "WP01", "WP02", "--timeout-min", "0"]) == 3
    (rep / "run-WP02.done").write_text("0 2.5min\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "wait", "WP01", "WP02", "--timeout-min", "0"]) == 0


def test_status_resolves_hyphenated_ids_against_packages(project, capsys):
    pk = json.loads((project / "ops" / "work_packages.json").read_text(encoding="utf-8"))
    pk["work_packages"].append({"id": "WP-X", "title": "hyphen", "goal": "", "depends_on": []})
    (project / "ops" / "work_packages.json").write_text(json.dumps(pk), encoding="utf-8")
    rep = project / "ops" / "reports"
    rep.mkdir(parents=True)
    (rep / "run-WP-X-fix1.prompt.md").write_text("p", encoding="utf-8")
    (rep / "run-WP-X-fix1.done").write_text("0 3.0min\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "status"]) == 0
    assert "WP-X-fix1: exit=0 3.0min" in capsys.readouterr().out


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
