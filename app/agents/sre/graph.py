"""SRE Agent — cyclic LangGraph with human-in-the-loop interrupt (spec §7.2).

```
poll_events --[no events]--> END
     |
     [events] v
  analyze -> propose_plan -> await_approval --[interrupt]--> apply_fix --> END
```

`MemorySaver` is module-level so the same checkpointer survives between the
initial `run_sre_agent` invocation (which pauses at the interrupt) and the
later `resume_sre_agent` call that supplies the user's decision. Thread ID
format `f"sre-{deployment_id}-{user_id}"` is shared with the chat handler so
plans started via REST can be approved via chat and vice versa (spec §6.4).

Each node returns a partial state update. Soft failures (LLM, K8s, DB) set
`error` and route to END so callers always get a structured response back
instead of an unhandled exception.
"""

import json
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from sqlalchemy.orm import Session

from app.agents.sre import tools
from app.agents.sre.state import SREState
from app.db.models import RemediationPlan

logger = logging.getLogger(__name__)


_CHECKPOINTER = MemorySaver()


def _thread_id(deployment_id: int, user_id: int) -> str:
    return f"sre-{deployment_id}-{user_id}"


def _build_graph(db: Session) -> Any:
    def poll_events(state: SREState) -> dict[str, Any]:
        try:
            events = tools.fetch_warning_events(state["namespace"])
        except Exception as exc:
            logger.warning("poll_events failed: %s", exc)
            return {"error": f"poll_events failed: {exc}", "should_continue": False}
        return {"events": events, "should_continue": bool(events)}

    async def analyze(state: SREState) -> dict[str, Any]:
        try:
            result = await tools.analyze_events(json.dumps(state["events"]))
        except Exception as exc:
            logger.warning("analyze failed: %s", exc)
            return {"error": f"analyze failed: {exc}"}
        return {
            "analysis": result["analysis"],
            "remediation_plan": result["remediation_plan"],
            "rationale": result["rationale"],
        }

    def propose_plan(state: SREState) -> dict[str, Any]:
        try:
            plan = RemediationPlan(
                deployment_id=state["deployment_id"],
                event_summary=json.dumps(state["events"]),
                analysis=state["analysis"],
                plan_json=json.dumps(state["remediation_plan"]),
                rationale=state["rationale"],
                approved=False,
                applied=False,
                source=state["source"],
            )
            db.add(plan)
            db.commit()
            db.refresh(plan)
        except Exception as exc:
            db.rollback()
            logger.warning("propose_plan DB write failed: %s", exc)
            return {"error": f"propose_plan failed: {exc}"}
        return {"plan_db_id": plan.id}

    def await_approval(state: SREState) -> dict[str, Any]:
        summary = {
            "plan_db_id": state["plan_db_id"],
            "analysis": state["analysis"],
            "rationale": state["rationale"],
            "action_count": len((state["remediation_plan"] or {}).get("actions", [])),
        }
        # interrupt() pauses the graph; on resume the value passed to
        # Command(resume=...) is returned here. We wrap the bool in a dict
        # because langgraph 0.2.60's map_command skips falsy resume values
        # (`if cmd.resume:`), which would drop a plain `False` rejection.
        resume_value = interrupt(summary)
        if isinstance(resume_value, dict):
            approved = bool(resume_value.get("approved", False))
        else:
            approved = bool(resume_value)

        plan_id = state["plan_db_id"]
        if plan_id is not None:
            row = db.get(RemediationPlan, plan_id)
            if row is not None:
                row.approved = approved
                db.commit()
        return {"approved": approved}

    def apply_fix(state: SREState) -> dict[str, Any]:
        if not state["approved"]:
            return {"applied": False}

        try:
            plan_json = json.dumps(state["remediation_plan"])
            tools.apply_remediation(state["namespace"], plan_json)
        except Exception as exc:
            logger.warning("apply_remediation failed: %s", exc)
            return {"error": f"apply_remediation failed: {exc}", "applied": False}

        plan_id = state["plan_db_id"]
        if plan_id is not None:
            row = db.get(RemediationPlan, plan_id)
            # NFR §12.1: applied=True only when approved=True (CHECK constraint
            # enforces this in the DB; we additionally guard here).
            if row is not None and row.approved:
                row.applied = True
                db.commit()
        return {"applied": True}

    g: StateGraph = StateGraph(SREState)
    g.add_node("poll_events", poll_events)
    g.add_node("analyze", analyze)
    g.add_node("propose_plan", propose_plan)
    g.add_node("await_approval", await_approval)
    g.add_node("apply_fix", apply_fix)

    g.set_entry_point("poll_events")
    g.add_conditional_edges(
        "poll_events",
        lambda s: "end" if s.get("error") or not s.get("should_continue") else "ok",
        {"end": END, "ok": "analyze"},
    )
    g.add_conditional_edges(
        "analyze",
        lambda s: "end" if s.get("error") else "ok",
        {"end": END, "ok": "propose_plan"},
    )
    g.add_conditional_edges(
        "propose_plan",
        lambda s: "end" if s.get("error") else "ok",
        {"end": END, "ok": "await_approval"},
    )
    g.add_edge("await_approval", "apply_fix")
    g.add_edge("apply_fix", END)

    return g.compile(checkpointer=_CHECKPOINTER)


def _initial_state(*, user_id: int, namespace: str, deployment_id: int, source: str) -> SREState:
    return {
        "user_id": user_id,
        "namespace": namespace,
        "deployment_id": deployment_id,
        "events": [],
        "analysis": "",
        "remediation_plan": {"actions": []},
        "rationale": "",
        "plan_db_id": None,
        "approved": False,
        "applied": False,
        "should_continue": False,
        "source": source,
        "error": None,
    }


def _config(deployment_id: int, user_id: int) -> dict[str, Any]:
    return {"configurable": {"thread_id": _thread_id(deployment_id, user_id)}}


def _values_from_checkpoint(graph: Any, config: dict[str, Any]) -> dict[str, Any]:
    snapshot = graph.get_state(config)
    return dict(snapshot.values) if snapshot and snapshot.values else {}


async def run_sre_agent(
    *,
    user_id: int,
    namespace: str,
    deployment_id: int,
    source: str = "manual",
    db: Session,
) -> dict[str, Any]:
    """Run the SRE graph until it pauses at the approval interrupt (or ends).

    Returns a dict shaped like `monitor.ScanResponse`:
      `{plan_db_id, analysis, rationale, status}`
    where `status` is `"awaiting_approval"`, `"no_issues"`, or `"failed"`.
    """
    graph = _build_graph(db)
    config = _config(deployment_id, user_id)
    initial = _initial_state(
        user_id=user_id, namespace=namespace, deployment_id=deployment_id, source=source
    )

    await graph.ainvoke(initial, config=config)
    values = _values_from_checkpoint(graph, config)

    if values.get("error"):
        return {
            "plan_db_id": values.get("plan_db_id"),
            "analysis": values.get("analysis") or None,
            "rationale": values.get("rationale") or None,
            "status": "failed",
        }
    if not values.get("should_continue"):
        return {
            "plan_db_id": None,
            "analysis": None,
            "rationale": None,
            "status": "no_issues",
        }
    return {
        "plan_db_id": values.get("plan_db_id"),
        "analysis": values.get("analysis"),
        "rationale": values.get("rationale"),
        "status": "awaiting_approval",
    }


async def resume_sre_agent(
    *,
    user_id: int,
    deployment_id: int,
    approved: bool,
    db: Session,
) -> dict[str, Any]:
    """Resume the paused SRE graph with the user's approval decision.

    Returns `{plan_id, approved, applied}` shaped like `monitor.ApproveResponse`.
    """
    graph = _build_graph(db)
    config = _config(deployment_id, user_id)

    await graph.ainvoke(Command(resume={"approved": approved}), config=config)
    values = _values_from_checkpoint(graph, config)

    return {
        "plan_id": values.get("plan_db_id"),
        "approved": bool(values.get("approved", approved)),
        "applied": bool(values.get("applied", False)),
    }
