"""Parse rendered K8s YAML into platform-graph node and edge objects.

Produced nodes
--------------
- ``K8sResource``    — one per resource document (workspace-scoped, labelled by kind)
- ``ExternalEndpoint`` — global hub for egress targets (from network.py)
- ``Role``           — workspace-scoped: Role or ClusterRole (from rbac.py)
- ``RoleBinding``    — workspace-scoped: RoleBinding or ClusterRoleBinding (from rbac.py)
- ``ApiPermission``  — global hub: one per exploded (apiGroup, resource, verb) rule (from rbac.py)
- ``NodePool``       — workspace-scoped: one per (pool_key, pool_value) pair (from topology.py)
- ``TopologyKey``    — workspace-scoped: one per topologyKey (from topology.py)

Produced edges (structural slice)
----------------------------------
- ``SELECTS``   — Service → workload: matched via pod-template label selectors
- ``ROUTES_TO`` — Ingress → Service: from spec.rules[].http.paths[].backend.service.name
- ``MOUNTS``    — workload → ConfigMap or Secret: from volumes[] and envFrom[]
- ``USES_SA``   — workload → ServiceAccount: from spec.template.spec.serviceAccountName

Produced edges (network reachability slice)
--------------------------------------------
- ``CAN_REACH`` — workload → workload: from NetworkPolicy ingress allow rules
- ``CAN_REACH`` — workload → ExternalEndpoint: from CiliumNetworkPolicy egress rules

Produced edges (RBAC slice)
----------------------------
- ``ALLOWS``  — Role → ApiPermission: one per exploded rule
- ``GRANTS``  — RoleBinding → Role (via roleRef)
- ``SUBJECT`` — RoleBinding → ServiceAccount (for each ServiceAccount subject)

Produced edges (scheduling / topology slice)
---------------------------------------------
- ``SCHEDULES_ON``    — workload → NodePool: from nodeSelector, nodeAffinity, tolerations
- ``SPREAD_ACROSS``   — workload → TopologyKey: from topologySpreadConstraints
- ``CO_LOCATES_WITH`` — workload → workload: from podAffinity labelSelector matches
- ``ANTI_AFFINITY``   — workload → workload: from podAntiAffinity labelSelector matches
"""

from __future__ import annotations

from platform_graph.k8s.network import derive_network_edges
from platform_graph.k8s.rbac import derive_rbac_edges
from platform_graph.k8s.topology import derive_topology_edges
from platform_graph.model import Edge, K8sResource

# Kinds that have a pod template (spec.template.spec)
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resource_key(doc: dict) -> tuple[str, str, str]:
    """Return a (namespace, name, kind) tuple that uniquely identifies a resource doc.

    Cluster-scoped resources have an empty namespace string.
    """
    metadata = doc.get("metadata", {}) or {}
    return (
        metadata.get("namespace", ""),
        metadata.get("name", ""),
        doc.get("kind", ""),
    )


def parse_resources(
    docs: list[dict],
    workspace: str,
    env: str,
) -> tuple[list, list[Edge]]:
    """Parse *docs* into platform-graph nodes and structural edges.

    Parameters
    ----------
    docs:
        Resource dicts from ``render_overlay``.
    workspace:
        Workspace name (from .platform-graph.toml).
    env:
        Env tag derived from the overlay path (e.g. "prod", "staging").

    Returns
    -------
    (nodes, edges)
        All nodes (K8sResource, Role, RoleBinding, ApiPermission, ServiceAccount)
        and all edges (structural, network reachability, and RBAC) extracted from
        *docs*.
    """
    nodes: list = []
    edges: list[Edge] = []

    # Build a lookup: (namespace, name, kind) -> K8sResource node
    node_map: dict[tuple[str, str, str], K8sResource] = {}

    # Pass 1 — create one K8sResource per document
    for doc in docs:
        node = _doc_to_node(doc, workspace, env)
        if node is None:
            continue
        nodes.append(node)
        node_map[resource_key(doc)] = node

    # Pass 2 — derive structural edges
    for doc in docs:
        kind = doc.get("kind", "")
        from_key = resource_key(doc)
        from_node = node_map.get(from_key)
        if from_node is None:
            continue

        if kind == "Service":
            edges.extend(_selects_edges(doc, from_node, docs, node_map))

        elif kind == "Ingress":
            edges.extend(_routes_to_edges(doc, from_node, node_map, workspace, env))

        elif kind in _WORKLOAD_KINDS:
            edges.extend(_mounts_edges(doc, from_node, node_map, workspace, env))
            sa_edge = _uses_sa_edge(doc, from_node, node_map, workspace, env)
            if sa_edge is not None:
                edges.append(sa_edge)

    # Pass 3 — derive network reachability edges (CAN_REACH)
    edges.extend(derive_network_edges(docs, workspace, env))

    # Pass 4 — derive RBAC nodes and edges (ALLOWS, GRANTS, SUBJECT)
    rbac_nodes, rbac_edges = derive_rbac_edges(docs, workspace, env)
    nodes.extend(rbac_nodes)
    edges.extend(rbac_edges)

    # Pass 5 — derive scheduling/topology nodes and edges
    # (SCHEDULES_ON, SPREAD_ACROSS, CO_LOCATES_WITH, ANTI_AFFINITY)
    topology_nodes, topology_edges = derive_topology_edges(docs, workspace, env)
    nodes.extend(topology_nodes)
    edges.extend(topology_edges)

    return nodes, edges


# ---------------------------------------------------------------------------
# Node factory
# ---------------------------------------------------------------------------


def _doc_to_node(doc: dict, workspace: str, env: str) -> K8sResource | None:
    """Convert a single resource dict to a K8sResource node.

    Returns None for documents that lack the minimum required fields.
    """
    kind = doc.get("kind")
    metadata = doc.get("metadata", {}) or {}
    name = metadata.get("name")

    if not kind or not name:
        return None

    namespace = metadata.get("namespace", "")

    node = K8sResource(
        workspace=workspace,
        env=env,
        kind=kind,
        namespace=namespace,
        name=name,
    )
    # Append the kind as a second label for easy filtering (e.g. :K8sResource:Deployment)
    node.labels = ["K8sResource", kind]
    return node


# ---------------------------------------------------------------------------
# Edge derivation helpers
# ---------------------------------------------------------------------------


def _selects_edges(
    svc_doc: dict,
    svc_node: K8sResource,
    docs: list[dict],
    node_map: dict[tuple[str, str, str], K8sResource],
) -> list[Edge]:
    """SELECTS: Service → workload by pod-template label matching.

    The Service's ``spec.selector`` must be a non-empty subset of the
    workload's pod-template labels.  Only workloads in the same namespace are
    considered.
    """
    svc_namespace = (svc_doc.get("metadata", {}) or {}).get("namespace", "")
    selector: dict[str, str] = (
        (svc_doc.get("spec", {}) or {}).get("selector") or {}
    )
    if not selector:
        return []

    edges: list[Edge] = []
    for doc in docs:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        doc_ns = (doc.get("metadata", {}) or {}).get("namespace", "")
        if doc_ns != svc_namespace:
            continue
        pod_labels = _pod_template_labels(doc)
        # selector must be a subset of pod_labels
        if selector.items() <= pod_labels.items():
            target_node = node_map.get(resource_key(doc))
            if target_node is not None:
                edges.append(Edge(from_node=svc_node, to_node=target_node, rel_type="SELECTS"))
    return edges


def _routes_to_edges(
    ing_doc: dict,
    ing_node: K8sResource,
    node_map: dict[tuple[str, str, str], K8sResource],
    workspace: str,
    env: str,
) -> list[Edge]:
    """ROUTES_TO: Ingress → Service from spec.rules[].http.paths[].backend.service.name."""
    ing_namespace = (ing_doc.get("metadata", {}) or {}).get("namespace", "")
    edges: list[Edge] = []
    spec = ing_doc.get("spec", {}) or {}
    rules = spec.get("rules", []) or []

    seen_services: set[str] = set()
    for rule in rules:
        http = (rule or {}).get("http", {}) or {}
        paths = http.get("paths", []) or []
        for path_entry in paths:
            backend = (path_entry or {}).get("backend", {}) or {}
            # networking.k8s.io/v1 format
            svc = backend.get("service", {}) or {}
            svc_name = svc.get("name")
            if not svc_name:
                # Fallback: older extensions/v1beta1 format
                svc_name = backend.get("serviceName")
            if not svc_name or svc_name in seen_services:
                continue
            seen_services.add(svc_name)

            target_node = _get_or_create_node(
                node_map, ing_namespace, svc_name, "Service", workspace, env
            )
            edges.append(Edge(from_node=ing_node, to_node=target_node, rel_type="ROUTES_TO"))

    return edges


def _mounts_edges(
    workload_doc: dict,
    workload_node: K8sResource,
    node_map: dict[tuple[str, str, str], K8sResource],
    workspace: str,
    env: str,
) -> list[Edge]:
    """MOUNTS: workload → ConfigMap or Secret from volumes[] and envFrom[]."""
    namespace = (workload_doc.get("metadata", {}) or {}).get("namespace", "")
    edges: list[Edge] = []
    pod_spec = _pod_spec(workload_doc)
    if not pod_spec:
        return []

    seen: set[tuple[str, str]] = set()  # (kind, name) to avoid duplicate edges

    # From volumes[]
    for volume in pod_spec.get("volumes", []) or []:
        volume = volume or {}
        for volume_type, ref_kind in [("configMap", "ConfigMap"), ("secret", "Secret")]:
            ref = volume.get(volume_type)
            if not ref:
                continue
            # configMap uses "name" directly; secret uses "secretName"
            ref_name = ref.get("name") if volume_type == "configMap" else ref.get("secretName")
            if not ref_name:
                continue
            if (ref_kind, ref_name) in seen:
                continue
            seen.add((ref_kind, ref_name))
            target_node = _get_or_create_node(
                node_map, namespace, ref_name, ref_kind, workspace, env
            )
            edges.append(Edge(from_node=workload_node, to_node=target_node, rel_type="MOUNTS"))

    # From envFrom[]
    for env_from in pod_spec.get("envFrom", []) or []:
        env_from = env_from or {}
        for field_name, ref_kind in [("configMapRef", "ConfigMap"), ("secretRef", "Secret")]:
            ref = env_from.get(field_name)
            if not ref:
                continue
            ref_name = ref.get("name")
            if not ref_name:
                continue
            if (ref_kind, ref_name) in seen:
                continue
            seen.add((ref_kind, ref_name))
            target_node = _get_or_create_node(
                node_map, namespace, ref_name, ref_kind, workspace, env
            )
            edges.append(Edge(from_node=workload_node, to_node=target_node, rel_type="MOUNTS"))

    return edges


def _uses_sa_edge(
    workload_doc: dict,
    workload_node: K8sResource,
    node_map: dict[tuple[str, str, str], K8sResource],
    workspace: str,
    env: str,
) -> Edge | None:
    """USES_SA: workload → ServiceAccount from spec.template.spec.serviceAccountName."""
    namespace = (workload_doc.get("metadata", {}) or {}).get("namespace", "")
    pod_spec = _pod_spec(workload_doc)
    if not pod_spec:
        return None

    sa_name = pod_spec.get("serviceAccountName")
    if not sa_name:
        return None

    target_node = _get_or_create_node(
        node_map, namespace, sa_name, "ServiceAccount", workspace, env
    )
    return Edge(from_node=workload_node, to_node=target_node, rel_type="USES_SA")


# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------


def _pod_spec(doc: dict) -> dict | None:
    """Extract spec.template.spec from a workload document.

    Handles CronJob's extra nesting (spec.jobTemplate.spec.template.spec).
    """
    kind = doc.get("kind", "")
    spec = doc.get("spec", {}) or {}

    if kind == "CronJob":
        spec = (spec.get("jobTemplate", {}) or {}).get("spec", {}) or {}

    template = spec.get("template", {}) or {}
    pod_spec = template.get("spec", {}) or {}
    return pod_spec or None


def _pod_template_labels(doc: dict) -> dict[str, str]:
    """Return pod-template labels from a workload document."""
    kind = doc.get("kind", "")
    spec = doc.get("spec", {}) or {}

    if kind == "CronJob":
        spec = (spec.get("jobTemplate", {}) or {}).get("spec", {}) or {}

    template = spec.get("template", {}) or {}
    metadata = template.get("metadata", {}) or {}
    return metadata.get("labels", {}) or {}


def _get_or_create_node(
    node_map: dict[tuple[str, str, str], K8sResource],
    namespace: str,
    name: str,
    kind: str,
    workspace: str,
    env: str,
) -> K8sResource:
    """Look up or create a placeholder K8sResource node in *node_map*."""
    key = (namespace, name, kind)
    if key not in node_map:
        node = K8sResource(
            workspace=workspace,
            env=env,
            kind=kind,
            namespace=namespace,
            name=name,
        )
        node.labels = ["K8sResource", kind]
        node_map[key] = node
    return node_map[key]
