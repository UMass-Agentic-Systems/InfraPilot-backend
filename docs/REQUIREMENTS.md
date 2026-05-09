# InfraPilot Backend — Non-Functional Requirements & Risks

This document collects the project-level non-functional requirements (NFRs) and the challenges/risks that shaped the system design.

---

## Non-Functional Requirements

### 1. Data Security

- Passwords are hashed with **bcrypt** before persistence — see [app/core/security.py](../app/core/security.py).
- Authentication uses **JWT (HS256)** tokens with a configurable expiration (`ACCESS_TOKEN_EXPIRE_MINUTES`, default 60 minutes).
- Secrets (`SECRET_KEY`, `GOOGLE_API_KEY`, DB credentials) are loaded from environment variables via `pydantic-settings` and are never logged or returned in API responses.
- The `.env` file is excluded from version control via `.gitignore`.

### 2. Tenant Isolation

- Every registered user is provisioned a unique Kubernetes namespace of the form `user-<sanitized-email-local>` — see `_derive_namespace()` in [app/api/v1/auth.py](../app/api/v1/auth.py).
- All deployment, monitor, and visualization endpoints enforce **strict ownership checks**: cross-user reads collapse to `404 Not Found` (rather than `403`) so the API does not leak the existence of resources owned by other users.
- The single intentional exception is `POST /api/v1/monitor/plans/{id}/approve`, which returns `403` because the plan ID is known to the caller (e.g., from a shared link) but not owned by them.

### 3. Auditability

- Every remediation plan persists its **rationale**, **approval decision**, **execution status**, and **source** (`manual` vs `background`) with timestamps in the `RemediationPlan` table.
- Chat messages persist with monotonic `position` indices so the full conversation, including agent decisions, is reconstructable.
- Approve/reject decisions also update the `metadata_json` of related chat messages, so reloaded chat history reflects the final state of any plan.

### 4. Availability

- The `/health` endpoint returns `200 OK` even when the database or Kubernetes cluster is temporarily unreachable — see [app/main.py](../app/main.py).
- The visualization endpoint degrades gracefully: when Kubernetes is unreachable it still returns `200` with the DB-only fields populated and a populated `error` string instead of failing the request (spec §6.5).
- Background SRE scans run as `asyncio` tasks managed by the FastAPI lifespan, so a failed scan does not crash the API server.

---

## Challenges & Risks

### 1. LLM Output Reliability

The Provisioning Agent depends on Google Gemini to generate valid Kubernetes YAML from natural-language requirements. LLMs may produce malformed manifests, hallucinate fields, or pick insecure defaults.

**Mitigations**

- All generated YAML is validated with `yaml.safe_load()` before being applied to the cluster.
- LLM temperature is kept low and prompts are structured with explicit output-format instructions.
- Generated manifests are stored as `desired_state_yaml` on the deployment row so they can be inspected and re-run deterministically.

### 2. Kubernetes Integration Complexity

Differences between Minikube, managed clusters (GKE/EKS), and bare-metal setups introduce inconsistencies in API behavior, CRD availability, and RBAC defaults.

**Mitigations**

- **Minikube** is the development baseline with a documented setup in [BUILD.md](../BUILD.md).
- All calls to the Kubernetes API are wrapped in structured error handling so cluster issues surface as user-visible messages rather than 500s.
- Visualization gracefully reports `"Kubernetes unavailable"` instead of failing the request when the cluster is unreachable.

### 3. Project Timeline & Dependency Drift

The project depends on rapidly evolving libraries (LangGraph, LangChain, the Kubernetes Python client) and must ship within an academic semester.

**Mitigations**

- Dependency versions are **pinned** in [pyproject.toml](../pyproject.toml).
- Scope is explicitly prioritized around the core workflow: register → deploy → scan → approve.
- Sprint-based schedule with bi-weekly milestones, plus CI (ruff, black, mypy, pytest) on every PR to catch regressions early.
