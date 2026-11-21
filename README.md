# CrewAI + Free Web AI

A three-service Docker stack:

```
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
- **crewai** — a FastAPI service running a two-agent crew (Researcher → Writer) against the gateway.

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

## API

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | none | Liveness probe |
| `GET` | `/api/models` | Bearer `CREWAI_API_KEY` | Proxy of the gateway model list |
| `POST` | `/api/crew/run` | Bearer `CREWAI_API_KEY` | Run the two-agent crew |

`POST /api/crew/run` body:

```json
{ "prompt": "Your task", "model": "chatgpt-auto" }
```

`model` is optional and defaults to `CREWAI_MODEL`.

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
