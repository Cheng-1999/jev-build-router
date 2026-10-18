import json
import pathlib

import pytest

from jbr import models

REPO = pathlib.Path(__file__).resolve().parents[1]

# What "auto:gemini" / "auto:claude-opus" resolve to in tests (the ids the chain tests were written against).
FIXED_AGY_MODELS = ["gemini-3.1-pro-high", "gemini-3.1-pro-low", "claude-opus-5-5-high", "claude-sonnet-5-5-high"]

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
    # ...and its own runs dir and ledger: no test writes run artifacts or ledger lines under the real ~/.jbr
    monkeypatch.setenv("JBR_RUNS", str(tmp_path / "jbr-runs"))
    monkeypatch.setenv("JBR_LEDGER", str(tmp_path / "jbr-ledger.jsonl"))
    # ...and prereview's ocr home derives from JBR_RUNS; a developer's own overrides never leak in
    monkeypatch.delenv("JBR_OCR_HOME", raising=False)
    monkeypatch.delenv("JBR_OCR_BIN", raising=False)
    # "auto:<family>" engines resolve against this fixed `agy models` list, never the real agy CLI.
    # A test that passes its own `run=` (or patches list_agy_models itself) gets the real lister.
    real = models.list_agy_models
    monkeypatch.setattr(
        models, "list_agy_models", lambda **kw: real(**kw) if "run" in kw else list(FIXED_AGY_MODELS)
    )
    return path


def reports(project):
    """Where run artifacts for `project` land in tests (the isolated JBR_RUNS)."""
    from jbr import paths
    return paths.reports_dir(project)


@pytest.fixture
def engines():
    """engines.json with the pre-chain availability (agy Opus and Codex off). Since 2026-10-02 every
    engine is `available: true` in engines.json and live probes decide; the routing tests written
    before that keep their two-engine fixture."""
    from jbr import router
    return router.load_engines(REPO / "engines.json", {"agy_claude_opus": False, "codex_astra": False,
                                                         "ollama_qwen": False})


@pytest.fixture
def packages(project):
    from jbr import runner
    return runner.load_packages(project / "ops" / "work_packages.json")[0]
