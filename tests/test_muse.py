"""muse_spark (Meta Muse Code): assign-only engine. No real muse call: subprocess is faked or never reached."""
import subprocess

import pytest

from jbr import router, runner, state
from tests.conftest import REPO

BILLING = ("agent loop failed: model failed: API error 402 [request_id=x]: Billing verification failed. Billing Issue. "
           "To use this API key, your team needs to resolve a billing issue first. (billing_error)")


@pytest.fixture
def shipped():
    return router.load_engines(REPO / "engines.json")


def test_build_command_reads_prompt_file_and_keeps_sandbox(tmp_path, shipped):
    cmd = runner.build_command(shipped["muse_spark"], tmp_path / "p.md", tmp_path, tmp_path / "l.md")
    assert cmd[:4] == ["muse", "exec", "--model", "muse-spark-1.3-contributor"]
    assert cmd[cmd.index("--prompt-file") + 1] == str(tmp_path / "p.md")
    assert cmd[cmd.index("--workspace") + 1] == str(tmp_path)
    assert cmd[cmd.index("--contributor-mode") + 1] == "on" and cmd[cmd.index("--reasoning-effort") + 1] == "high"
    assert "--disable-approval" in cmd and "--no-foreign-personal-context" in cmd
    assert "--yolo" not in cmd and "--disable-sandbox" not in cmd


def test_probe_command_has_every_tool_off(shipped):
    cmd = state.probe_command(shipped["muse_spark"])
    assert cmd[1:4] == ["exec", "--model", "muse-spark-1.3-contributor"] and cmd[-1] == state.PROBE_PROMPT
    for flag in ("--disable-shell", "--disable-write", "--disable-web-tools", "--no-session-log", "--no-foreign-personal-context"):
        assert flag in cmd


def test_billing_error_is_unavailable_and_a_crash_not_quota(shipped, monkeypatch):
    monkeypatch.setattr(state.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, BILLING, ""))
    p = state.probe("muse_spark", shipped)
    assert p.status == state.UNAVAILABLE and "Billing verification failed" in p.detail
    assert runner.classify_run(1, 0.1, "# h\n\n" + BILLING) == ("CRASH", "")


def test_never_chosen_automatically(shipped, monkeypatch):
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "READY", ""))
    only = {k: dict(v, available=k in ("muse_spark", "claude_subagent")) for k, v in shipped.items()}
    assert state.pick(only)[0] == "claude_subagent" and calls == []
    avail = state.availability(shipped)  # live, yet muse is never called here
    assert not any("muse" in str(c[0]) for c in calls) and avail["muse_spark"]["status"] == state.UNKNOWN
    assert "muse_spark" not in router.usable_engines(shipped, avail)
    assert all("muse_spark" not in router.chain_for(k, shipped, avail) for k in ("codex_astra", "ollama_qwen"))
    body = router.build_request([{"id": "WP01", "depends_on": []}], shipped, availability=avail)
    assert "muse_spark" not in body["questions"]["engine_WP01"]["criteria"] and "muse_spark" not in body["state"]["engines"]


def test_explicit_assign_works_and_respects_live_status(tmp_path, shipped):
    avail = state.availability(shipped, live=False)
    doc = router.assign(tmp_path, {"WP01": "muse_spark"}, shipped, availability=avail, write=False)
    assert doc["chains"]["WP01"][0] == "muse_spark" and doc["chains"]["WP01"][-1] == "claude_subagent"
    dead = dict(avail, muse_spark={"status": "UNAVAILABLE"})
    with pytest.raises(ValueError, match="not usable"):
        router.assign(tmp_path, {"WP01": "muse_spark"}, shipped, availability=dead, write=False)
    off = router.load_engines(REPO / "engines.json", {"muse_spark": False})
    with pytest.raises(ValueError, match="not usable"):
        router.assign(tmp_path, {"WP01": "muse_spark"}, off, availability=avail, write=False)
