from typing import TypedDict


class ProvisioningState(TypedDict):
    """State carried through the provisioning LangGraph pipeline (spec §7.1).

    Status transitions: pending -> generating -> storing -> deploying -> deployed
                                                                       -> failed
    """

    user_id: int
    namespace: str
    app_name: str
    requirements: str
    manifests: str
    deployment_id: int | None
    chat_session_id: int | None
    status: str
    error: str | None
