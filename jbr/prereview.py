"""Cheap first-pass review with alibaba/open-code-review (`ocr`) on a local model, ahead of the Claude review.

ocr turns a git diff into line-level comments (deterministic file selection, bundling, rule matching and
comment positioning around LLM sub-agents). Here it runs against local Ollama through its OpenAI-compatible
API, so nothing leaves the machine, and its findings go to the Claude reviewer as unverified hints
(workflows/*.js `preReview`). It never decides anything and never fails a build: every outcome is a status.

Artifacts under paths.reports_dir(project), stem prereview-<WP>[-tag]:
  .json       {"wp", "status", "reason", "model", "seconds", "summary", "warnings", "findings": [...]}
  .raw.json   ocr's own --format json output
  .log        ocr stderr + jbr notes (argv, scope)
  .done       "<exit> <minutes>min <STATUS>"; STATUS = OK | SKIPPED | FAILED | TIMEOUT; exit -1 = no ocr exit code
  .stdout     detached (--detach) wrapper output

Isolation, so a cloud endpoint is never used by accident: ocr resolves its LLM from its saved config, then
OCR_LLM_*, then Claude Code's ANTHROPIC_* variables, then shell rc files. jbr runs it with HOME pointed at
its own ocr home (~/.jbr/ocr-home, holding only the jbr-local provider), an environment allow-list (no
ANTHROPIC_*/OPENAI_*/OCR_LLM_* from the caller) and an explicit --provider; a non-loopback URL is refused
unless allow_remote is set. ocr's agent can read any file of the repo, so with allow_remote more than the
diff would leave the machine.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from typing import Any, Callable, Iterator

from jbr import paths, router, runner, state

PREFIX = "prereview"
OK, SKIPPED, FAILED, TIMEOUT = "OK", "SKIPPED", "FAILED", "TIMEOUT"
PROVIDER = "jbr-local"
DEFAULT_URL = "http://127.0.0.1:11434/v1"   # Ollama's OpenAI-compatible API
BACKEND_ENGINE = "ollama_qwen"              # default model + on/off switch (engines.json `available`)
DEFAULT_EFFORT = "low"                      # ocr review rounds per file group: low 1, medium 2, high 3
ROUNDS = {"low": 1, "medium": 2, "high": 3}
DEFAULT_BUDGET_MIN = 45.0                   # whole prereview, lock wait included (spike: ~30 min per package)
LOCK_WAIT_MAX_S = 300                       # waiting for another prereview: then SKIPPED busy (plain review)
LOCK_POLL_S = 2.0
GRACE_S = 30.0                              # SIGTERM -> SIGKILL: time for ocr to publish a partial result
REQUEST_TIMEOUT_S = 900                     # one LLM request; ocr's default 300 s is too short for a local 27B
EXTRA_BODY = {"reasoning_effort": "none"}   # Qwen thinking off: a first pass, not a deep review
MAX_EXCLUDE_CHARS = 8000                    # Windows caps a whole command line at 32K
ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP",
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")  # the last four: Windows needs them to start processes
GIT_C = ["-c", "core.quotePath=false"]


def ocr_home() -> pathlib.Path:
    """jbr's private HOME for ocr (config, sessions); never the user's ~/.opencodereview."""
    return pathlib.Path(os.environ.get("JBR_OCR_HOME") or paths.runs_root().parent / "ocr-home")


def ocr_bin() -> str | None:
    env = os.environ.get("JBR_OCR_BIN")
    if env:
        return env
    # npm installs `ocr` (a node launcher that also checks npm for updates); the release binary is opencodereview
    return shutil.which("opencodereview") or shutil.which("ocr")


def artifact_paths(project_root: str | os.PathLike, wp_id: str, tag: str = "",
                   reports_dir: str | None = None) -> dict[str, pathlib.Path]:
    rep = paths.reports_dir(project_root, reports_dir)
    stem = f"{PREFIX}-{wp_id}" + (f"-{tag}" if tag else "")
    return {"dir": rep, "json": rep / f"{stem}.json", "raw": rep / f"{stem}.raw.json", "log": rep / f"{stem}.log",
            "done": rep / f"{stem}.done", "stdout": rep / f"{stem}.stdout"}


# ----------------------------------------------------------------------------- scope


def expand_braces(pattern: str) -> list[str]:
    """`src/{a,b}.ts` -> [`src/a.ts`, `src/b.ts`] (nested and repeated groups too)."""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    return [x for alt in m.group(1).split(",") for x in expand_braces(head + alt + tail)]


def glob_regex(pattern: str) -> re.Pattern:
    """work_packages.json `files` glob -> regex: `**` crosses directories, `*` and `?` do not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def _rel(p: str) -> str:
    p = p.strip()
    while p.startswith("./"):  # a `./` prefix, never the dot of `.env.example` or `.github/`
        p = p[2:]
    return p


def owned_matcher(globs: list[str]) -> Callable[[str], bool]:
    regs = [glob_regex(_rel(p)) for g in globs for p in expand_braces(g)]
    return lambda path: any(r.match(path) for r in regs)


def _git_paths(root: pathlib.Path, args: list[str], run: Callable[..., Any]) -> list[str]:
    """NUL-separated, unquoted paths (non-ASCII names, spaces and quotes survive)."""
    p = run(["git", *GIT_C, *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(p.stderr or '').strip()[:200]}")
    return [x for x in (p.stdout or "").split("\0") if x.strip()]


def changed_paths(root: pathlib.Path, commit: str = "", run: Callable[..., Any] | None = None) -> list[str]:
    """What ocr will see: the commit's files, or (workspace mode) `git diff HEAD` plus untracked files."""
    run = run or subprocess.run
    if commit:
        return _git_paths(root, ["diff-tree", "-z", "--no-commit-id", "--name-only", "-r", "--root", commit], run)
    tracked = _git_paths(root, ["diff", "-z", "--name-only", "HEAD"], run)
    untracked = _git_paths(root, ["ls-files", "-z", "--others", "--exclude-standard"], run)
    return list(dict.fromkeys(tracked + untracked))


def split_scope(changed: list[str], owned_globs: list[str]) -> tuple[list[str], list[str]]:
    """(in scope, to exclude). build_dag.js builds packages side by side in ONE checkout, so the workspace
    diff holds other packages' work too; only the package's owned files are reviewed."""
    if not owned_globs:
        return list(changed), []
    owned = owned_matcher(owned_globs)
    return [p for p in changed if owned(p)], [p for p in changed if not owned(p)]


def _glob_escape(path: str) -> str:
    # glob syntax for ocr's matcher (checked against v1.13.1): `[[]` and `[*]` match `[` and `*`, but no escape
    # of `?`, `{` or `}` matched (nor a backslash, which is the separator on Windows anyway), so those match
    # as `?`, any one character; so does `,`, which would split the --exclude list. exclude_patterns() checks
    # that no such wildcard catches an owned file.
    return re.sub(r"[\[*?{},\\]", lambda m: {"[": "[[]", "*": "[*]"}.get(m.group(0), "?"), path)


def exclude_patterns(excluded: list[str], in_scope: list[str]) -> list[str]:
    """ocr --exclude patterns, per excluded file: its shallowest directory whose pattern catches no in-scope file
    (`dir/**`), else the file itself, else nothing (it is then reviewed: a hint too many beats an owned file
    missed). ocr's CLI matches each pattern as a glob against the whole repo-relative path (checked against
    v1.13.1 with --preview: `README.md` drops the root file only, never `pkg/README.md`; a leading `/` or a
    trailing `/` matches nothing)."""
    def hits_owned(pat: str) -> bool:  # `?` stands for one char, as in ocr: glob_regex reads `[[]`/`[*]` alike
        r = glob_regex(pat.replace("[[]", "\0").replace("[*]", "\1"))
        return any(r.match(x.replace("[", "\0").replace("*", "\1")) for x in in_scope)
    keep = set()
    for p in excluded:
        parts = p.split("/")
        cands = [_glob_escape("/".join(parts[:i])) + "/**" for i in range(1, len(parts))] + [_glob_escape(p)]
        pat = next((c for c in cands if not hits_owned(c)), None)
        if pat:
            keep.add(pat)
    return sorted(keep)


# ----------------------------------------------------------------------------- command / env


def write_config(home: pathlib.Path, url: str, model: str, extra_body: dict | None = None) -> pathlib.Path:
    """ocr's config.json in jbr's ocr home: the jbr-local provider and nothing else."""
    cfg = home / ".opencodereview" / "config.json"
    state._write(cfg, {"provider": PROVIDER, "custom_providers": {PROVIDER: {
        "url": url, "protocol": "openai", "model": model, "api_key": "local",  # Ollama ignores it; ocr wants one
        "timeout_sec": REQUEST_TIMEOUT_S, "extra_body": extra_body if extra_body is not None else EXTRA_BODY}}})
    return cfg


def git_passthrough(root: pathlib.Path, run: Callable[..., Any]) -> dict[str, str]:
    """GIT_CONFIG_* for the user settings ocr's git loses with jbr's HOME, so its file list matches jbr's: the
    global ignore file, line-ending settings, and safe.directory (repos owned by another user / external disks)."""
    def get(*args: str) -> list[str]:
        p = run(["git", "config", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", stdin=subprocess.DEVNULL, timeout=30)
        return [ln for ln in (p.stdout or "").splitlines() if ln.strip()] if p.returncode == 0 else []
    pairs = []
    excludes = get("--path", "--get", "core.excludesFile")
    if not excludes:  # git's default when core.excludesFile is unset
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(pathlib.Path.home() / ".config")
        default = pathlib.Path(xdg) / "git" / "ignore"
        excludes = [str(default)] if default.exists() else []
    pairs += [("core.excludesFile", excludes[-1])] if excludes else []
    for key in ("core.autocrlf", "core.eol"):  # else ocr's `git diff HEAD` lists line-ending-only rewrites
        pairs += [(key, v) for v in get("--get", key)[-1:]]
    pairs += [("safe.directory", d) for d in get("--get-all", "safe.directory")]
    env = {"GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env if pairs else {}


def ocr_env(home: pathlib.Path, environ: dict[str, str] | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    env = {k: environ[k] for k in ENV_KEEP if k in environ}
    env.update(HOME=str(home), USERPROFILE=str(home),   # USERPROFILE: Go's home dir on Windows
               OCR_NO_UPDATE="1", OCR_LLM_TIMEOUT=str(REQUEST_TIMEOUT_S), NO_COLOR="1")
    env.update(extra or {})
    return env


def build_command(binary: str, *, model: str, effort: str, timeout_min: float, output: pathlib.Path,
                  commit: str = "", excludes: list[str] | None = None, background_file: str = "") -> list[str]:
    cmd = [binary, "review", "--provider", PROVIDER, "--model", model, "--format", "json", "--audience", "agent",
           "--output", str(output), "--effort", effort, "--concurrency", "1",   # Ollama serves one request at a time
           "--timeout", str(max(1, int(timeout_min)))]
    if commit:
        cmd += ["--commit", commit]
    if excludes:
        cmd += ["--exclude", ",".join(excludes)]
    if background_file:
        cmd += ["--background-file", background_file]
    return cmd


def normalize(raw: dict) -> list[dict]:
    """ocr comments -> findings (drops `thinking`: long, and not evidence)."""
    keep = ("path", "start_line", "end_line", "content", "existing_code", "suggestion_code", "severity", "category")
    return [{k: c[k] for k in keep if c.get(k) not in (None, "")} for c in raw.get("comments") or [] if isinstance(c, dict)]


def backend_ready(url: str, model: str, timeout: float = 10.0) -> str:
    """'' when the endpoint lists `model`, else why not. A plain GET of /models: it records nothing in the
    engine state and loads no model (a busy Ollama would time a `jbr probe` out and mark ollama_qwen
    UNAVAILABLE for the implementer chain)."""
    # a shell http_proxy must not route a loopback check through a remote proxy
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if router.is_local(url) else urllib.request.build_opener()
    try:
        with opener.open(url.rstrip("/") + "/models", timeout=timeout) as r:
            ids = [m.get("id") for m in json.loads(r.read().decode("utf-8")).get("data", [])]
    except Exception as e:  # down, refused, not JSON
        return f"{url} not reachable: {e!r}"[:300]
    return "" if model in ids or f"{model}:latest" in ids else f"{url} does not serve {model!r} (has {ids[:8]})"


# ----------------------------------------------------------------------------- run


def run_group(cmd: list[str], *, timeout: float, grace: float = 0.0, **kw: Any) -> subprocess.CompletedProcess:
    """subprocess.run for ocr in its own process group. At the timeout the group gets SIGTERM (Windows:
    CTRL_BREAK), which ocr answers by publishing a partial result; `grace` seconds later, or on any
    interruption, the whole tree is killed (the npm `ocr` launcher is a node process whose Go child would
    otherwise keep querying Ollama). TimeoutExpired is raised either way."""
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(cmd, **kw)
    try:
        try:
            return subprocess.CompletedProcess(cmd, p.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            if grace > 0:
                with contextlib.suppress(OSError):
                    if os.name == "nt":
                        p.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(p.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    p.wait(timeout=grace)
            raise
    finally:
        if p.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(p.pid, signal.SIGKILL)
            p.wait()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # os.kill would terminate it on Windows; ask the kernel instead
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.GetLastError() == 5  # access denied: it exists
        code = ctypes.c_ulong()
        try:
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_stale(lock: pathlib.Path) -> str | None:
    """The lock's content when its holder is gone, else None (the caller unlinks only that exact content)."""
    try:
        txt, mtime = lock.read_text(encoding="utf-8"), lock.stat().st_mtime
    except OSError:
        return None  # gone already: the next O_EXCL attempt decides
    try:
        held = json.loads(txt)
        # past its deadline by a margin (a sleeping Mac stops the holder's clock, not the wall clock), or dead
        gone = time.time() > float(held["deadline"]) + 120 or not _pid_alive(int(held["pid"]))
    except (ValueError, TypeError, KeyError):
        gone = time.time() - mtime > 10  # half-written or foreign content: stale once it has sat a while
    except OSError:
        gone = False
    return txt if gone else None


@contextlib.contextmanager
def _machine_lock(lock: pathlib.Path, wait_s: float, hold_s: float) -> Iterator[None]:
    """One prereview per machine. The lock holds {pid, deadline}; a waiter breaks it once the holder is dead or
    past its own deadline, and only its owner removes it. TimeoutError after wait_s."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    me = json.dumps({"pid": os.getpid(), "deadline": time.time() + hold_s})
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, me.encode())
            os.close(fd)
            break
        except FileExistsError:
            stale = _lock_stale(lock)
            if stale is not None:
                with contextlib.suppress(OSError):
                    if lock.read_text(encoding="utf-8") == stale:  # not a lock someone took meanwhile
                        lock.unlink()
                continue
        except PermissionError:  # Windows: the lock file is being deleted right now
            pass
        if time.monotonic() - t0 >= wait_s:
            raise TimeoutError(f"another prereview holds {lock}")
        time.sleep(LOCK_POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            if lock.read_text(encoding="utf-8") == me:
                lock.unlink()


def prereview(project_root: str | os.PathLike, wp_id: str, engines: dict[str, dict],
              packages_path: str | os.PathLike, prompts_dir: str | os.PathLike = "ops/prompts", *,
              tag: str = "", reports_dir: str | None = None, url: str = DEFAULT_URL, model: str = "",
              backend_engine: str = BACKEND_ENGINE, effort: str = DEFAULT_EFFORT,
              budget_min: float = DEFAULT_BUDGET_MIN, commit: str = "", spec: bool = True,
              allow_remote: bool = False, dry_run: bool = False,
              run: Callable[..., Any] | None = None, ready: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Run ocr once for one package and write the artifacts. Never raises for an engine-side problem:
    the result's `status` says what happened (OK | SKIPPED | FAILED | TIMEOUT).
    `run` replaces both the git calls and the ocr call (tests); by default git goes through
    subprocess.run and ocr through run_group. `ready` replaces backend_ready."""
    run_ocr = run or run_group
    run = run or subprocess.run
    ready = ready or backend_ready
    root = pathlib.Path(project_root).resolve()
    ap = artifact_paths(root, wp_id, tag, reports_dir)
    t0 = time.monotonic()
    notes: list[str] = []
    res: dict[str, Any] = {"wp": wp_id, "tool": "open-code-review", "status": FAILED, "reason": "", "url": url,
                           "model": model, "seconds": 0.0, "summary": {}, "warnings": [], "findings": [],
                           "paths": {k: str(v) for k, v in ap.items()}}
    exit_code = -1

    def finish(status: str, reason: str) -> dict[str, Any]:
        res.update(status=status, reason=reason, seconds=round(time.monotonic() - t0, 1))
        if dry_run:
            return res
        try:
            out = {k: v for k, v in res.items() if k not in {"paths", "argv"}}
            ap["json"].write_text(json.dumps(out, indent=2), encoding="utf-8")
            with open(ap["log"], "a", encoding="utf-8") as fh:
                fh.write("".join(f"# {n}\n" for n in notes) + f"# {status}: {reason}\n")
        finally:  # the marker is what pollers wait for: it is written even when the json/log write fails
            ap["done"].write_text(f"{exit_code} {res['seconds'] / 60:.1f}min {status}\n", encoding="utf-8")
        return res

    try:
        ap["dir"].mkdir(parents=True, exist_ok=True)
        if not dry_run:
            for k in ("done", "json", "raw"):
                with contextlib.suppress(FileNotFoundError):
                    ap[k].unlink()
            ap["log"].write_text(f"# jbr prereview {wp_id} url={url}\n", encoding="utf-8")
        if not router.is_local(url) and not allow_remote:
            return finish(SKIPPED, f"refusing non-local endpoint {url} (repository content would leave the machine; pass allow_remote)")
        binary = ocr_bin()
        if not binary:
            return finish(SKIPPED, "ocr not installed (npm i -g @alibaba-group/open-code-review, or set JBR_OCR_BIN)")
        if os.name == "nt" and binary.lower().endswith((".cmd", ".bat")):
            # cmd.exe would interpret & | % in file names inside --exclude
            return finish(SKIPPED, f"{binary} is a cmd.exe launcher: set JBR_OCR_BIN to the release opencodereview.exe")
        backend = engines.get(backend_engine) or {}
        if backend and not backend.get("available", True):
            return finish(SKIPPED, f"backend engine {backend_engine} is disabled (available=false / --available no)")
        model = model or backend.get("model", "")
        res["model"] = model
        if not model:
            return finish(SKIPPED, f"no model: --model not given and engine {backend_engine!r} has none")
        packages, _ = runner.load_packages(packages_path)
        owned = runner._as_list(runner.find_package(packages, wp_id).get("files"))
        if not owned and not commit:
            return finish(SKIPPED, "package lists no `files`: in a shared checkout the workspace holds other packages' work")
        in_scope, excluded = split_scope(changed_paths(root, commit, run), owned)
        notes.append(f"scope: {len(in_scope)} owned changed file(s) reviewed, {len(excluded)} other changed file(s) excluded")
        res["scope"] = in_scope
        if not in_scope:
            return finish(SKIPPED, "no changed files owned by this package")
        excludes = exclude_patterns(excluded, in_scope)
        if len(",".join(excludes)) > MAX_EXCLUDE_CHARS:
            return finish(SKIPPED, f"{len(excludes)} exclude patterns would overflow the command line")
        spec_file = pathlib.Path(prompts_dir) / f"{wp_id}.md"
        spec_file = spec_file if spec_file.is_absolute() else root / spec_file
        background = str(spec_file) if spec and spec_file.exists() else ""
        home = ocr_home()
        if home.resolve() == pathlib.Path.home().resolve():
            return finish(SKIPPED, "JBR_OCR_HOME is your home: jbr would overwrite your own ~/.opencodereview config")

        def command(minutes: float) -> list[str]:
            return build_command(binary, model=model, effort=effort, timeout_min=minutes, output=ap["raw"],
                                 commit=commit, excludes=excludes, background_file=background)
        if dry_run:
            res.update(argv=command(budget_min), env=sorted(ocr_env(home)))
            return finish(OK, "dry-run: ocr not executed")
        why = ready(url, model)
        if why:
            return finish(SKIPPED, f"backend not ready: {why}")
        budget_s = budget_min * 60
        # one prereview per machine: parallel packages would only queue inside Ollama and time out there
        try:
            with _machine_lock(home / "prereview.lock", wait_s=min(LOCK_WAIT_MAX_S, budget_s), hold_s=budget_s + 60):
                left = budget_s - (time.monotonic() - t0)
                if left < 60:
                    return finish(TIMEOUT, f"under a minute of the {budget_min:g} min budget left after waiting")
                write_config(home, url, model)
                shutil.rmtree(home / ".opencodereview" / "sessions", ignore_errors=True)  # no diff archive in ocr-home
                # ocr's --timeout is per file group and scaled by its review rounds: stop inside ocr before our kill
                cmd = command(left * 0.9 / 60 / ROUNDS.get(effort, 1))
                res["argv"] = cmd
                notes.append("argv: " + json.dumps(cmd))
                env = ocr_env(home, extra=git_passthrough(root, run))
                kw = {"grace": GRACE_S} if run_ocr is run_group else {}
                with open(ap["log"], "a", encoding="utf-8") as fh:
                    try:
                        p = run_ocr(cmd, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, timeout=max(1.0, left - GRACE_S), **kw)
                    except subprocess.TimeoutExpired:
                        p = None
        except TimeoutError:
            return finish(SKIPPED, f"busy: another prereview held the machine lock for {min(LOCK_WAIT_MAX_S, budget_s):.0f}s")
        if p is None:  # stopped at the budget: ocr publishes what it has on SIGTERM
            try:
                raw = json.loads(ap["raw"].read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return finish(TIMEOUT, f"ocr still running after the {budget_min:g} min budget (killed, no partial result)")
            res.update(summary=raw.get("summary") or {}, warnings=raw.get("warnings") or [], findings=normalize(raw))
            if not res["findings"]:
                return finish(TIMEOUT, f"stopped at the {budget_min:g} min budget with no findings yet")
            return finish(OK, f"{len(res['findings'])} finding(s) from a partial result (stopped at the {budget_min:g} min budget)")
        exit_code = p.returncode
        try:
            raw = json.loads(ap["raw"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return finish(FAILED, f"ocr exit {p.returncode} without a JSON result (see log)")
        res.update(summary=raw.get("summary") or {}, warnings=raw.get("warnings") or [], findings=normalize(raw))
        if raw.get("status") == "skipped":
            return finish(SKIPPED, raw.get("message") or "ocr found no supported files")
        if p.returncode != 0:
            return finish(FAILED, f"ocr exit {p.returncode} (status {raw.get('status')!r})")
        return finish(OK, f"{len(res['findings'])} finding(s), ocr status {raw.get('status')!r}")
    except Exception as e:  # a prereview problem must never become a build problem
        notes.append(traceback.format_exc().rstrip())
        try:
            return finish(FAILED, f"jbr prereview crashed: {e!r}")
        except Exception:
            res.update(status=FAILED, reason=f"jbr prereview crashed: {e!r}")
            return res


def spawn(project_root: str | os.PathLike, wp_id: str, args: list[str], tag: str = "",
          reports_dir: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Launch `python -m jbr prereview <wp_id> <args...>` detached (the workflow agent polls the .done)."""
    root = pathlib.Path(project_root).resolve()
    ap = artifact_paths(root, wp_id, tag, reports_dir)
    ap["dir"].mkdir(parents=True, exist_ok=True)
    if not dry_run and ap["done"].exists():
        ap["done"].unlink()
    argv = [sys.executable, "-m", "jbr", "prereview", wp_id, "--project", str(root), *args]
    if tag and "--tag" not in args:
        argv += ["--tag", tag]
    if dry_run:
        return {"pid": None, "argv": argv, "marker": str(ap["done"])}
    return {"pid": runner.launch_detached(argv, root, ap["stdout"]), "argv": argv, "marker": str(ap["done"])}
