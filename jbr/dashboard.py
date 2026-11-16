"""`jbr dashboard`: a local page to see every engine and switch it on or off.

Serves one HTML page and a small JSON API on 127.0.0.1 only (stdlib http.server, no dependencies):

    GET  /                 the page
    GET  /api/engines      every engine: switch, effective on/off, last known status, track record
    POST /api/switch       {"engine": k, "state": "on" | "off" | "default", "note": "..."}
    POST /api/probe        {"engine": k}: one live probe now (the same `jbr probe` every build uses)

Switches are saved with jbr.switches, so every jbr command on this machine obeys them, not only the
page. Loading the page never calls an engine: statuses come from the shared state file
(~/.jbr/engine_state.json); only the Probe button runs a live probe.

A local server can be reached by any web page the user has open (CSRF, DNS rebinding), so every POST
must carry the per-launch token embedded in the page (X-JBR-Token), and every request's Host header
must name the loopback address and port the server is bound to.
"""
from __future__ import annotations

import html
import json
import pathlib
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jbr import ledger, router, state, switches

DEFAULT_PORT = 8765
_PROBE_LOCK = threading.Lock()


def engine_rows(engines_path: str | pathlib.Path) -> list[dict[str, Any]]:
    """One row per engine, by priority. Never probes live."""
    shipped = router.load_engines(engines_path, use_switches=False)
    saved = switches.load()
    engines = switches.apply(router.load_engines(engines_path, use_switches=False), saved)
    records = state.load()
    card = ledger.scorecard(ledger.load())
    rows = []
    for k in router.by_priority(engines):
        e = engines[k]
        p = state.probe(k, engines, live=False)
        rec = records.get(state.bucket_of(k, e), {})
        status, line = p.status, p.line()
        if status == state.UNKNOWN and rec.get("last_status"):
            # the AVAILABLE cache (10 min) or the backoff window has run out: show what the last probe said
            status = rec["last_status"]
            line = f"last seen {status}" + (f": {rec['detail'][:120]}" if rec.get("detail") else "")
        rows.append({
            "engine": k, "runner": e.get("runner", "agy"), "model": e.get("model", ""), "priority": e.get("priority"),
            "on": bool(e.get("available", True)), "default_on": bool(shipped[k].get("available", True)),
            "switch": saved.get(k), "explicit_only": router.is_explicit_only(e),
            "status": status, "status_line": line, "stale": p.status == state.UNKNOWN, "last_probe": rec.get("last_probe"),
            "description": e.get("description", ""), "record": card.get(k), "local": bool(e.get("local_provider")),
        })
    return rows


def probe_now(engine_key: str, engines_path: str | pathlib.Path) -> dict[str, str]:
    with _PROBE_LOCK:  # one live probe at a time from the page
        p = state.probe(engine_key, router.load_engines(engines_path), live=True)
    return {"engine": engine_key, "status": p.status, "status_line": p.line()}


class _Handler(BaseHTTPRequestHandler):
    server_version = "jbr-dashboard"
    token = ""
    engines_path = ""
    allowed_hosts: set[str] = set()

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet: the terminal shows only the URL
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")  # no other page may frame the buttons (clickjacking)
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: Any) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.allowed_hosts

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if not self._host_ok():
            return self._json(403, {"error": "bad Host header"})
        if self.path == "/":
            return self._send(200, page(self.token).encode("utf-8"), "text/html; charset=utf-8")
        if self.path == "/api/engines":
            return self._json(200, {"engines": engine_rows(self.engines_path), "switches_file": str(switches.switches_path())})
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        given = self.headers.get("X-JBR-Token", "").encode("utf-8", "replace")
        if not self._host_ok() or not secrets.compare_digest(given, self.token.encode("ascii")):
            return self._json(403, {"error": "forbidden"})
        try:
            n = max(0, min(int(self.headers.get("Content-Length") or 0), 10_000))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be a JSON object"})
        engines = router.load_engines(self.engines_path, use_switches=False)
        key = body.get("engine")
        if key not in engines:
            return self._json(400, {"error": f"unknown engine {key!r}"})
        if self.path == "/api/switch":
            wanted = {"on": True, "off": False, "default": None}
            if body.get("state") not in wanted:
                return self._json(400, {"error": "state must be on, off or default"})
            switches.set_switch(key, wanted[body["state"]], str(body.get("note", ""))[:200])
            return self._json(200, {"ok": True})
        if self.path == "/api/probe":
            return self._json(200, probe_now(key, self.engines_path))
        return self._json(404, {"error": "not found"})


def make_server(engines_path: str | pathlib.Path, port: int = DEFAULT_PORT) -> tuple[ThreadingHTTPServer, str, str]:
    """(server, url, token); port 0 picks a free port. Bound to 127.0.0.1 only."""
    handler = type("Handler", (_Handler,), {"token": secrets.token_urlsafe(24), "engines_path": str(engines_path)})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    port = httpd.server_address[1]
    handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return httpd, f"http://127.0.0.1:{port}/", handler.token


def serve(engines_path: str | pathlib.Path, port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    httpd, url, _ = make_server(engines_path, port)
    print(f"jbr dashboard: {url}  (Ctrl-C to stop; switches saved in {switches.switches_path()})", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def page(token: str) -> str:
    return _PAGE.replace("__TOKEN__", html.escape(token, quote=True))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="jbr-token" content="__TOKEN__">
<title>jbr engines</title>
<style>
:root{--bg:#f4f1ea;--ink:#1d1b17;--mute:#6d675c;--rule:#d8d1c3;--on:#2f6b3a;--off:#9a3b2c;--warn:#8a6414;--chip:#ebe5d8;
font-family:ui-sans-serif,-apple-system,"Segoe UI",system-ui,"Noto Sans TC",sans-serif}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171613;--ink:#ece6da;--mute:#9b9486;--rule:#33302a;
--on:#7fbf8a;--off:#e08a78;--warn:#d8b25a;--chip:#24221d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.45}
main{max-width:1180px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;border-bottom:2px solid var(--ink);padding-bottom:10px}
h1{font:600 22px/1.2 ui-serif,Georgia,"Noto Serif TC",serif;margin:0;letter-spacing:.01em}
.meta{color:var(--mute);font-size:12.5px}
.note{margin:14px 0 18px;color:var(--mute);max-width:72ch}
table{width:100%;border-collapse:collapse}
th{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);text-align:left;font-weight:600;
padding:8px 10px;border-bottom:1px solid var(--rule)}
td{padding:11px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
.k{font:600 13.5px ui-monospace,SFMono-Regular,Menlo,monospace}
.sub{color:var(--mute);font-size:12px;margin-top:2px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.seg{display:inline-flex;border:1px solid var(--ink);border-radius:3px;overflow:hidden}
.seg button{all:unset;cursor:pointer;padding:4px 10px;font-size:12px;font-weight:600}
.seg button+button{border-left:1px solid var(--ink)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.seg button:focus-visible,.btn:focus-visible{outline:2px solid var(--warn);outline-offset:2px}
.src{display:block;color:var(--mute);font-size:11.5px;margin-top:5px}
.st{font-weight:600}.st.AVAILABLE{color:var(--on)}.st.EXHAUSTED{color:var(--warn)}.st.UNAVAILABLE{color:var(--off)}
.st.UNKNOWN{color:var(--mute)}.st.stale{font-weight:500;opacity:.8}
.btn{all:unset;cursor:pointer;font-size:12px;border-bottom:1px solid currentColor;color:var(--mute)}
.btn:hover{color:var(--ink)}
.rec{font-variant-numeric:tabular-nums;font-size:12.5px}
.chip{display:inline-block;background:var(--chip);padding:1px 6px;border-radius:2px;font-size:11px;color:var(--mute)}
#msg{min-height:20px;margin-top:12px;color:var(--warn);font-size:12.5px}
@media (max-width:760px){thead{display:none}tr{display:block;border-bottom:1px solid var(--rule);padding:8px 0}
td{display:block;border:0;padding:4px 0}.lbl{display:inline}}
.lbl{display:none;color:var(--mute)}
</style></head><body><main>
<header><h1>jbr engines</h1><span class="meta" id="meta">loading</span></header>
<p class="note">Switches apply to every jbr command and build on this machine. Default follows engines.json.
Statuses come from the last probe; Probe checks one engine live now.</p>
<table><thead><tr><th>Engine</th><th>Switch</th><th>Status</th><th>Priority</th><th>Track record</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="msg" role="status" aria-live="polite"></div>
</main>
<script>
const TOKEN=document.querySelector('meta[name=jbr-token]').content;
const $=s=>document.querySelector(s);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-JBR-Token':TOKEN},
body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
function rec(r){if(!r)return '<span class="sub">no builds recorded</span>';
return `${r.accepted}/${r.packages} accepted<div class="sub">${r.mean_fix_rounds} fix rounds/pkg${r.mean_minutes!=null?`, ${r.mean_minutes} min/pkg`:''}</div>`}
function seg(e){const cur=e.switch?(e.switch.available?'on':'off'):'default';
return `<div class="seg" role="group" aria-label="Switch ${esc(e.engine)}">${['on','off','default'].map(s=>
`<button data-e="${esc(e.engine)}" data-s="${s}" aria-pressed="${cur===s}">${s==='default'?'Default':s==='on'?'On':'Off'}</button>`).join('')}</div>
<span class="src">${e.on?'on':'off'} now${e.switch?` (saved ${esc((e.switch.at||'').slice(0,10))})`:` (engines.json: ${e.default_on?'on':'off'})`}</span>`}
async function load(){try{const r=await fetch('/api/engines');const j=await r.json();
$('#meta').textContent=`${j.engines.length} engines, switches in ${j.switches_file}`;
$('#rows').innerHTML=j.engines.map(e=>`<tr>
<td><div class="k">${esc(e.engine)}</div><div class="sub">${esc(e.runner)} · ${esc(e.model)}</div>
${e.explicit_only?'<span class="chip">only when assigned</span>':''}</td>
<td>${seg(e)}</td>
<td><span class="st ${esc(e.status)}${e.stale?' stale':''}">${esc(e.status_line)}</span><div class="sub">${e.last_probe?'last probe '+esc(e.last_probe.replace('T',' ').slice(0,16)):'never probed'}</div>
${e.runner!=='claude_subagent'&&e.runner!=='manual'?`<button class="btn" data-probe="${esc(e.engine)}" data-local="${e.local?1:''}">Probe now</button>`:''}</td>
<td class="rec"><span class="lbl">priority </span>${e.priority??''}</td><td class="rec">${rec(e.record)}</td></tr>`).join('')}
catch(err){$('#msg').textContent='Could not load engines: '+err.message}}
document.addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;
try{if(b.dataset.s){await post('/api/switch',{engine:b.dataset.e,state:b.dataset.s});$('#msg').textContent=`${b.dataset.e}: ${b.dataset.s}`}
else if(b.dataset.probe){if(b.dataset.local&&!confirm(`${b.dataset.probe} runs a local model: probing loads it into memory (about 18 GB). Run /system-check first if anything else is using the GPU. Probe now?`))return;
b.textContent='Probing…';const r=await post('/api/probe',{engine:b.dataset.probe});
$('#msg').textContent=`${r.engine}: ${r.status_line}`}await load()}catch(err){$('#msg').textContent='Failed: '+err.message}});
load();
</script></body></html>
"""
