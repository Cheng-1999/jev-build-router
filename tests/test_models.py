import json
import subprocess

import pytest

from jbr import models, runner, state

# Real `agy models` output shape (2026-10-16, Mac), plus the retired Pro and a Flash-8B style id.
AGY_MODELS = """Fetching available models...
gemini-4-argon-high\tGemini 4 Argon (High)
gemini-4-argon-medium\tGemini 4 Argon (Medium)
gemini-4-argon-low\tGemini 4 Argon (Low)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-opus-5-5-medium\tClaude Opus 5.5 (Medium)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
claude-sonnet-5-5-low\tClaude Sonnet 5.5 (Low)
claude-sonnet-5-5-medium\tClaude Sonnet 5.5 (Medium)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


class FakeRun:
    def __init__(self, stdout=AGY_MODELS, returncode=0, raises=None):
        self.calls, self.stdout, self.returncode, self.raises = [], stdout, returncode, raises

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def ids():
    return models.parse_agy_models(AGY_MODELS)


def test_parse_skips_banner_and_prose():
    got = ids()
    assert got[0] == "gemini-4-argon-high" and "gpt-oss-120b-medium" in got and len(got) == 13
    assert models.parse_agy_models("too-many requests, retry later\nError: quota\n") == []


def test_pick_order_version_then_effort_then_id():
    assert models.pick(ids(), "gemini") == "gemini-4-argon-high"            # 4 > 3.8
    assert models.pick(ids(), "claude-opus") == "claude-opus-5-5-high"
    assert models.pick(ids(), "claude-opus", effort="medium") == "claude-opus-5-5-medium"  # engine effort wins
    assert models.pick(ids() + ["claude-opus-4-6-thinking"], "claude-opus") == "claude-opus-5-5-high"
    # only the leading version counts: -8b must not beat 3.1
    assert models.pick(["gemini-3-flash-8b-high", "gemini-3.1-pro-high"], "gemini") == "gemini-3.1-pro-high"
    # ties are broken by id, not listing order
    a = models.pick(["gemini-3.8-pro-high", "gemini-3.8-flash-high"], "gemini")
    assert a == models.pick(["gemini-3.8-flash-high", "gemini-3.8-pro-high"], "gemini") == "gemini-3.8-pro-high"
    assert models.pick(ids(), "gemini-pro") is None


def test_pick_rejects_ambiguous_family():
    with pytest.raises(LookupError, match="ambiguous"):
        models.pick(ids(), "claude")


def test_resolve_caches_and_keeps_spec(tmp_path):
    run, cache = FakeRun(), tmp_path / "agy_models.json"
    eng = {"runner": "agy", "model": "auto:gemini", "effort": "high"}
    got = models.resolve(eng, run=run, path=cache, now=lambda: 1000.0)
    assert got["model"] == "gemini-4-argon-high" and got["model_spec"] == "auto:gemini"
    assert eng["model"] == "auto:gemini"  # input untouched
    assert run.calls[0][1]["stdin"] is subprocess.DEVNULL and run.calls[0][1]["encoding"] == "utf-8"
    models.resolve(eng, run=run, path=cache, now=lambda: 1000.0 + models.CACHE_TTL_S - 1)
    assert len(run.calls) == 1  # served from the cache
    models.resolve(eng, run=run, path=cache, now=lambda: 1000.0 + models.CACHE_TTL_S + 1)
    assert len(run.calls) == 2 and json.loads(cache.read_text())["models"][0] == "gemini-4-argon-high"


def test_failed_listing_falls_back_to_stale_cache(tmp_path):
    cache = tmp_path / "agy_models.json"
    models.list_agy_models(run=FakeRun(), path=cache, now=lambda: 0.0)
    for bad in (FakeRun(returncode=1, stdout=""), FakeRun(raises=subprocess.TimeoutExpired(["agy"], 60)),
                FakeRun(raises=FileNotFoundError("agy"))):
        assert models.list_agy_models(run=bad, path=cache, now=lambda: 10 * models.CACHE_TTL_S)[0] == "gemini-4-argon-high"
    assert json.loads(cache.read_text())["at"] == 0.0  # failures never overwrite the cache


@pytest.mark.parametrize("bad", [FakeRun(returncode=1, stdout=""), FakeRun(stdout="Error: not logged in\n"),
                                 FakeRun(raises=subprocess.TimeoutExpired(["agy"], 60)),
                                 FakeRun(raises=FileNotFoundError("agy"))])
def test_no_cache_and_failed_listing_raises_lookup(tmp_path, bad):
    with pytest.raises(LookupError):
        models.resolve({"runner": "agy", "model": "auto:gemini"}, run=bad, path=tmp_path / "c.json")


@pytest.mark.parametrize("junk", ["[]", "null", '{"at": null, "models": ["x-1"]}', '{"at": 1, "models": [1]}', "{"])
def test_corrupt_cache_is_ignored(tmp_path, junk):
    cache = tmp_path / "c.json"
    cache.write_text(junk, encoding="utf-8")
    assert models.list_agy_models(run=FakeRun(), path=cache, now=lambda: 5.0)[0] == "gemini-4-argon-high"


def test_offline_reads_cache_only(tmp_path):
    run, eng = FakeRun(), {"runner": "agy", "model": "auto:gemini"}
    assert models.resolve(eng, offline=True, run=run, path=tmp_path / "c.json") is eng  # no cache: unresolved
    models.list_agy_models(run=run, path=tmp_path / "c.json", now=lambda: 0.0)
    got = models.resolve(eng, offline=True, run=run, path=tmp_path / "c.json", now=lambda: 1e9)
    assert got["model"] == "gemini-4-argon-high" and len(run.calls) == 1  # stale cache, no listing


def test_resolve_passes_pinned_and_rejects_auto_on_codex(tmp_path):
    run = FakeRun()
    pinned = {"runner": "agy", "model": "claude-opus-5-5-high"}
    assert models.resolve(pinned, run=run, path=tmp_path / "c.json") is pinned and run.calls == []
    with pytest.raises(ValueError, match="agy"):
        models.resolve({"runner": "codex", "model": "auto:gpt"}, run=run, path=tmp_path / "c.json")


def test_auto_claude_shares_the_claude_bucket():
    assert state.bucket_of("agy_claude_opus", {"runner": "agy", "model": "auto:claude-opus"}) == state.CLAUDE_BUCKET
    assert state.bucket_of("agy_gemini_pro", {"runner": "agy", "model": "auto:gemini"}) == "agy_gemini_pro"


def test_probe_reports_unresolvable_model_as_unavailable(monkeypatch):
    def boom(**kw):
        raise LookupError("agy lists no models (exit 1: not logged in)")
    monkeypatch.setattr(models, "list_agy_models", boom)
    engines = {"agy_gemini_pro": {"runner": "agy", "model": "auto:gemini", "priority": 1}}
    p = state.probe("agy_gemini_pro", engines, run=lambda *a, **k: pytest.fail("engine must not run"))
    assert p.status == state.UNAVAILABLE and "cannot resolve model" in p.detail
    again = state.probe("agy_gemini_pro", engines, run=lambda *a, **k: pytest.fail("cached"))
    assert again.status == state.EXHAUSTED and again.cached  # recorded: no second 60 s listing


def test_build_command_is_pure_and_probe_uses_resolved_id(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("build_command must not spawn"))
    eng = models.resolve({"runner": "agy", "model": "auto:gemini", "effort": "high"},
                         run=FakeRun(), path=tmp_path / "c.json")
    cmd = runner.build_command(eng, tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert cmd[cmd.index("--model") + 1] == "gemini-4-argon-high" and "--effort" in cmd
    opus = models.resolve({"runner": "agy", "model": "auto:claude-opus"}, run=FakeRun(), path=tmp_path / "c.json")
    ocmd = runner.build_command(opus, tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert ocmd[ocmd.index("--model") + 1] == "claude-opus-5-5-high" and "--effort" not in ocmd
    monkeypatch.setattr(models, "list_agy_models", lambda **kw: ids())
    probe = state.probe_command({"runner": "agy", "model": "auto:gemini"})
    assert probe[probe.index("--model") + 1] == "gemini-4-argon-high"


def test_tie_prefers_plain_id_over_variants():
    assert models.pick(["gemini-3.8-flash-lite-high", "gemini-3.8-flash-high"], "gemini") == "gemini-3.8-flash-high"
    assert models.pick(["gemini-4-argon-preview-high", "gemini-4-argon-high"], "gemini") == "gemini-4-argon-high"


def test_unparseable_exit_0_warns_and_uses_cache(tmp_path, capsys):
    cache = tmp_path / "c.json"
    models.list_agy_models(run=FakeRun(), path=cache, now=lambda: 0.0)
    got = models.list_agy_models(run=FakeRun(stdout="gemini-4-argon-high Gemini 4 Argon\n"), path=cache, now=lambda: 1e9)
    assert got[0] == "gemini-4-argon-high" and "format change" in capsys.readouterr().err


def test_unresolved_auto_claude_never_gets_effort(tmp_path):
    cmd = runner.build_command({"runner": "agy", "model": "auto:claude-opus", "effort": "high"},
                               tmp_path / "p.md", tmp_path, tmp_path / "last.md")
    assert "--effort" not in cmd  # cold-cache dry-run argv stays truthful
