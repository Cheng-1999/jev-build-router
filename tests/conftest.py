import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

PACKAGES = {
    "rules": "Rules: Python 3.12, src layout under src/demo, pytest.",  # prefix only; runner.DEFAULT_RULES is always appended
    "work_packages": [
        {"id": "WP01", "title": "Foundation", "goal": "core types", "depends_on": [], "files": ["src/demo/core.py"],
         "acceptance": ["tests/test_core.py::test_a"], "tests": ["see acceptance"], "spec_refs": ["PLAN 1"],
         "implementer_prompt": "build the core"},
        {"id": "WP02", "title": "Ledger", "goal": "signed ledger", "depends_on": ["WP01"], "files": ["src/demo/ledger.py"],
         "acceptance": ["tests/test_ledger.py::test_b"], "tests": [], "spec_refs": [],
         "implementer_prompt": "build the ledger", "routing_hints": {"numerical_difficulty": "low", "sign_convention_critical": True}},
        {"id": "WP03", "title": "Pricer", "goal": "black scholes", "depends_on": ["WP01", "WP02"], "files": ["src/demo/bs.py"],
         "acceptance": [], "tests": [], "spec_refs": [], "implementer_prompt": "price it",
         "routing_hints": {"numerical_difficulty": "high", "depth": 9}},
    ],
}


@pytest.fixture
def project(tmp_path):
    """A throwaway project with ops/work_packages.json and one spec file."""
    ops = tmp_path / "ops"
    (ops / "prompts").mkdir(parents=True)
    (ops / "work_packages.json").write_text(json.dumps(PACKAGES), encoding="utf-8")
    (ops / "prompts" / "WP01.md").write_text("# WP01 spec\nLocked decision: Direction.sign() is +1/-1.\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_engine_state(tmp_path, monkeypatch):
    """Every test gets its own engine state file: never touch ~/.jbr, never inherit a real quota."""
    path = tmp_path / "jbr-state" / "engine_state.json"
    monkeypatch.setenv("JBR_STATE", str(path))
    return path


@pytest.fixture
def engines():
    """engines.json with the pre-chain availability (agy Opus and Codex off). Since 2026-10-02 every
    engine is `available: true` in engines.json and live probes decide; the routing tests written
    before that keep their two-engine fixture."""
    from jbr import router
    return router.load_engines(REPO / "engines.json", {"agy_claude_opus": False, "codex_astra": False})


@pytest.fixture
def packages(project):
    from jbr import runner
    return runner.load_packages(project / "ops" / "work_packages.json")[0]
