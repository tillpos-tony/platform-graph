"""Parse Role/ClusterRole/RoleBinding resources into ApiPermission hubs and binding chains.

Produced nodes
--------------
- ``Role``           — workspace-scoped: one per Role or ClusterRole document
- ``RoleBinding``    — workspace-scoped: one per RoleBinding or ClusterRoleBinding document
- ``ApiPermission``  — global hub: one per exploded (apiGroup, resource, verb) rule
- ``ServiceAccount`` — workspace-scoped: referenced subjects (kind: ServiceAccount)

Produced edges
--------------
- ``ALLOWS``  — Role → ApiPermission: one edge per exploded rule
- ``GRANTS``  — RoleBinding → Role (the roleRef)
- ``SUBJECT`` — RoleBinding → ServiceAccount (for each ServiceAccount subject)

Full chain enabling who-can-reach queries::

    (Workload)-[:USES_SA]->(ServiceAccount)<-[:SUBJECT]-(RoleBinding)
        -[:GRANTS]->(Role)-[:ALLOWS]->(ApiPermission)

Wildcard expansion
------------------
- Wildcard verbs (``*``) expand to the standard RBAC verb set.
- Wildcard resources (``*``) are left as a single ApiPermission with resource="*"
  since the full resource list is not known at parse time.
"""

from __future__ import annotations

from graphsearch.model import ApiPermission, Edge, Role, RoleBinding, ServiceAccount

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Standard RBAC verbs that ``*`` expands to.
_COMMON_VERBS: tuple[str, ...] = (
    "get",
    "list",
    "watch",
    "create",
    "update",
    "patch",
    "delete",
)

#: Kubernetes kinds handled as Role variants.
_ROLE_KINDS = {"Role", "ClusterRole"}

#: Kubernetes kinds handled as RoleBinding variants.
_BINDING_KINDS = {"RoleBinding", "ClusterRoleBinding"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_rbac_edges(
    resources: list[dict],
    workspace: str,
    env: str,
) -> tuple[list[Role | RoleBinding | ApiPermission | ServiceAccount], list[Edge]]:
    """Parse RBAC resources into Role/RoleBinding/ApiPermission nodes and edges.

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
        All RBAC-derived nodes (Role, RoleBinding, ApiPermission, ServiceAccount)
        and edges (ALLOWS, GRANTS, SUBJECT).
    """
    nodes: list[Role | RoleBinding | ApiPermission | ServiceAccount] = []
    edges: list[Edge] = []

    # Index Role nodes by (namespace, name, kind) for RoleBinding → Role lookup
    role_index: dict[tuple[str, str, str], Role] = {}

    # Pass 1 — build Role nodes and ALLOWS edges
    for doc in resources:
        kind = doc.get("kind", "")
        if kind not in _ROLE_KINDS:
            continue
        role_node, role_nodes, role_edges = _parse_role(doc, workspace, env)
        role_index[_role_key(doc)] = role_node
        nodes.append(role_node)
        nodes.extend(role_nodes)  # ApiPermission hubs
        edges.extend(role_edges)  # ALLOWS edges

    # Pass 2 — build RoleBinding nodes, GRANTS, and SUBJECT edges
    for doc in resources:
        kind = doc.get("kind", "")
        if kind not in _BINDING_KINDS:
            continue
        binding_node, binding_nodes, binding_edges = _parse_role_binding(
            doc, workspace, env, role_index
        )
        nodes.append(binding_node)
        nodes.extend(binding_nodes)  # ServiceAccount stubs
        edges.extend(binding_edges)  # GRANTS + SUBJECT edges

    return nodes, edges


# ---------------------------------------------------------------------------
# Role parsing
# ---------------------------------------------------------------------------


def _role_key(doc: dict) -> tuple[str, str, str]:
    """Return (namespace, name, kind) for a Role/ClusterRole doc."""
    metadata = doc.get("metadata", {}) or {}
    return (
        metadata.get("namespace", ""),
        metadata.get("name", ""),
        doc.get("kind", ""),
    )


def _parse_role(
    doc: dict,
    workspace: str,
    env: str,
) -> tuple[Role, list[ApiPermission], list[Edge]]:
    """Parse a Role or ClusterRole document.

    Returns the Role node, all ApiPermission hub nodes, and ALLOWS edges.
    """
    metadata = doc.get("metadata", {}) or {}
    kind = doc.get("kind", "")
    namespace = metadata.get("namespace", "")
    name = metadata.get("name", "")

    role_node = Role(
        workspace=workspace,
        env=env,
        kind=kind,
        namespace=namespace,
        name=name,
    )

    api_permission_nodes: list[ApiPermission] = []
    allows_edges: list[Edge] = []

    rules = doc.get("rules", []) or []
    for rule in rules:
        rule = rule or {}
        api_groups: list[str] = rule.get("apiGroups", []) or []
        resources: list[str] = rule.get("resources", []) or []
        verbs: list[str] = rule.get("verbs", []) or []

        # Expand wildcard verbs
        expanded_verbs = _expand_verbs(verbs)

        for api_group in api_groups:
            for resource in resources:
                for verb in expanded_verbs:
                    perm = ApiPermission(
                        api_group=api_group,
                        resource=resource,
                        verb=verb,
                    )
                    api_permission_nodes.append(perm)
                    allows_edges.append(Edge(from_node=role_node, to_node=perm, rel_type="ALLOWS"))

    return role_node, api_permission_nodes, allows_edges


def _expand_verbs(verbs: list[str]) -> list[str]:
    """Expand wildcard verb ``*`` to the standard RBAC verb set.

    Non-wildcard verbs are returned as-is.  If the list contains ``*`` mixed
    with explicit verbs, the union is returned (deduped, preserving order).
    """
    if not verbs:
        return []

    if "*" in verbs:
        # Include the common set plus any explicit non-wildcard verbs
        seen: set[str] = set(_COMMON_VERBS)
        result = list(_COMMON_VERBS)
        for v in verbs:
            if v != "*" and v not in seen:
                seen.add(v)
                result.append(v)
        return result

    return list(verbs)


# ---------------------------------------------------------------------------
# RoleBinding parsing
# ---------------------------------------------------------------------------


def _parse_role_binding(
    doc: dict,
    workspace: str,
    env: str,
    role_index: dict[tuple[str, str, str], Role],
) -> tuple[RoleBinding, list[ServiceAccount], list[Edge]]:
    """Parse a RoleBinding or ClusterRoleBinding document.

    Returns the RoleBinding node, any ServiceAccount stub nodes created for
    subjects, and the GRANTS + SUBJECT edges.
    """
    metadata = doc.get("metadata", {}) or {}
    kind = doc.get("kind", "")
    namespace = metadata.get("namespace", "")
    name = metadata.get("name", "")

    binding_node = RoleBinding(
        workspace=workspace,
        env=env,
        kind=kind,
        namespace=namespace,
        name=name,
    )

    sa_nodes: list[ServiceAccount] = []
    binding_edges: list[Edge] = []

    # GRANTS edge: RoleBinding → Role (via roleRef)
    role_ref = doc.get("roleRef", {}) or {}
    ref_kind = role_ref.get("kind", "")
    ref_name = role_ref.get("name", "")
    if ref_kind and ref_name:
        # ClusterRole can be referenced from both namespace-scoped and cluster-scoped bindings
        # Try namespace-scoped lookup first, then cluster-scoped (empty namespace)
        ref_namespace = namespace if ref_kind == "Role" else ""
        role_node = role_index.get((ref_namespace, ref_name, ref_kind))
        if role_node is not None:
            binding_edges.append(Edge(from_node=binding_node, to_node=role_node, rel_type="GRANTS"))

    # SUBJECT edges: RoleBinding → ServiceAccount (for ServiceAccount subjects)
    subjects = doc.get("subjects", []) or []
    for subject in subjects:
        subject = subject or {}
        if subject.get("kind") != "ServiceAccount":
            continue
        sa_name = subject.get("name", "")
        sa_namespace = subject.get("namespace", "") or namespace
        if not sa_name:
            continue

        sa_node = ServiceAccount(
            workspace=workspace,
            env=env,
            namespace=sa_namespace,
            name=sa_name,
        )
        sa_nodes.append(sa_node)
        binding_edges.append(Edge(from_node=binding_node, to_node=sa_node, rel_type="SUBJECT"))

    return binding_node, sa_nodes, binding_edges
