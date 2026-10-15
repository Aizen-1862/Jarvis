"""Local JARVIS software builder. Standard-library-only server and tool loop."""
from __future__ import annotations

import json
import base64
import hmac
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WORKSPACE = (ROOT / "workspace" / "projects").resolve()
STATE_FILE = ROOT / "workspace" / "state.json"
WEB = ROOT / "web"
MAX_BODY = 64_000
MAX_FILE = 200_000
MAX_TOOL_CALLS = 12
TIMEOUT = 90
WORKSPACE.mkdir(parents=True, exist_ok=True)
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

def load_local_env() -> None:
    """Load local server secrets without ever returning or logging their values."""
    env_file = ROOT / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip("\"'")
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass

load_local_env()

SYSTEM_PROMPT = """You are JARVIS, a software-building agent. Turn the user's natural-language request into a working project. Inspect existing files before edits. Use scaffold only for a new project; preserve existing work. Make a short plan, then execute using tools, inspect outputs, repair errors, and verify with an available build/test command. Be honest about what was and was not verified. Ask only essential questions; use reasonable defaults. Never ask for or expose credentials. Never attempt deployment, publishing, privileged commands, deleting files, or accessing paths outside the project. Use project_state to track requirements, completed work, bugs, and recent changes. For a website, start the dev server and return its local preview URL if possible. Browser automation is unavailable; do not claim visual/browser testing."""

TOOLS = [
 {"type":"function","function":{"name":"select_project","description":"Select or create an isolated project by its short name. Use a distinct name for a new project; select an existing project to continue it.","parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}},
 {"type":"function","function":{"name":"list_files","description":"List files in the current project (relative path optional).","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":[]}}},
 {"type":"function","function":{"name":"read_file","description":"Read a UTF-8 text file in the project.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
 {"type":"function","function":{"name":"write_file","description":"Create or replace a UTF-8 text file inside the project. Parent directories are created. Do not write secrets.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
 {"type":"function","function":{"name":"scaffold_website","description":"Create a minimal responsive static website when the project is new or empty.","parameters":{"type":"object","properties":{"name":{"type":"string"},"description":{"type":"string"}},"required":["name","description"]}}},
 {"type":"function","function":{"name":"run_command","description":"Run an approved development/build/test command in the project. Allowed: python -m http.server, python -m unittest, python -m compileall, npm install, npm run build/test/dev, npm test, npm run, and git status/diff/log. Each command is time-limited.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
 {"type":"function","function":{"name":"start_dev_server","description":"Start a local preview server. Static projects can use Python http.server. JS projects can use npm run dev. Returns URL and process status.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
 {"type":"function","function":{"name":"project_state","description":"Read or update project requirements, completed tasks, unresolved bugs, and recent changes.","parameters":{"type":"object","properties":{"requirements":{"type":"array","items":{"type":"string"}},"completed":{"type":"array","items":{"type":"string"}},"bugs":{"type":"array","items":{"type":"string"}},"changes":{"type":"array","items":{"type":"string"}}},"required":[]}}}
]

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current_project":"website","projects":{},"tasks":[]}

def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

def project_name() -> str:
    name = load_state().get("current_project", "website")
    return name if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}", name or "") else "website"

def project_dir(name: str | None = None) -> Path:
    name = name or project_name()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}", name):
        raise ValueError("Project name must contain only letters, digits, underscores, or hyphens.")
    path = (WORKSPACE / name).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError("Project path is outside the approved workspace.")
    path.mkdir(parents=True, exist_ok=True)
    return path

def safe_file(rel: str, *, create_parent: bool = False) -> Path:
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        raise ValueError("A relative file path is required.")
    candidate = (project_dir() / rel).resolve()
    if not candidate.is_relative_to(project_dir()):
        raise ValueError("File path is outside the current project.")
    if create_parent:
        candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate

def list_files(rel: str = "") -> dict[str, Any]:
    base = project_dir() if not rel else safe_file(rel)
    if not base.exists(): return {"files": [], "directory_exists": False}
    if base.is_file(): return {"files": [str(base.relative_to(project_dir()))]}
    files = [str(p.relative_to(project_dir())) for p in base.rglob("*") if p.is_file() and not any(x in {".git", "node_modules", ".venv", "__pycache__"} for x in p.parts)]
    return {"files": files[:500], "truncated": len(files) > 500}

def run_approved(command: str, *, background: bool = False) -> dict[str, Any]:
    if not isinstance(command, str) or len(command) > 1000 or any(x in command for x in ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r")):
        return {"error":"Only one simple approved command is allowed; shell composition and redirection are disabled."}
    try: args = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as e: return {"error":str(e)}
    if not args: return {"error":"Empty command."}
    allowed = False
    if args[0] in {"python", "python3"} and len(args) >= 3 and args[1] == "-m" and args[2] in {"http.server", "unittest", "compileall"}:
        allowed = True
    elif args[0] == "npm" and len(args) >= 2:
        allowed = ((args[1] == "install" and len(args) <= 3) or
                   (args[1] == "test" and len(args) == 2) or
                   (args[1] == "run" and len(args) >= 3 and args[2] in {"build", "test", "dev", "start", "preview"} and len(args) <= 4))
    elif args[0] == "git" and len(args) >= 2:
        allowed = args[1] in {"status", "diff", "log"} and len(args) <= 3
    if not allowed: return {"error":"Command is not on the approved development-command list."}
    cwd = project_dir()
    env = {k:v for k,v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", k, re.I)}
    env.update({"CI":"1", "NO_COLOR":"1"})
    if background:
        if not (args[0] == "python" and "http.server" in args or args[0] == "npm" and len(args) >= 3 and args[1:3] == ["run", "dev"]):
            return {"error":"Preview server command must be python -m http.server PORT or npm run dev."}
        try:
            p = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False, creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0), start_new_session=(os.name != "nt"))
            port = next((int(x) for x in args if x.isdigit() and 1024 <= int(x) <= 65535), 5173 if args[0] == "npm" else 8000)
            time.sleep(0.8)
            if p.poll() is not None: return {"error":f"Server exited immediately with code {p.returncode}. Check project scripts and build output."}
            return {"url":f"http://127.0.0.1:{port}","pid":p.pid,"status":"running"}
        except OSError as e: return {"error":f"Could not start server: {e}"}
    try:
        done = subprocess.run(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT, shell=False)
        return {"exit_code":done.returncode,"output":done.stdout[-12000:],"timed_out":False}
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {"exit_code":None,"output":out[-12000:],"timed_out":True,"error":f"Command exceeded {TIMEOUT} seconds."}
    except OSError as e: return {"error":f"Could not execute command: {e}"}

def scaffold(name: str, description: str) -> dict[str, Any]:
    d = project_dir()
    if any(d.iterdir()): return {"error":"Project is not empty; inspect its files and preserve existing work."}
    title = name.replace("<", "&lt;").replace(">", "&gt;")[:100]
    desc = description.replace("<", "&lt;").replace(">", "&gt;")[:500]
    (d/"index.html").write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{desc}"><title>{title}</title><link rel="stylesheet" href="style.css"></head><body><main><p class="eyebrow">{title}</p><h1>{desc}</h1><p>A clean starting point, ready to customize.</p><button id="action">Get started</button><p id="feedback" aria-live="polite"></p></main><script src="app.js"></script></body></html>''',encoding="utf-8")
    (d/"style.css").write_text("*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#101827;color:#f8fafc;font:18px/1.6 system-ui,sans-serif;padding:24px}main{max-width:760px}.eyebrow{color:#67e8f9;text-transform:uppercase;letter-spacing:.16em;font-size:.8rem}h1{font-size:clamp(2.5rem,8vw,5rem);line-height:1.05}button{border:0;border-radius:999px;padding:14px 24px;background:#67e8f9;color:#0f172a;font-weight:700;cursor:pointer}button:focus-visible{outline:3px solid white;outline-offset:4px}",encoding="utf-8")
    (d/"app.js").write_text("document.querySelector('#action').addEventListener('click',()=>{document.querySelector('#feedback').textContent='Thanks for stopping by!'})\n",encoding="utf-8")
    (d/"README.md").write_text(f"# {title}\n\n{desc}\n\nOpen index.html directly or run `python -m http.server 8000` from this directory.\n",encoding="utf-8")
    return {"created":["index.html","style.css","app.js","README.md"]}

def tool_call(name: str, args: dict[str, Any]) -> Any:
    if name == "select_project":
        name=args.get("name", "")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}", name): return {"error":"Project name must contain only letters, digits, underscores, or hyphens."}
        state=load_state(); state["current_project"]=name; state.setdefault("projects",{}).setdefault(name,{"requirements":[],"completed":[],"bugs":[],"changes":[]}); save_state(state)
        return {"current_project":name,"directory":str(project_dir(name)),"files":list_files()}
    if name == "list_files": return list_files(args.get("path", ""))
    if name == "read_file":
        p=safe_file(args["path"])
        if not p.is_file(): return {"error":"File not found."}
        if p.stat().st_size > MAX_FILE: return {"error":"File exceeds the 200 KB read limit."}
        try: return {"content":p.read_text(encoding="utf-8")}
        except UnicodeDecodeError: return {"error":"Binary files cannot be read as text."}
    if name == "write_file":
        content=args.get("content","")
        if not isinstance(content,str) or len(content.encode("utf-8")) > MAX_FILE: return {"error":"File exceeds the 200 KB write limit."}
        p=safe_file(args["path"],create_parent=True); p.write_text(content,encoding="utf-8")
        return {"written":str(p.relative_to(project_dir())),"bytes":p.stat().st_size}
    if name == "scaffold_website": return scaffold(args["name"],args["description"])
    if name == "run_command": return run_approved(args["command"])
    if name == "start_dev_server": return run_approved(args["command"],background=True)
    if name == "project_state":
        state=load_state(); p=project_name(); projects=state.setdefault("projects",{}); entry=projects.setdefault(p,{"requirements":[],"completed":[],"bugs":[],"changes":[]})
        for key in ("requirements","completed","bugs","changes"):
            if key in args: entry[key]=list(args[key])[-100:]
        entry.update({"directory":str(project_dir()),"updated_at":utc_now()}); save_state(state)
        return entry
    return {"error":"Unknown tool."}

def call_model(messages: list[dict[str, Any]]) -> dict[str, Any]:
    key=os.environ.get("GROQ_API_KEY")
    if not key: raise RuntimeError("GROQ_API_KEY is not configured. Set it in the server environment and restart JARVIS.")
    payload={"model":os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile"),"messages":messages,"tools":TOOLS,"tool_choice":"auto","temperature":0.2}
    req=urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=json.dumps(payload).encode(),headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=90) as res: return json.loads(res.read())
    except urllib.error.HTTPError as e:
        detail=e.read(3000).decode(errors="replace")
        raise RuntimeError(f"Model API returned HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError,TimeoutError) as e: raise RuntimeError(f"Could not reach model API: {e}") from None

def agent_turn(prompt: str) -> dict[str, Any]:
    if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>12000: raise ValueError("Message must contain 1–12,000 characters.")
    state=load_state(); current=project_name(); project_dir(current)
    messages=[{"role":"system","content":SYSTEM_PROMPT+f"\nCurrent project: {current}. Project directory: workspace/projects/{current}. State: {json.dumps(state.get('projects',{}).get(current,{}),ensure_ascii=False)}"},{"role":"user","content":prompt}]
    used=[]
    for _ in range(MAX_TOOL_CALLS):
        response=call_model(messages); choice=response["choices"][0]["message"]; messages.append(choice)
        calls=choice.get("tool_calls") or []
        if not calls:
            answer=choice.get("content") or "The model returned no text."
            state=load_state(); state.setdefault("tasks",[]).append({"at":utc_now(),"project":current,"request":prompt[:1000],"result":answer[:2000],"tools":used})
            state["tasks"]=state["tasks"][-100:]; save_state(state)
            return {"answer":answer,"tools":used,"project":current,"directory":str(project_dir()),"state":state.get("projects",{}).get(current,{})}
        for call in calls:
            fn=call.get("function",{}); name=fn.get("name");
            try:
                args=json.loads(fn.get("arguments") or "{}")
                result=tool_call(name,args)
            except Exception as e: result={"error":str(e)}
            used.append({"tool":name,"result":result})
            messages.append({"role":"tool","tool_call_id":call.get("id"),"content":json.dumps(result,ensure_ascii=False)[:14000]})
    return {"answer":"I reached the tool-call limit before finishing. Review the recorded tool results and continue with a follow-up.","tools":used,"project":current,"directory":str(project_dir())}

HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>JARVIS Software Builder</title><style>
:root{color-scheme:dark;--bg:#0c1220;--panel:#131d2e;--line:#29364b;--text:#e8eef8;--muted:#9bacbf;--cyan:#68e2ef}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 70% -30%,#263d59,var(--bg) 55%);color:var(--text);font:16px/1.55 system-ui,sans-serif;min-height:100vh}header{border-bottom:1px solid var(--line);padding:22px clamp(18px,5vw,72px);display:flex;align-items:center;justify-content:space-between}.brand{font-weight:750;letter-spacing:.08em}.brand span{color:var(--cyan)}.status{color:var(--muted);font-size:13px}main{max-width:1000px;margin:0 auto;padding:50px 20px}.intro h1{font-size:clamp(34px,6vw,58px);line-height:1.06;margin:0 0 12px}.intro p{color:var(--muted);max-width:660px;margin:0}.panel{background:#101a2bcc;border:1px solid var(--line);border-radius:16px;margin-top:32px;overflow:hidden}.toolbar{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--line);color:var(--muted);font-size:13px}.log{padding:20px;min-height:180px;max-height:52vh;overflow:auto;display:flex;flex-direction:column;gap:14px}.msg{max-width:88%;padding:12px 15px;border-radius:12px;white-space:pre-wrap;overflow-wrap:anywhere}.user{align-self:flex-end;background:#18384a}.agent{align-self:flex-start;background:#1a273a}.meta{display:block;color:var(--muted);font-size:12px;margin-top:8px}.composer{padding:16px;border-top:1px solid var(--line);display:flex;gap:12px}.composer textarea{flex:1;resize:vertical;min-height:54px;max-height:180px;background:#0c1422;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:13px;font:inherit}.composer button{border:0;border-radius:10px;background:var(--cyan);color:#07131c;font:700 15px system-ui;padding:0 22px;cursor:pointer}.composer button:disabled{opacity:.55;cursor:wait}.help{font-size:13px;color:var(--muted);margin:14px 4px}.help code{color:#d7e7f3}.preview{display:none;margin:16px 20px 20px}.preview a{color:var(--cyan)}@media(max-width:560px){header{padding:16px}.status{max-width:48%;text-align:right}.composer{flex-direction:column}.composer button{padding:13px}.log{max-height:60vh}}
</style></head><body><header><div class="brand">JARVIS<span> / BUILD</span></div><div class="status" id="status">Checking agent…</div></header><main><section class="intro"><h1>Describe it.<br>Watch it take shape.</h1><p>JARVIS plans, edits project files, runs approved build commands, and reports what it verified. Projects stay in a controlled local workspace.</p></section><section class="panel"><div class="toolbar"><span id="project">Project: website</span><span>Local workspace</span></div><div id="log" class="log" aria-live="polite"><div class="msg agent">Tell JARVIS what to build or change. It will inspect existing files before editing.</div></div><div class="preview" id="preview"></div><form class="composer" id="form"><textarea id="input" maxlength="12000" placeholder="Build a site for my electronics repair business…" required></textarea><button id="send">Build</button></form></section><p class="help">Enter to send · Shift+Enter for a new line · Credentials stay on the server. No browser automation is configured.</p></main><script>
const log=document.querySelector('#log'),input=document.querySelector('#input'),send=document.querySelector('#send');function add(text,who,meta=''){let d=document.createElement('div');d.className='msg '+who;d.textContent=text;if(meta){let s=document.createElement('span');s.className='meta';s.textContent=meta;d.append(s)}log.append(d);log.scrollTop=log.scrollHeight}
async function status(){try{let r=await fetch('/api/status'),d=await r.json();document.querySelector('#status').textContent=d.model_configured?'Agent ready · '+d.model:'Model key required';document.querySelector('#project').textContent='Project: '+d.project}catch{document.querySelector('#status').textContent='Local agent'}}status();
document.querySelector('#form').addEventListener('submit',async e=>{e.preventDefault();let text=input.value.trim();if(!text)return;add(text,'user');input.value='';send.disabled=true;send.textContent='Working…';add('Planning, using project tools, and checking results…','agent');let waiting=log.lastElementChild;try{let r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:text})}),d=await r.json();waiting.remove();if(!r.ok)throw Error(d.error||'Request failed');let n=d.tools?.length||0;add(d.answer,'agent',`${n} tool action${n===1?'':'s'} · ${d.directory}`);if(d.tools?.length){let preview=[...d.tools].reverse().find(x=>x.tool==='start_dev_server'&&x.result?.url);if(preview){let p=document.querySelector('#preview');p.style.display='block';let a=document.createElement('a');a.href=preview.result.url;a.target='_blank';a.rel='noopener';a.textContent='Open local preview '+preview.result.url;p.replaceChildren(a)}}document.querySelector('#project').textContent='Project: '+d.project}catch(err){waiting.remove();add(err.message,'agent')}finally{send.disabled=false;send.textContent='Build';status()}});input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.querySelector('#form').requestSubmit()}});
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    server_version = "JARVISBuilder/1.0"
    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid request bodies and credentials in routine logs.
        print(f"{self.address_string()} {fmt % args}")
    def send_json(self, status: int, data: Any) -> None:
        body=json.dumps(data,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
    def authorized(self) -> bool:
        expected=os.environ.get("JARVIS_ACCESS_PASSWORD","")
        if not expected:
            return True
        header=self.headers.get("Authorization","")
        try:
            scheme, encoded=header.split(" ",1)
            userpass=base64.b64decode(encoded,validate=True).decode("utf-8")
            supplied_user, supplied_password=userpass.split(":",1)
        except (ValueError,UnicodeDecodeError):
            supplied_user=supplied_password=""
        if hmac.compare_digest(supplied_user,"jarvis") and hmac.compare_digest(supplied_password,expected):
            return True
        body=b'{"error":"Authentication required"}'
        self.send_response(401); self.send_header("WWW-Authenticate",'Basic realm="JARVIS", charset="UTF-8"'); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
        return False
    def do_GET(self) -> None:
        if not self.authorized(): return
        path=urlparse(self.path).path
        if path=="/":
            body=(WEB / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(body)
        elif path=="/api/status": self.send_json(200,{"project":project_name(),"model_configured":bool(os.environ.get("GROQ_API_KEY")),"model":os.environ.get("GROQ_MODEL","llama-3.3-70b-versatile"),"browser_automation":False,"workspace":str(WORKSPACE)})
        elif path=="/api/state": self.send_json(200,load_state())
        else: self.send_json(404,{"error":"Not found"})
    def do_POST(self) -> None:
        if not self.authorized(): return
        if urlparse(self.path).path!="/api/chat": self.send_json(404,{"error":"Not found"}); return
        try:
            length=int(self.headers.get("Content-Length","0"))
            if length<1 or length>MAX_BODY: self.send_json(413,{"error":"Request body must be between 1 byte and 64 KB."}); return
            data=json.loads(self.rfile.read(length)); result=agent_turn(data.get("message"))
            self.send_json(200,result)
        except RuntimeError as e: self.send_json(503,{"error":str(e)})
        except (ValueError,KeyError,json.JSONDecodeError) as e: self.send_json(400,{"error":str(e)})
        except Exception as e: self.send_json(500,{"error":f"Agent failed safely: {type(e).__name__}: {e}"})

def main() -> None:
    host=os.environ.get("JARVIS_HOST","127.0.0.1"); port=int(os.environ.get("PORT",os.environ.get("JARVIS_PORT","8765")))
    if host not in {"127.0.0.1","localhost","::1","0.0.0.0"}: raise SystemExit("JARVIS_HOST must be localhost or 0.0.0.0.")
    if host=="0.0.0.0" and not os.environ.get("JARVIS_ACCESS_PASSWORD"):
        raise SystemExit("Refusing public binding without JARVIS_ACCESS_PASSWORD. Set a strong password in the server environment first.")
    print(f"JARVIS Software Builder: http://127.0.0.1:{port}")
    print(f"Workspace: {WORKSPACE}")
    if not os.environ.get("GROQ_API_KEY"): print("GROQ_API_KEY is not set; configure it in the server environment to enable agent actions.")
    ThreadingHTTPServer((host,port),Handler).serve_forever()

if __name__=="__main__": main()
