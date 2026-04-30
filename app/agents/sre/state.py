from typing import Any, TypedDict


class SREState(TypedDict):
    """State carried through the SRE LangGraph (spec §7.2).

    Flow: poll_events -> [no events] -> END
                       \\-> analyze -> propose_plan -> await_approval
                                                          (interrupt)
                                                          -> apply_fix -> END
    """

    user_id: int
    namespace: str
    deployment_id: int
    events: list[dict[str, Any]]
    analysis: str
    remediation_plan: dict[str, Any]
    rationale: str
    plan_db_id: int | None
    approved: bool
    applied: bool
    should_continue: bool
    source: str
    error: str | None
