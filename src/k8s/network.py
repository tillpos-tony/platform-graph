"""Parse NetworkPolicy and CiliumNetworkPolicy resources into CAN_REACH edges.

Produced nodes
--------------
- ``ExternalEndpoint`` — global hub for egress targets outside the cluster
  (toFQDNs DNS name or toEntities: world).  No workspace property.

Produced edges
--------------
- ``CAN_REACH`` — workload → workload (in-cluster, from NetworkPolicy ingress allow rules)
- ``CAN_REACH`` — workload → ExternalEndpoint (egress to external, from CiliumNetworkPolicy)

Design notes
------------
- v1 explicit-allow only.  Default-deny is NOT modelled.
- NetworkPolicy ingress rules are parsed: each ``from[]`` entry's podSelector
  identifies source workloads; the policy's podSelector identifies the target.
- CiliumNetworkPolicy egress rules are parsed: toFQDNs creates an
  ExternalEndpoint keyed by FQDN; toEntities: world creates an ExternalEndpoint
  keyed by entity "world".
- CAN_REACH edge props carry ``source_namespace`` and ``policy_type`` for
  provenance queries.
"""

from __future__ import annotations

from graphsearch.model import Edge, ExternalEndpoint, K8sResource

# Kinds that have a pod template (spec.template.spec)
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_network_edges(
    resources: list[dict],
    workspace: str,
    env: str,
) -> list[Edge]:
    """Derive CAN_REACH edges from NetworkPolicy and CiliumNetworkPolicy resources.

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
    list[Edge]
        All CAN_REACH edges derived from explicit allow rules.  Default-deny
        policies are silently ignored — only explicit allows produce edges.
    """
    edges: list[Edge] = []

    # Build an index of workload nodes by (namespace, labels) for source matching
    workload_index = _build_workload_index(resources, workspace, env)

    for doc in resources:
        kind = doc.get("kind", "")
        if kind == "NetworkPolicy":
            edges.extend(_parse_network_policy(doc, workspace, env, workload_index))
        elif kind == "CiliumNetworkPolicy":
            edges.extend(_parse_cilium_network_policy(doc, workspace, env, workload_index))

    return edges


# ---------------------------------------------------------------------------
# Workload index helpers
# ---------------------------------------------------------------------------


def _build_workload_index(
    resources: list[dict],
    workspace: str,
    env: str,
) -> list[tuple[str, dict[str, str], K8sResource]]:
    """Return a list of (namespace, pod_template_labels, K8sResource) for all workloads.

    Used to resolve podSelector matches against source/target workloads.
    """
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


def _pod_template_labels(doc: dict) -> dict[str, str]:
    """Return pod-template labels from a workload document."""
    kind = doc.get("kind", "")
    spec = doc.get("spec", {}) or {}

    if kind == "CronJob":
        spec = (spec.get("jobTemplate", {}) or {}).get("spec", {}) or {}

    template = spec.get("template", {}) or {}
    metadata = template.get("metadata", {}) or {}
    return metadata.get("labels", {}) or {}


def _workloads_matching_selector(
    selector: dict[str, str],
    namespace: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
) -> list[K8sResource]:
    """Return workloads in *namespace* whose pod-template labels satisfy *selector*.

    An empty selector matches nothing (conservative — avoids accidental
    "match all" semantics from an empty podSelector on ingress from[]).
    """
    if not selector:
        return []
    matched: list[K8sResource] = []
    for wl_ns, wl_labels, wl_node in workload_index:
        if wl_ns != namespace:
            continue
        if selector.items() <= wl_labels.items():
            matched.append(wl_node)
    return matched


def _target_workloads_for_policy(
    pod_selector: dict,
    namespace: str,
    workspace: str,
    env: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
) -> list[K8sResource]:
    """Resolve the policy's podSelector to target workload nodes.

    An empty ``matchLabels`` dict (or absent podSelector) means "all pods in
    the namespace" — we match all workloads in that namespace.
    """
    match_labels: dict[str, str] = (pod_selector or {}).get("matchLabels", {}) or {}

    matched: list[K8sResource] = []
    for wl_ns, wl_labels, wl_node in workload_index:
        if wl_ns != namespace:
            continue
        # Empty matchLabels → select all pods in namespace
        if not match_labels or match_labels.items() <= wl_labels.items():
            matched.append(wl_node)
    return matched


# ---------------------------------------------------------------------------
# NetworkPolicy parsing
# ---------------------------------------------------------------------------


def _parse_network_policy(
    doc: dict,
    workspace: str,
    env: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
) -> list[Edge]:
    """Parse a NetworkPolicy document into CAN_REACH edges.

    Reads ``spec.ingress[].from[]`` rules.  Each rule's podSelector (and
    optional namespaceSelector, which we simplify to same-namespace matching
    for v1) identifies source workloads.  The policy's ``spec.podSelector``
    identifies target workloads.

    Only explicit allow-from rules produce edges.  A NetworkPolicy with no
    ingress rules (pure default-deny) produces no edges.
    """
    metadata = doc.get("metadata", {}) or {}
    namespace = metadata.get("namespace", "")
    spec = doc.get("spec", {}) or {}

    pod_selector = spec.get("podSelector", {}) or {}
    ingress_rules = spec.get("ingress", []) or []

    if not ingress_rules:
        # No ingress rules → pure default-deny or no ingress policy; skip
        return []

    target_workloads = _target_workloads_for_policy(
        pod_selector, namespace, workspace, env, workload_index
    )
    if not target_workloads:
        return []

    edges: list[Edge] = []
    for rule in ingress_rules:
        rule = rule or {}
        from_peers = rule.get("from", []) or []
        for peer in from_peers:
            peer = peer or {}
            pod_sel = (peer.get("podSelector", {}) or {}).get("matchLabels", {}) or {}
            # Determine source namespace: prefer namespaceSelector if present,
            # but for v1 explicit-allow we conservatively use the policy namespace.
            # (Cross-namespace flows require a namespaceSelector; if absent we
            # assume same namespace.)
            src_namespace = namespace

            source_workloads = _workloads_matching_selector(pod_sel, src_namespace, workload_index)
            for src_wl in source_workloads:
                for tgt_wl in target_workloads:
                    if src_wl is tgt_wl:
                        continue
                    edges.append(
                        Edge(
                            from_node=src_wl,
                            to_node=tgt_wl,
                            rel_type="CAN_REACH",
                            props={
                                "source_namespace": src_namespace,
                                "policy_type": "NetworkPolicy",
                            },
                        )
                    )
    return edges


# ---------------------------------------------------------------------------
# CiliumNetworkPolicy parsing
# ---------------------------------------------------------------------------


def _parse_cilium_network_policy(
    doc: dict,
    workspace: str,
    env: str,
    workload_index: list[tuple[str, dict[str, str], K8sResource]],
) -> list[Edge]:
    """Parse a CiliumNetworkPolicy document into CAN_REACH edges.

    Reads ``spec.egress[]`` rules:
    - ``toFQDNs: [{matchName: "example.com"}]`` → ExternalEndpoint(fqdn="example.com")
    - ``toEntities: ["world"]`` → ExternalEndpoint(entity="world")

    The policy's ``spec.endpointSelector`` (matchLabels) identifies the
    source workload(s).  Only explicit allow rules produce edges.
    """
    metadata = doc.get("metadata", {}) or {}
    namespace = metadata.get("namespace", "")
    spec = doc.get("spec", {}) or {}

    endpoint_selector = spec.get("endpointSelector", {}) or {}
    ep_match_labels: dict[str, str] = endpoint_selector.get("matchLabels", {}) or {}

    egress_rules = spec.get("egress", []) or []
    if not egress_rules:
        return []

    # Resolve source workloads from endpointSelector
    source_workloads = _workloads_matching_selector(ep_match_labels, namespace, workload_index)
    if not source_workloads:
        return []

    edges: list[Edge] = []
    for rule in egress_rules:
        rule = rule or {}

        # toFQDNs: [{matchName: "example.com"}, ...]
        to_fqdns = rule.get("toFQDNs", []) or []
        for fqdn_entry in to_fqdns:
            fqdn_entry = fqdn_entry or {}
            fqdn = fqdn_entry.get("matchName", "")
            if not fqdn:
                continue
            endpoint = ExternalEndpoint(fqdn=fqdn)
            for src_wl in source_workloads:
                edges.append(
                    Edge(
                        from_node=src_wl,
                        to_node=endpoint,
                        rel_type="CAN_REACH",
                        props={
                            "source_namespace": namespace,
                            "policy_type": "CiliumNetworkPolicy",
                        },
                    )
                )

        # toEntities: ["world", ...]
        to_entities = rule.get("toEntities", []) or []
        for entity in to_entities:
            if not entity:
                continue
            endpoint = ExternalEndpoint(entity=entity)
            for src_wl in source_workloads:
                edges.append(
                    Edge(
                        from_node=src_wl,
                        to_node=endpoint,
                        rel_type="CAN_REACH",
                        props={
                            "source_namespace": namespace,
                            "policy_type": "CiliumNetworkPolicy",
                        },
                    )
                )

    return edges
