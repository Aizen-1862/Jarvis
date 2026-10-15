# JARVIS Software Builder

A local, tool-using software creation agent with a small web UI. It plans work, edits a project inside its own workspace, runs only approved development commands, inspects results, and keeps project/task history. It is a standalone implementation because no existing JARVIS source was present in the supplied workspace.

The home screen follows the supplied futuristic dashboard reference and includes local task, goal, and shortcut widgets. Those personal widgets are stored in browser local storage; build-agent project state is stored server-side in the workspace.

## Requirements

- Node.js 20+
- A Groq API key (set as `GROQ_API_KEY` in the server environment; never put it in source files)
- Optional: Node.js/npm for JavaScript projects

## Run

Copy `.env.example` to `.env`, add your Groq API key (and an optional local access password), then run `node server.mjs`. Open http://127.0.0.1:8765. The app loads credentials from `.env` or the process environment. The default model is `llama-3.3-70b-versatile`; override with `GROQ_MODEL`. The dashboard works without an API key, while AI build requests require one. Keep `.env` private; it is excluded by `.gitignore`.

## Free hosted URL

`render.yaml` prepares a password-protected Render web service with a free `onrender.com` subdomain. To publish it, connect a Git repository containing this directory in Render and select the Blueprint deployment flow. Render will ask you to set `JARVIS_ACCESS_PASSWORD` and `GROQ_API_KEY` as private environment values. Choose a long, unique password; do not reuse an account password. The provided Fish Audio values are included as private environment entries, but audio features are not wired into this version. Render requires the app to bind to `0.0.0.0`; this is enabled by the blueprint and protected by HTTP Basic authentication. The service uses Render's `PORT` automatically.

The public subdomain is assigned only after the service is created in a Render account. This workspace has no connected hosting account or Git remote, so the hostname cannot be reserved from here. Render's free web services may spin down when idle and do not provide persistent disks; project files and server-side project history may be lost when the instance is replaced. Do not use this free deployment for important project data.

Projects live under `workspace/projects/`. `workspace/state.json` tracks requirements, task history, recent changes, and unresolved issues. No command can address paths outside that workspace. The terminal accepts only a bounded set of development commands and runs them with timeouts. File writes are restricted to the selected project; there is no delete-file tool. Destructive shell commands, privileged operations, deployment, and external publishing are not exposed.

## Agent tools

The agent can inspect/list/read/write project files, scaffold a basic website, run approved build/test/dev commands, and inspect project state. It uses Groq's OpenAI-compatible tool-calling API in a bounded plan/execute/inspect loop. Server-side secrets are excluded from child process environments and are never sent to the browser. Browser preview is provided by the local dev server URL; automated browser interaction is not included in the software agent.

## Security

Bind to localhost by default. Do not expose this server to an untrusted network: it can write files and run the explicitly allowed development commands. Supply credentials only through `.env` or environment variables; do not commit `.env`. Rotate any API key pasted into chat or source control. The default workspace is local to this app; it cannot read arbitrary user documents.

