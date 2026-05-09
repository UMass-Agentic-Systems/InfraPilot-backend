# InfraPilot Backend — Build & Deployment

This document covers everything needed to install, configure, run, and deploy the InfraPilot backend. For the API surface see [docs/API.md](docs/API.md); for non-functional requirements see [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md).

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | 3.11+ | Runtime |
| Docker | latest | Local PostgreSQL container |
| Minikube | latest | Local Kubernetes cluster (required for K8s features) |
| Google Gemini API key | — | LLM backing the Provisioning & SRE agents |

Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/app/apikey). It is **required** — the agents will fail to start without it.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/UMass-Agentic-Systems/InfraPilot-backend.git
cd InfraPilot-backend
```

### 2. Create and activate a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -e ".[dev]"
```

The `[dev]` extras include `pytest`, `ruff`, `black`, and `mypy`.

---

## Configuration

### 1. Start PostgreSQL

With Docker Desktop running:

```bash
docker run -d --name infrapilot-db \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 \
  postgres:16
docker exec -i infrapilot-db psql -U postgres -c "CREATE DATABASE infrapilot;"
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Set the variables below. `SECRET_KEY` and `GOOGLE_API_KEY` are required; the rest have sensible defaults.

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | **Yes** | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GOOGLE_API_KEY` | **Yes** | From [Google AI Studio](https://aistudio.google.com/app/apikey) |
| `DATABASE_URL` | No | Defaults to `postgresql://postgres:postgres@localhost:5432/infrapilot` — matches the Docker command above |
| `ALGORITHM` | No | JWT algorithm, defaults to `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | Defaults to `60` |
| `KUBECONFIG_PATH` | No | Defaults to `~/.kube/config` |
| `SRE_SCAN_INTERVAL_SECONDS` | No | Background SRE monitor cadence, defaults to `120` |

---

## Local Development

### 1. Start Minikube (optional — required for K8s features)

```bash
minikube start
```

If Minikube is not running, deploy/visualize/SRE endpoints will return `error` payloads with `cluster: null`, but the API itself will still respond.

### 2. Start the dev server

```bash
uvicorn app.main:app --reload
```

Database tables are created automatically on startup via `create_all` — no migration step is needed in development.

The API is available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

### 3. Verify

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 4. Run tests

```bash
pytest
```

Run linters and type checks (matches CI):

```bash
ruff check .
black --check .
mypy app
```

---

## Deployment

The sections below describe a production-style deployment. These are guidelines, not a turnkey playbook — adapt to your hosting environment.

### Environment variables (production checklist)

| Variable | Production guidance |
|---|---|
| `SECRET_KEY` | Strong, random, **never** the dev value. Rotate on suspected compromise; rotation invalidates all outstanding JWTs. |
| `GOOGLE_API_KEY` | Use a service-account-scoped key; do not share with non-prod. |
| `DATABASE_URL` | Point to a managed Postgres (RDS / Cloud SQL / etc.). Use TLS; do not use `postgres:postgres` credentials. |
| `KUBECONFIG_PATH` | Either mount a kubeconfig, or run in-cluster (see below). |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Tune to your session policy; the default `60` is convenient for development. |
| `SRE_SCAN_INTERVAL_SECONDS` | Increase if you have many deployments to avoid load on the cluster API. |

Never commit `.env`. The `.gitignore` already excludes it.

### Database migrations

In development, tables are created via SQLAlchemy `create_all` on startup. **In production, run Alembic instead** so the app does not perform schema work at boot:

```bash
alembic upgrade head
```

Migrations live in [alembic/](alembic/) and are configured in [alembic.ini](alembic.ini). Run `alembic upgrade head` as part of your deploy pipeline before starting the new app version.

### Running the server

```bash
uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --proxy-headers \
  --forwarded-allow-ips '*'
```

`--workers` should be roughly `2 × CPU cores`. Set `--proxy-headers` when running behind a reverse proxy so `X-Forwarded-For` / `X-Forwarded-Proto` are honored.

### Reverse proxy & WebSockets

If you front the app with nginx, Caddy, or a cloud load balancer, ensure WebSocket upgrades are forwarded for the chat endpoint at `/api/v1/chat/sessions/{id}/ws`. For nginx:

```nginx
location /api/v1/chat/ {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

A long `proxy_read_timeout` is important — chat sockets are intentionally long-lived so the SRE monitor can push alerts.

### Kubernetes credentials

The app talks to Kubernetes via the official Python client. Two modes are supported:

- **External kubeconfig** — set `KUBECONFIG_PATH` to a file the process can read. Suitable for staging or when running outside the target cluster.
- **In-cluster** — when running inside the same cluster you manage, mount a `ServiceAccount` with namespace-scoped permissions; no `KUBECONFIG_PATH` is needed. Grant only the verbs the app uses (`get`, `list`, `create`, `apply`, `delete`) on the resource kinds it deploys (Deployment, Service, HPA, PVC, ConfigMap, Secret, Namespace).

### Health & observability

- `/health` is intentionally cheap and returns `200` even if Postgres or K8s are unreachable — wire it to your platform's liveness probe. For readiness, use a custom probe that also checks DB connectivity.
- Logs are written to stdout via the standard `logging` module — collect with whatever your platform provides.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `sqlalchemy.exc.OperationalError` on startup | Postgres not running, or `DATABASE_URL` wrong. |
| Deploy returns `status: "failed"` with a Gemini error | `GOOGLE_API_KEY` missing, invalid, or rate-limited. |
| `cluster: null` and `error` set on `/visualize/{id}` | Kubernetes not reachable — start Minikube, or check `KUBECONFIG_PATH`. |
| WebSocket immediately closes with code `4001` | JWT missing or expired in the `token` query parameter. |
| WebSocket immediately closes with code `4004` | The session ID does not belong to the authenticated user. |
