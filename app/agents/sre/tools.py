"""SRE agent tools (spec §7.2).

Three tools called from the LangGraph nodes:
  - fetch_warning_events: read Warning events from a user's namespace
  - analyze_events: ask Gemini for root-cause + remediation plan + rationale
  - apply_remediation: execute approved actions on the cluster
"""

import json
import logging
import re
from typing import Any

from kubernetes import client
from kubernetes.client.exceptions import ApiException
from langchain_google_genai import ChatGoogleGenerativeAI

from app.core.config import settings
from app.services.k8s_client import KubernetesService

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"
_TEMPERATURE = 0.1

_SYSTEM_PROMPT = """You are a Kubernetes Site Reliability Engineer.

Given a JSON list of Kubernetes Warning events from a single namespace,
analyze the root cause and propose a structured remediation plan.

Respond with ONLY a JSON object (no markdown fences, no commentary) shaped
exactly like:

{
  "analysis": "<short root-cause summary>",
  "remediation_plan": {
    "actions": [
      {"type": "restart_deployment", "target": "<deployment-name>", "params": {}},
      {"type": "delete_pod",         "target": "<pod-name>",        "params": {}},
      {"type": "scale",              "target": "<deployment-name>", "params": {"replicas": 3}}
    ]
  },
  "rationale": "<auditable justification linking each action to the events>"
}

Allowed action types: restart_deployment, delete_pod, scale.
If no remediation is appropriate, return an empty actions list."""


_FENCE_OPEN = re.compile(r"^\s*```(?:[a-zA-Z0-9_+-]*)\s*\n?")
_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$")


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = _FENCE_OPEN.sub("", text, count=1)
    text = _FENCE_CLOSE.sub("", text, count=1)
    return text.strip()


def _make_llm() -> ChatGoogleGenerativeAI:
    """Lazy LLM construction so missing keys don't crash unrelated callers."""
    return ChatGoogleGenerativeAI(  # type: ignore[call-arg]
        model=_GEMINI_MODEL,
        temperature=_TEMPERATURE,
        google_api_key=settings.GOOGLE_API_KEY.get_secret_value() or None,
    )


def fetch_warning_events(namespace: str) -> list[dict[str, Any]]:
    """Return Warning events for `namespace` in the shape spec'd in §7.2."""
    k8s = KubernetesService.get_instance()
    return k8s.get_warning_events(namespace)


async def analyze_events(events_json: str) -> dict[str, Any]:
    """Ask Gemini for analysis + plan + rationale.

    Falls back to a manual-review-required placeholder when the LLM response
    isn't valid JSON (spec §9.4) so the graph can still produce a persistable
    RemediationPlan row.
    """
    llm = _make_llm()
    prompt = f"{_SYSTEM_PROMPT}\n\nEvents:\n{events_json}\n"
    response = await llm.ainvoke(prompt)
    raw: Any = getattr(response, "content", response)
    text = _strip_fences(str(raw))

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("analyze_events: LLM returned non-JSON, falling back")
        return {
            "analysis": text or "LLM returned no analysis",
            "remediation_plan": {"actions": []},
            "rationale": "Automated parsing failed; manual review required.",
        }

    return {
        "analysis": str(parsed.get("analysis", "")) or "No analysis provided",
        "remediation_plan": parsed.get("remediation_plan") or {"actions": []},
        "rationale": str(parsed.get("rationale", "")) or "No rationale provided",
    }


def _scale_deployment(namespace: str, name: str, replicas: int) -> None:
    """Patch spec.replicas on a Deployment.

    Lives here (rather than in KubernetesService) because `scale` is the only
    consumer right now and issue #12 doesn't extend the k8s service surface.
    """
    apps = client.AppsV1Api()
    apps.patch_namespaced_deployment(name, namespace, {"spec": {"replicas": replicas}})


def apply_remediation(namespace: str, plan_json: str) -> dict[str, Any]:
    """Execute each action in `plan_json` in order.

    Per-action failures are isolated: an action that raises is recorded as
    `status="failed"` in the result list, but the loop continues so subsequent
    actions still get a chance to run (spec §7.2 / AC #5).
    """
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        return {"results": [], "error": f"plan_json invalid: {exc}"}

    actions = plan.get("actions") or []
    k8s = KubernetesService.get_instance()
    results: list[dict[str, Any]] = []

    for action in actions:
        action_type = action.get("type")
        target = action.get("target", "")
        params = action.get("params") or {}
        entry: dict[str, Any] = {"type": action_type, "target": target}

        try:
            if action_type == "restart_deployment":
                k8s.restart_deployment(namespace, target)
                entry["status"] = "ok"
            elif action_type == "delete_pod":
                k8s.delete_resource(namespace, "Pod", target)
                entry["status"] = "ok"
            elif action_type == "scale":
                replicas = int(params.get("replicas", 1))
                _scale_deployment(namespace, target, replicas)
                entry["status"] = "ok"
                entry["replicas"] = replicas
            else:
                entry["status"] = "skipped"
                entry["reason"] = f"unsupported action type: {action_type}"
        except (ApiException, ValueError, KeyError) as exc:
            logger.warning("remediation action %s on %s failed: %s", action_type, target, exc)
            entry["status"] = "failed"
            entry["error"] = str(exc)
        except Exception as exc:  # last-resort isolation per spec §7.2
            logger.warning("remediation action %s on %s raised: %s", action_type, target, exc)
            entry["status"] = "failed"
            entry["error"] = str(exc)

        results.append(entry)

    return {"results": results}
