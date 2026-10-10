"""Where jbr keeps its files: never inside the project it builds.

Per project, everything jbr writes (run prompts, engine logs, .done markers, fix files, review json,
routing.json, the Qwen queue and its worktrees) lives under

    ~/.jbr/runs/<project key>/          (root overridable with JBR_RUNS)

where the key is the project's absolute path with every run of non-[A-Za-z0-9._] characters turned
into "-" (/Users/ad/code/app -> Users-ad-code-app), so two checkouts with the same folder name never
share a directory. `python -m jbr paths` prints these for the Workflow scripts, which take them as
args (`reportsDir`) instead of deriving them.

An explicit reports dir still wins: absolute as given, relative = inside the project (the layout jbr
used before 2026-10-10, kept for anyone who wants it).
"""
from __future__ import annotations

import os
import pathlib
import re

_KEY_RE = re.compile(r"[^A-Za-z0-9._]+")


def runs_root() -> pathlib.Path:
    return pathlib.Path(os.environ.get("JBR_RUNS") or pathlib.Path.home() / ".jbr" / "runs")


def project_key(project_root: str | os.PathLike) -> str:
    return _KEY_RE.sub("-", pathlib.Path(project_root).resolve().as_posix()).strip("-") or "root"


def project_dir(project_root: str | os.PathLike) -> pathlib.Path:
    return runs_root() / project_key(project_root)


def reports_dir(project_root: str | os.PathLike, override: str | os.PathLike | None = None) -> pathlib.Path:
    """The run-artifact dir for a project. `override`: absolute as given, relative = under the project."""
    if override:
        return pathlib.Path(project_root) / override  # an absolute override replaces the project part
    return project_dir(project_root) / "reports"


def routing_path(project_root: str | os.PathLike) -> pathlib.Path:
    return project_dir(project_root) / "routing.json"


LEGACY_ROUTING = pathlib.Path("ops") / "routing.json"


def legacy_routing_path(project_root: str | os.PathLike) -> pathlib.Path:
    """Where routing.json lived before 2026-10-10; read as a fallback, never written."""
    return pathlib.Path(project_root) / LEGACY_ROUTING
