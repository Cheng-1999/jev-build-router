import io
import json

import pytest

from jbr import router


def test_compute_depths_from_depends_on(packages):
    d = router.compute_depths(packages)
    assert d == {"WP01": 1, "WP02": 2, "WP03": 3}


def test_compute_depths_rejects_cycle():
    pk = [{"id": "A", "depends_on": ["B"]}, {"id": "B", "depends_on": ["A"]}]
    with pytest.raises(ValueError, match="cycle"):
        router.compute_depths(pk)


def test_package_attrs_uses_routing_hints_and_done(packages):
    attrs = router.package_attrs(packages, done={"WP01"})
    assert "WP01" not in attrs
    assert attrs["WP02"]["sign_convention_critical"] is True
    assert attrs["WP02"]["numerical_difficulty"] == "low"
    assert attrs["WP02"]["depth"] == 2  # computed
    assert attrs["WP03"]["depth"] == 9  # routing_hints override wins


def test_parse_available_and_override(engines):
    ov = router.parse_available(["codex_astra=yes", "agy_gemini_pro=no"])
    assert ov == {"codex_astra": True, "agy_gemini_pro": False}
    eng = router.load_engines(router.pathlib.Path(__file__).resolve().parents[1] / "engines.json", ov)
    assert eng["codex_astra"]["available"] is True
    assert eng["agy_gemini_pro"]["available"] is False
    with pytest.raises(ValueError):
        router.parse_available(["codex_astra"])
    with pytest.raises(KeyError):
        router.load_engines(router.pathlib.Path(__file__).resolve().parents[1] / "engines.json", {"nope": True})


def test_build_request_criteria_only_available_engines(packages, engines):
    body = router.build_request(packages, engines, {"now_local_time": "04:15"})
    avail = {k for k, e in engines.items() if e["available"]}
    assert avail == {"claude_subagent", "agy_gemini_pro"}
    assert set(body["questions"]) == {"engine_WP01", "engine_WP02", "engine_WP03"}
    q = body["questions"]["engine_WP02"]
    assert q["type"] == "choice"
    assert set(q["criteria"]) == avail
    assert body["state"]["now_local_time"] == "04:15"
    assert body["model"] == "jev-latest"
    # unavailable engines still described (Jev sees the whole picture), each with its availability
    assert "currently available: no" in body["state"]["engines"]["codex_astra"]


def test_route_dry_run_does_not_call_api(project, packages, engines, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("urlopen must not be called in dry-run")
    monkeypatch.setattr(router.urllib.request, "urlopen", boom)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    doc = router.route(project, packages, engines, dry_run=True)
    assert doc["dry_run"] is True
    assert "engine_WP03" in doc["request"]["questions"]
    assert not (project / "ops" / "routing.json").exists()


def test_route_with_mocked_urlopen_writes_routing_json(project, packages, engines, monkeypatch):
    captured = {}

    class FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data)
        answers = {}
        for q in captured["body"]["questions"]:
            crit = list(captured["body"]["questions"][q]["criteria"])
            answers[q] = {"choice": crit[-1], "confidence": 0.7, "probabilities": {c: 1 / len(crit) for c in crit}}
        return FakeResp(json.dumps({"model": "jev-1.13.0", "answers": answers}).encode())

    monkeypatch.setattr(router.urllib.request, "urlopen", fake_urlopen)
    doc = router.route(project, packages, engines, {"now_local_time": "04:15"}, done={"WP01"}, api_key="k-test")
    assert captured["url"] == router.TYPESAFE_URL
    assert captured["auth"] == "Bearer k-test"
    assert set(captured["body"]["questions"]) == {"engine_WP02", "engine_WP03"}
    assert doc["model"] == "jev-1.13.0"
    assert doc["routing"]["WP02"]["engine"] == "agy_gemini_pro"
    on_disk = json.loads((project / "ops" / "routing.json").read_text(encoding="utf-8"))
    assert on_disk["routing"]["WP03"]["confidence"] == 0.7
    assert router.load_routing(project) == {"WP02": "agy_gemini_pro", "WP03": "agy_gemini_pro"}
    assert "WP02: agy_gemini_pro (p=0.50, conf=0.70)" in router.format_routing(doc)


def test_route_single_available_engine_skips_api(project, packages, engines, monkeypatch):
    monkeypatch.setattr(router.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no API")))
    eng = router.load_engines(router.pathlib.Path(__file__).resolve().parents[1] / "engines.json", {"agy_gemini_pro": False})
    doc = router.route(project, packages, eng, write=False)
    assert all(r["engine"] == "claude_subagent" for r in doc["routing"].values())
    assert not (project / "ops" / "routing.json").exists()


def test_route_requires_key_when_api_needed(project, packages, engines, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        router.route(project, packages, engines)


def test_build_request_no_available_engine_raises(packages, engines):
    for e in engines.values():
        e["available"] = False
    with pytest.raises(ValueError, match="no engine"):
        router.build_request(packages, engines)
