"""`jbr dashboard`: a local page to see every engine and switch it on or off.

Serves one HTML page and a small JSON API on 127.0.0.1 only (stdlib http.server, no dependencies):

    GET  /                 the page
    GET  /api/engines      every engine: switch, effective on/off, last known status, track record
    GET  /api/teams        configured teams, allowed engines for role lists, and validation errors
    POST /api/switch       {"engine": k, "state": "on" | "off" | "default", "note": "..."}
    POST /api/probe        {"engine": k}: one live probe now (the same `jbr probe` every build uses)
    POST /api/teams        {"teams": {...}}: validate and save the full teams object (<= 64 KB)

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

from jbr import ledger, router, state, switches, teams

DEFAULT_PORT = 8765
MAX_TEAMS_BODY = 64 * 1024
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


def teams_payload(engines_path: str | pathlib.Path) -> dict[str, Any]:
    engines = router.load_engines(engines_path, use_switches=False)
    try:
        t = teams.load(strict=True)
    except ValueError as e:  # show the problem and block Save, so the editor never overwrites a broken file
        return {"teams": {}, "engines": teams.allowed_engines(engines), "errors": [str(e)], "file_error": str(e)}
    return {"teams": t, "engines": teams.allowed_engines(engines), "errors": teams.validate(t, engines)}


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
        if self.path == "/api/teams":
            return self._json(200, teams_payload(self.engines_path))
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        given = self.headers.get("X-JBR-Token", "").encode("utf-8", "replace")
        if not self._host_ok() or not secrets.compare_digest(given, self.token.encode("ascii")):
            return self._json(403, {"error": "forbidden"})
        try:
            raw_len = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if raw_len > MAX_TEAMS_BODY:
            self.close_connection = True  # do not drain: a client that claims more than it sends would block this thread
            return self._json(400, {"error": f"body too large (max {MAX_TEAMS_BODY} bytes)", "errors": ["body exceeds 64 KB"]})
        try:
            n = max(0, raw_len) if self.path == "/api/teams" else max(0, min(raw_len, 10_000))
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be a JSON object"})
        engines = router.load_engines(self.engines_path, use_switches=False)
        if self.path == "/api/teams":
            if "teams" not in body or not isinstance(body["teams"], dict):
                return self._json(400, {"error": "body must contain a 'teams' object", "errors": ["body must contain a 'teams' object"]})
            errs = teams.validate(body["teams"], engines)
            if errs:
                return self._json(400, {"error": "; ".join(errs), "errors": errs})
            try:
                saved = teams.save(body["teams"], engines=engines)
            except ValueError as e:  # the file on disk is malformed: refuse to overwrite it
                return self._json(409, {"error": str(e), "errors": [str(e)]})
            except (TimeoutError, OSError) as e:
                return self._json(500, {"error": f"could not save teams: {e}", "errors": [f"could not save teams: {e}"]})
            return self._json(200, {"ok": True, "teams": saved, "errors": []})
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
h1,h2{font:600 22px/1.2 ui-serif,Georgia,"Noto Serif TC",serif;margin:0;letter-spacing:.01em}
h2{font-size:19px}
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
.seg button:focus-visible,.btn:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--warn);outline-offset:2px}
.src{display:block;color:var(--mute);font-size:11.5px;margin-top:5px}
.st{font-weight:600}.st.AVAILABLE{color:var(--on)}.st.EXHAUSTED{color:var(--warn)}.st.UNAVAILABLE{color:var(--off)}
.st.UNKNOWN{color:var(--mute)}.st.stale{font-weight:500;opacity:.8}
.btn{all:unset;cursor:pointer;font-size:12px;border-bottom:1px solid currentColor;color:var(--mute)}
.btn:hover{color:var(--ink)}
.btn-prim{all:unset;cursor:pointer;padding:5px 12px;font-size:12.5px;font-weight:600;background:var(--ink);color:var(--bg);border-radius:3px}
.rec{font-variant-numeric:tabular-nums;font-size:12.5px}
.chip{display:inline-block;background:var(--chip);padding:1px 6px;border-radius:2px;font-size:11px;color:var(--mute)}
#msg,#team-msg{min-height:20px;margin-top:12px;color:var(--warn);font-size:12.5px}
#team-err{color:var(--off);font-size:12.5px;margin:8px 0}
.teams-sec{margin-top:36px}
.team-block{border:1px solid var(--rule);border-radius:4px;padding:12px;margin-bottom:14px}
.team-hdr,.role-row,.chips{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.team-hdr{padding-bottom:8px;border-bottom:1px solid var(--rule);margin-bottom:8px}
.role-row{padding:8px 0;border-bottom:1px solid var(--rule);align-items:flex-start}
.role-row:last-child{border-bottom:0}
.in{background:var(--bg);color:var(--ink);border:1px solid var(--rule);border-radius:3px;padding:4px 7px;font:inherit;font-size:12.5px;max-width:100%}
textarea.in{width:100%;min-height:46px;resize:vertical;margin-top:6px}
.eng-chip{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;font-size:12px;color:var(--ink)}
.eng-chip button{all:unset;cursor:pointer;font-size:11px;color:var(--mute);padding:0 2px}
.eng-chip button:hover{color:var(--ink)}
@media (max-width:760px){thead{display:none}tr{display:block;border-bottom:1px solid var(--rule);padding:8px 0}
td{display:block;border:0;padding:4px 0}.lbl{display:inline}.team-hdr,.role-row{flex-direction:column;align-items:stretch}}
.lbl{display:none;color:var(--mute)}
</style></head><body><main>
<header><h1>jbr engines</h1><span class="meta" id="meta">loading</span></header>
<p class="note">Switches apply to every jbr command and build on this machine. Default follows engines.json.
Statuses come from the last probe; Probe checks one engine live now.</p>
<table><thead><tr><th>Engine</th><th>Switch</th><th>Status</th><th>Priority</th><th>Track record</th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="msg" role="status" aria-live="polite"></div>
<section class="teams-sec" aria-labelledby="teams-h">
<header><h2 id="teams-h">Teams</h2>
<button type="button" class="btn" data-tact="add-team">+ Add team</button>
<button type="button" class="btn-prim" data-tact="save-teams">Save</button></header>
<p class="note">Named teams (code or doc) and ordered role engine lists for <code>jbr quick --team T --role R</code>. First role is the default.</p>
<div id="team-err" role="alert"></div>
<div id="teams-list"></div>
<div id="team-msg" role="status" aria-live="polite"></div>
</section>
</main>
<script>
const TOKEN=document.querySelector('meta[name=jbr-token]').content;
const $=s=>document.querySelector(s);
let ENG_MAP={},TEAMS_STATE=[],ALLOWED_ENGINES=[];
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-JBR-Token':TOKEN},
body:JSON.stringify(body)});const j=await r.json();if(!r.ok){const e=new Error((j.errors&&j.errors.join('; '))||j.error||r.status);e.errors=j.errors||[j.error||String(r.status)];throw e}return j}
function rec(r){if(!r)return '<span class="sub">no builds recorded</span>';
return `${r.accepted}/${r.packages} accepted<div class="sub">${r.mean_fix_rounds} fix rounds/pkg${r.mean_minutes!=null?`, ${r.mean_minutes} min/pkg`:''}</div>`}
function seg(e){const cur=e.switch?(e.switch.available?'on':'off'):'default';
return `<div class="seg" role="group" aria-label="Switch ${esc(e.engine)}">${['on','off','default'].map(s=>
`<button data-e="${esc(e.engine)}" data-s="${s}" aria-pressed="${cur===s}">${s==='default'?'Default':s==='on'?'On':'Off'}</button>`).join('')}</div>
<span class="src">${e.on?'on':'off'} now${e.switch?` (saved ${esc((e.switch.at||'').slice(0,10))})`:` (engines.json: ${e.default_on?'on':'off'})`}</span>`}
function toTeamsState(obj){return Object.entries(obj||{}).map(([name,t])=>({name,kind:t.kind||'code',description:t.description||'',
roles:Object.entries(t.roles||{}).map(([rname,r])=>({name:rname,engines:[...(r.engines||[])],instructions:r.instructions||''}))}))}
function dupNames(){const errs=[],tn=new Set();for(const t of TEAMS_STATE){if(tn.has(t.name))errs.push(`duplicate team name ${t.name}`);tn.add(t.name);const rn=new Set();for(const r of t.roles){if(rn.has(r.name))errs.push(`duplicate role name ${r.name} in team ${t.name}`);rn.add(r.name)}}return errs}
function fromTeamsState(){const out={};for(const t of TEAMS_STATE){const roles={};for(const r of t.roles){roles[r.name]={engines:[...r.engines],instructions:r.instructions}}
out[t.name]={description:t.description,kind:t.kind,roles}}return out}
function renderChip(ti,ri,ei,ek){const e=ENG_MAP[ek]||{on:false,status:'UNKNOWN'};
return `<span class="chip eng-chip"><span class="st ${esc(e.status)}">${esc(ek)} (${e.on?'on':'off'} · ${esc(e.status)})</span>`+
`<button type="button" title="Move up" data-tact="eng-up" data-ti="${ti}" data-ri="${ri}" data-ei="${ei}">↑</button>`+
`<button type="button" title="Move down" data-tact="eng-down" data-ti="${ti}" data-ri="${ri}" data-ei="${ei}">↓</button>`+
`<button type="button" title="Remove engine" data-tact="eng-rm" data-ti="${ti}" data-ri="${ri}" data-ei="${ei}">×</button></span>`}
function renderTeams(){if(!TEAMS_STATE.length){$('#teams-list').innerHTML='<p class="sub">No teams configured. Click "+ Add team" then Save.</p>';return}
$('#teams-list').innerHTML=TEAMS_STATE.map((t,ti)=>`<div class="team-block" data-ti="${ti}">
<div class="team-hdr">
<input class="in k" aria-label="Team name" placeholder="team-name" value="${esc(t.name)}" data-tfield="tname" data-ti="${ti}">
<select class="in" aria-label="Team kind" data-tfield="tkind" data-ti="${ti}">
<option value="code"${t.kind==='code'?' selected':''}>code</option>
<option value="doc"${t.kind==='doc'?' selected':''}>doc</option></select>
<input class="in" style="flex:1;min-width:160px" aria-label="Team description" placeholder="description" value="${esc(t.description)}" data-tfield="tdesc" data-ti="${ti}">
<button type="button" class="btn" data-tact="add-role" data-ti="${ti}">+ Add role</button>
<button type="button" class="btn" data-tact="rm-team" data-ti="${ti}">Remove team</button>
</div>
<div>${t.roles.map((r,ri)=>`<div class="role-row" data-ti="${ti}" data-ri="${ri}">
<input class="in k" aria-label="Role name" placeholder="role-name" value="${esc(r.name)}" data-tfield="rname" data-ti="${ti}" data-ri="${ri}">
<div class="chips" style="flex:1">${r.engines.map((ek,ei)=>renderChip(ti,ri,ei,ek)).join('')}
<select class="in" aria-label="Add engine" data-tact="add-eng" data-ti="${ti}" data-ri="${ri}">
<option value="">+ add engine</option>${ALLOWED_ENGINES.filter(k=>!r.engines.includes(k)).map(k=>`<option value="${esc(k)}">${esc(k)}</option>`).join('')}</select>
</div>
<button type="button" class="btn" data-tact="rm-role" data-ti="${ti}" data-ri="${ri}">Remove role</button>
<textarea class="in" aria-label="Role instructions" placeholder="Role instructions..." data-tfield="rinst" data-ti="${ti}" data-ri="${ri}">${esc(r.instructions)}</textarea>
</div>`).join('')}</div></div>`).join('')}
async function load(){try{const [er,tr]=await Promise.all([fetch('/api/engines'),fetch('/api/teams')]);
const j=await er.json(),tj=await tr.json();
ENG_MAP=Object.fromEntries(j.engines.map(e=>[e.engine,e]));
ALLOWED_ENGINES=tj.engines||[];
TEAMS_STATE=toTeamsState(tj.teams);
$('#team-err').innerHTML=(tj.errors||[]).map(e=>`<div>${esc(e)}</div>`).join('');
$('#meta').textContent=`${j.engines.length} engines, switches in ${j.switches_file}`;
$('#rows').innerHTML=j.engines.map(e=>`<tr>
<td><div class="k">${esc(e.engine)}</div><div class="sub">${esc(e.runner)} · ${esc(e.model)}</div>
${e.explicit_only?'<span class="chip">only when assigned</span>':''}</td>
<td>${seg(e)}</td>
<td><span class="st ${esc(e.status)}${e.stale?' stale':''}">${esc(e.status_line)}</span><div class="sub">${e.last_probe?'last probe '+esc(e.last_probe.replace('T',' ').slice(0,16)):'never probed'}</div>
${e.runner!=='claude_subagent'&&e.runner!=='manual'?`<button class="btn" data-probe="${esc(e.engine)}" data-local="${e.local?1:''}">Probe now</button>`:''}</td>
<td class="rec"><span class="lbl">priority </span>${e.priority??''}</td><td class="rec">${rec(e.record)}</td></tr>`).join('');
renderTeams()}catch(err){$('#msg').textContent='Could not load engines: '+err.message}}
document.addEventListener('input',ev=>{const el=ev.target;const ti=+el.dataset.ti,ri=+el.dataset.ri;
if(el.dataset.tfield==='tname')TEAMS_STATE[ti].name=el.value.trim();
else if(el.dataset.tfield==='tkind')TEAMS_STATE[ti].kind=el.value;
else if(el.dataset.tfield==='tdesc')TEAMS_STATE[ti].description=el.value;
else if(el.dataset.tfield==='rname')TEAMS_STATE[ti].roles[ri].name=el.value.trim();
else if(el.dataset.tfield==='rinst')TEAMS_STATE[ti].roles[ri].instructions=el.value});
document.addEventListener('change',ev=>{const el=ev.target;if(el.dataset.tact==='add-eng'&&el.value){
const ti=+el.dataset.ti,ri=+el.dataset.ri,r=TEAMS_STATE[ti].roles[ri];
if(!r.engines.includes(el.value))r.engines.push(el.value);renderTeams()}});
document.addEventListener('click',async ev=>{const b=ev.target.closest('button');if(!b)return;
if(b.dataset.tact){const a=b.dataset.tact,ti=+b.dataset.ti,ri=+b.dataset.ri,ei=+b.dataset.ei;
if(a==='add-team'){TEAMS_STATE.push({name:`team-${TEAMS_STATE.length+1}`,kind:'code',description:'',roles:[{name:'default',engines:[ALLOWED_ENGINES[0]||'claude_subagent'],instructions:''}]});renderTeams()}
else if(a==='rm-team'){TEAMS_STATE.splice(ti,1);renderTeams()}
else if(a==='add-role'){TEAMS_STATE[ti].roles.push({name:`role-${TEAMS_STATE[ti].roles.length+1}`,engines:[ALLOWED_ENGINES[0]||'claude_subagent'],instructions:''});renderTeams()}
else if(a==='rm-role'){TEAMS_STATE[ti].roles.splice(ri,1);renderTeams()}
else if(a==='eng-up'&&ei>0){const es=TEAMS_STATE[ti].roles[ri].engines;[es[ei-1],es[ei]]=[es[ei],es[ei-1]];renderTeams()}
else if(a==='eng-down'){const es=TEAMS_STATE[ti].roles[ri].engines;if(ei<es.length-1){[es[ei+1],es[ei]]=[es[ei],es[ei+1]];renderTeams()}}
else if(a==='eng-rm'){TEAMS_STATE[ti].roles[ri].engines.splice(ei,1);renderTeams()}
else if(a==='save-teams'){const dn=dupNames();if(dn.length){$('#team-err').innerHTML=dn.map(e=>`<div>${esc(e)}</div>`).join('');return}try{$('#team-err').innerHTML='';const res=await post('/api/teams',{teams:fromTeamsState()});
TEAMS_STATE=toTeamsState(res.teams);renderTeams();$('#team-msg').textContent='Teams saved.'}
catch(err){$('#team-err').innerHTML=(err.errors||[err.message]).map(e=>`<div>${esc(e)}</div>`).join('');$('#team-msg').textContent=''}}
return}
try{if(b.dataset.s){await post('/api/switch',{engine:b.dataset.e,state:b.dataset.s});$('#msg').textContent=`${b.dataset.e}: ${b.dataset.s}`}
else if(b.dataset.probe){if(b.dataset.local&&!confirm(`${b.dataset.probe} runs a local model: probing loads it into memory (about 18 GB). Run /system-check first if anything else is using the GPU. Probe now?`))return;
b.textContent='Probing…';const r=await post('/api/probe',{engine:b.dataset.probe});
$('#msg').textContent=`${r.engine}: ${r.status_line}`}await load()}catch(err){$('#msg').textContent='Failed: '+err.message}});
load();
</script></body></html>
"""
