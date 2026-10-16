import json

import pytest

from jbr import models, runner, state

AGY_MODELS = """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


class FakeRun:
    def __init__(self, stdout=AGY_MODELS):
        self.calls, self.stdout = [], stdout

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return type("R", (), {"stdout": self.stdout, "stderr": "", "returncode": 0})()


def test_parse_skips_banner_and_keeps_ids():
    ids = models.parse_agy_models(AGY_MODELS)
    assert ids[0] == "gemini-3.8-flash-high" and "gpt-oss-120b-medium" in ids
    assert all(" " not in i for i in ids) and len(ids) == 8


def test_pick_highest_version_then_effort():
    ids = models.parse_agy_models(AGY_MODELS)
    assert models.pick(ids, "gemini") == "gemini-3.8-flash-high"
    assert models.pick(ids, "claude-opus") == "claude-opus-5-5-high"
    assert models.pick(ids + ["gemini-4-argon-medium"], "gemini") == "gemini-4-argon-medium"
    assert models.pick(ids + ["claude-opus-4-6-thinking"], "claude-opus") == "claude-opus-5-5-high"
    assert models.pick(ids, "gemini-pro") is None


def test_resolve_auto_caches_and_keeps_spec(tmp_path):
    run, cache = FakeRun(), tmp_path / "agy_models.json"
    eng = {"runner": "agy", "model": "auto:gemini", "effort": "high"}
    got = models.resolve(eng, run=run, path=cache, now=lambda: 1000.0)
    assert got["model"] == "gemini-3.8-flash-high" and got["model_spec"] == "auto:gemini"
    assert eng["model"] == "auto:gemini"  # input untouched
    models.resolve(eng, run=run, path=cache, now=lambda: 1000.0 + models.CACHE_TTL_S - 1)
    assert len(run.calls) == 1  # second call served from the cache
    models.resolve(eng, run=run, path=cache, now=lambda: 1000.0 + models.CACHE_TTL_S + 1)
    assert len(run.calls) == 2 and json.loads(cache.read_text())["models"][0] == "gemini-3.8-flash-high"


def test_resolve_passes_pinned_and_codex_through(tmp_path):
    run = FakeRun()
    pinned = {"runner": "agy", "model": "claude-opus-5-5-high"}
    codex = {"runner": "codex", "model": "auto:gpt"}
    assert models.resolve(pinned, run=run, path=tmp_path / "c.json") is pinned
    assert models.resolve(codex, run=run, path=tmp_path / "c.json") is codex
    assert run.calls == []


def test_resolve_unknown_family_raises(tmp_path):
    with pytest.raises(LookupError, match="gemini-pro"):
        models.resolve({"runner": "agy", "model": "auto:gemini-pro"}, run=FakeRun(), path=tmp_path / "c.json")


def test_auto_claude_shares_the_claude_bucket():
    assert state.bucket_of("agy_claude_opus", {"runner": "agy", "model": "auto:claude-opus"}) == state.CLAUDE_BUCKET
    assert state.bucket_of("agy_gemini_pro", {"runner": "agy", "model": "auto:gemini"}) == "agy_gemini_pro"


def test_commands_use_the_resolved_id(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "list_agy_models", lambda **kw: models.parse_agy_models(AGY_MODELS))
    eng = {"runner": "agy", "model": "auto:gemini", "effort": "high"}
    cmd = runner.build_command(eng, tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high" and "--effort" in cmd
    opus = runner.build_command({"runner": "agy", "model": "auto:claude-opus"}, tmp_path / "p.md", tmp_path,
                                tmp_path / "last.md")
    assert opus[opus.index("--model") + 1] == "claude-opus-5-5-high" and "--effort" not in opus
    probe = state.probe_command(eng)
    assert probe[probe.index("--model") + 1] == "gemini-3.8-flash-high"
