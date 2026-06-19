"""Tests for graphsearch.k8s.rbac — RBAC node and edge derivation.

Covers:
- Role/ClusterRole rules explode into one ApiPermission hub per (group, resource, verb)
- Wildcard verb ``*`` expands to the standard RBAC verb set
- ApiPermission nodes carry no workspace in their identity_props
- RoleBinding GRANTS edges link the binding to the Role
- RoleBinding SUBJECT edges link the binding to each ServiceAccount subject
- ClusterRoleBinding referencing a ClusterRole resolves correctly
- Full chain: Role → ApiPermission traversable via ALLOWS edges
- Integration: RBAC nodes/edges appear in parse_resources output
"""

from __future__ import annotations

from graphsearch.k8s.rbac import (
    _COMMON_VERBS,
    _expand_verbs,
    derive_rbac_edges,
)
from graphsearch.model import ApiPermission, Role, RoleBinding, ServiceAccount

WS = "my-workspace"
ENV = "prod"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _role(
    name: str,
    namespace: str = "default",
    rules: list[dict] | None = None,
    kind: str = "Role",
) -> dict:
    """Build a minimal Role or ClusterRole document."""
    doc: dict = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": name},
        "rules": rules or [],
    }
    if namespace and kind == "Role":
        doc["metadata"]["namespace"] = namespace
    return doc


def _cluster_role(name: str, rules: list[dict] | None = None) -> dict:
    return _role(name, namespace="", rules=rules, kind="ClusterRole")


def _role_binding(
    name: str,
    role_ref_kind: str,
    role_ref_name: str,
    subjects: list[dict] | None = None,
    namespace: str = "default",
    kind: str = "RoleBinding",
) -> dict:
    """Build a minimal RoleBinding or ClusterRoleBinding document."""
    doc: dict = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": role_ref_kind,
            "name": role_ref_name,
        },
        "subjects": subjects or [],
    }
    return doc


def _cluster_role_binding(
    name: str,
    role_ref_kind: str,
    role_ref_name: str,
    subjects: list[dict] | None = None,
) -> dict:
    return _role_binding(
        name,
        role_ref_kind=role_ref_kind,
        role_ref_name=role_ref_name,
        subjects=subjects,
        namespace="",
        kind="ClusterRoleBinding",
    )


def _sa_subject(name: str, namespace: str = "default") -> dict:
    return {"kind": "ServiceAccount", "name": name, "namespace": namespace}


def _rule(
    api_groups: list[str],
    resources: list[str],
    verbs: list[str],
) -> dict:
    return {"apiGroups": api_groups, "resources": resources, "verbs": verbs}


def _allows_edges(edges):
    return [e for e in edges if e.rel_type == "ALLOWS"]


def _grants_edges(edges):
    return [e for e in edges if e.rel_type == "GRANTS"]


def _subject_edges(edges):
    return [e for e in edges if e.rel_type == "SUBJECT"]


# ---------------------------------------------------------------------------
# _expand_verbs
# ---------------------------------------------------------------------------


class TestExpandVerbs:
    def test_wildcard_expands_to_common_verbs(self) -> None:
        result = _expand_verbs(["*"])
        assert set(result) == set(_COMMON_VERBS)

    def test_explicit_verbs_returned_unchanged(self) -> None:
        result = _expand_verbs(["get", "list"])
        assert result == ["get", "list"]

    def test_empty_list_returns_empty(self) -> None:
        assert _expand_verbs([]) == []

    def test_wildcard_mixed_with_explicit_includes_all(self) -> None:
        # "*" + "escalate" should include all common verbs plus "escalate"
        result = _expand_verbs(["*", "escalate"])
        assert set(_COMMON_VERBS) <= set(result)
        assert "escalate" in result

    def test_no_duplicates_in_expansion(self) -> None:
        result = _expand_verbs(["get", "list"])
        assert len(result) == len(set(result))

    def test_wildcard_expansion_no_duplicates(self) -> None:
        result = _expand_verbs(["*"])
        assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# ApiPermission node identity
# ---------------------------------------------------------------------------


class TestApiPermissionIdentity:
    def test_api_permission_has_no_workspace_in_identity(self) -> None:
        """Global hub node — workspace must NOT appear in identity_props."""
        perm = ApiPermission(api_group="", resource="secrets", verb="create")
        assert "workspace" not in perm.identity_props
        assert "env" not in perm.identity_props

    def test_api_permission_identity_is_tuple_of_group_resource_verb(self) -> None:
        perm = ApiPermission(api_group="apps", resource="deployments", verb="get")
        assert perm.identity_props == {
            "api_group": "apps",
            "resource": "deployments",
            "verb": "get",
        }

    def test_api_permission_labels_include_capability(self) -> None:
        perm = ApiPermission(api_group="", resource="secrets", verb="create")
        assert "ApiPermission" in perm.labels
        assert "Capability" in perm.labels


# ---------------------------------------------------------------------------
# Role → ApiPermission (ALLOWS edges)
# ---------------------------------------------------------------------------


class TestRoleAllowsEdges:
    def test_single_rule_explodes_into_one_permission_per_verb(self) -> None:
        """One rule with 2 verbs → 2 ApiPermission hubs and 2 ALLOWS edges."""
        docs = [
            _role(
                "read-secrets",
                rules=[_rule([""], ["secrets"], ["get", "list"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 2
        verbs = {e.to_node.verb for e in allows}
        assert verbs == {"get", "list"}

    def test_wildcard_verb_expands_to_common_set(self) -> None:
        """Wildcard ``*`` verb expands to all common verbs."""
        docs = [
            _role(
                "all-secrets",
                rules=[_rule([""], ["secrets"], ["*"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        verbs = {e.to_node.verb for e in allows}
        assert verbs == set(_COMMON_VERBS)

    def test_multiple_resources_produce_separate_permissions(self) -> None:
        """2 resources x 1 verb = 2 ApiPermission hubs."""
        docs = [
            _role(
                "read-config",
                rules=[_rule([""], ["configmaps", "secrets"], ["get"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 2
        resources = {e.to_node.resource for e in allows}
        assert resources == {"configmaps", "secrets"}

    def test_multiple_api_groups_produce_separate_permissions(self) -> None:
        """2 groups x 1 resource x 1 verb = 2 ApiPermission hubs."""
        docs = [
            _role(
                "read-pods",
                rules=[_rule(["", "apps"], ["pods"], ["get"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 2
        groups = {e.to_node.api_group for e in allows}
        assert groups == {"", "apps"}

    def test_allows_edge_from_role_to_api_permission(self) -> None:
        """ALLOWS edges go from a Role node to an ApiPermission hub node."""
        docs = [
            _role(
                "read-secrets",
                rules=[_rule([""], ["secrets"], ["get"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 1
        assert isinstance(allows[0].from_node, Role)
        assert isinstance(allows[0].to_node, ApiPermission)
        assert allows[0].from_node.name == "read-secrets"
        assert allows[0].to_node.resource == "secrets"
        assert allows[0].to_node.verb == "get"

    def test_api_permission_hub_has_no_workspace(self) -> None:
        """The ApiPermission produced as a hub must carry no workspace."""
        docs = [
            _role(
                "read-secrets",
                rules=[_rule([""], ["secrets"], ["get"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        perm = allows[0].to_node
        assert "workspace" not in perm.identity_props

    def test_cluster_role_also_produces_allows_edges(self) -> None:
        """ClusterRole is parsed identically to Role."""
        docs = [
            _cluster_role(
                "cluster-reader",
                rules=[_rule([""], ["pods"], ["get", "list"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 2

    def test_role_with_no_rules_produces_no_allows_edges(self) -> None:
        docs = [_role("empty-role", rules=[])]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        assert _allows_edges(edges) == []

    def test_multiple_rules_accumulate(self) -> None:
        """Two rules in a Role → their ApiPermission hubs are summed."""
        docs = [
            _role(
                "multi-rule",
                rules=[
                    _rule([""], ["secrets"], ["get"]),
                    _rule(["apps"], ["deployments"], ["list", "watch"]),
                ],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 3  # 1 + 2

    def test_wildcard_resource_creates_single_star_permission(self) -> None:
        """Wildcard resource ``*`` creates ApiPermission(resource='*')."""
        docs = [
            _role(
                "all-resources",
                rules=[_rule([""], ["*"], ["get"])],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        allows = _allows_edges(edges)
        assert len(allows) == 1
        assert allows[0].to_node.resource == "*"


# ---------------------------------------------------------------------------
# RoleBinding → Role (GRANTS edges)
# ---------------------------------------------------------------------------


class TestRoleBindingGrantsEdge:
    def test_role_binding_grants_referenced_role(self) -> None:
        """RoleBinding with roleRef matching a parsed Role → GRANTS edge."""
        docs = [
            _role("read-secrets", rules=[_rule([""], ["secrets"], ["get"])]),
            _role_binding(
                "read-secrets-binding",
                role_ref_kind="Role",
                role_ref_name="read-secrets",
                subjects=[_sa_subject("my-sa")],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        grants = _grants_edges(edges)
        assert len(grants) == 1
        assert isinstance(grants[0].from_node, RoleBinding)
        assert isinstance(grants[0].to_node, Role)
        assert grants[0].from_node.name == "read-secrets-binding"
        assert grants[0].to_node.name == "read-secrets"

    def test_cluster_role_binding_grants_cluster_role(self) -> None:
        """ClusterRoleBinding → ClusterRole resolved via role_index."""
        docs = [
            _cluster_role(
                "cluster-reader",
                rules=[_rule([""], ["pods"], ["get"])],
            ),
            _cluster_role_binding(
                "cluster-reader-binding",
                role_ref_kind="ClusterRole",
                role_ref_name="cluster-reader",
                subjects=[_sa_subject("my-sa", "default")],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        grants = _grants_edges(edges)
        assert len(grants) == 1
        assert grants[0].to_node.name == "cluster-reader"
        assert grants[0].to_node.kind == "ClusterRole"

    def test_no_grants_edge_when_role_ref_not_found(self) -> None:
        """If the roleRef doesn't match any parsed Role, no GRANTS edge is produced."""
        docs = [
            _role_binding(
                "orphan-binding",
                role_ref_kind="Role",
                role_ref_name="nonexistent-role",
                subjects=[_sa_subject("my-sa")],
            )
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        assert _grants_edges(edges) == []


# ---------------------------------------------------------------------------
# RoleBinding → ServiceAccount (SUBJECT edges)
# ---------------------------------------------------------------------------


class TestRoleBindingSubjectEdges:
    def test_service_account_subject_produces_subject_edge(self) -> None:
        """Each ServiceAccount subject → one SUBJECT edge."""
        docs = [
            _role("read-secrets", rules=[_rule([""], ["secrets"], ["get"])]),
            _role_binding(
                "read-secrets-binding",
                role_ref_kind="Role",
                role_ref_name="read-secrets",
                subjects=[_sa_subject("my-sa")],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        subjects = _subject_edges(edges)
        assert len(subjects) == 1
        assert isinstance(subjects[0].from_node, RoleBinding)
        assert isinstance(subjects[0].to_node, ServiceAccount)
        assert subjects[0].to_node.name == "my-sa"

    def test_multiple_sa_subjects_produce_multiple_edges(self) -> None:
        docs = [
            _role("reader", rules=[_rule([""], ["pods"], ["get"])]),
            _role_binding(
                "multi-subject-binding",
                role_ref_kind="Role",
                role_ref_name="reader",
                subjects=[
                    _sa_subject("sa-one"),
                    _sa_subject("sa-two"),
                ],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        subjects = _subject_edges(edges)
        assert len(subjects) == 2
        sa_names = {e.to_node.name for e in subjects}
        assert sa_names == {"sa-one", "sa-two"}

    def test_non_service_account_subjects_are_skipped(self) -> None:
        """User and Group subjects are not modelled — only ServiceAccount."""
        docs = [
            _role("reader", rules=[_rule([""], ["pods"], ["get"])]),
            _role_binding(
                "user-binding",
                role_ref_kind="Role",
                role_ref_name="reader",
                subjects=[
                    {"kind": "User", "name": "alice"},
                    {"kind": "Group", "name": "devs"},
                    _sa_subject("my-sa"),
                ],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        subjects = _subject_edges(edges)
        assert len(subjects) == 1
        assert subjects[0].to_node.name == "my-sa"

    def test_empty_subjects_list_produces_no_subject_edges(self) -> None:
        docs = [
            _role("reader", rules=[_rule([""], ["pods"], ["get"])]),
            _role_binding(
                "no-subjects",
                role_ref_kind="Role",
                role_ref_name="reader",
                subjects=[],
            ),
        ]
        _nodes, edges = derive_rbac_edges(docs, WS, ENV)
        assert _subject_edges(edges) == []

    def test_service_account_node_carries_workspace(self) -> None:
        """ServiceAccount stubs created from subjects are workspace-scoped."""
        docs = [
            _role("reader", rules=[_rule([""], ["pods"], ["get"])]),
            _role_binding(
                "reader-binding",
                role_ref_kind="Role",
                role_ref_name="reader",
                subjects=[_sa_subject("my-sa")],
            ),
        ]
        nodes, _edges = derive_rbac_edges(docs, WS, ENV)
        sa_nodes = [n for n in nodes if isinstance(n, ServiceAccount)]
        assert len(sa_nodes) == 1
        assert sa_nodes[0].workspace == WS
        assert sa_nodes[0].env == ENV


# ---------------------------------------------------------------------------
# Full chain: Workload → SA → RoleBinding → Role → ApiPermission
# ---------------------------------------------------------------------------


class TestFullBindingChain:
    def test_full_chain_nodes_and_edges_are_present(self) -> None:
        """All components of the binding chain are produced together."""
        docs = [
            _role(
                "secrets-reader",
                rules=[_rule([""], ["secrets"], ["get", "list"])],
            ),
            _role_binding(
                "secrets-reader-binding",
                role_ref_kind="Role",
                role_ref_name="secrets-reader",
                subjects=[_sa_subject("app-sa")],
            ),
        ]
        nodes, edges = derive_rbac_edges(docs, WS, ENV)

        role_nodes = [n for n in nodes if isinstance(n, Role)]
        binding_nodes = [n for n in nodes if isinstance(n, RoleBinding)]
        perm_nodes = [n for n in nodes if isinstance(n, ApiPermission)]
        sa_nodes = [n for n in nodes if isinstance(n, ServiceAccount)]

        # Structure
        assert len(role_nodes) == 1
        assert len(binding_nodes) == 1
        assert len(perm_nodes) == 2  # get + list
        assert len(sa_nodes) == 1

        # Edges
        allows = _allows_edges(edges)
        grants = _grants_edges(edges)
        subjects = _subject_edges(edges)

        assert len(allows) == 2
        assert len(grants) == 1
        assert len(subjects) == 1

        # Chain integrity
        assert grants[0].from_node.name == "secrets-reader-binding"
        assert grants[0].to_node.name == "secrets-reader"
        assert subjects[0].from_node.name == "secrets-reader-binding"
        assert subjects[0].to_node.name == "app-sa"
        assert all(e.from_node.name == "secrets-reader" for e in allows)

    def test_non_rbac_resources_are_ignored(self) -> None:
        """Pods, Services, etc. are silently skipped."""
        docs = [
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "p", "namespace": "default"}},
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "svc", "namespace": "default"},
            },
        ]
        nodes, edges = derive_rbac_edges(docs, WS, ENV)
        assert nodes == []
        assert edges == []


# ---------------------------------------------------------------------------
# Role node identity (workspace-scoped)
# ---------------------------------------------------------------------------


class TestRoleNodeIdentity:
    def test_role_identity_includes_workspace(self) -> None:
        docs = [_role("my-role", rules=[])]
        nodes, _ = derive_rbac_edges(docs, WS, ENV)
        role_nodes = [n for n in nodes if isinstance(n, Role)]
        assert role_nodes[0].identity_props["workspace"] == WS

    def test_role_identity_includes_kind_namespace_name(self) -> None:
        docs = [_role("my-role", namespace="mynamespace", rules=[])]
        nodes, _ = derive_rbac_edges(docs, WS, ENV)
        role_nodes = [n for n in nodes if isinstance(n, Role)]
        props = role_nodes[0].identity_props
        assert props["kind"] == "Role"
        assert props["namespace"] == "mynamespace"
        assert props["name"] == "my-role"

    def test_role_binding_identity_includes_workspace(self) -> None:
        docs = [
            _role("r", rules=[]),
            _role_binding("rb", "Role", "r"),
        ]
        nodes, _ = derive_rbac_edges(docs, WS, ENV)
        rb_nodes = [n for n in nodes if isinstance(n, RoleBinding)]
        assert rb_nodes[0].identity_props["workspace"] == WS


# ---------------------------------------------------------------------------
# Integration: derive_rbac_edges called via parse_resources
# ---------------------------------------------------------------------------


class TestIntegrationWithParseResources:
    def test_parse_resources_includes_rbac_nodes_and_edges(self) -> None:
        """RBAC nodes and edges appear in the output of parse_resources."""
        from graphsearch.k8s.parse import parse_resources

        docs = [
            _role(
                "secrets-reader",
                rules=[_rule([""], ["secrets"], ["get"])],
            ),
            _role_binding(
                "secrets-reader-binding",
                role_ref_kind="Role",
                role_ref_name="secrets-reader",
                subjects=[_sa_subject("app-sa")],
            ),
        ]
        nodes, edges = parse_resources(docs, WS, ENV)

        role_nodes = [n for n in nodes if isinstance(n, Role)]
        perm_nodes = [n for n in nodes if isinstance(n, ApiPermission)]
        allows = [e for e in edges if e.rel_type == "ALLOWS"]
        grants = [e for e in edges if e.rel_type == "GRANTS"]
        subjects = [e for e in edges if e.rel_type == "SUBJECT"]

        assert len(role_nodes) == 1
        assert len(perm_nodes) == 1
        assert len(allows) == 1
        assert len(grants) == 1
        assert len(subjects) == 1

    def test_parse_resources_rbac_and_structural_edges_coexist(self) -> None:
        """RBAC edges and structural K8s edges are both present in parse output."""
        from graphsearch.k8s.parse import parse_resources

        docs = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "app", "namespace": "default"},
                "spec": {
                    "template": {
                        "metadata": {"labels": {"app": "myapp"}},
                        "spec": {"serviceAccountName": "app-sa", "containers": []},
                    }
                },
            },
            _role("reader", rules=[_rule([""], ["pods"], ["get"])]),
            _role_binding(
                "reader-binding",
                role_ref_kind="Role",
                role_ref_name="reader",
                subjects=[_sa_subject("app-sa")],
            ),
        ]
        _nodes, edges = parse_resources(docs, WS, ENV)

        rel_types = {e.rel_type for e in edges}
        assert "USES_SA" in rel_types
        assert "ALLOWS" in rel_types
        assert "GRANTS" in rel_types
        assert "SUBJECT" in rel_types
