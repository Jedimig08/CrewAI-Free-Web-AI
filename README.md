# CrewAI + Free Web AI

A four-service Docker stack:

```
Web UI (http://localhost:3000)
        │  /api/*  (key injected server-side)
        ▼
CrewAI API (FastAPI, :8000)
        │  OpenAI-compatible calls
        ▼
Free Web AI gateway (:3456, internal)
        │  CDP
        ▼
Chromium browser desktop (https://localhost:6081)
```

- **browser** — LinuxServer Chromium with a persistent profile. You log into ChatGPT here once.
- **gateway** — the `free-web-ai` Bun app. It exposes `/v1/chat/completions` and drives Chromium over CDP.
- **crewai** — a FastAPI service running agent crews against the gateway.
- **web** — an nginx container serving the web UI and proxying `/api` to CrewAI with the bearer token added server-side.

The gateway and browser code live in the sibling **`free-web-ai`** repo. This repo only contains the CrewAI service and the Compose orchestration, so the gateway code is not duplicated here.

## Why `browser:3456` and not `gateway:3456`?

The gateway uses `network_mode: "service:browser"`, so it shares the browser container's network namespace. It is reachable from other containers at **`http://browser:3456/v1`**. There is no `gateway` DNS name on the network. This is also why CDP port `9222` never reaches your host.

## Prerequisites

- Docker Engine + Docker Compose v2
- The two repos checked out side by side:

```
~/Work/free-web-ai
~/Work/CrewAI-Free-Web-AI
```

## Setup

```bash
cd ~/Work/CrewAI-Free-Web-AI
./setup.sh
```

`setup.sh` creates `.env` from `.env.example` and generates three secrets:

- `TFG_API_KEY` — bearer token for the gateway.
- `BROWSER_PASSWORD` — login for the web desktop on 6081.
- `CREWAI_API_KEY` — bearer token for the CrewAI API.

It also fills `PUID`/`PGID` from `id -u`/`id -g` and `TZ` from the host timezone. Existing non-empty values are kept; use `./setup.sh --force` to regenerate the secrets. The script prints the values once; they are also stored in `.env`.

`TFG_API_KEY` is optional. If it is empty, the gateway disables auth. That is only safe when the gateway is not reachable outside the Docker network, so the script generates one by default.

If you are migrating from the standalone `free-web-ai` stack and want to keep its existing secrets, put them into `.env` before running `setup.sh`; the script keeps non-empty values.

If the standalone stack is still running, stop it first (this keeps the volumes and the ChatGPT login):

```bash
cd ~/Work/free-web-ai
docker compose down
```

Then start this stack:

```bash
cd ~/Work/CrewAI-Free-Web-AI
docker compose config
docker compose up -d --build
```

The first build pulls Chromium and installs CrewAI, so it can take several minutes.

If Compose warns that the volumes were "created for project `free-web-ai`", that is expected: those are the existing volumes from the standalone stack, so your ChatGPT login and gateway config are reused.

## Authorize ChatGPT

1. Open `https://localhost:6081`, accept the self-signed certificate, and log in with `BROWSER_USER` / `BROWSER_PASSWORD`.
2. Inside the desktop, open `https://chatgpt.com` and log in.
3. In a terminal:

```bash
cd ~/Work/CrewAI-Free-Web-AI
docker compose exec gateway bun index.ts webauth
```

It waits up to 5 minutes for the session token and prints `ChatGPT login successful!` when done.

## Verify

```bash
cd ~/Work/CrewAI-Free-Web-AI
set -a; . ./.env; set +a

curl http://localhost:3457/health
curl http://localhost:8000/health
curl -H "Authorization: Bearer $CREWAI_API_KEY" http://localhost:8000/api/models
```

`/health` on the gateway should show `"providers":1`. The CrewAI `/api/models` proxies the gateway model list.

## Run a crew

```bash
curl -X POST http://localhost:8000/api/crew/run \
  -H "Authorization: Bearer $CREWAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"Explain how Docker networking works in three bullets."}'
```

Interactive docs: `http://localhost:8000/docs`.

## Web interface

Open `http://localhost:3000` (bound to `127.0.0.1` by default). Pick a crew and model, type a prompt, and click **Run crew**. The UI discovers crews through `GET /api/crews`.

The **Explain a repository** form starts an explain job, polls it with elapsed
time and ETA, then shows the result with download links for every generated
Markdown file. Tick **Show the explanation file as it is being built** to watch
`live.md` grow in real time; use **Cancel job** to stop a running job. The
correlation pass changes appear in a separate panel. The same proxy injects the
bearer token, so the browser never sees `CREWAI_API_KEY` here either.

Your browser never sees `CREWAI_API_KEY`: nginx in the `web` container adds the `Authorization` header when proxying `/api/*` to `crewai:8000`. The UI has no login of its own, so keep `WEB_HOST_BIND` at `127.0.0.1` unless you put it behind an authenticated reverse proxy.

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | none | Liveness probe |
| `GET` | `/api/models` | Bearer `CREWAI_API_KEY` | Proxy of the gateway model list |
| `GET` | `/api/crews` | Bearer `CREWAI_API_KEY` | List registered crew names |
| `POST` | `/api/crew/run` | Bearer `CREWAI_API_KEY` | Run the selected crew |
| `POST` | `/api/explain` | Bearer `CREWAI_API_KEY` | Start an async repo explanation job |
| `GET` | `/api/explain` | Bearer `CREWAI_API_KEY` | List explain job ids |
| `GET` | `/api/explain/{job_id}` | Bearer `CREWAI_API_KEY` | Poll job status, progress, ETA and result |
| `POST` | `/api/explain/{job_id}/cancel` | Bearer `CREWAI_API_KEY` | Request cancellation of a running job |
| `GET` | `/api/explain/{job_id}/files/{path}` | Bearer `CREWAI_API_KEY` | Download a generated Markdown file |

`POST /api/crew/run` body:

```json
{ "prompt": "Your task", "model": "chatgpt-auto", "crew": "research_write" }
```

`model` is optional and defaults to `CREWAI_MODEL`. `crew` is optional and defaults to `research_write`. Built-in crews:

- `research_write` — Researcher → Writer, two sequential tasks.
- `summarize` — one summarizer agent.

## Repo explainer

The explainer clones a **public** git repository, reads it in numbered chunks,
and writes Markdown explanations to `./explained-outputs/<job_id>/` on the host.
It starts at entry points, injects a symbol index so a caller can be explained
without seeing the file that defines a name, then runs a second pass that
correlates the chunk summaries and revises explanations that needed the bigger
picture. Chunks are processed in parallel by a bounded worker pool; results are
keyed by line anchors so the output order is deterministic.

Start a job:

```bash
curl -X POST http://localhost:8000/api/explain \
  -H "Authorization: Bearer $CREWAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/user/repo.git","mode":"wiki","granularity":"blocks","refine":true,"max_workers":4}'
```

Poll it:

```bash
curl -H "Authorization: Bearer $CREWAI_API_KEY" \
  http://localhost:8000/api/explain/<job_id>
```

`POST /api/explain` body:

| Field | Default | Description |
|---|---|---|
| `repo_url` | required | Public http(s) git URL. Credentials and private addresses are rejected. |
| `model` | `CREWAI_MODEL` | Model slug used by the explainer agents. |
| `mode` | `wiki` | `wiki` (overview + linked pages) or `line_by_line` (one `line-by-line.md`). |
| `granularity` | `blocks` | `blocks` (logical blocks with line ranges) or `literal` (one note per physical line). |
| `refine` | `true` | Run the correlation and revision pass. |
| `max_workers` | `4` | Parallel chunk workers for this job, capped by `EXPLAIN_MAX_WORKERS`. |
| `max_files` | `150` | Maximum files to read. |
| `focus_paths` | `null` | Optional list of paths/subtrees to restrict the explanation to. |
| `live` | `false` | Write `live.md` after each finished chunk so the file can be watched while it builds. |

The response returns `job_id` and `poll_url` immediately. The status response
includes `status`, `progress`, `elapsed_seconds`, `eta_seconds`, `partial`,
`files`, `error` and `text`. Statuses
move through `queued → cloning → indexing → explaining → correlating → revising →
assembling → completed` (or `failed`/`partial`/`cancelled`). `job.json` is written into each
job directory, so polling survives an API restart.

With `"live": true`, `live.md` is rewritten after each finished chunk in file/line
order, with pending sections marked, so you can watch the explanation form in
real time. `EXPLAIN_LIVE_EVERY` throttles those writes. `POST /api/explain/{job_id}/cancel`
sets a flag checked between chunks; remaining work is skipped and the job ends as
`cancelled`. When the correlation pass changes a section, `changes.md` records the
before/after text and the instruction behind each change.

Wiki output contains `overview.md` (start-here reading order, relationship
narrative, file map, Mermaid dependency graph), `files/*.md` and `symbols/*.md`
with cross-links. Line-by-line output is a single `line-by-line.md`.

Limits and worker counts are configured in `.env.example`; see the
"Repo explainer" section there. `EXPLAIN_ALLOW_LOCAL=true` enables `file://`
URLs for local tests only and should never be enabled in production.

### Adding a crew

Crews are discovered automatically; there is nothing to register in `__init__.py`.

1. Create `crewai/crews/my_crew.py` with exactly one builder function whose name starts with `build_`:
   ```python
   from crewai import Agent, Crew, LLM, Process, Task

   def build_my_crew(llm: LLM, max_sec: int) -> Crew:
       ...
   ```
2. Rebuild the API image so the new file is copied into the container:
   ```bash
   docker compose up -d --build crewai
   ```
3. Call it with `{"prompt": "...", "crew": "my_crew"}`, or select it in the web UI.

The registry key defaults to the filename (`my_crew.py` → `my_crew`). Set `CREW_NAME = "another_name"` in the module to override it, or `CREW_BUILDER = build_my_crew` to pick the builder explicitly (useful if the module has more than one `build_*` function). Files starting with `_` are ignored; a module that fails to import or has no unique builder is logged and skipped. No changes to `app.py`, `__init__.py`, or the endpoints are needed.

## Operations

```bash
docker compose ps
docker compose logs -f crewai
docker compose logs -f gateway
docker compose logs -f browser
docker compose stop
docker compose start
docker compose down          # keep volumes and login
docker compose down -v       # delete volumes and login
```

## Security notes

- Keep `BROWSER_GUI_HOST_PORT` (6081) on a trusted LAN or behind Tailscale. The desktop provides interactive access to the ChatGPT session.
- `BROWSER_PASSWORD` is only the HTTP basic-auth password for the Selkies desktop on 6081. The gateway and CrewAI never use it; the gateway talks to Chromium over CDP on 9222. Keep it private because the desktop holds your ChatGPT session.
- `CREWAI_HOST_BIND` defaults to `127.0.0.1`; do not expose port 8000 publicly without a reverse proxy and TLS.
- `TFG_API_KEY` and `CREWAI_API_KEY` are different secrets. Never commit `.env`.
- CDP `9222` is not published to the host, but it is reachable container-to-container on `free-web-ai-net`. Do not add untrusted services to that network.
