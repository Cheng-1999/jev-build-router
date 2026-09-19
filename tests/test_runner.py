import json
import pathlib

import pytest

from jbr import runner


def test_load_packages_accepts_wrapped_and_bare(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"work_packages": [{"id": "X"}], "rules": "r"}), encoding="utf-8")
    pk, meta = runner.load_packages(p)
    assert pk == [{"id": "X"}] and meta == {"rules": "r"}
    p.write_text(json.dumps([{"id": "Y"}]), encoding="utf-8")
    assert runner.load_packages(p) == ([{"id": "Y"}], {})
    with pytest.raises(KeyError):
        runner.find_package([{"id": "Y"}], "Z")


def test_compose_prompt_inlines_spec_rules_and_extra(project, packages):
    wp = runner.find_package(packages, "WP01")
    text = runner.compose_prompt(project, wp, "ops/prompts", extra="fix the sign", rules="Rules: demo")
    assert text.startswith("You are the software engineer on work package WP01: Foundation.")
    assert "Rules: demo" in text
    assert "- src/demo/core.py" in text
    assert "- tests/test_core.py::test_a" in text
    assert "Locked decision: Direction.sign() is +1/-1." in text  # spec inlined verbatim
    assert "ADDITIONAL INSTRUCTIONS FROM THE MANAGER\nfix the sign" in text
    # package without a spec file: no spec section, default rules
    text2 = runner.compose_prompt(project, runner.find_package(packages, "WP02"), "ops/prompts")
    assert "FULL PACKAGE SPEC" not in text2
    assert runner.DEFAULT_RULES in text2


def test_build_command_agy_claude_omits_effort(tmp_path, engines):
    cmd = runner.build_command(engines["agy_claude_opus"], tmp_path / "p.md", tmp_path, tmp_path / "last.md", "40m")
    assert cmd[:2] == ["agy", "-p"]
    assert str(tmp_path / "p.md") in cmd[2]  # file pointer, not the prompt body
    assert "--dangerously-skip-permissions" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6-thinking"
    assert cmd[cmd.index("--print-timeout") + 1] == "40m"
    assert "--effort" not in cmd


def test_build_command_agy_gemini_adds_effort(tmp_path, engines):
    cmd = runner.build_command(engines["agy_gemini_pro"], tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_build_command_codex_shape(tmp_path, engines):
    cmd = runner.build_command(engines["codex_astra"], tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert cmd[:5] == ["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check"]
    assert cmd[cmd.index("-m") + 1] == "gpt-6-astra"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=high"
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert cmd[cmd.index("-o") + 1] == str(tmp_path / "last.md")
    assert "Read that file in full" in cmd[-1]


def test_build_command_claude_subagent_is_none_and_unknown_runner_raises(tmp_path, engines):
    assert runner.build_command(engines["claude_subagent"], tmp_path / "p.md", tmp_path, tmp_path / "l.md") is None
    with pytest.raises(ValueError, match="unknown runner"):
        runner.build_command({"runner": "bogus", "model": "m"}, tmp_path / "p.md", tmp_path, tmp_path / "l.md")


def test_run_dry_run_writes_prompt_and_never_executes(project, engines, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess.run must not be called in dry-run")
    monkeypatch.setattr(runner.subprocess, "run", boom)
    res = runner.run(project, "WP01", "agy_gemini_pro", engines, project / "ops" / "work_packages.json",
                     "ops/prompts", extra="", tag="fix1", dry_run=True)
    prompt = pathlib.Path(res["paths"]["prompt"])
    assert prompt.name == "run-WP01-fix1.prompt.md" and prompt.stat().st_size > 0
    assert res["exit"] is None and res["cmd"][0] == "agy"
    assert "Rules: Python 3.12, src layout under src/demo" in prompt.read_text(encoding="utf-8")  # top-level rules
    assert not pathlib.Path(res["paths"]["done"]).exists()


def test_run_claude_subagent_hands_off_to_workflow(project, engines, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no exec")))
    res = runner.run(project, "WP02", "claude_subagent", engines, project / "ops" / "work_packages.json")
    assert res["exit"] == "workflow" and res["cmd"] is None
    assert "build_dag.js" in res["note"]


def test_run_executes_and_writes_done_marker(project, engines, monkeypatch):
    calls = {}

    class P:
        returncode = 3

    def fake_run(cmd, cwd, stdout, stderr, env):
        calls["cmd"] = cmd
        stdout.write("engine transcript\n")
        return P()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    res = runner.run(project, "WP01", "codex_astra", engines, project / "ops" / "work_packages.json")
    assert calls["cmd"][0] == "codex" and res["exit"] == 3
    done = runner.read_done(project, "WP01")
    assert done["exit"] == 3
    assert "engine transcript" in pathlib.Path(res["paths"]["log"]).read_text(encoding="utf-8")
    rows = runner.status(project)
    assert rows[0]["wp"] == "WP01" and rows[0]["done"]["exit"] == 3


def test_spawn_dry_run_argv(project):
    res = runner.spawn(project, "WP05", ["--engine", "agy_gemini_pro"], tag="fix2", dry_run=True)
    argv = res["argv"]
    assert argv[1:5] == ["-m", "jbr", "run", "WP05"]
    assert argv[argv.index("--project") + 1] == str(project.resolve())
    assert argv[argv.index("--tag") + 1] == "fix2"
    assert res["marker"].endswith("run-WP05-fix2.done")


def test_wait_polls_until_markers_exist(project):
    rep = project / "ops" / "reports"
    rep.mkdir(parents=True)
    ticks = []

    def sleep(s):
        ticks.append(s)
        (rep / "run-WP01.done").write_text("0 4.2min\n", encoding="utf-8")

    state = runner.wait(project, ["WP01"], timeout_s=10, poll_s=1, sleep=sleep)
    assert ticks == [1]
    assert state["WP01"] == {"exit": 0, "minutes": 4.2}
    # timeout path: marker never appears
    state = runner.wait(project, ["WP09"], timeout_s=0, poll_s=1, sleep=lambda s: None)
    assert state["WP09"] is None


def test_compose_fix_writes_fix_file_only_for_fix_verdicts(project):
    review = [
        {"wp": "WP01", "verdict": "fix",
         "confirmed_failures": [{"criterion": "sign()", "severity": "high", "evidence": "returns 0", "fix": "return +1/-1"}],
         "low_findings": [{"criterion": "docstring", "severity": "low", "evidence": "missing", "fix": "add"}],
         "ownership_violations": ["tests/test_other.py"], "mutation_check": "flipped sign: NOT caught"},
        {"wp": "WP02", "verdict": "accept", "confirmed_failures": [], "low_findings": []},
    ]
    src = project / "review.json"
    src.write_text(json.dumps({"result": review}), encoding="utf-8")
    rows = runner.compose_fix(project, src, 1)
    assert [(r["wp"], r["verdict"], r["confirmed"], r["low"]) for r in rows] == [("WP01", "fix", 1, 1), ("WP02", "accept", 0, 0)]
    fix = (project / "ops" / "reports" / "fix-WP01-round1.md").read_text(encoding="utf-8")
    assert fix.startswith("FIX ROUND 1.")
    assert "1. [high] sign()" in fix and "2. [low] docstring" in fix
    assert "- tests/test_other.py" in fix and "NOT caught" in fix
    assert not (project / "ops" / "reports" / "fix-WP02-round1.md").exists()
    assert (project / "ops" / "reports" / "review-WP02-round1.json").exists()
