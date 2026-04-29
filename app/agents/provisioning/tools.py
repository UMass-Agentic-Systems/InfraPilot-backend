"""Provisioning agent tools (spec §7.1).

Three tools called from the LangGraph nodes:
  - generate_manifests: ask Gemini for multi-document K8s YAML
  - store_manifests: persist YAML on the Deployment row
  - apply_to_cluster: apply manifests to the user's namespace
"""

import logging
import re
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Deployment
from app.services.k8s_client import KubernetesService

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-2.5-flash"
_TEMPERATURE = 0.2

_SYSTEM_PROMPT = """You are a Kubernetes manifest generator.

Given the user's free-text application requirements, output ONLY a valid
multi-document Kubernetes YAML manifest. The output must:

- Use ONLY these kinds: Deployment, Service, ConfigMap, Secret.
- Separate documents with '---'.
- Be directly applicable with `kubectl apply -f`.
- Contain NO commentary, NO prose, NO markdown fences — pure YAML.
- Avoid runtime hostPath, privileged: true, or hostNetwork.

If the user asks for something the listed kinds cannot express, produce the
closest reasonable subset; never emit unsupported kinds."""


_FENCE_OPEN = re.compile(r"^\s*```(?:[a-zA-Z0-9_+-]*)\s*\n?")
_FENCE_CLOSE = re.compile(r"\n?\s*```\s*$")


def _strip_fences(text: str) -> str:
    """Remove surrounding ```yaml ... ``` (or ``` ... ```) markdown fences.

    Only strips one outermost fence; inner fences inside YAML are unlikely
    in valid K8s manifests but if present are left alone.
    """
    text = text.strip()
    text = _FENCE_OPEN.sub("", text, count=1)
    text = _FENCE_CLOSE.sub("", text, count=1)
    return text.strip()


def _make_llm() -> ChatGoogleGenerativeAI:
    """Construct the Gemini chat model lazily so import-time failures
    (missing key, no network) don't crash unrelated callers."""
    return ChatGoogleGenerativeAI(  # type: ignore[call-arg]
        model=_GEMINI_MODEL,
        temperature=_TEMPERATURE,
        google_api_key=settings.GOOGLE_API_KEY.get_secret_value() or None,
    )


async def generate_manifests(requirements: str) -> str:
    """Prompt Gemini for multi-document K8s YAML matching `requirements`.

    Markdown fences are stripped from the response. Validation that the
    output is parseable YAML is the caller's responsibility (the graph node
    does it via `yaml.safe_load_all` so a malformed response is surfaced as
    a `status="failed"` rather than an unhandled exception).
    """
    llm = _make_llm()
    prompt = f"{_SYSTEM_PROMPT}\n\nRequirements:\n{requirements}\n"
    response = await llm.ainvoke(prompt)
    raw: Any = getattr(response, "content", response)
    return _strip_fences(str(raw))


def store_manifests(deployment_id: int, yaml_str: str, db: Session) -> dict[str, bool]:
    """Persist generated YAML on the Deployment row."""
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise ValueError(f"Deployment {deployment_id} not found")
    deployment.desired_state_yaml = yaml_str
    db.commit()
    return {"ok": True}


def apply_to_cluster(namespace: str, yaml_str: str) -> list[dict[str, Any]]:
    """Ensure the namespace exists, then apply the multi-doc manifest."""
    k8s = KubernetesService.get_instance()
    k8s.ensure_namespace(namespace)
    return k8s.apply_manifest(namespace, yaml_str)
