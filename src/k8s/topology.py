"""Parse scheduling and topology constraints into NodePool, TopologyKey, and workload edges.

Produced nodes
--------------
- ``NodePool``    — workspace-scoped: one per (pool_key, pool_value) pair observed in
  nodeSelector or toleration key=value entries.
- ``TopologyKey`` — workspace-scoped: one per topologyKey in topologySpreadConstraints.

Produced edges
--------------
- ``SCHEDULES_ON``   — workload → NodePool: from nodeSelector key=value pairs,
  nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution matchExpressions (In),
  and toleration key=value pairs.
- ``SPREAD_ACROSS``  — workload → TopologyKey: from topologySpreadConstraints[].topologyKey.
- ``CO_LOCATES_WITH`` — workload → workload: from podAffinity labelSelector matches.
- ``ANTI_AFFINITY``  — workload → workload: from podAntiAffinity labelSelector matches.

Design notes
------------
- Only the ``In`` operator is handled for nodeAffinity matchExpressions — each value
  in the ``values`` list produces a separate SCHEDULES_ON edge (pool_key=key, pool_value=value).
- Pod (anti)affinity is resolved by matching labelSelector against the already-parsed
  K8sResource workload index for the same workspace+env.  An empty labelSelector
  (match-all) is conservatively ignored to avoid accidental all-to-all edges.
- Toleration key=value pairs use the ``key`` and ``value`` fields; entries without a
  value (e.g. ``operator: Exists``) are skipped (no deterministic pool value).
"""

from __future__ import annotations

from graphsearch.model import Edge, K8sResource, NodePool, TopologyKey

# Kinds that have a pod template (spec.template.spec)
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_topology_edges(
    resources: list[dict],
    workspace: str,
    env: str,
) -> tuple[list[NodePool | TopologyKey], list[Edge]]:
    """Parse scheduling/topology constraints into nodes and edges.

    Parameters
    ----------
    resources:
        Raw resource dicts (already rendered from a Kustomize overlay).
    workspace:
        Workspace name (from .graphsearch.toml).
    env:
        Env tag derived from the overlay path (e.g. "prod", "staging").

    Returns
    -------
    (nodes, edges)
        All NodePool and TopologyKey nodes, plus SCHEDULES_ON, SPREAD_ACROSS,
        CO_LOCATES_WITH, and ANTI_AFFINITY edges.
    """
    nodes: list[NodePool | TopologyKey] = []
    edges: list[Edge] = []

    # Build workload index: (namespace, pod_template_labels, K8sResource) for
    # pod (anti)affinity resolution.
    workload_index = _build_workload_index(resources, workspace, env)

    for doc in resources:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue

        metadata = doc.get("metadata", {}) or {}
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")
        if not name:
            continue

        workload_node = K8sResource(
            workspace=workspace,
            env=env,
            kind=doc["kind"],
            namespace=namespace,
            name=name,
        )
        workload_node.labels = ["K8sResource", doc["kind"]]

        pod_spec = _pod_spec(doc)
        if not pod_spec:
            continue

        # nodeSelector → SCHEDULES_ON
        new_nodes, new_edges = _node_selector_edges(pod_spec, workload_node, workspace, env)
        nodes.extend(new_nodes)
        edges.extend(new_edges)

        # nodeAffinity → SCHEDULES_ON
        new_nodes, new_edges = _node_affinity_edges(pod_spec, workload_node, workspace, env)
        nodes.extend(new_nodes)
        edges.extend(new_edges)

        # tolerations → SCHEDULES_ON
        new_nodes, new_edges = _toleration_edges(pod_spec, workload_node, workspace, env)
        nodes.extend(new_nodes)
        edges.extend(new_edges)

        # topologySpreadConstraints → SPREAD_ACROSS
        new_nodes, new_edges = _topology_spread_edges(pod_spec, workload_node, workspace, env)
        nodes.extend(new_nodes)
        edges.extend(new_edges)

        # podAffinity → CO_LOCATES_WITH
        new_edges = _pod_affinity_edges(
            pod_spec, workload_node, namespace, workload_index, "CO_LOCATES_WITH"
        )
        edges.extend(new_edges)

        # podAntiAffinity → ANTI_AFFINITY
        new_edges = _pod_affinity_edges(
            pod_spec, workload_node, namespace, workload_index, "ANTI_AFFINITY"
        )
        edges.extend(new_edges)

    return nodes, edges


# ---------------------------------------------------------------------------
# nodeSelector
# ---------------------------------------------------------------------------


def _node_selector_edges(
    pod_spec: dict,
    workload_node: K8sResource,
    workspace: str,
    env: str,
) -> tuple[list[NodePool], list[Edge]]:
    """SCHEDULES_ON edges from spec.nodeSelector key=value pairs."""
    node_selector: dict[str, str] = pod_spec.get("nodeSelector", {}) or {}
    nodes: list[NodePool] = []
    edges: list[Edge] = []

    for key, value in node_selector.items():
        pool = NodePool(workspace=workspace, env=env, pool_key=key, pool_value=str(value))
        nodes.append(pool)
        edges.append(Edge(from_node=workload_node, to_node=pool, rel_type="SCHEDULES_ON"))

    return nodes, edges


# ---------------------------------------------------------------------------
# nodeAffinity
# ---------------------------------------------------------------------------


def _node_affinity_edges(
    pod_spec: dict,
    workload_node: K8sResource,
    workspace: str,
    env: str,
) -> tuple[list[NodePool], list[Edge]]:
    """SCHEDULES_ON edges from nodeAffinity requiredDuringScheduling matchExpressions.

    Only the ``In`` operator is handled; each value in the ``values`` list
    produces one SCHEDULES_ON edge (pool_key=key, pool_value=value).
    """
    affinity = pod_spec.get("affinity", {}) or {}
    node_affinity = affinity.get("nodeAffinity", {}) or {}
    required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {}) or {}
    selector_terms = required.get("nodeSelectorTerms", []) or []

    nodes: list[NodePool] = []
    edges: list[Edge] = []

    for term in selector_terms:
        term = term or {}
        match_expressions = term.get("matchExpressions", []) or []
        for expr in match_expressions:
            expr = expr or {}
            if expr.get("operator") != "In":
                continue
            key = expr.get("key", "")
            if not key:
                continue
            for value in expr.get("values", []) or []:
                if not value:
                    continue
                pool = NodePool(workspace=workspace, env=env, pool_key=key, pool_value=str(value))
                nodes.append(pool)
                edges.append(Edge(from_node=workload_node, to_node=pool, rel_type="SCHEDULES_ON"))

    return nodes, edges


# ---------------------------------------------------------------------------
# Tolerations
# ---------------------------------------------------------------------------


def _toleration_edges(
    pod_spec: dict,
    workload_node: K8sResource,
    workspace: str,
    env: str,
) -> tuple[list[NodePool], list[Edge]]:
    """SCHEDULES_ON edges from tolerations key=value pairs.

    Only entries that have both ``key`` and ``value`` fields are modelled.
    Entries with ``operator: Exists`` (no value) are skipped.
    """
    tolerations: list[dict] = pod_spec.get("tolerations", []) or []
    nodes: list[NodePool] = []
    edges: list[Edge] = []

    for toleration in tolerations:
        toleration = toleration or {}
        key = toleration.get("key", "")
        value = toleration.get("value", "")
        if not key or not value:
            continue
        pool = NodePool(workspace=workspace, env=env, pool_key=key, pool_value=str(value))
        nodes.append(pool)
        edges.append(Edge(from_node=workload_node, to_node=pool, rel_type="SCHEDULES_ON"))

    return nodes, edges


# ---------------------------------------------------------------------------
# topologySpreadConstraints
# ---------------------------------------------------------------------------


def _topology_spread_edges(
    pod_spec: dict,
    workload_node: K8sResource,
    workspace: str,
    env: str,
) -> tuple[list[TopologyKey], list[Edge]]:
    """SPREAD_ACROSS edges from topologySpreadConstraints[].topologyKey."""
    constraints: list[dict] = pod_spec.get("topologySpreadConstraints", []) or []
    nodes: list[TopologyKey] = []
    edges: list[Edge] = []

    seen_keys: set[str] = set()
    for constraint in constraints:
        constraint = constraint or {}
        topology_key = constraint.get("topologyKey", "")
        if not topology_key:
            continue
        # Deduplicate: one TopologyKey node per unique key per workload
        if topology_key in seen_keys:
            # Still create an edge if there are multiple constraints with the same key,
            # but only one node — reuse implies the same node.
            tk = TopologyKey(workspace=workspace, env=env, topology_key=topology_key)
            edges.append(Edge(from_node=workload_node, to_node=tk, rel_type="SPREAD_ACROSS"))
            continue
        seen_keys.add(topology_key)
        tk = TopologyKey(workspace=workspace, env=env, topology_key=topology_key)
        nodes.append(tk)
        edges.append(Edge(from_node=workload_node, to_node=tk, rel_type="SPREAD_ACROSS"))

    return nodes, edges


# ---------------------------------------------------------------------------
# Pod (anti)affinity
# ---------------------------------------------------------------------------


def _pod_affinity_edges(
    pod_spec: dict,
    workload_node: K8sResource,
    namespace: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
    rel_type: str,
) -> list[Edge]:
    """CO_LOCATES_WITH or ANTI_AFFINITY edges from pod(Anti)Affinity labelSelector.

    *rel_type* must be either ``"CO_LOCATES_WITH"`` (podAffinity) or
    ``"ANTI_AFFINITY"`` (podAntiAffinity).

    Both requiredDuringScheduling and preferredDuringScheduling terms are
    processed.  A labelSelector that is absent or has empty matchLabels is
    conservatively skipped (to avoid accidental all-to-all edges).
    """
    affinity = pod_spec.get("affinity", {}) or {}

    if rel_type == "CO_LOCATES_WITH":
        affinity_section = affinity.get("podAffinity", {}) or {}
    else:
        affinity_section = affinity.get("podAntiAffinity", {}) or {}

    if not affinity_section:
        return []

    edges: list[Edge] = []
    seen_pairs: set[tuple[str, str]] = set()

    # requiredDuringSchedulingIgnoredDuringExecution → list of PodAffinityTerm
    required_terms = (
        affinity_section.get("requiredDuringSchedulingIgnoredDuringExecution", []) or []
    )
    for term in required_terms:
        term = term or {}
        label_selector = term.get("labelSelector", {}) or {}
        _collect_affinity_edges(
            label_selector,
            workload_node,
            namespace,
            workload_index,
            rel_type,
            seen_pairs,
            edges,
        )

    # preferredDuringSchedulingIgnoredDuringExecution → list of WeightedPodAffinityTerm
    preferred_terms = (
        affinity_section.get("preferredDuringSchedulingIgnoredDuringExecution", []) or []
    )
    for weighted in preferred_terms:
        weighted = weighted or {}
        pod_affinity_term = weighted.get("podAffinityTerm", {}) or {}
        label_selector = pod_affinity_term.get("labelSelector", {}) or {}
        _collect_affinity_edges(
            label_selector,
            workload_node,
            namespace,
            workload_index,
            rel_type,
            seen_pairs,
            edges,
        )

    return edges


def _collect_affinity_edges(
    label_selector: dict,
    workload_node: K8sResource,
    namespace: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
    rel_type: str,
    seen_pairs: set[tuple[str, str]],
    edges: list[Edge],
) -> None:
    """Resolve *label_selector* against *workload_index* and append matched edges."""
    match_labels: dict[str, str] = label_selector.get("matchLabels", {}) or {}
    if not match_labels:
        # Also check matchExpressions for In operators
        match_expressions = label_selector.get("matchExpressions", []) or []
        if not match_expressions:
            return
        # Build a synthetic match_labels from In expressions for conservative matching
        for expr in match_expressions:
            expr = expr or {}
            if expr.get("operator") != "In":
                continue
            key = expr.get("key", "")
            values = expr.get("values", []) or []
            for value in values:
                if not key or not value:
                    continue
                # Attempt match with this single key-value pair
                for wl_ns, wl_labels, wl_node in workload_index:
                    if wl_ns != namespace:
                        continue
                    if wl_node.identity_props == workload_node.identity_props:
                        continue
                    if wl_labels.get(key) == value:
                        pair = (workload_node.name, wl_node.name)
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            edges.append(
                                Edge(from_node=workload_node, to_node=wl_node, rel_type=rel_type)
                            )
        return

    # Match against workloads whose pod-template labels are a superset of match_labels
    for wl_ns, wl_labels, wl_node in workload_index:
        if wl_ns != namespace:
            continue
        if wl_node.identity_props == workload_node.identity_props:
            continue
        if match_labels.items() <= wl_labels.items():
            pair = (workload_node.name, wl_node.name)
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                edges.append(Edge(from_node=workload_node, to_node=wl_node, rel_type=rel_type))


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


def _build_workload_index(
    resources: list[dict],
    workspace: str,
    env: str,
) -> list[tuple[str, dict[str, str], K8sResource]]:
    """Return (namespace, pod_template_labels, K8sResource) for all workload docs."""
    index: list[tuple[str, dict[str, str], K8sResource]] = []
    for doc in resources:
        if doc.get("kind") not in _WORKLOAD_KINDS:
            continue
        metadata = doc.get("metadata", {}) or {}
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")
        if not name:
            continue
        labels = _pod_template_labels(doc)
        node = K8sResource(
            workspace=workspace,
            env=env,
            kind=doc["kind"],
            namespace=namespace,
            name=name,
        )
        node.labels = ["K8sResource", doc["kind"]]
        index.append((namespace, labels, node))
    return index
