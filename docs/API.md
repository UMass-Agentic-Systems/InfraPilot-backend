# InfraPilot Backend — API Reference

This document specifies the HTTP and WebSocket surface exposed by the InfraPilot backend (FastAPI). Endpoint contracts are derived from the Pydantic models in [app/api/schemas/](../app/api/schemas/) and the route handlers in [app/api/v1/](../app/api/v1/).

---

## Conventions

| Item | Value |
|---|---|
| Base URL (dev) | `http://localhost:8000` |
| API prefix | `/api/v1` |
| Content type | `application/json` |
| Auth scheme | `Authorization: Bearer <jwt>` (HS256) |
| Token TTL | 60 minutes (configurable via `ACCESS_TOKEN_EXPIRE_MINUTES`) |
| Live OpenAPI | `GET /openapi.json` &nbsp;·&nbsp; Interactive docs at `/docs` |

### Error envelope

FastAPI's default shape is used:

```json
{ "detail": "Invalid email or password" }
```

### Common status codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Resource created (register, create session) |
| 204 | Success, no body (delete session) |
| 400 | Validation error |
| 401 | Missing / invalid JWT |
| 403 | Authenticated but not allowed (e.g., approving another user's plan) |
| 404 | Resource not found *or* cross-user access (collapsed to 404 to avoid information leakage) |
| 409 | Conflict (email already registered) |

---

## Health

### `GET /health`

Liveness probe. Returns `200 OK` even when the database or Kubernetes cluster is unreachable.

**Response 200**

```json
{ "status": "ok" }
```

Source: [app/main.py:67](../app/main.py#L67).

---

## Authentication

Routes mounted at `/api/v1/auth` — see [app/api/v1/auth.py](../app/api/v1/auth.py).

### `POST /api/v1/auth/register`

Register a new user and provision an isolated Kubernetes namespace (`user-<sanitized-email-local>`).

**Request body** ([RegisterRequest](../app/api/schemas/auth.py#L6))

```json
{
  "email": "alice@umass.edu",
  "password": "s3cret"
}
```

**Response 201** ([TokenResponse](../app/api/schemas/auth.py#L16))

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer"
}
```

**Errors**: `409` — email already registered.

### `POST /api/v1/auth/login`

Authenticate and receive a JWT.

**Request body** ([LoginRequest](../app/api/schemas/auth.py#L11))

```json
{
  "email": "alice@umass.edu",
  "password": "s3cret"
}
```

**Response 200** — same `TokenResponse` shape as register.

**Errors**: `401` — invalid email or password.

---

## Deployments

Routes mounted at `/api/v1/deploy` — see [app/api/v1/deploy.py](../app/api/v1/deploy.py). All routes require `Authorization`.

### `POST /api/v1/deploy/`

Standalone deploy (no chat context). Translates natural-language requirements into a Kubernetes manifest via the Provisioning Agent and applies it to the user's namespace.

**Request body** ([DeployRequest](../app/api/schemas/deploy.py#L6))

```json
{
  "app_name": "my-web-app",
  "requirements": "A 3-tier web app with nginx, a Flask API, and Postgres."
}
```

**Response 200** ([DeployResponse](../app/api/schemas/deploy.py#L11))

```json
{
  "deployment_id": 42,
  "status": "deployed",
  "error": null
}
```

> **Note:** Provisioning failures return HTTP 200 with `status: "failed"` and a populated `error` — the agent failure mode is in-band so the chat UI can render it without exception handling. The 404 response code is reserved for the GET path.

### `GET /api/v1/deploy/{deployment_id}`

Fetch a deployment owned by the caller.

**Response 200** ([DeploymentDetail](../app/api/schemas/deploy.py#L17))

```json
{
  "id": 42,
  "app_name": "my-web-app",
  "status": "deployed",
  "desired_state_yaml": "apiVersion: apps/v1\nkind: Deployment\n...",
  "created_at": "2026-05-09T12:00:00Z",
  "updated_at": "2026-05-09T12:00:00Z"
}
```

**Errors**: `404` — deployment not found *or* not owned by caller.

---

## Monitoring & Remediation

Routes mounted at `/api/v1/monitor` — see [app/api/v1/monitor.py](../app/api/v1/monitor.py). All routes require `Authorization`. The same SRE graph backs the chat/WebSocket flow, so plans created here can be approved via chat and vice versa.

### `POST /api/v1/monitor/scan`

Run the SRE Agent against a deployment. Halts at the human-approval interrupt when the agent recommends a remediation.

**Request body** ([ScanRequest](../app/api/schemas/monitor.py#L6))

```json
{ "deployment_id": 42 }
```

**Response 200** ([ScanResponse](../app/api/schemas/monitor.py#L10))

```json
{
  "plan_db_id": 7,
  "analysis": "CrashLoopBackOff on web-pod-2; image pull failure...",
  "rationale": "Increase memory limit and pin image tag to v1.4.2.",
  "status": "awaiting_approval"
}
```

`status` is one of `awaiting_approval`, `no_issues`, `failed`. When `no_issues`, `plan_db_id` and the analysis fields are `null`.

**Errors**: `404` — deployment not found / not owned by caller.

### `GET /api/v1/monitor/plans`

List remediation plans across all of the caller's deployments, newest first.

**Response 200** — array of [PlanSummary](../app/api/schemas/monitor.py#L17):

```json
[
  {
    "id": 7,
    "deployment_id": 42,
    "analysis": "CrashLoopBackOff on web-pod-2...",
    "rationale": "Increase memory limit...",
    "approved": false,
    "applied": false,
    "rejected": false,
    "source": "manual",
    "created_at": "2026-05-09T12:05:00Z"
  }
]
```

`source` is `"manual"` (user-triggered scan) or `"background"` (background SRE monitor).

### `POST /api/v1/monitor/plans/{plan_id}/approve`

Resume the paused SRE graph with the user's approve/reject decision. On approval, the agent applies the remediation; on rejection, the plan is marked rejected without changes.

**Request body** ([ApproveRequest](../app/api/schemas/monitor.py#L31))

```json
{ "approved": true }
```

**Response 200** ([ApproveResponse](../app/api/schemas/monitor.py#L35))

```json
{
  "plan_id": 7,
  "approved": true,
  "applied": true
}
```

A `plan_update` WebSocket event is broadcast to the user's active sessions:

```json
{ "type": "plan_update", "plan_id": 7, "approved": true, "applied": true }
```

**Errors**: `404` — plan does not exist. `403` — plan exists but is not owned by the caller (distinct from deployment lookups, which collapse to 404).

---

## Chat — REST

Routes mounted at `/api/v1/chat` — see [app/api/v1/chat.py](../app/api/v1/chat.py). All REST routes require `Authorization`.

### `POST /api/v1/chat/sessions`

Create a chat session.

**Request body** ([SessionCreate](../app/api/schemas/chat.py#L15))

```json
{ "title": "Deploying my Flask app" }
```

**Response 201** ([SessionSummary](../app/api/schemas/chat.py#L19))

```json
{
  "id": 11,
  "title": "Deploying my Flask app",
  "created_at": "2026-05-09T12:00:00Z",
  "updated_at": "2026-05-09T12:00:00Z"
}
```

### `GET /api/v1/chat/sessions`

List the caller's sessions, most-recently-updated first. Returns an array of `SessionSummary`.

### `GET /api/v1/chat/sessions/{session_id}`

Fetch a session with its message history (capped at 200 messages).

**Response 200** ([SessionDetail](../app/api/schemas/chat.py#L39)) — `SessionSummary` plus:

```json
{
  "messages": [
    {
      "id": 101,
      "role": "user",
      "content": "Deploy my Flask web app",
      "metadata_json": null,
      "position": 0,
      "created_at": "2026-05-09T12:01:00Z"
    },
    {
      "id": 102,
      "role": "infra-agent",
      "content": "Deployed successfully — app: my-app, deployment ID: 42, status: deployed",
      "metadata_json": null,
      "position": 1,
      "created_at": "2026-05-09T12:01:14Z"
    }
  ]
}
```

`role` is one of `user`, `infra-agent`, `sre-agent`.

**Errors**: `404` — session not found / not owned.

### `GET /api/v1/chat/sessions/{session_id}/deployments`

Authoritative list of deployments created in this session. Used by the frontend to populate the Visualization tab dropdown. Standalone deploys (no chat session) are excluded.

**Response 200** — array of [DeploymentSummary](../app/api/schemas/chat.py#L43):

```json
[
  {
    "id": 42,
    "app_name": "my-web-app",
    "status": "deployed",
    "created_at": "2026-05-09T12:01:14Z",
    "updated_at": "2026-05-09T12:01:14Z"
  }
]
```

### `DELETE /api/v1/chat/sessions/{session_id}`

Delete a session and all of its messages. Any active WebSocket on this session is closed with code `4010` (session deleted).

**Response 204** — no body.

---

## Chat — WebSocket

### `WS /api/v1/chat/sessions/{session_id}/ws?token={jwt}`

Primary interaction surface for deploy and SRE operations. Authentication is via the `token` query parameter (JWT) since browsers cannot set custom headers on `WebSocket` upgrades.

**Connection lifecycle**

1. Client opens the socket with `?token=<jwt>`.
2. Server validates the JWT and verifies the session is owned by the user.
3. On success, the server registers the connection and enters the receive loop.
4. On failure, the server closes the socket with one of the codes below.

**Close codes**

| Code | Meaning |
|---|---|
| `4001` | Authentication failed (invalid/expired JWT or unknown user) |
| `4004` | Forbidden (session not owned by the authenticated user) |
| `4010` | Session deleted while the socket was open |

**Inbound frame** ([WSClientMessage](../app/api/schemas/chat.py#L62))

```json
{ "type": "message", "content": "Deploy my Flask web app" }
```

The server classifies intent (deploy / delete / sre / general) by keyword regex, persists the user message, dispatches to the appropriate agent, and returns the agent's reply.

**Outbound frames**

`typing` — emitted before and after agent dispatch:

```json
{ "type": "typing", "agent": "infra-agent", "is_typing": true }
```

`agent_response` — final reply, includes the persisted message:

```json
{
  "type": "agent_response",
  "message": {
    "id": 102,
    "role": "infra-agent",
    "content": "Deployed successfully — app: my-app, deployment ID: 42, status: deployed",
    "metadata_json": null,
    "position": 1,
    "created_at": "2026-05-09T12:01:14Z"
  }
}
```

`sre_alert` — pushed by the background SRE monitor when an issue is detected on any of the user's deployments:

```json
{
  "type": "sre_alert",
  "deployment_id": 42,
  "app_name": "my-web-app",
  "message": { "id": 250, "role": "sre-agent", "content": "...", "metadata_json": "{\"plan_id\": 7}", "position": 14, "created_at": "2026-05-09T12:30:00Z" }
}
```

`plan_update` — broadcast after a remediation plan is approved/rejected (from REST or chat):

```json
{ "type": "plan_update", "plan_id": 7, "approved": true, "applied": true }
```

`error` — invalid frame format:

```json
{ "type": "error", "detail": "Invalid message format" }
```

Frame models live in [app/api/schemas/chat.py:62-87](../app/api/schemas/chat.py#L62-L87).

---

## Visualization

### `GET /api/v1/visualize/{deployment_id}`

Live cluster state for a deployment, plus the deployment's remediation history. Requires `Authorization`.

**Query parameters**

| Name | Type | Description |
|---|---|---|
| `session_id` | int (optional) | If provided, the deployment must belong to this chat session — prevents cross-session leakage. |

**Response 200** ([VisualizeResponse](../app/api/schemas/visualize.py#L88))

```json
{
  "deployment_id": 42,
  "app_name": "my-web-app",
  "status": "deployed",
  "namespace": "user-alice",
  "created_at": "2026-05-09T12:00:00Z",
  "updated_at": "2026-05-09T12:00:00Z",
  "cluster": {
    "name": "minikube",
    "provider": "minikube",
    "region": "local",
    "k8s_version": "v1.30.0",
    "nodes": { "ready": 1, "total": 1 }
  },
  "tiers": [
    {
      "name": "web",
      "kind": "Deployment",
      "containers": [{ "name": "nginx", "image": "nginx:1.25" }],
      "pods": { "running": 2, "pending": 0, "failed": 0, "total": 2 },
      "resources": {
        "cpu_usage_percent": 12.4,
        "memory_usage_percent": 33.1,
        "cpu_requests": "100m",
        "memory_requests": "128Mi"
      },
      "service": { "type": "ClusterIP", "port": 80 },
      "hpa": { "min_replicas": 2, "max_replicas": 5, "cpu_target_percent": 70 },
      "storage": null
    }
  ],
  "traffic": { "uptime_percent": 100.0 },
  "remediation_plans": [
    {
      "id": 7,
      "analysis": "CrashLoopBackOff on web-pod-2...",
      "approved": true,
      "applied": true,
      "rejected": false,
      "source": "manual",
      "created_at": "2026-05-09T12:05:00Z"
    }
  ],
  "error": null
}
```

**Kubernetes-unreachable fallback**: when the cluster is unreachable, the response still returns `200` with the DB-only fields populated and `cluster: null`, `tiers: []`, `traffic: null`, and a populated `error` string (per spec §6.5).

**Errors**: `404` — deployment not found, not owned, or (when `session_id` is given) not in that session.

Field-level component schemas: [ClusterInfo](../app/api/schemas/visualize.py#L11), [TierInfo](../app/api/schemas/visualize.py#L55), [PodCounts](../app/api/schemas/visualize.py#L24), [ResourceInfo](../app/api/schemas/visualize.py#L31), [HPAInfo](../app/api/schemas/visualize.py#L43), [StorageInfo](../app/api/schemas/visualize.py#L49), [TrafficMetrics](../app/api/schemas/visualize.py#L66).

---

## Endpoint summary

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | — | Liveness probe |
| POST | `/api/v1/auth/register` | — | Register, provision namespace, return JWT |
| POST | `/api/v1/auth/login` | — | Login, return JWT |
| POST | `/api/v1/deploy/` | Bearer | Provision a deployment from natural-language requirements |
| GET | `/api/v1/deploy/{id}` | Bearer | Fetch deployment + desired-state YAML |
| POST | `/api/v1/monitor/scan` | Bearer | Run SRE scan, halt at approval interrupt |
| GET | `/api/v1/monitor/plans` | Bearer | List remediation plans for caller |
| POST | `/api/v1/monitor/plans/{id}/approve` | Bearer | Approve / reject a plan |
| POST | `/api/v1/chat/sessions` | Bearer | Create chat session |
| GET | `/api/v1/chat/sessions` | Bearer | List chat sessions |
| GET | `/api/v1/chat/sessions/{id}` | Bearer | Get session + messages |
| GET | `/api/v1/chat/sessions/{id}/deployments` | Bearer | Deployments created in this session |
| DELETE | `/api/v1/chat/sessions/{id}` | Bearer | Delete session |
| WS | `/api/v1/chat/sessions/{id}/ws?token=` | JWT in query | Primary chat / agent dispatch surface |
| GET | `/api/v1/visualize/{id}` | Bearer | Live cluster state + remediation history |

The live OpenAPI schema is always available at `GET /openapi.json` and rendered at `/docs` (Swagger UI) and `/redoc`.
