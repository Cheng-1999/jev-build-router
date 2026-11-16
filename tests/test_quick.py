import json

import pytest

from jbr import cli, paths, runner, state


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@pytest.fixture
def spawned(monkeypatch):
    """runner.launch_detached stand-in: records every argv, launches nothing."""
    calls = []
    monkeypatch.setattr(runner, "launch_detached", lambda argv, cwd, out: calls.append(argv) or 4242)
    return calls


def test_quick_writes_a_one_package_plan_outside_the_project(project, capsys, monkeypatch):
    monkeypatch.setenv("JBR_RUNS", str(project.parent / (project.name + "-runs")))  # the fixture's runs dir is under tmp_path
    before = _files(project)
    rc = cli.main(["--project", str(project), "quick", "Add a --version flag", "--engine", "codex_astra",
                   "--files", "src/demo/cli.py, tests/test_cli.py", "--accept", "prints the version", "--test-cmd", "pytest -q",
                   "--dry-run"])
    assert rc == 0
    assert _files(project) == before  # never writes into the project
    qp = runner.quick_plan_paths(project, "Q1")
    assert qp["dir"].parent == paths.quick_dir(project)
    (wp,) = runner.load_packages(qp["packages"])[0]
    assert wp["id"] == "Q1" and wp["title"] == "Add a --version flag"
    assert wp["files"] == ["src/demo/cli.py", "tests/test_cli.py"]
    assert wp["acceptance"] == ["prints the version"] and wp["tests"] == ["pytest -q"]
    prompt = runner.compose_prompt(project, wp, qp["prompts"])
    assert "Add a --version flag" in prompt and "FULL PACKAGE SPEC" in prompt  # the engine gets the task text
    out = capsys.readouterr().out
    argv = json.loads(out.split("[dry-run] ", 1)[1].splitlines()[0])
    assert argv[argv.index("--packages") + 1] == str(qp["packages"])
    assert argv[argv.index("--engine") + 1] == "codex_astra" and None not in argv
    review = json.loads(out.split("args=", 1)[1].splitlines()[0])
    assert review["ids"] == ["Q1"] and review["promptsDir"] == str(qp["prompts"]) and review["testCmd"] == "pytest -q"
    assert "wait Q1 --timeout-min 70" in out
    assert "quick --id Q1 --exclude codex_astra" in out


def test_quick_ids_count_up_and_id_reruns_the_plan(project, capsys):
    for task in ("first", "second"):
        assert cli.main(["--project", str(project), "quick", task, "--engine", "codex_astra", "--dry-run"]) == 0
    assert sorted(p.name for p in paths.quick_dir(project).iterdir()) == ["Q1", "Q2"]
    capsys.readouterr()
    assert cli.main(["--project", str(project), "quick", "--id", "Q1", "--engine", "codex_astra", "--dry-run"]) == 0
    assert "Q1: engine=codex_astra" in capsys.readouterr().out
    assert sorted(p.name for p in paths.quick_dir(project).iterdir()) == ["Q1", "Q2"]  # no new plan


def test_quick_picks_and_spawns_and_waits_longer_for_local_qwen(project, capsys, monkeypatch, spawned):
    seen = []
    monkeypatch.setattr(state, "pick", lambda engines, exclude, **kw: seen.append(exclude) or ("ollama_qwen", []))
    assert cli.main(["--project", str(project), "quick", "rename a helper", "--exclude", "codex_astra"]) == 0
    assert seen == [{"codex_astra"}]
    (argv,) = spawned
    assert argv[argv.index("run") + 1] == "Q1" and argv[argv.index("--engine") + 1] == "ollama_qwen"
    assert "--reports" not in argv  # only passed on when the caller gave one
    out = capsys.readouterr().out
    assert "spawned pid=4242" in out and "wait Q1 --timeout-min 140" in out
    assert "--exclude codex_astra,ollama_qwen" in out  # the retry skips the engine that just ran too


def test_quick_with_no_external_engine_keeps_the_plan_and_exits_3(project, capsys, monkeypatch, spawned):
    monkeypatch.setattr(state, "pick", lambda engines, exclude, **kw: ("claude_subagent", []))
    assert cli.main(["--project", str(project), "quick", "anything"]) == 3
    assert spawned == []
    assert runner.quick_plan_paths(project, "Q1")["spec"].exists()
    out = capsys.readouterr().out
    assert "no external engine available" in out and "quick --id Q1" in out


@pytest.mark.parametrize("args", [
    ["quick"],                                              # no task
    ["quick", "   "],                                       # blank task
    ["quick", "task", "--id", "Q1"],                        # both
    ["quick", "--id", "Q9"],                                # unknown plan
    ["quick", "task", "--engine", "nope"],
    ["quick", "task", "--engine", "agy_manual"],            # the user runs it, never jbr
    ["quick", "task", "--engine", "claude_subagent"],
])
def test_quick_refuses_bad_input_before_writing_or_launching(project, args, spawned):
    assert cli.main(["--project", str(project), *args]) == 2
    assert spawned == []
    assert not paths.quick_dir(project).exists()


def test_spawn_without_reports_never_puts_none_in_the_child_argv(project, spawned):
    assert cli.main(["--project", str(project), "spawn", "WP01", "--engine", "codex_astra"]) == 0
    (argv,) = spawned
    assert None not in argv and "--reports" not in argv
    assert cli.main(["--project", str(project), "--reports", "rep", "spawn", "WP01", "--engine", "codex_astra"]) == 0
    assert spawned[1][spawned[1].index("--reports") + 1] == "rep"


def test_quick_printed_commands_keep_this_calls_globals(project, capsys):
    assert cli.main(["--project", str(project), "--reports", "build/rep", "--available", "codex_astra=yes",
                     "quick", "task", "--engine", "codex_astra", "--dry-run"]) == 0
    out = capsys.readouterr().out
    wait_line = next(l for l in out.splitlines() if l.startswith("wait:"))
    assert "--reports build/rep" in wait_line and "--available codex_astra=yes" in wait_line
    assert "--engines" not in wait_line  # the shipped engines.json is the default: not repeated
    assert "wait Q1 --tag fix1" in out and 'logTag \"fix1\"' in out


def test_quick_dry_run_never_deletes_a_finished_runs_marker(project, capsys):
    assert cli.main(["--project", str(project), "quick", "task", "--engine", "codex_astra", "--dry-run"]) == 0
    done = runner.report_paths(project, "Q1")["done"]
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("0 3.0min OK\n", encoding="utf-8")
    assert cli.main(["--project", str(project), "quick", "--id", "q1", "--engine", "codex_astra", "--dry-run"]) == 0
    assert done.exists()  # lower-case id accepted and canonicalised, marker untouched
    assert "Q1: engine=codex_astra" in capsys.readouterr().out


@pytest.mark.parametrize("qid", ["../x", "WP01", "Q", "Q1/../Q2"])
def test_quick_id_must_be_a_quick_id(project, qid, capsys):
    assert cli.main(["--project", str(project), "quick", "--id", qid]) == 2
    assert "--id must look like" in capsys.readouterr().err


def test_quick_task_and_task_file_together_is_refused(project, tmp_path, spawned):
    f = tmp_path / "task.md"
    f.write_text("from the file", encoding="utf-8")
    assert cli.main(["--project", str(project), "quick", "from the argument", "--task-file", str(f), "--engine", "codex_astra"]) == 2
    assert spawned == [] and not paths.quick_dir(project).exists()
    assert cli.main(["--project", str(project), "quick", "--task-file", str(f), "--engine", "codex_astra"]) == 0
    assert "from the file" in runner.quick_plan_paths(project, "Q1")["spec"].read_text(encoding="utf-8")


def test_quick_explicit_engine_out_of_quota_warns_but_spawns(project, capsys, all_engines_state, spawned):
    assert cli.main(["--project", str(project), "quick", "task", "--engine", "codex_astra"]) == 0
    assert len(spawned) == 1
    assert "warning: codex_astra: EXHAUSTED" in capsys.readouterr().err


@pytest.fixture
def all_engines_state():
    from jbr import router
    from tests.conftest import REPO
    engines = router.load_engines(REPO / "engines.json")
    state.mark_exhausted("codex_astra", engines["codex_astra"], "2h")


def test_quick_team_doc_and_code_modes_and_id_reuse(project, capsys, monkeypatch, spawned):
    from jbr import teams
    teams.save(teams.example())
    monkeypatch.setattr(state, "probe", lambda k, engs, **kw: state.Probe(k, state.AVAILABLE))

    # 1. --team with default role (drafter, kind=doc)
    assert cli.main(["--project", str(project), "quick", "--team", "marketing", "Write launch post", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Q1: engine=agy_gemini_flash" in out
    qp = runner.quick_plan_paths(project, "Q1")
    (wp,) = runner.load_packages(qp["packages"])[0]
    assert wp["team"] == "marketing" and wp["role"] == "drafter" and wp["kind"] == "doc"
    assert wp["tests"] == ["none: document task, reviewed against the acceptance criteria"]
    spec = qp["spec"].read_text(encoding="utf-8")
    assert spec.index("## Role: marketing/drafter") < spec.index("## Task")
    assert "Write the first draft" in spec
    review = json.loads(out.split("args=", 1)[1].splitlines()[0])
    assert review["mode"] == "doc" and "testCmd" not in review
    assert "quick --id Q1 --team marketing --role drafter --exclude agy_gemini_flash" in out
    # the engine prompt for a doc task must not tell the engine to run the test suite
    prompt = runner.compose_prompt(project, wp, qp["prompts"])
    assert "run the full test suite" not in prompt and "document task" in prompt

    # 2. Explicit --test-cmd still wins on doc mode
    assert cli.main(["--project", str(project), "quick", "--team", "marketing", "--role", "editor",
                     "Edit launch post", "--test-cmd", "pytest -k doc", "--dry-run"]) == 0
    out2 = capsys.readouterr().out
    (wp2,) = runner.load_packages(runner.quick_plan_paths(project, "Q2")["packages"])[0]
    assert wp2["role"] == "editor" and wp2["tests"] == ["pytest -k doc"]
    assert "no_tests" not in wp2  # an explicit test command keeps the default rules (run it)
    review2 = json.loads(out2.split("args=", 1)[1].splitlines()[0])
    assert review2["mode"] == "doc" and review2["testCmd"] == "pytest -k doc"

    # 3. quick --id Q1 reuses the stored team/role chain when --team is not given
    assert cli.main(["--project", str(project), "quick", "--id", "Q1", "--exclude", "agy_gemini_flash", "--dry-run"]) == 0
    out3 = capsys.readouterr().out
    assert "Q1: engine=hermes_qwen" in out3
    assert "--team marketing --role drafter" in out3


def test_quick_team_refuses_engine_combination_and_unknown_team_or_role(project, capsys, spawned):
    from jbr import teams
    teams.save(teams.example())

    assert cli.main(["--project", str(project), "quick", "--team", "marketing", "--engine", "codex_astra", "task"]) == 2
    assert spawned == [] and not paths.quick_dir(project).exists()

    capsys.readouterr()
    assert cli.main(["--project", str(project), "quick", "--team", "sales", "task"]) == 2
    err = capsys.readouterr().err
    assert "unknown team 'sales'" in err and "marketing" in err and "engineering" in err

    assert cli.main(["--project", str(project), "quick", "--team", "marketing", "--role", "cfo", "task"]) == 2
    err = capsys.readouterr().err
    assert "unknown role 'cfo'" in err and "drafter" in err and "editor" in err
    assert spawned == [] and not paths.quick_dir(project).exists()

