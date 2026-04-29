"""Provisioning Agent — placeholder.

The real LangGraph pipeline (parse_requirements -> gen_manifests -> store_in_db
-> deploy_to_k8s) lands in a follow-up issue. This stub exists so the deploy
REST endpoint can wire its call site and exercise the failure path under
tests (the agent always raising lets us prove the A1 fix: HTTP 200 with
status="failed" rather than an HTTP error).
"""

from typing import Any

from sqlalchemy.orm import Session


async def run_provisioning_agent(
    *,
    user_id: int,
    namespace: str,
    app_name: str,
    requirements: str,
    chat_session_id: int | None = None,
    db: Session,
) -> dict[str, Any]:
    """Run the provisioning pipeline; return a result mappable to DeployResponse.

    Raises:
        NotImplementedError: until the real pipeline is built.
    """
    raise NotImplementedError(
        "Provisioning agent is not yet implemented; tracked in a follow-up issue"
    )
