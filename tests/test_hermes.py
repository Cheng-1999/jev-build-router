"""hermes_qwen: local Qwen driven by Hermes Agent. No real hermes or ollama call: subprocess is faked."""
import subprocess

import pytest

from jbr import router, runner, state
from tests.conftest import REPO


@pytest.fixture
def shipped():
    return router.load_engines(REPO / "engines.json")


def test_build_command_is_headless_and_reads_the_prompt_file(tmp_path, shipped):
    cmd = runner.build_command(shipped["hermes_qwen"], tmp_path / "p.md", tmp_path, tmp_path / "l.md", timeout="55m")
    assert cmd[:2] == ["hermes", "chat"]
    assert cmd[cmd.index("--query-file") + 1] == str(tmp_path / "p.md")  # nothing shell-interpreted
    assert {"--oneshot", "--quiet", "--yolo"} <= set(cmd)
    assert cmd[cmd.index("--in") + 1] == str(tmp_path)
    assert cmd[cmd.index("-m") + 1] == "qwen3.8:27b-mlx"
    assert cmd[cmd.index("--provider") + 1] == "custom"
    assert cmd[cmd.index("--base-url") + 1] == "http://localhost:11434/v1"
    assert cmd[cmd.index("--reasoning") + 1] == "high"
    assert cmd[cmd.index("--run-budget") + 1] == "3300"


def test_build_command_without_optional_fields(tmp_path):
    cmd = runner.build_command({"runner": "hermes", "model": "m", "effort": ""}, tmp_path / "p.md", tmp_path,
                               tmp_path / "l.md", timeout="bogus")
    assert "--provider" not in cmd and "--base-url" not in cmd and "--reasoning" not in cmd and "--run-budget" not in cmd


@pytest.mark.parametrize("text,secs", [("55m", 3300), ("1h30m", 5400), ("90s", 90), ("300", 300), ("", 0), ("soon", 0)])
def test_duration_seconds(text, secs):
    assert runner.duration_seconds(text) == secs


def test_probe_asks_ollama_directly_when_hermes_is_installed(shipped, monkeypatch):
    monkeypatch.setattr(state.shutil, "which", lambda name: "/usr/local/bin/hermes" if "hermes" in name else None)
    cmd = state.probe_command(shipped["hermes_qwen"])
    assert cmd[1:4] == ["run", "--think=false", "qwen3.8:27b-mlx"] and cmd[-1] == state.PROBE_PROMPT


def test_probe_without_hermes_is_unavailable(shipped, monkeypatch):
    monkeypatch.setattr(state.shutil, "which", lambda name: None)

    def missing(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(state.subprocess, "run", missing)
    p = state.probe("hermes_qwen", shipped)
    assert p.status == state.UNAVAILABLE and "cannot run hermes" in p.detail


def test_remote_hermes_probe_uses_hermes_with_one_turn(monkeypatch):
    monkeypatch.setattr(state.shutil, "which", lambda name: "/usr/local/bin/hermes")
    eng = {"runner": "hermes", "model": "nous/hermes-5", "provider": "nous"}
    cmd = state.probe_command(eng)
    assert cmd[1:3] == ["chat", "-q"] and cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--provider") + 1] == "nous" and "--yolo" not in cmd


def test_hermes_qwen_sits_in_the_local_part_of_the_chain(shipped):
    order = [k for k in router.by_priority(shipped) if not router.is_explicit_only(shipped[k])]
    assert order[-4:] == ["ollama_qwen", "lan_qwen", "hermes_qwen", "claude_subagent"]


def test_run_uses_the_engines_own_timeout_unless_one_is_given(project, shipped, monkeypatch):
    from tests.conftest import reports  # noqa: F401  (fixture module import keeps paths isolated)
    monkeypatch.setattr(state.shutil, "which", lambda name: f"/bin/{name}")
    res = runner.run(project, "WP01", "hermes_qwen", shipped, project / "ops" / "work_packages.json", dry_run=True)
    assert res["cmd"][res["cmd"].index("--run-budget") + 1] == "7800"  # engines.json timeout 130m, not 55m
    assert "--ignore-user-config" in res["cmd"]
    res = runner.run(project, "WP01", "hermes_qwen", shipped, project / "ops" / "work_packages.json", dry_run=True, timeout="20m")
    assert res["cmd"][res["cmd"].index("--run-budget") + 1] == "1200"


def test_spawn_passes_timeout_only_when_given(project, monkeypatch):
    from jbr import cli
    calls = []
    monkeypatch.setattr(runner, "launch_detached", lambda argv, cwd, out: calls.append(argv) or 1)
    assert cli.main(["--project", str(project), "spawn", "WP01", "--engine", "hermes_qwen"]) == 0
    assert "--timeout" not in calls[0]
    assert cli.main(["--project", str(project), "spawn", "WP01", "--engine", "hermes_qwen", "--timeout", "90m"]) == 0
    assert calls[1][calls[1].index("--timeout") + 1] == "90m"
