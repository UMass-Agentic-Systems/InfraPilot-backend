import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypedDict

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return-shape contracts consumed by the visualization endpoint (#17)
# ---------------------------------------------------------------------------


class ContainerInfo(TypedDict):
    name: str
    image: str


class TierInfo(TypedDict):
    name: str
    kind: str  # "Deployment" | "StatefulSet"
    containers: list[ContainerInfo]
    pods: dict[str, int]  # {"running": int, "pending": int, "failed": int, "total": int}
    resources: dict[
        str, object
    ]  # {"cpu_usage_percent", "memory_usage_percent", "cpu_requests", "memory_requests"}
    service: dict[str, object] | None
    hpa: dict[str, object] | None
    storage: dict[str, object] | None


class NamespaceStatus(TypedDict):
    cluster: dict[
        str, object
    ]  # {"name", "provider", "region", "k8s_version", "nodes": {"ready", "total"}}
    tiers: list[TierInfo]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class KubernetesService:
    _instance: "KubernetesService | None" = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        config.load_kube_config(config_file=settings.KUBECONFIG_PATH)
        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()
        self._autoscaling = client.AutoscalingV1Api()

    @classmethod
    def get_instance(cls) -> "KubernetesService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Namespace management
    # ------------------------------------------------------------------

    def ensure_namespace(self, namespace: str) -> None:
        try:
            self._core.read_namespace(namespace)
        except ApiException as e:
            if e.status == 404:
                body = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace))
                self._core.create_namespace(body)
            else:
                raise

    # ------------------------------------------------------------------
    # Manifest application
    # ------------------------------------------------------------------

    def apply_manifest(self, namespace: str, yaml_str: str) -> list[dict[str, Any]]:
        if not yaml_str or not yaml_str.strip():
            raise ValueError("yaml_str must not be empty")
        docs = list(yaml.safe_load_all(yaml_str))
        results = []
        for doc in docs:
            if doc is None:
                continue
            doc.setdefault("metadata", {})
            doc["metadata"]["namespace"] = namespace
            results.append(self._apply_single(namespace, doc))
        return results

    def _apply_single(self, namespace: str, manifest: dict[str, Any]) -> dict[str, str]:
        kind = manifest.get("kind", "")
        name = manifest.get("metadata", {}).get("name", "")

        if kind == "Deployment":
            return self._upsert(
                name,
                replace_fn=lambda: self._apps.replace_namespaced_deployment(
                    name, namespace, manifest
                ),
                create_fn=lambda: self._apps.create_namespaced_deployment(namespace, manifest),
            )
        elif kind == "Service":
            return self._upsert(
                name,
                replace_fn=lambda: self._core.replace_namespaced_service(name, namespace, manifest),
                create_fn=lambda: self._core.create_namespaced_service(namespace, manifest),
            )
        elif kind == "ConfigMap":
            return self._upsert(
                name,
                replace_fn=lambda: self._core.replace_namespaced_config_map(
                    name, namespace, manifest
                ),
                create_fn=lambda: self._core.create_namespaced_config_map(namespace, manifest),
            )
        elif kind == "Secret":
            return self._upsert(
                name,
                replace_fn=lambda: self._core.replace_namespaced_secret(name, namespace, manifest),
                create_fn=lambda: self._core.create_namespaced_secret(namespace, manifest),
            )
        else:
            raise ApiException(status=400, reason=f"Unsupported resource kind: {kind}")

    def _upsert(
        self,
        name: str,
        replace_fn: Callable[[], object],
        create_fn: Callable[[], object],
    ) -> dict[str, str]:
        try:
            replace_fn()
            return {"name": name, "action": "replaced"}
        except ApiException as e:
            if e.status == 404:
                create_fn()
                return {"name": name, "action": "created"}
            raise

    # ------------------------------------------------------------------
    # Event / log helpers
    # ------------------------------------------------------------------

    def get_warning_events(self, namespace: str) -> list[dict[str, object]]:
        try:
            resp = self._core.list_namespaced_event(namespace)
        except ApiException as e:
            logger.warning("Failed to list events in %s: %s", namespace, e)
            return []
        events = []
        for ev in resp.items:
            if ev.type != "Warning":
                continue
            involved = ev.involved_object
            events.append(
                {
                    "reason": ev.reason,
                    "message": ev.message,
                    "involved_object": {
                        "kind": involved.kind,
                        "name": involved.name,
                        "namespace": involved.namespace,
                    },
                    "count": ev.count,
                    "last_timestamp": (
                        ev.last_timestamp.isoformat() if ev.last_timestamp else None
                    ),
                }
            )
        return events

    def get_pod_logs(self, namespace: str, pod_name: str, tail_lines: int = 200) -> str:
        try:
            return str(
                self._core.read_namespaced_pod_log(pod_name, namespace, tail_lines=tail_lines)
            )
        except ApiException as e:
            return f"Error fetching logs for {pod_name}: {e}"

    # ------------------------------------------------------------------
    # Resource mutation
    # ------------------------------------------------------------------

    def delete_resource(self, namespace: str, kind: str, name: str) -> None:
        try:
            if kind == "Deployment":
                self._apps.delete_namespaced_deployment(name, namespace)
            elif kind == "Service":
                self._core.delete_namespaced_service(name, namespace)
            elif kind == "ConfigMap":
                self._core.delete_namespaced_config_map(name, namespace)
            elif kind == "Secret":
                self._core.delete_namespaced_secret(name, namespace)
            elif kind == "Pod":
                self._core.delete_namespaced_pod(name, namespace)
            else:
                logger.warning("delete_resource: unsupported kind %s", kind)
        except ApiException as e:
            if e.status != 404:
                raise

    def restart_deployment(self, namespace: str, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}
                }
            }
        }
        try:
            self._apps.patch_namespaced_deployment(name, namespace, patch)
        except ApiException as e:
            logger.error("Failed to restart deployment %s/%s: %s", namespace, name, e)
            raise

    # ------------------------------------------------------------------
    # Namespace status (consumed by visualization endpoint)
    # ------------------------------------------------------------------

    def get_namespace_status(self, namespace: str) -> NamespaceStatus:
        # Cluster-level info
        try:
            version_info = client.VersionApi().get_code()
            k8s_version = version_info.git_version
        except ApiException:
            k8s_version = "unknown"

        try:
            nodes_resp = self._core.list_node()
            total_nodes = len(nodes_resp.items)
            ready_nodes = sum(
                1
                for n in nodes_resp.items
                if any(
                    c.type == "Ready" and c.status == "True" for c in (n.status.conditions or [])
                )
            )
        except ApiException:
            total_nodes = 0
            ready_nodes = 0

        cluster: dict[str, object] = {
            "name": "default",
            "provider": "unknown",
            "region": "unknown",
            "k8s_version": k8s_version,
            "nodes": {"ready": ready_nodes, "total": total_nodes},
        }

        # Per-namespace resources
        try:
            pods_resp = self._core.list_namespaced_pod(namespace)
        except ApiException:
            pods_resp = None

        try:
            deployments_resp = self._apps.list_namespaced_deployment(namespace)
        except ApiException:
            deployments_resp = None

        try:
            statefulsets_resp = self._apps.list_namespaced_stateful_set(namespace)
        except ApiException:
            statefulsets_resp = None

        try:
            services_resp = self._core.list_namespaced_service(namespace)
        except ApiException:
            services_resp = None

        try:
            hpas_resp = self._autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace)
        except ApiException:
            hpas_resp = None

        try:
            pvcs_resp = self._core.list_namespaced_persistent_volume_claim(namespace)
        except ApiException:
            pvcs_resp = None

        # Build pod counts keyed by app label
        pod_counts: dict[str, dict[str, int]] = {}
        if pods_resp:
            for pod in pods_resp.items:
                key = (pod.metadata.labels or {}).get("app", pod.metadata.name)
                entry = pod_counts.setdefault(
                    key, {"running": 0, "pending": 0, "failed": 0, "total": 0}
                )
                entry["total"] += 1
                phase = (pod.status.phase or "").lower()
                if phase == "running":
                    entry["running"] += 1
                elif phase == "pending":
                    entry["pending"] += 1
                elif phase in ("failed", "error"):
                    entry["failed"] += 1

        # Service lookup keyed by app selector
        svc_map: dict[str, dict[str, object]] = {}
        if services_resp:
            for svc in services_resp.items:
                key = (svc.spec.selector or {}).get("app", svc.metadata.name)
                port = svc.spec.ports[0].port if svc.spec.ports else None
                svc_map[key] = {"type": svc.spec.type, "port": port}

        # HPA lookup keyed by target deployment name
        hpa_map: dict[str, dict[str, object]] = {}
        if hpas_resp:
            for hpa in hpas_resp.items:
                target = hpa.spec.scale_target_ref.name
                hpa_map[target] = {
                    "min_replicas": hpa.spec.min_replicas,
                    "max_replicas": hpa.spec.max_replicas,
                    "cpu_target_percent": hpa.spec.target_cpu_utilization_percentage,
                }

        # PVC lookup keyed by app label
        pvc_map: dict[str, dict[str, object]] = {}
        if pvcs_resp:
            for pvc in pvcs_resp.items:
                key = (pvc.metadata.labels or {}).get("app", pvc.metadata.name)
                capacity = (pvc.status.capacity or {}).get("storage", "0Gi")
                pvc_map[key] = {
                    "used_gi": 0.0,
                    "capacity_gi": _parse_gi(capacity),
                    "storage_class": pvc.spec.storage_class_name or "",
                }

        # Assemble tiers from Deployments + StatefulSets
        tiers: list[TierInfo] = []
        workloads: list[tuple[str, object]] = []
        if deployments_resp:
            workloads += [("Deployment", d) for d in deployments_resp.items]
        if statefulsets_resp:
            workloads += [("StatefulSet", s) for s in statefulsets_resp.items]

        for kind, wl in workloads:
            wl_name: str = wl.metadata.name  # type: ignore[attr-defined]
            containers: list[ContainerInfo] = [
                {"name": c.name, "image": c.image}
                for c in (wl.spec.template.spec.containers or [])  # type: ignore[attr-defined]
            ]
            resources: dict[str, object] = {
                "cpu_usage_percent": None,
                "memory_usage_percent": None,
                "cpu_requests": None,
                "memory_requests": None,
            }
            spec_containers = wl.spec.template.spec.containers  # type: ignore[attr-defined]
            if spec_containers:
                first = spec_containers[0]
                if first.resources and first.resources.requests:
                    resources["cpu_requests"] = first.resources.requests.get("cpu")
                    resources["memory_requests"] = first.resources.requests.get("memory")

            app_key = (wl.metadata.labels or {}).get("app", wl_name)  # type: ignore[attr-defined]
            tier: TierInfo = {
                "name": wl_name,
                "kind": kind,
                "containers": containers,
                "pods": pod_counts.get(
                    app_key, {"running": 0, "pending": 0, "failed": 0, "total": 0}
                ),
                "resources": resources,
                "service": svc_map.get(app_key) or svc_map.get(wl_name),
                "hpa": hpa_map.get(wl_name),
                "storage": pvc_map.get(app_key) or pvc_map.get(wl_name),
            }
            tiers.append(tier)

        return NamespaceStatus(cluster=cluster, tiers=tiers)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_gi(storage: str) -> float:
    """Parse a Kubernetes storage quantity (e.g. '10Gi', '512Mi') into GiB."""
    try:
        if storage.endswith("Gi"):
            return float(storage[:-2])
        if storage.endswith("Mi"):
            return float(storage[:-2]) / 1024
        if storage.endswith("G"):
            return float(storage[:-1]) * (10**9) / (1024**3)
    except (ValueError, AttributeError):
        pass
    return 0.0
