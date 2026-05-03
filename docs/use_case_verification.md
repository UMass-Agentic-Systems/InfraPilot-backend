# Use Case Verification

End-to-end curl commands for all 22 InfraPilot use cases.

**Prerequisites**

```bash
BASE=http://localhost:8000
# After UC2 (login), capture the token:
TOKEN=<access_token from login response>
```

---

## UC1 — User Registration

Register a new user; the backend provisions an isolated Kubernetes namespace.

```bash
curl -s -X POST $BASE/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"s3cr3t!"}' | jq .
```

**Expected:** `201 Created` — `{"access_token":"<jwt>","token_type":"bearer"}`

---

## UC2 — User Login

Authenticate an existing user and obtain a JWT.

```bash
curl -s -X POST $BASE/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","password":"s3cr3t!"}' | jq .
```

**Expected:** `200 OK` — `{"access_token":"<jwt>","token_type":"bearer"}`

---

## UC3 — Deploy Application

Submit a natural-language description; the Provisioning Agent generates and applies Kubernetes manifests.

```bash
curl -s -X POST $BASE/api/v1/deploy/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"app_name":"web","requirements":"Deploy a 2-replica nginx web server with a ClusterIP service on port 80."}' | jq .
```

**Expected:** `200 OK` — `{"deployment_id":<id>,"app_name":"web","status":"deployed",...}`

---

## UC4 — View Deployment Status

Retrieve desired-state YAML and current status for an owned deployment.

```bash
DEPLOYMENT_ID=1
curl -s $BASE/api/v1/deploy/$DEPLOYMENT_ID \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected:** `200 OK` — deployment object with `desired_state_yaml` and `status` fields.

Cross-user access denial (spec §11.4 — no information leakage):

```bash
# Use a token belonging to a different user; any deployment ID returns 404.
curl -s $BASE/api/v1/deploy/$DEPLOYMENT_ID \
  -H "Authorization: Bearer $OTHER_TOKEN" | jq .
```

**Expected:** `404 Not Found`

---

## UC5 — Trigger SRE Health Scan

Run the SRE Agent against a deployment; returns a remediation plan when issues are found.

```bash
curl -s -X POST $BASE/api/v1/monitor/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"deployment_id":1}' | jq .
```

**Expected:** `200 OK` — `{"status":"awaiting_approval","plan_id":<id>,...}` or `{"status":"no_issues"}`

---

## UC6 — List Remediation Plans

Retrieve all remediation plans for the authenticated user, ordered newest-first.

```bash
curl -s $BASE/api/v1/monitor/plans \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected:** `200 OK` — array of plan objects (may be empty `[]`).

---

## UC7 — Approve Remediation Plan

Human approves a pending plan; the backend applies the remediation actions to the cluster.

```bash
PLAN_ID=1
curl -s -X POST $BASE/api/v1/monitor/plans/$PLAN_ID/approve \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approve":true}' | jq .
```

**Expected:** `200 OK` — `{"plan_id":<id>,"approved":true,"applied":true}`

---

## UC8 — Reject Remediation Plan

Human rejects a pending plan; no cluster changes are made.

```bash
curl -s -X POST $BASE/api/v1/monitor/plans/$PLAN_ID/approve \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approve":false}' | jq .
```

**Expected:** `200 OK` — `{"plan_id":<id>,"approved":false,"applied":false}`

---

## UC9 — Create Chat Session

Open a persistent conversation session backed by PostgreSQL.

```bash
curl -s -X POST $BASE/api/v1/chat/sessions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"My first session"}' | jq .
```

**Expected:** `201 Created` — `{"id":<id>,"title":"My first session","created_at":...}`

---

## UC10 — List Chat Sessions

List all sessions for the authenticated user, ordered by most recently updated.

```bash
curl -s $BASE/api/v1/chat/sessions \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected:** `200 OK` — array of session summaries.

---

## UC11 — Get Chat Session with Message History

Retrieve a session with up to 200 messages in position order.

```bash
SESSION_ID=1
curl -s $BASE/api/v1/chat/sessions/$SESSION_ID \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected:** `200 OK` — session object with `messages` array.

---

## UC12 — Delete Chat Session

Delete a session and all its messages; closes any active WebSocket connection with code 4010.

```bash
curl -s -X DELETE $BASE/api/v1/chat/sessions/$SESSION_ID \
  -H "Authorization: Bearer $TOKEN" -v
```

**Expected:** `204 No Content`

---

## UC13 — WebSocket Deploy Intent

Send a deploy-intent message over WebSocket; the Provisioning Agent is invoked.

```bash
# Requires websocat: https://github.com/vi/websocat
websocat "ws://localhost:8000/api/v1/chat/sessions/$SESSION_ID/ws?token=$TOKEN"
# Then send:
{"type":"message","content":"deploy a redis cache with 1 replica"}
```

**Expected sequence of server frames:**
1. `{"type":"typing","agent":"infra-agent","is_typing":true}`
2. `{"type":"typing","agent":"infra-agent","is_typing":false}`
3. `{"type":"agent_response","message":{...}}`

---

## UC14 — WebSocket SRE Intent

Send a scan-intent message; the SRE Agent is invoked against the most recent deployment.

```bash
websocat "ws://localhost:8000/api/v1/chat/sessions/$SESSION_ID/ws?token=$TOKEN"
# Then send:
{"type":"message","content":"scan my cluster for issues"}
```

**Expected sequence of server frames:**
1. `{"type":"typing","agent":"sre-agent","is_typing":true}`
2. `{"type":"typing","agent":"sre-agent","is_typing":false}`
3. `{"type":"agent_response","message":{...}}`

---

## UC15 — WebSocket General Intent

Send a message that does not match deploy or SRE keywords; returns a helpful fallback.

```bash
websocat "ws://localhost:8000/api/v1/chat/sessions/$SESSION_ID/ws?token=$TOKEN"
# Then send:
{"type":"message","content":"hello, what can you do?"}
```

**Expected:** `agent_response` from `infra-agent` describing available commands.

---

## UC16 — WebSocket Authentication Failure

Connecting with an invalid token is rejected before the session is accepted.

```bash
websocat "ws://localhost:8000/api/v1/chat/sessions/$SESSION_ID/ws?token=bad-token"
```

**Expected:** Connection closed immediately with code `4001`.

---

## UC17 — Visualize Live Cluster State

Return live pod counts, services, HPAs, PVCs, and remediation history for a deployment.

```bash
curl -s $BASE/api/v1/visualize/$DEPLOYMENT_ID \
  -H "Authorization: Bearer $TOKEN" | jq .
```

**Expected:** `200 OK` — `{"cluster":{...},"tiers":[...],"remediation_plans":[...],"error":null}`

---

## UC18 — Background SRE Alert Delivered to Connected User

The background loop scans all namespaces every `SRE_SCAN_INTERVAL_SECONDS` seconds. When a connected user's deployment has warning events, a remediation plan is created **and** a WebSocket alert is pushed.

**Verification (observe in a running server):**

1. Connect a WebSocket as in UC13.
2. Wait for the next background scan cycle (default 120 s, configurable via `SRE_SCAN_INTERVAL_SECONDS`).
3. If K8s emits `Warning` events for the user's namespace the server pushes:

```json
{"type":"sre_alert","message":{"role":"sre-agent","content":"<analysis>",...}}
```

4. Confirm a `RemediationPlan` row was created:

```bash
curl -s $BASE/api/v1/monitor/plans \
  -H "Authorization: Bearer $TOKEN" | jq '[.[] | select(.source=="background")]'
```

**Expected:** at least one plan with `"source":"background"`.

---

## UC19 — Background SRE Plan Persisted for Disconnected User

When the background scan finds issues but the user has no active WebSocket connection, the plan is still created in the database — no message is sent.

**Verification:**

```bash
# Do not open a WebSocket. After a scan cycle:
curl -s $BASE/api/v1/monitor/plans \
  -H "Authorization: Bearer $TOKEN" | jq '[.[] | select(.source=="background")]'
```

**Expected:** plan row exists with `"source":"background"`; no corresponding `ChatMessage` row is written (verifiable only via direct DB query).

---

## UC20 — Background SRE Deduplication

The background loop skips creating a new plan when an unresolved plan already exists for the same deployment and the K8s warning events have not changed since the last scan.

**Verification:**

1. Confirm a background plan was created (UC18/UC19).
2. Do **not** approve or apply the plan.
3. Wait for another scan cycle.
4. Count plans:

```bash
curl -s $BASE/api/v1/monitor/plans \
  -H "Authorization: Bearer $TOKEN" | jq 'length'
```

**Expected:** count does not increase (duplicate suppressed).

---

## UC21 — Visualization Fallback when Kubernetes is Unreachable

When the K8s API server is unavailable, the endpoint returns a degraded response instead of a 5xx error.

```bash
# Stop Minikube or disconnect K8s, then:
curl -s $BASE/api/v1/visualize/$DEPLOYMENT_ID \
  -H "Authorization: Bearer $TOKEN" | jq '{cluster,tiers,error}'
```

**Expected:** `200 OK` — `{"cluster":null,"tiers":[],"error":"Kubernetes unavailable: ..."}`

---

## UC22 — Health Check Independent of Database / K8s

The liveness probe returns `200 OK` even when downstream services are unavailable.

```bash
curl -s $BASE/health | jq .
```

**Expected:** `200 OK` — `{"status":"ok"}` (no auth required).
