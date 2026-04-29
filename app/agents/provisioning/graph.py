"""Provisioning Agent — linear LangGraph pipeline (spec §7.1).

```
parse_requirements -> gen_manifests -> store_in_db -> deploy_to_k8s -> END
        | failed            | failed
        +-> END             +-> END
```

Each node returns a *partial* state update. Failure inside any node sets
`status="failed"` and an `error` message rather than raising — the deploy
endpoint relies on this to surface failures as HTTP 200 with `status="failed"`
(spec §6.2 / A1 fix).
"""

import logging
from typing import Any

import yaml
from langgraph.graph import END, StateGraph
from sqlalchemy.orm import Session

from app.agents.provisioning import tools
from app.agents.provisioning.state import ProvisioningState
from app.db.models import Deployment

logger = logging.getLogger(__name__)


def _build_graph(db: Session) -> Any:
    def parse_requirements(state: ProvisioningState) -> dict[str, Any]:
        if not state["requirements"].strip():
            return {"status": "failed", "error": "requirements cannot be empty"}
        return {"status": "generating"}

    async def gen_manifests(state: ProvisioningState) -> dict[str, Any]:
        try:
            yaml_str = await tools.generate_manifests(state["requirements"])
        except Exception as exc:
            logger.warning("manifest generation failed: %s", exc)
            return {"status": "failed", "error": f"manifest generation failed: {exc}"}

        try:
            # Spec §12.5: validate LLM output before any cluster contact.
            list(yaml.safe_load_all(yaml_str))
        except yaml.YAMLError as exc:
            return {"status": "failed", "error": f"generated YAML is invalid: {exc}"}

        if not yaml_str.strip():
            return {"status": "failed", "error": "generated YAML is empty"}

        return {"manifests": yaml_str, "status": "storing"}

    def store_in_db(state: ProvisioningState) -> dict[str, Any]:
        try:
            deployment = Deployment(
                user_id=state["user_id"],
                chat_session_id=state["chat_session_id"],
                app_name=state["app_name"],
                desired_state_yaml=state["manifests"],
                status="deploying",
            )
            db.add(deployment)
            db.commit()
            db.refresh(deployment)
        except Exception as exc:
            db.rollback()
            logger.warning("DB store failed: %s", exc)
            return {"status": "failed", "error": f"db store failed: {exc}"}
        return {"deployment_id": deployment.id, "status": "deploying"}

    def deploy_to_k8s(state: ProvisioningState) -> dict[str, Any]:
        deployment_id = state["deployment_id"]
        try:
            tools.apply_to_cluster(state["namespace"], state["manifests"])
        except Exception as exc:
            logger.warning("k8s apply failed: %s", exc)
            if deployment_id is not None:
                row = db.get(Deployment, deployment_id)
                if row is not None:
                    row.status = "failed"
                    db.commit()
            return {"status": "failed", "error": f"k8s apply failed: {exc}"}

        if deployment_id is not None:
            row = db.get(Deployment, deployment_id)
            if row is not None:
                row.status = "deployed"
                db.commit()
        return {"status": "deployed"}

    g: StateGraph = StateGraph(ProvisioningState)
    g.add_node("parse_requirements", parse_requirements)
    g.add_node("gen_manifests", gen_manifests)
    g.add_node("store_in_db", store_in_db)
    g.add_node("deploy_to_k8s", deploy_to_k8s)

    g.set_entry_point("parse_requirements")
    g.add_conditional_edges(
        "parse_requirements",
        lambda s: "fail" if s["status"] == "failed" else "ok",
        {"fail": END, "ok": "gen_manifests"},
    )
    g.add_conditional_edges(
        "gen_manifests",
        lambda s: "fail" if s["status"] == "failed" else "ok",
        {"fail": END, "ok": "store_in_db"},
    )
    g.add_edge("store_in_db", "deploy_to_k8s")
    g.add_edge("deploy_to_k8s", END)
    return g.compile()


async def run_provisioning_agent(
    *,
    user_id: int,
    namespace: str,
    app_name: str,
    requirements: str,
    chat_session_id: int | None = None,
    db: Session,
) -> dict[str, Any]:
    """Run the provisioning pipeline and return a DeployResponse-shaped dict.

    Returns `{"deployment_id": int|None, "status": str, "error": str|None}`.
    Never raises for ordinary failure paths (LLM, YAML, DB, K8s) — the graph
    catches and converts to `status="failed"` so callers get a uniform shape.
    """
    initial: ProvisioningState = {
        "user_id": user_id,
        "namespace": namespace,
        "app_name": app_name,
        "requirements": requirements,
        "manifests": "",
        "deployment_id": None,
        "chat_session_id": chat_session_id,
        "status": "pending",
        "error": None,
    }
    graph = _build_graph(db)
    final: dict[str, Any] = await graph.ainvoke(initial)
    return {
        "deployment_id": final.get("deployment_id"),
        "status": final["status"],
        "error": final.get("error"),
    }
