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
- **WebSocket Chat Interface** — Primary interaction surface for all deployment and SRE operations; persistent chat sessions with full history backed by PostgreSQL.
- **Infrastructure Visualization** — Live cluster state per deployment: pod counts, resource requests, services, HPAs, PVCs, and remediation history.

## Documentation

| Document | Audience | What's inside |
|---|---|---|
| [BUILD.md](BUILD.md) | Operators | Prerequisites, installation, configuration, local dev, and production deployment guidance |
| [docs/API.md](docs/API.md) | API consumers, Project Document | Full HTTP + WebSocket reference with request/response schemas |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | Reviewers | Non-functional requirements and challenges & risks |
| [docs/use_case_verification.md](docs/use_case_verification.md) | Reviewers | Use-case-by-use-case verification matrix |

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env   # set SECRET_KEY and GOOGLE_API_KEY
uvicorn app.main:app --reload
```

See [BUILD.md](BUILD.md) for the full setup (PostgreSQL, Minikube, environment variables, and production deployment).

## License

See [LICENSE](LICENSE).
