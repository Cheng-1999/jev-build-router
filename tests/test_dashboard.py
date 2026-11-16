"""Saved engine switches and the local dashboard. No engine is ever called: probes are faked."""
import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from jbr import cli, dashboard, router, state, switches
from tests.conftest import REPO

ENGINES = REPO / "engines.json"


def test_switch_applies_to_every_load_and_cli_overrides_it():
    switches.set_switch("codex_astra", False, "subscription ended")
    assert router.load_engines(ENGINES)["codex_astra"]["available"] is False
    assert router.load_engines(ENGINES, use_switches=False)["codex_astra"]["available"] is True
    assert router.load_engines(ENGINES, {"codex_astra": True})["codex_astra"]["available"] is True  # --available wins
    assert state.probe("codex_astra", router.load_engines(ENGINES)).line().startswith("UNAVAILABLE disabled (jbr disable")
    switches.set_switch("codex_astra", None)
    assert router.load_engines(ENGINES)["codex_astra"]["available"] is True


def test_switch_file_lives_beside_the_state_file_and_ignores_junk(monkeypatch, tmp_path):
    assert switches.switches_path().parent == state.state_path().parent
    switches.switches_path().parent.mkdir(parents=True, exist_ok=True)
    switches.switches_path().write_text('{"codex_astra": {"available": "no"}, "gone_engine": {"available": false}', encoding="utf-8")
    assert switches.load() == {}  # half-written / hand-broken file: no switches, no crash
    switches.switches_path().write_text(json.dumps({"gone_engine": {"available": False}}), encoding="utf-8")
    assert "gone_engine" not in router.load_engines(ENGINES)  # a switch for an engine engines.json lacks is ignored
    monkeypatch.setenv("JBR_SWITCHES", str(tmp_path / "x.json"))
    assert switches.switches_path() == tmp_path / "x.json"


def test_cli_disable_engines_reset(capsys):
    assert cli.main(["disable", "codex_astra", "--note", "unsubscribed"]) == 0
    assert "codex_astra: off" in capsys.readouterr().out
    assert cli.main(["engines"]) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("codex_astra"))
    assert " off " in line and "unsubscribed" in line
    assert cli.main(["--project", ".", "pick", "--no-live", "--exclude", "agy_gemini_pro,agy_claude_opus"]) == 0
    assert capsys.readouterr().out.strip() != "codex_astra"
    assert cli.main(["reset", "codex_astra"]) == 0
    assert "back to engines.json (on)" in capsys.readouterr().out
    assert switches.load() == {}
    assert cli.main(["disable", "nope"]) == 2


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(state.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "READY", ""))
    monkeypatch.setattr(state.shutil, "which", lambda name: f"/bin/{name}")
    httpd, url, token = dashboard.make_server(ENGINES, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield url, token
    httpd.shutdown()
    httpd.server_close()


def _req(url, path, *, data=None, token=None, host=None):
    r = urllib.request.Request(url.rstrip("/") + path, data=None if data is None else json.dumps(data).encode(),
                               method="POST" if data is not None else "GET")
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-JBR-Token", token)
    if host:
        r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_dashboard_page_and_engine_list_never_probe(server, monkeypatch):
    url, token = server
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: pytest.fail("loading the page must not probe"))
    code, body = _req(url, "/")
    assert code == 200 and token in body and "jbr engines" in body and "—" not in body
    code, body = _req(url, "/api/engines")
    rows = json.loads(body)["engines"]
    assert code == 200 and rows[0]["engine"] == "agy_gemini_pro"
    assert {r["engine"] for r in rows} >= {"codex_astra", "hermes_qwen", "claude_subagent"}


def test_dashboard_switch_and_probe_need_the_token_and_a_loopback_host(server):
    url, token = server
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "off"})[0] == 403          # no token
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "off"}, token="x")[0] == 403
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "off"}, token=token,
                host="evil.example:80")[0] == 403                                                        # DNS rebinding
    assert _req(url, "/api/engines", host="evil.example:80")[0] == 403
    assert switches.load() == {}
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "off", "note": "n"}, token=token)[0] == 200
    assert switches.load()["codex_astra"]["available"] is False
    code, body = _req(url, "/api/engines")
    row = next(r for r in json.loads(body)["engines"] if r["engine"] == "codex_astra")
    assert row["on"] is False and row["default_on"] is True and row["switch"]["note"] == "n"
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "default"}, token=token)[0] == 200
    assert switches.load() == {}
    assert _req(url, "/api/switch", data={"engine": "nope", "state": "off"}, token=token)[0] == 400
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "maybe"}, token=token)[0] == 400
    code, body = _req(url, "/api/probe", data={"engine": "codex_astra"}, token=token)
    assert code == 200 and json.loads(body)["status"] == "AVAILABLE"


def test_malformed_switch_files_never_crash_a_command(capsys):
    p = switches.switches_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2]", encoding="utf-8")
    assert router.load_engines(ENGINES)["codex_astra"]["available"] is True
    p.write_text(json.dumps({"codex_astra": {"available": False}}), encoding="utf-8")  # no "at"
    assert cli.main(["engines"]) == 0
    assert "codex_astra        off" in capsys.readouterr().out


def test_dashboard_refuses_framing_and_odd_bodies(server):
    url, token = server
    with urllib.request.urlopen(url, timeout=10) as resp:
        assert resp.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert _req(url, "/api/switch", data=[], token=token)[0] == 400
    assert _req(url, "/api/switch", data={"engine": "codex_astra", "state": "off"}, token="tökén")[0] == 403
    r = urllib.request.Request(url + "api/switch", data=b'{"engine": "codex_astra", "state": "off"}', method="POST")
    r.add_header("X-JBR-Token", token)
    r.add_header("Content-Length", "-1")
    try:
        urllib.request.urlopen(r, timeout=10)
    except urllib.error.HTTPError as e:
        assert e.code == 400  # body ignored: empty object, no engine


def test_dashboard_teams_get_and_post_security_and_validation(server, monkeypatch):
    from jbr import teams
    url, token = server
    monkeypatch.setattr(state.subprocess, "run", lambda *a, **k: pytest.fail("/api/teams must not probe"))

    # Page contains the Teams section and controls
    code, page_html = _req(url, "/")
    assert code == 200 and 'id="teams-h">Teams</h2>' in page_html and "save-teams" in page_html

    # GET /api/teams
    code, body = _req(url, "/api/teams")
    data = json.loads(body)
    assert code == 200 and data["teams"] == {} and data["errors"] == []
    assert "codex_astra" in data["engines"] and "agy_manual" not in data["engines"]

    valid_payload = {"teams": teams.example()}
    # Bad/missing token -> 403
    assert _req(url, "/api/teams", data=valid_payload)[0] == 403
    assert _req(url, "/api/teams", data=valid_payload, token="wrong")[0] == 403
    # Foreign Host -> 403
    assert _req(url, "/api/teams", host="evil.example:80")[0] == 403
    assert _req(url, "/api/teams", data=valid_payload, token=token, host="evil.example:80")[0] == 403
    assert teams.load() == {}

    # Invalid body / validation errors -> 400, nothing written
    assert _req(url, "/api/teams", data=[], token=token)[0] == 400
    assert _req(url, "/api/teams", data={"no_teams_key": 1}, token=token)[0] == 400
    code, err_body = _req(url, "/api/teams", data={"teams": {"Bad!": {"kind": "doc", "roles": {"r": {"engines": ["agy_manual"]}}}}}, token=token)
    assert code == 400
    err_json = json.loads(err_body)
    assert any("invalid name" in e for e in err_json["errors"]) and any("manual engine" in e for e in err_json["errors"])
    assert not teams.teams_path().exists()

    # Oversize body (> 64 KB) -> 400, nothing written
    big = {"teams": {"marketing": {"kind": "doc", "description": "x" * 70_000, "roles": {"drafter": {"engines": ["claude_subagent"]}}}}}
    assert _req(url, "/api/teams", data=big, token=token)[0] == 400
    assert not teams.teams_path().exists()

    # Valid POST /api/teams saves and round-trips through GET /api/teams
    code, ok_body = _req(url, "/api/teams", data=valid_payload, token=token)
    assert code == 200 and json.loads(ok_body)["ok"] is True
    assert set(teams.load()) == {"marketing", "engineering"}
    code, get_body = _req(url, "/api/teams")
    assert code == 200 and set(json.loads(get_body)["teams"]) == {"marketing", "engineering"}



def test_dashboard_never_overwrites_a_malformed_teams_file(server):
    from jbr import teams
    url, token = server
    p = teams.teams_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"teams": {"marketing": {},}}', encoding="utf-8")  # hand-edit typo: trailing comma
    code, body = _req(url, "/api/teams")
    data = json.loads(body)
    assert code == 200 and data["teams"] == {} and data["file_error"] and "malformed" in data["errors"][0]
    good = {"teams": {"sales": {"kind": "doc", "roles": {"writer": {"engines": ["codex_astra"]}}}}}
    code, body = _req(url, "/api/teams", data=good, token=token)
    assert code == 409 and "nothing was written" in json.loads(body)["error"]
    assert p.read_text(encoding="utf-8") == '{"teams": {"marketing": {},}}'


def test_dashboard_save_checks_duplicate_names_client_side(server):
    url, _ = server
    _, page = _req(url, "/")
    assert "function dupNames()" in page and "const dn=dupNames();if(dn.length)" in page
