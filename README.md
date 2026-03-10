# InfraPilot

**AI-Powered Kubernetes Infrastructure Management for the Five College Community**

> CS 520 Project — University of Massachusetts Amherst (Team 40)

## Team

| Name | GitHub |
|------|--------|
| Jerin Thomas | [@jerinthomas1404](https://github.com/jerinthomas1404) |
| Yash Sant | [@JaycePiltover](https://github.com/JaycePiltover) |
| Pranjeet Dhanapune | [@pranjeet](https://github.com/pranjeet) |
| Atharva Patil | [@atharvadpatil](https://github.com/atharvadpatil) |

## Repositories

- **Backend:** [InfraPilot-backend](https://github.com/UMass-Agentic-Systems/InfraPilot-backend)
- **Frontend:** [InfraPilot-frontend](https://github.com/UMass-Agentic-Systems/InfraPilot-frontend)

## Overview

Managing Kubernetes infrastructure is a significant barrier for student teams, researchers, and small organizations within the Five College community (UMass Amherst, Amherst College, Hampshire College, Mount Holyoke College, and Smith College). InfraPilot addresses this gap by providing an AI-powered backend that automates Kubernetes provisioning and site-reliability engineering through intelligent, conversational agents.

The system targets students, faculty, and lab administrators who need to deploy three-tier applications on shared Kubernetes clusters without mastering `kubectl`, YAML manifests, or observability tooling. It combines a **Provisioning Agent** that translates natural-language requirements into production-ready K8s manifests with an **SRE Agent** that continuously monitors cluster health and proposes auditable remediation plans with human-in-the-loop approval.

Multi-tenant namespace isolation ensures that each user's resources are securely partitioned, making it safe for shared academic environments.

## Features

- **User Authentication & Multi-Tenant Namespace Management** — Secure registration, login, and automatic provisioning of isolated Kubernetes namespaces per user.
- **AI-Powered Infrastructure Provisioning** — Natural-language interface that translates application requirements into production-ready Kubernetes manifests and deploys them to the cluster.
- **Intelligent SRE Monitoring with Human-in-the-Loop Approval** — Continuous cluster health monitoring using AI to analyze warning events, generate remediation plans, and require explicit human approval before applying any fix.
- **Auditable Remediation Trail** — Every SRE-proposed action is persisted with a rationale, approval status, and execution record for full traceability.
- **Deployment State Management** — Persistent tracking of desired-state YAML, deployment status, and history in PostgreSQL.

## API Endpoints

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/auth/register` | Register a new user and provision a K8s namespace |
| POST | `/api/v1/auth/login` | Authenticate and receive a JWT access token |

### Deployments

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/deploy/` | Deploy an application via natural-language requirements |
| GET | `/api/v1/deploy/{deployment_id}` | View deployment status and desired-state YAML |

### Monitoring & Remediation

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/monitor/scan` | Trigger an SRE health scan for a deployment |
| GET | `/api/v1/monitor/plans` | List all remediation plans for the authenticated user |
| POST | `/api/v1/monitor/plans/{plan_id}/approve` | Approve or reject a pending remediation plan |

## Non-Functional Requirements

- **Data Security** — Passwords hashed with bcrypt; JWT-based authentication with configurable expiration; secrets excluded from logs and responses.
- **Tenant Isolation** — Unique Kubernetes namespace per user; strict ownership checks on all API endpoints.
- **Auditability** — Every remediation plan persists a rationale, approval decision, and execution status with timestamps.
- **Availability** — The `/health` endpoint returns `200 OK` even when the database or K8s cluster is temporarily unreachable.

## Challenges & Risks

### 1. LLM Output Reliability
The Provisioning Agent depends on Gemini-Pro to generate valid Kubernetes YAML from natural language. LLMs may produce malformed manifests or insecure defaults.

**Mitigations:** YAML validation with `yaml.safe_load()`, low LLM temperature, structured prompts with explicit output format instructions.

### 2. Kubernetes Integration Complexity
Differences between Minikube, managed clusters (GKE/EKS), and bare-metal setups introduce inconsistencies in API behavior and RBAC policies.

**Mitigations:** Minikube as the development baseline with a documented setup guide; structured error handling around all K8s API calls.

### 3. Project Timeline & Dependency Drift
The project depends on rapidly evolving libraries (LangGraph, LangChain, Kubernetes Python client) and must ship within an academic semester.

**Mitigations:** Pinned dependency versions in `pyproject.toml`; prioritized core workflow (register → deploy → scan → approve); sprint-based schedule with bi-weekly milestones.
