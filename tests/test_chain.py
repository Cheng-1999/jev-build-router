"""Engine priority chain: reset-hint parsing, the shared state file, `jbr probe` / `pick`, runner QUOTA
-> state, chain-aware Jev routing, and build_dag.js chain walking. No network, no real engine: every
live probe goes through a fake `run`, TypeSafe through a mocked urlopen, workflows through stub agents."""
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from jbr import cli, router, runner, state
from tests.conftest import REPO
from tests.test_quota import AGY_QUOTA_LOG, _fake_engine, run_stub
from tests.test_workflows import BUILD_DAG, NODE, run_workflow

NOW = datetime(2026, 10, 2, 1, 0, 0, tzinfo=timezone.utc)
TPE = timezone(timedelta(hours=8))  # the user's machine (Asia/Taipei)
CODEX_QUOTA = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
               "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 3:51 AM.")
CODEX_QUOTA_DATED = "You've hit your usage limit. ... or try again at Sep 25th, 2026 3:49 AM."
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


@pytest.fixture
def all_engines():
    """engines.json as shipped: all four engines available, live probes decide."""
    return router.load_engines(REPO / "engines.json")


class FakeRun:
    """Stands in for subprocess.run in probes: replies per engine model, records every call."""

    def __init__(self, replies):
        self.replies = replies  # model -> (returncode, output) | Exception
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        model = cmd[cmd.index("--model") + 1] if "--model" in cmd else cmd[cmd.index("-m") + 1]
        r = self.replies[model]
        if isinstance(r, BaseException):
            raise r
        return subprocess.CompletedProcess(cmd, r[0], r[1], "")


# ----------------------------------------------------------------------------- reset hints


@pytest.mark.parametrize("hint,expected", [
    ("2h9m31s", NOW + timedelta(hours=2, minutes=9, seconds=31)),
    ("27m28s", NOW + timedelta(minutes=27, seconds=28)),
    ("Resets in 4h53m49s", NOW + timedelta(hours=4, minutes=53, seconds=49)),
    # 01:00 UTC = 09:00 Taipei: 3:05 PM today Taipei = 07:05 UTC
    ("3:05 PM", datetime(2026, 10, 2, 7, 5, tzinfo=timezone.utc)),
    ("3:05PM", datetime(2026, 10, 2, 7, 5, tzinfo=timezone.utc)),     # space-stripped .done form
    # 3:51 AM Taipei already passed today (it is 09:00) -> tomorrow 03:51 Taipei = Oct 2 19:51 UTC
    ("3:51 AM", datetime(2026, 10, 2, 19, 51, tzinfo=timezone.utc)),
    ("Sep 25th, 2026 3:49 AM", datetime(2026, 9, 24, 19, 49, tzinfo=timezone.utc)),
    ("Sep25th,20263:49AM", datetime(2026, 9, 24, 19, 49, tzinfo=timezone.utc)),
    ("in 5 minutes", NOW + timedelta(minutes=5)),
    ("", NOW + timedelta(minutes=30)),
    ("soonish", NOW + timedelta(minutes=30)),
    (None, NOW + timedelta(minutes=30)),
])
def test_parse_reset_hint_formats(hint, expected):
    assert state.parse_reset_hint(hint, NOW, TPE) == expected


def test_reset_hints_from_real_transcripts_round_trip():
    for text, until in [(AGY_QUOTA_LOG, NOW + timedelta(hours=2, minutes=9, seconds=31)),
                        (CODEX_QUOTA, datetime(2026, 10, 2, 19, 51, tzinfo=timezone.utc)),
                        (CODEX_QUOTA_DATED, datetime(2026, 9, 24, 19, 49, tzinfo=timezone.utc))]:
        status, hint = runner.classify_run(1, 0.1, "# h\n\n" + text)
        assert status == "QUOTA"
        assert state.parse_reset_hint(hint, NOW, TPE) == until  # the space-stripped .done hint
        assert state.parse_reset_hint(state.reset_hint_of(text), NOW, TPE) == until  # the raw probe hint


# ----------------------------------------------------------------------------- probe


def test_probe_commands_use_the_real_cli_flags(all_engines, monkeypatch):
    monkeypatch.setenv("JBR_AGY_BIN", "agy")
    monkeypatch.setenv("JBR_CODEX_BIN", "codex")
    assert state.probe_command(all_engines["agy_gemini_pro"]) == [
        "agy", "--model", "gemini-3.1-pro-high", "--print-timeout", "45s", "--print", "Reply only READY."]
    codex = state.probe_command(all_engines["codex_astra"])
    assert codex == ["codex", "exec", "--skip-git-repo-check", "-m", "gpt-6-astra", "-s", "workspace-write",
                     "Reply only READY. Do not run tools."]
    assert "--full-auto" not in codex
    assert state.probe_command(all_engines["claude_subagent"]) is None
    run_cmd = runner.build_command(all_engines["codex_astra"], REPO / "p.md", REPO, REPO / "l.md")
    assert "--full-auto" not in run_cmd and run_cmd[run_cmd.index("-s") + 1] == "workspace-write"


def test_probe_ready_is_cached_for_ten_minutes(all_engines):
    fake = FakeRun({"gemini-3.1-pro-high": (0, "READY\n")})
    r = state.probe("agy_gemini_pro", all_engines, now=NOW, run=fake)
    assert (r.status, r.exit_code, r.line(), r.cached) == ("AVAILABLE", 0, "AVAILABLE", False)
    rec = state.load()["agy_gemini_pro"]
    assert rec["last_status"] == "AVAILABLE" and rec["exhausted_until"] is None and rec["last_probe"] == "2026-10-02T01:00:00Z"
    r = state.probe("agy_gemini_pro", all_engines, now=NOW + timedelta(minutes=9, seconds=59), run=fake)
    assert r.status == "AVAILABLE" and r.cached and len(fake.calls) == 1  # inside the window: no live call
    r = state.probe("agy_gemini_pro", all_engines, now=NOW + timedelta(minutes=10, seconds=1), run=fake)
    assert r.status == "AVAILABLE" and not r.cached and len(fake.calls) == 2  # expired: live again


def test_probe_quota_sets_exhausted_until_then_recovers_after_reset(all_engines, monkeypatch):
    monkeypatch.setattr(state, "local_tz", lambda: TPE)
    fake = FakeRun({"gpt-6-astra": (1, CODEX_QUOTA)})
    r = state.probe("codex_astra", all_engines, now=NOW, run=fake)
    assert (r.status, r.exit_code) == ("EXHAUSTED", 3)
    assert r.line().startswith("EXHAUSTED 2026-10-02T19:51:00Z")
    assert state.load()["codex_astra"]["exhausted_until"] == "2026-10-02T19:51:00Z"
    # inside the window: answered from the state file, engine never called
    r = state.probe("codex_astra", all_engines, now=NOW + timedelta(hours=5), run=fake)
    assert r.status == "EXHAUSTED" and r.cached and len(fake.calls) == 1
    # after the reset: live probe, AVAILABLE again, exhausted cleared
    fake.replies["gpt-6-astra"] = (0, "codex\nuser\nReply only READY. Do not run tools.\ncodex\nREADY\ntokens used 12\n")
    r = state.probe("codex_astra", all_engines, now=NOW + timedelta(hours=19), run=fake)
    assert r.status == "AVAILABLE" and len(fake.calls) == 2
    assert state.load()["codex_astra"]["exhausted_until"] is None


def test_probe_echoed_prompt_alone_is_not_ready(all_engines):
    fake = FakeRun({"gpt-6-astra": (1, "user\nReply only READY. Do not run tools.\nerror: model not found\n")})
    r = state.probe("codex_astra", all_engines, now=NOW, run=fake)
    assert r.status == "UNAVAILABLE" and "model not found" in r.detail


def test_probe_other_failure_is_unavailable_for_15_minutes(all_engines):
    fake = FakeRun({"gemini-3.1-pro-high": (2, "agy: internal error\n")})
    r = state.probe("agy_gemini_pro", all_engines, now=NOW, run=fake)
    assert (r.status, r.exit_code) == ("UNAVAILABLE", 4)
    assert r.line() == "UNAVAILABLE exit 2: agy: internal error"
    rec = state.load()["agy_gemini_pro"]
    assert rec["last_status"] == "UNAVAILABLE" and rec["exhausted_until"] == "2026-10-02T01:15:00Z"
    r = state.probe("agy_gemini_pro", all_engines, now=NOW + timedelta(minutes=14), run=fake)
    assert r.status == "EXHAUSTED" and "after UNAVAILABLE" in r.detail and len(fake.calls) == 1
    fake.replies["gemini-3.1-pro-high"] = (0, "READY")
    assert state.probe("agy_gemini_pro", all_engines, now=NOW + timedelta(minutes=16), run=fake).status == "AVAILABLE"


def test_probe_timeout_and_missing_binary_are_unavailable(all_engines):
    fake = FakeRun({"gemini-3.1-pro-high": subprocess.TimeoutExpired(["agy"], 120),
                    "gpt-6-astra": FileNotFoundError("codex")})
    r = state.probe("agy_gemini_pro", all_engines, now=NOW, run=fake)
    assert r.status == "UNAVAILABLE" and "timed out after 120s" in r.detail
    r = state.probe("codex_astra", all_engines, now=NOW, run=fake)
    assert r.status == "UNAVAILABLE" and "cannot run" in r.detail


def test_claude_subagent_always_available_never_probed(all_engines):
    fake = FakeRun({})
    r = state.probe("claude_subagent", all_engines, now=NOW, run=fake)
    assert r.status == "AVAILABLE" and fake.calls == [] and state.load() == {}


def test_agy_claude_engines_share_one_quota_bucket(all_engines):
    engines = dict(all_engines, agy_claude_sonnet={"runner": "agy", "model": "claude-sonnet-4-6", "priority": 2})
    state.mark_exhausted("agy_claude_opus", engines["agy_claude_opus"], "1h", now=NOW)
    assert set(state.load()) == {"agy_claude"}
    fake = FakeRun({"claude-sonnet-4-6": (0, "READY")})
    r = state.probe("agy_claude_sonnet", engines, now=NOW + timedelta(minutes=5), run=fake)
    assert r.status == "EXHAUSTED" and fake.calls == []  # Sonnet is out too: same Claude bucket


def test_probe_without_live_reports_unknown(all_engines):
    fake = FakeRun({})
    r = state.probe("agy_gemini_pro", all_engines, now=NOW, run=fake, live=False)
    assert r.status == "UNKNOWN" and r.usable and fake.calls == []


def test_cli_probe_exit_codes_and_no_engine_call_while_exhausted(all_engines, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(state.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or
                        subprocess.CompletedProcess(cmd, 0, "READY\n", ""))
    assert cli.main(["probe", "agy_gemini_pro"]) == 0
    assert capsys.readouterr().out.strip() == "AVAILABLE" and len(calls) == 1
    state.mark_exhausted("codex_astra", all_engines["codex_astra"], "2h")
    assert cli.main(["probe", "codex_astra"]) == 3
    assert capsys.readouterr().out.startswith("EXHAUSTED 20") and len(calls) == 1
    assert cli.main(["probe", "claude_subagent"]) == 0
    assert cli.main(["--available", "agy_claude_opus=no", "probe", "agy_claude_opus"]) == 4
    assert "UNAVAILABLE disabled" in capsys.readouterr().out and len(calls) == 1


# ----------------------------------------------------------------------------- pick


def test_pick_walks_priority_and_falls_through(all_engines):
    fake = FakeRun({"gemini-3.1-pro-high": (3, AGY_QUOTA_LOG), "claude-opus-4-6-thinking": (3, "RESOURCE_EXHAUSTED (code 429)"),
                    "gpt-6-astra": (0, "READY")})
    key, seen = state.pick(all_engines, now=NOW, run=fake)
    assert key == "codex_astra"
    assert [(r.engine, r.status) for r in seen] == [("agy_gemini_pro", "EXHAUSTED"), ("agy_claude_opus", "EXHAUSTED"),
                                                    ("codex_astra", "AVAILABLE")]
    # second pick: both agy buckets answered from the state file, codex from the AVAILABLE cache
    key, _ = state.pick(all_engines, now=NOW + timedelta(minutes=1), run=fake)
    assert key == "codex_astra" and len(fake.calls) == 3
    # everything out -> claude_subagent (never probed)
    fake.replies["gpt-6-astra"] = (1, CODEX_QUOTA)
    key, seen = state.pick(all_engines, now=NOW + timedelta(minutes=11), run=fake)
    assert key == "claude_subagent" and seen[-1].engine == "claude_subagent"


def test_pick_prefers_gemini_and_honours_exclude(all_engines):
    fake = FakeRun({"gemini-3.1-pro-high": (0, "READY"), "claude-opus-4-6-thinking": (0, "READY"), "gpt-6-astra": (0, "READY")})
    assert state.pick(all_engines, now=NOW, run=fake)[0] == "agy_gemini_pro"
    assert state.pick(all_engines, {"agy_gemini_pro"}, now=NOW, run=fake)[0] == "agy_claude_opus"
    assert state.pick(all_engines, {"agy_gemini_pro", "agy_claude_opus", "codex_astra"}, now=NOW, run=fake)[0] == "claude_subagent"


def test_cli_pick_prints_first_available(all_engines, monkeypatch, capsys):
    state.mark_exhausted("agy_gemini_pro", all_engines["agy_gemini_pro"], "1h")
    monkeypatch.setattr(state.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "READY", ""))
    assert cli.main(["pick"]) == 0
    out, err = capsys.readouterr()
    assert out.strip() == "agy_claude_opus" and "agy_gemini_pro: EXHAUSTED" in err
    assert cli.main(["pick", "--exclude", "agy_claude_opus,codex_astra"]) == 0
    assert capsys.readouterr().out.strip() == "claude_subagent"


# ----------------------------------------------------------------------------- state file


def test_state_file_atomic_and_safe_under_two_processes(tmp_path):
    path = tmp_path / "shared" / "engine_state.json"
    code = (
        "import sys\nfrom jbr import state\n"
        "who, n = sys.argv[1], int(sys.argv[2])\n"
        "for i in range(n):\n"
        "    state.update(lambda d: d.setdefault('counter', {}).__setitem__(f'{who}-{i}', i))\n"
        "    state.update(lambda d: d.__setitem__('total', d.get('total', 0) + 1))\n"
    )
    env = dict(os.environ, JBR_STATE=str(path), PYTHONPATH=str(REPO))
    procs = [subprocess.Popen([sys.executable, "-c", code, who, "40"], env=env, cwd=REPO) for who in ("a", "b")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total"] == 80  # 2 processes x 40 locked increments: no lost update
    assert len(data["counter"]) == 80
    assert not list(path.parent.glob("*.tmp")) and not list(path.parent.glob("*.lock"))


def test_stale_lock_is_broken(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    lock = tmp_path / "s.json.lock"
    lock.write_text("999999")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    state.update(lambda d: d.__setitem__("x", 1), path)
    assert state.load(path) == {"x": 1}


# ----------------------------------------------------------------------------- runner -> state


def test_runner_quota_run_writes_exhausted_until(project, all_engines, monkeypatch):
    _fake_engine(monkeypatch, AGY_QUOTA_LOG.split("\n\n", 1)[1], 3)
    t0 = datetime.now(timezone.utc)
    res = runner.run(project, "WP01", "agy_gemini_pro", all_engines, project / "ops" / "work_packages.json")
    assert res["status"] == "quota"
    rec = state.load()["agy_gemini_pro"]
    until = state.parse_iso(rec["exhausted_until"])
    assert rec["last_status"] == "EXHAUSTED" and res["exhausted_until"] == rec["exhausted_until"]
    assert abs((until - t0) - timedelta(hours=2, minutes=9, seconds=31)) < timedelta(minutes=1)
    # the next probe answers from the file without touching the engine
    r = state.probe("agy_gemini_pro", all_engines, run=FakeRun({}))
    assert r.status == "EXHAUSTED"


def test_runner_quota_on_agy_opus_marks_the_claude_bucket(project, all_engines, monkeypatch):
    _fake_engine(monkeypatch, "error: Individual quota reached. Resets in 27m28s.\n", 3)
    runner.run(project, "WP01", "agy_claude_opus", all_engines, project / "ops" / "work_packages.json")
    assert state.load()["agy_claude"]["engines"] == ["agy_claude_opus"]


def test_runner_ok_run_marks_available_and_state_errors_never_fail_a_run(project, all_engines, monkeypatch):
    from tests.test_quota import WORK_LOG
    _fake_engine(monkeypatch, WORK_LOG, 0)
    state.mark_exhausted("codex_astra", all_engines["codex_astra"], "1h")
    res = runner.run(project, "WP01", "codex_astra", all_engines, project / "ops" / "work_packages.json")
    assert res["status"] == "ok" and state.load()["codex_astra"]["last_status"] == "AVAILABLE"
    monkeypatch.setattr(state, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = runner.run(project, "WP01", "codex_astra", all_engines, project / "ops" / "work_packages.json")
    assert res["status"] == "ok"
    assert "could not update engine state" in runner.report_paths(project, "WP01")["log"].read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- Jev routing follows the chain


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _jev(captured, choose):
    def fake_urlopen(req, timeout=0):
        body = json.loads(req.data)
        captured["body"] = body
        answers = {}
        for q, spec in body["questions"].items():
            crit = list(spec["criteria"])
            c = choose(q, crit)
            answers[q] = {"choice": c, "confidence": 0.8, "probabilities": {k: (0.8 if k == c else 0.1) for k in crit}}
        return _Resp(json.dumps({"model": "jev-test", "answers": answers}).encode())
    return fake_urlopen


def test_route_passes_priority_and_live_status_and_writes_chains(project, packages, all_engines, monkeypatch):
    avail = {"agy_gemini_pro": {"status": "EXHAUSTED", "until": "2026-10-02T03:09:31Z", "priority": 1},
             "agy_claude_opus": {"status": "AVAILABLE", "priority": 2},
             "codex_astra": {"status": "AVAILABLE", "priority": 3},
             "claude_subagent": {"status": "AVAILABLE", "priority": 4}}
    captured = {}
    # Jev: highest priority by default, codex for the numerically hard WP03
    monkeypatch.setattr(router.urllib.request, "urlopen",
                        _jev(captured, lambda q, crit: "codex_astra" if q == "engine_WP03" else "agy_claude_opus"))
    doc = router.route(project, packages, all_engines, done={"WP01"}, api_key="k", availability=avail)
    body = captured["body"]
    for q in body["questions"].values():
        assert set(q["criteria"]) == {"agy_claude_opus", "codex_astra", "claude_subagent"}  # exhausted engine never offered
        assert "routing_policy" in q["instructions"]
    st = body["state"]
    assert st["routing_policy"] == router.CHAIN_POLICY
    assert st["engine_chain"] == [{"engine": "agy_gemini_pro", "priority": 1, "live_status": "EXHAUSTED"},
                                  {"engine": "agy_claude_opus", "priority": 2, "live_status": "AVAILABLE"},
                                  {"engine": "codex_astra", "priority": 3, "live_status": "AVAILABLE"},
                                  {"engine": "claude_subagent", "priority": 4, "live_status": "AVAILABLE"}]
    assert "live_status: EXHAUSTED until 2026-10-02T03:09:31Z" in st["engines"]["agy_gemini_pro"]
    assert "chain priority 1" in st["engines"]["agy_gemini_pro"]
    assert doc["routing"]["WP02"]["chain"] == ["agy_claude_opus", "codex_astra", "claude_subagent"]
    assert doc["routing"]["WP03"]["chain"] == ["codex_astra", "agy_claude_opus", "claude_subagent"]
    on_disk = json.loads((project / "ops" / "routing.json").read_text(encoding="utf-8"))
    assert on_disk["chains"] == {"WP02": ["agy_claude_opus", "codex_astra", "claude_subagent"],
                                 "WP03": ["codex_astra", "agy_claude_opus", "claude_subagent"]}
    assert router.load_chains(project)["WP03"][0] == "codex_astra"
    assert router.load_routing(project) == {"WP02": "agy_claude_opus", "WP03": "codex_astra"}
    assert "chain: agy_claude_opus > codex_astra > claude_subagent" in router.format_routing(doc)


def test_route_never_routes_to_an_unusable_engine_even_if_jev_says_so(project, packages, all_engines, monkeypatch):
    avail = {k: {"status": "AVAILABLE"} for k in all_engines}
    avail["agy_gemini_pro"] = {"status": "UNAVAILABLE"}
    monkeypatch.setattr(router.urllib.request, "urlopen", _jev({}, lambda q, crit: "agy_gemini_pro"))
    doc = router.route(project, packages, all_engines, api_key="k", availability=avail, write=False)
    for r in doc["routing"].values():
        assert r["engine"] == "agy_claude_opus" and "overridden" in r
        assert r["chain"] == ["agy_claude_opus", "codex_astra", "claude_subagent"]


def test_chain_for_claude_pick_is_just_claude(all_engines):
    assert router.chain_for("claude_subagent", all_engines) == ["claude_subagent"]
    assert router.chain_for("codex_astra", all_engines) == ["codex_astra", "agy_gemini_pro", "agy_claude_opus", "claude_subagent"]


def test_route_all_external_out_goes_to_claude_without_api(project, packages, all_engines, monkeypatch):
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API")))
    avail = {k: {"status": "EXHAUSTED"} for k in all_engines}
    avail["claude_subagent"] = {"status": "AVAILABLE"}
    doc = router.route(project, packages, all_engines, availability=avail, write=False)
    assert {wp: r["chain"] for wp, r in doc["routing"].items()} == {wp: ["claude_subagent"] for wp in ("WP01", "WP02", "WP03")}


def test_cli_route_probes_engines_and_follows_the_chain(project, monkeypatch, capsys):
    """End to end through the CLI: live probes (mocked subprocess) feed Jev (mocked urlopen)."""
    replies = {"gemini-3.1-pro-high": (3, AGY_QUOTA_LOG), "claude-opus-4-6-thinking": (0, "READY"),
               "gpt-6-astra": (0, "READY")}
    probed = []

    def fake_run(cmd, **kw):
        model = cmd[cmd.index("--model") + 1] if "--model" in cmd else cmd[cmd.index("-m") + 1]
        probed.append(model)
        rc, out = replies[model]
        return subprocess.CompletedProcess(cmd, rc, out, "")

    monkeypatch.setattr(state.subprocess, "run", fake_run)
    captured = {}
    monkeypatch.setattr(router.urllib.request, "urlopen", _jev(captured, lambda q, crit: "agy_claude_opus"))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k-test")
    assert cli.main(["--project", str(project), "route"]) == 0
    out, err = capsys.readouterr()
    assert sorted(probed) == ["claude-opus-4-6-thinking", "gemini-3.1-pro-high", "gpt-6-astra"]
    assert "agy_gemini_pro: EXHAUSTED" in err
    assert "agy_gemini_pro" not in next(iter(captured["body"]["questions"].values()))["criteria"]
    assert router.load_chains(project) == {wp: ["agy_claude_opus", "codex_astra", "claude_subagent"] for wp in ("WP01", "WP02", "WP03")}
    doc = json.loads((project / "ops" / "routing.json").read_text(encoding="utf-8"))
    assert doc["availability"]["agy_gemini_pro"]["status"] == "EXHAUSTED"
    # dry-run never probes live
    probed.clear()
    assert cli.main(["--project", str(project), "route", "--dry-run"]) == 0
    assert probed == []


# ----------------------------------------------------------------------------- build_dag.js chain


JBR = {"root": "C:\\p", "jbr": "/c/jbr"}
ONE = [{"id": "WP01", "depends_on": []}]


def probe_out(wp, line):
    return dict(run_stub(wp, "quota", line.split(" ", 1)[1]), probe=line)


@needs_node
def test_build_dag_auto_is_default_and_walks_the_chain():
    rc, out = run_workflow(BUILD_DAG, dict(JBR, packages=ONE), {"runs": {
        "impl:WP01:agy_gemini_pro": probe_out("WP01", "EXHAUSTED 2026-10-02T05:00:00Z"),
        "impl:WP01:agy_claude_opus:fallback": probe_out("WP01", "EXHAUSTED 2026-10-02T04:00:00Z"),
        "impl:WP01:codex_astra:fallback": run_stub("WP01", "ok")}})
    assert rc == 0
    calls = out["calls"]
    assert calls[:4] == ["impl:WP01:agy_gemini_pro", "impl:WP01:agy_claude_opus:fallback",
                         "impl:WP01:codex_astra:fallback", "review:WP01:r1"]
    assert "impl:WP01:fallback" not in calls  # claude_subagent not needed
    row = out["result"][0]
    assert row["chain"] == ["agy_gemini_pro", "agy_claude_opus", "codex_astra", "claude_subagent"]
    assert row["engine"] == "agy_gemini_pro" and row["final"] == "accept"
    assert [(t["engine"], t["status"]) for t in row["history"][0]["engines"]] == [
        ("agy_gemini_pro", "quota"), ("agy_claude_opus", "quota"), ("codex_astra", "ok")]
    assert all("engine_status" in out["required"][c] for c in calls[:3])


def test_build_dag_runner_prompt_probes_before_spawning():
    src = BUILD_DAG.read_text(encoding="utf-8")
    i_probe, i_spawn = src.index("python -m jbr${JBR_ARGS} probe ${eng}"), src.index("spawn ${id} --engine ${eng}")
    assert i_probe < i_spawn
    assert "EXHAUSTED <until>" in src and "Exit 3 or 4: STOP HERE" in src
    assert "EXHAUSTED.add" not in src and "new Set()" not in src  # no run-wide permanent skip list


@needs_node
def test_build_dag_recovered_engine_preferred_again_in_the_same_run():
    pk = [{"id": "WP01", "depends_on": []}, {"id": "WP02", "depends_on": ["WP01"]}]
    rc, out = run_workflow(BUILD_DAG, dict(JBR, packages=pk), {"runs": {
        "impl:WP01:agy_gemini_pro": run_stub("WP01", "quota", "27m28s"),   # hit quota mid-run
        "impl:WP01:agy_claude_opus:fallback": run_stub("WP01", "ok"),
        "impl:WP02:agy_gemini_pro": run_stub("WP02", "ok")}})               # its probe said AVAILABLE again
    assert rc == 0
    calls = out["calls"]
    assert "impl:WP02:agy_gemini_pro" in calls and not any(c.startswith("impl:WP02:") and "fallback" in c for c in calls)
    assert out["result"][1]["history"][0]["engines"] == [{"engine": "agy_gemini_pro", "status": "ok", "reset_hint": ""}]


@needs_node
def test_build_dag_fix_round_restarts_at_the_top_of_the_chain():
    fix_review = {"wp": "WP01", "pytest_summary": "1 failed", "test_count": 5, "passed_criteria": [],
                  "failures": [{"criterion": "c", "severity": "high", "evidence": "e", "fix": "f"}],
                  "mutation_check": "", "ownership_violations": [], "verdict": "fix"}
    accept = dict(fix_review, failures=[], verdict="accept")
    rc, out = run_workflow(BUILD_DAG, dict(JBR, packages=ONE, maxFixRounds=1), {
        "reviews": {"WP01": [fix_review, accept]},
        "runs": {"impl:WP01:agy_gemini_pro": run_stub("WP01", "quota"),
                 "impl:WP01:agy_claude_opus:fallback": run_stub("WP01", "ok"),
                 "fix:WP01:r1:agy_gemini_pro": run_stub("WP01", "ok")}})
    assert rc == 0
    assert "fix:WP01:r1:agy_gemini_pro" in out["calls"] and not any(c.startswith("fixfile:") for c in out["calls"])
    row = out["result"][0]
    assert (row["final"], row["rounds"]) == ("accept", 2)


@needs_node
def test_build_dag_routing_accepts_string_pin_list_and_auto():
    pk = [{"id": f"WP0{i}", "depends_on": []} for i in range(1, 5)]
    routing = {"WP01": "codex_astra", "WP02": ["agy_claude_opus", "claude_subagent"], "WP03": "auto",
               "WP04": "claude_subagent"}
    rc, out = run_workflow(BUILD_DAG, dict(JBR, packages=pk, routing=routing))
    assert rc == 0
    chains = {r["wp"]: r["chain"] for r in out["result"]}
    assert chains == {"WP01": ["codex_astra", "claude_subagent"],                  # pin, then the chain after it
                      "WP02": ["agy_claude_opus", "claude_subagent"],              # list from routing.json .chains
                      "WP03": ["agy_gemini_pro", "agy_claude_opus", "codex_astra", "claude_subagent"],
                      "WP04": ["claude_subagent"]}
    assert "impl:WP04" in out["calls"] and "impl:WP02:agy_claude_opus" in out["calls"]


@needs_node
def test_build_dag_without_jbr_auto_runs_on_claude_but_explicit_external_needs_jbr():
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": ONE})
    assert rc == 0 and out["result"][0]["chain"] == ["claude_subagent"] and out["calls"][0] == "impl:WP01"
    assert any("args.jbr not set" in m for m in out["logs"])
    rc, out = run_workflow(BUILD_DAG, {"root": "C:\\p", "packages": ONE, "routing": {"WP01": ["codex_astra"]}})
    assert rc == 1 and "args.jbr" in out["error"]


@needs_node
def test_build_dag_whole_chain_dead_is_blocked_without_review():
    rc, out = run_workflow(BUILD_DAG, dict(JBR, packages=ONE, engineChain=["agy_gemini_pro", "codex_astra"],
                                           fallbackEngine="codex_astra"), {"runs": {
        "impl:WP01:agy_gemini_pro": probe_out("WP01", "UNAVAILABLE exit 2: boom"),
        "impl:WP01:codex_astra:fallback": run_stub("WP01", "crash")}})
    assert rc == 0
    row = out["result"][0]
    assert row["final"] == "blocked" and not any(c.startswith("review:") for c in out["calls"])
    assert row["blocked"]["tried"][0]["probe"] == "UNAVAILABLE exit 2: boom"
