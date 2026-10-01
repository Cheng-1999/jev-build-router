"""Live engine availability: a shared JSON state file, reset-hint parsing, cheap live probes, pick.

State file: $JBR_STATE or ~/.jbr/engine_state.json. One record per quota BUCKET:

    {"agy_gemini_pro": {"exhausted_until": "2026-10-02T05:12:00Z" | null,
                        "last_probe": "2026-10-02T02:58:41Z",
                        "last_status": "AVAILABLE" | "EXHAUSTED" | "UNAVAILABLE",
                        "detail": "Resets in 2h9m31s" ...,
                        "engines": ["agy_gemini_pro"]}, ...}

Buckets: every agy engine on a Claude model shares ONE Claude quota (agy Opus and agy Sonnet drain
the same bucket), so they are stored under the key `agy_claude`; every other engine is its own
bucket. `claude_subagent` (runner claude_subagent) is never probed and always AVAILABLE.

Concurrency: every read-modify-write holds a lock file (O_CREAT|O_EXCL, stale after 60 s) and
writes via temp file + os.replace, so concurrent jbr processes never lose an update and a
reader never sees a half-written file. A live probe additionally holds a per-bucket probe lock,
and re-checks the state after acquiring it, so N parallel runner agents cause one live probe.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Iterator

from jbr import runner
from jbr.router import by_priority

AVAILABLE, EXHAUSTED, UNAVAILABLE, UNKNOWN = "AVAILABLE", "EXHAUSTED", "UNAVAILABLE", "UNKNOWN"
USABLE = {AVAILABLE, UNKNOWN}       # UNKNOWN = not probed (offline mode): optimistic, the run itself will tell
EXIT_CODES = {AVAILABLE: 0, UNKNOWN: 0, EXHAUSTED: 3, UNAVAILABLE: 4}

AVAILABLE_CACHE = timedelta(minutes=10)     # no live probe inside this window after an AVAILABLE
UNAVAILABLE_BACKOFF = timedelta(minutes=15)  # non-quota probe failure: skip the engine this long
DEFAULT_RESET = timedelta(minutes=30)        # quota without a parseable reset hint
PROBE_TIMEOUT_S = 120
PROBE_PROMPT = "Reply only READY."
CODEX_PROBE_PROMPT = "Reply only READY. Do not run tools."
CLAUDE_BUCKET = "agy_claude"
AGY_FALLBACK_PATHS = [pathlib.Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"]

READY_RE = re.compile(r"^\W*READY\W*$", re.MULTILINE)


# ----------------------------------------------------------------------------- time helpers


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# "2h9m31s", "27m28s", "45s", "1h" (spaces tolerated: "2h 9m")
_DUR_RE = re.compile(r"^(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$", re.IGNORECASE)
# "in 5 minutes", "2 hours", "30min"
_WORDS_RE = re.compile(r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.IGNORECASE)
# "Sep 25th, 2026 3:49 AM" and its space-stripped .done form "Sep25th,20263:49AM"
_DATE_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4}),?\s*(?:at\s*)?(\d{1,2}):(\d{2})\s*([AaPp][Mm])?")
# "3:05 PM", "3:05PM", "15:05"
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?")


def _hour24(h: int, ampm: str | None) -> int:
    if not ampm:
        return h
    pm = ampm.lower() == "pm"
    return (h % 12) + (12 if pm else 0)


def local_tz() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_reset_hint(hint: str | None, now: datetime | None = None, tz: tzinfo | None = None) -> datetime:
    """Absolute UTC time at which a quota resets.

    Accepts the reset hints captured by runner.RESET_RE, with or without spaces (the .done marker
    strips them): durations "2h9m31s" / "27m28s" (relative to now), clock times "3:05 PM"
    (next occurrence, in `tz`, default the machine's local zone: the CLIs print local time) and
    dates "Sep 25th, 2026 3:49 AM" (in `tz`). Anything unparseable -> now + 30 min.
    """
    now = now or utcnow()
    tz = tz or local_tz()
    text = (hint or "").strip().rstrip(".")
    text = re.sub(r"^(?:resets?\s*in|try\s*again\s*(?:at|in)|in|at)\s*", "", text, flags=re.IGNORECASE)
    if not text:
        return now + DEFAULT_RESET
    m = _DUR_RE.match(text)
    if m and any(m.groups()):
        d, h, mi, s = (int(g or 0) for g in m.groups())
        return now + timedelta(days=d, hours=h, minutes=mi, seconds=s)
    words = _WORDS_RE.findall(text)
    if words and not _CLOCK_RE.search(text):
        delta = timedelta()
        for n, unit in words:
            u = unit.lower()
            delta += timedelta(days=int(n)) if u.startswith("d") else timedelta(hours=int(n)) if u.startswith("h") \
                else timedelta(minutes=int(n)) if u.startswith("m") else timedelta(seconds=int(n))
        return now + delta
    m = _DATE_RE.search(text)
    if m and m.group(1).lower()[:3] in _MONTHS:
        mon, day, year, hh, mm, ampm = m.groups()
        try:
            local = datetime(int(year), _MONTHS[mon.lower()[:3]], int(day), _hour24(int(hh), ampm), int(mm), tzinfo=tz)
            return local.astimezone(timezone.utc)
        except ValueError:
            pass
    m = _CLOCK_RE.search(text)
    if m:
        hh, mm, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if hh <= 23 and mm <= 59:
            now_local = now.astimezone(tz)
            cand = now_local.replace(hour=_hour24(hh, ampm), minute=mm, second=0, microsecond=0)
            if cand <= now_local:
                cand += timedelta(days=1)
            return cand.astimezone(timezone.utc)
    return now + DEFAULT_RESET


def reset_hint_of(text: str) -> str:
    """The raw reset hint in an engine transcript ('' when none)."""
    m = runner.RESET_RE.search(text or "")
    return (m.group(1) or m.group(2)).strip() if m else ""


# ----------------------------------------------------------------------------- state file


def state_path() -> pathlib.Path:
    env = os.environ.get("JBR_STATE")
    return pathlib.Path(env) if env else pathlib.Path.home() / ".jbr" / "engine_state.json"


def bucket_of(engine_key: str, engine: dict | None) -> str:
    """Quota bucket: all agy engines on a Claude model share one."""
    e = engine or {}
    if e.get("runner") == "agy" and str(e.get("model", "")).lower().startswith("claude"):
        return CLAUDE_BUCKET
    return engine_key


@contextlib.contextmanager
def _lock(lock: pathlib.Path, timeout: float = 30.0, stale: float = 60.0) -> Iterator[None]:
    lock.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale:  # holder died: break the lock
                    lock.unlink()
                    continue
            except (FileNotFoundError, PermissionError):
                pass
        except PermissionError:  # Windows: the lock file is being deleted right now
            pass
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(f"could not lock {lock} within {timeout}s")
        time.sleep(0.005)
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError, PermissionError):
            lock.unlink()


def _retry(fn: Callable[[], Any], tries: int = 200) -> Any:
    """Windows refuses replace/open while another process holds the file for a moment."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.005)


def load(path: pathlib.Path | None = None) -> dict:
    p = path or state_path()

    def read() -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            return {}  # never written by us half-way (atomic replace); a hand-broken file resets

    data = _retry(read)
    return data if isinstance(data, dict) else {}


def _write(p: pathlib.Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        _retry(lambda: os.replace(tmp, p))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def update(fn: Callable[[dict], None], path: pathlib.Path | None = None) -> dict:
    """Locked read-modify-write; fn mutates the state dict in place. Returns the new state."""
    p = path or state_path()
    with _lock(p.with_name(p.name + ".lock")):
        data = load(p)
        fn(data)
        _write(p, data)
        return data


def record(engine_key: str, engine: dict | None, status: str, *, until: datetime | None = None,
           detail: str = "", now: datetime | None = None, path: pathlib.Path | None = None) -> dict:
    """Store one probe/run outcome for the engine's bucket."""
    now = now or utcnow()
    bucket = bucket_of(engine_key, engine)

    def fn(data: dict) -> None:
        rec = data.setdefault(bucket, {})
        rec["last_probe"] = iso(now)
        rec["last_status"] = status
        rec["exhausted_until"] = None if status == AVAILABLE else iso(until)
        rec["detail"] = detail
        rec["engines"] = sorted(set(rec.get("engines", [])) | {engine_key})

    return update(fn, path)[bucket]


def mark_exhausted(engine_key: str, engine: dict | None, hint: str = "", *, now: datetime | None = None,
                   path: pathlib.Path | None = None, tz: tzinfo | None = None) -> datetime:
    """Quota hit (by a probe or a real run): exhausted until the parsed reset time."""
    now = now or utcnow()
    until = parse_reset_hint(hint, now, tz)
    record(engine_key, engine, EXHAUSTED, until=until, detail=f"quota; reset hint {hint!r}" if hint else "quota; no reset hint", now=now, path=path)
    return until


# ----------------------------------------------------------------------------- probe


@dataclass
class Probe:
    engine: str
    status: str            # AVAILABLE | EXHAUSTED | UNAVAILABLE | UNKNOWN
    until: datetime | None = None
    detail: str = ""
    cached: bool = False

    @property
    def usable(self) -> bool:
        return self.status in USABLE

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def line(self) -> str:
        if self.status == EXHAUSTED:
            return f"EXHAUSTED {iso(self.until)}" + (f" ({self.detail})" if self.detail else "")
        if self.status == UNAVAILABLE:
            return f"UNAVAILABLE {self.detail}".rstrip()
        if self.status == UNKNOWN:
            return "UNKNOWN (not probed)"
        return "AVAILABLE"


def _bin(name: str) -> str:
    env = os.environ.get(f"JBR_{name.upper()}_BIN")
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    if name == "agy":
        for p in AGY_FALLBACK_PATHS:
            if p.exists():
                return str(p)
    return name


def probe_command(engine: dict) -> list[str] | None:
    """The cheap live-probe argv for an engine; None for claude_subagent."""
    r, model = engine.get("runner", "agy"), engine.get("model", "")
    if r == "claude_subagent":
        return None
    if r == "agy":
        return [_bin("agy"), "--model", model, "--print-timeout", "45s", "--print", PROBE_PROMPT]
    if r == "codex":
        # NB: this codex build has no --full-auto (it exits immediately); -s workspace-write is the sandbox flag
        return [_bin("codex"), "exec", "--skip-git-repo-check", "-m", model, "-s", "workspace-write", CODEX_PROBE_PROMPT]
    raise ValueError(f"unknown runner {r!r}")


def classify_probe(returncode: int | None, output: str) -> tuple[str, str]:
    """(status, hint_or_reason) of one live probe. returncode None = timed out."""
    if runner.QUOTA_RE.search(output or ""):
        return EXHAUSTED, reset_hint_of(output)
    if READY_RE.search(output or ""):
        return AVAILABLE, ""
    if returncode is None:
        return UNAVAILABLE, f"probe timed out after {PROBE_TIMEOUT_S}s"
    last = next((ln.strip() for ln in reversed((output or "").splitlines()) if ln.strip()), "no output")
    return UNAVAILABLE, f"exit {returncode}: {last[:200]}"


def _from_record(engine_key: str, rec: dict, now: datetime) -> Probe | None:
    """A decision from the state file alone, or None when a live probe is needed."""
    until = parse_iso(rec.get("exhausted_until"))
    if until and until > now:
        detail = rec.get("detail", "")
        if rec.get("last_status") == UNAVAILABLE:
            detail = f"after UNAVAILABLE: {detail}"
        return Probe(engine_key, EXHAUSTED, until, detail, cached=True)
    last = parse_iso(rec.get("last_probe"))
    if rec.get("last_status") == AVAILABLE and last and now - last < AVAILABLE_CACHE:
        return Probe(engine_key, AVAILABLE, cached=True)
    return None


def probe(engine_key: str, engines: dict[str, dict], *, live: bool = True, now: datetime | None = None,
          path: pathlib.Path | None = None, run: Callable[..., Any] | None = None,
          clock: Callable[[], datetime] | None = None) -> Probe:
    """Availability of one engine. Exhausted window and AVAILABLE cache are honoured without calling
    the engine; otherwise (live=True) one cheap live probe is run and recorded.
    run/clock default to subprocess.run / utcnow (looked up at call time, so tests can patch them)."""
    run = run or subprocess.run
    clock = clock or utcnow
    if engine_key not in engines:
        raise KeyError(f"unknown engine {engine_key!r}; known: {sorted(engines)}")
    eng = engines[engine_key]
    if eng.get("runner") == "claude_subagent":
        return Probe(engine_key, AVAILABLE, detail="claude_subagent is never probed")
    if not eng.get("available", True):
        return Probe(engine_key, UNAVAILABLE, detail="disabled (available=false / --available no)")
    p = path or state_path()
    bucket = bucket_of(engine_key, eng)
    hit = _from_record(engine_key, load(p).get(bucket, {}), now or clock())
    if hit or not live:
        return hit or Probe(engine_key, UNKNOWN)
    with _lock(p.with_name(f"{p.name}.probe-{bucket}.lock"), timeout=PROBE_TIMEOUT_S + 60, stale=PROBE_TIMEOUT_S + 90):
        hit = _from_record(engine_key, load(p).get(bucket, {}), now or clock())  # someone probed while we waited
        if hit:
            return hit
        cmd = probe_command(eng)
        try:
            proc = run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, cwd=str(p.parent) if p.parent.exists() else None)
            rc, out = proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired as e:
            rc, out = None, ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        except OSError as e:  # binary missing / not executable
            rc, out = 127, f"cannot run {cmd[0]}: {e}"
        status, info = classify_probe(rc, out)
        t = now or clock()
        if status == AVAILABLE:
            record(engine_key, eng, AVAILABLE, now=t, path=p)
            return Probe(engine_key, AVAILABLE)
        if status == EXHAUSTED:
            until = mark_exhausted(engine_key, eng, info, now=t, path=p)
            return Probe(engine_key, EXHAUSTED, until, f"quota; reset hint {info!r}" if info else "quota; no reset hint")
        until = t + UNAVAILABLE_BACKOFF
        record(engine_key, eng, UNAVAILABLE, until=until, detail=info, now=t, path=p)
        return Probe(engine_key, UNAVAILABLE, until, info)


def pick(engines: dict[str, dict], exclude: set[str] | None = None, **kw: Any) -> tuple[str, list[Probe]]:
    """First usable engine by priority (probing each on the way); claude_subagent when none is."""
    exclude = exclude or set()
    seen: list[Probe] = []
    for k in by_priority(engines):
        if k in exclude:
            continue
        r = probe(k, engines, **kw)
        seen.append(r)
        if r.usable:
            return k, seen
    return "claude_subagent", seen


def availability(engines: dict[str, dict], live: bool = True, **kw: Any) -> dict[str, dict]:
    """{engine: {"status", "detail", "until", "priority"}} for every engine (probing live when asked)."""
    out = {}
    for k in by_priority(engines):
        r = probe(k, engines, live=live, **kw)
        out[k] = {"status": r.status, "detail": r.detail, "until": iso(r.until), "priority": engines[k].get("priority")}
    return out
