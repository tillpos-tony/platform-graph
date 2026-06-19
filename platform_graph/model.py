"""Node and Edge dataclasses for the platform-graph domain model.

Two node categories exist in the graph:

1. **Workspace-scoped nodes** — identity includes ``workspace`` (and ``env``
   where applicable).  Examples: K8sResource, TerraformModule, Resource, Role,
   ServiceAccount.  These are unique *per workspace* (and per env).

2. **Global hub nodes** — identity is the natural key only; no ``workspace``
   property.  Examples: ApiPermission, ExternalEndpoint, Capability.  They are
   shared across all workspaces — two repos granting ``secrets:create`` point at
   the *same* ApiPermission hub node.

The distinction drives how MERGE keys are built in ``db.py``: scoped nodes key
on (workspace, env?, …natural fields), hub nodes key on (natural fields only).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceScopedNode:
    """Base for nodes whose identity is scoped to a workspace (and env).

    Subclasses MUST set ``labels`` and populate ``_natural_key_props()`` with
    the fields that, together with ``workspace`` and ``env``, uniquely identify
    the node.
    """

    workspace: str
    env: str = ""

    # Memgraph node labels — override in every subclass.
    labels: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def identity_props(self) -> dict:
        """Properties used in the MERGE clause (the unique composite key)."""
        base = {"workspace": self.workspace}
        if self.env:
            base["env"] = self.env
        base.update(self._natural_key_props())
        return base

    @property
    def extra_props(self) -> dict:
        """Additional properties SET after MERGE (may be empty)."""
        return {}

    def _natural_key_props(self) -> dict:
        """Override to return the non-workspace key fields for this node type."""
        return {}


@dataclass
class GlobalHubNode:
    """Base for nodes shared across workspaces, identified by natural key only.

    No ``workspace`` property — the workspace context lives on connecting edges.
    Subclasses MUST set ``labels`` and implement ``_natural_key_props()``.
    """

    # Memgraph node labels — override in every subclass.
    labels: list[str] = field(default_factory=list, init=False, repr=False)

    @property
    def identity_props(self) -> dict:
        """Properties used in the MERGE clause (natural key only)."""
        return self._natural_key_props()

    @property
    def extra_props(self) -> dict:
        """Additional properties SET after MERGE (may be empty)."""
        return {}

    def _natural_key_props(self) -> dict:
        """Override to return the natural-key fields for this hub node type."""
        return {}


# ---------------------------------------------------------------------------
# Workspace-scoped node types
# ---------------------------------------------------------------------------


@dataclass
class K8sResource(WorkspaceScopedNode):
    """A Kubernetes resource rendered from a Kustomize overlay.

    Identity: (workspace, env, kind, namespace, name).
    """

    kind: str = ""
    namespace: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["K8sResource"]

    def _natural_key_props(self) -> dict:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}


@dataclass
class TerraformModule(WorkspaceScopedNode):
    """A Terraform module root within a workspace.

    Identity: (workspace, env, module_path).
    """

    module_path: str = ""

    def __post_init__(self) -> None:
        self.labels = ["TerraformModule"]

    def _natural_key_props(self) -> dict:
        return {"module_path": self.module_path}


@dataclass
class Resource(WorkspaceScopedNode):
    """A Terraform-managed infrastructure resource within a workspace.

    Identity: (workspace, resource_type, resource_name).
    Labels include the generic ``Resource`` label plus the resource_type label
    so that e.g. ``aws_s3_bucket`` resources can be queried by type.
    """

    resource_type: str = ""
    resource_name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["Resource", self.resource_type]

    def _natural_key_props(self) -> dict:
        return {"resource_type": self.resource_type, "resource_name": self.resource_name}


@dataclass
class Role(WorkspaceScopedNode):
    """A Kubernetes Role or ClusterRole within a workspace.

    Identity: (workspace, env, kind, namespace, name).
    """

    kind: str = ""  # "Role" or "ClusterRole"
    namespace: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["Role"]

    def _natural_key_props(self) -> dict:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}


@dataclass
class RoleBinding(WorkspaceScopedNode):
    """A Kubernetes RoleBinding or ClusterRoleBinding within a workspace.

    Identity: (workspace, env, kind, namespace, name).
    """

    kind: str = ""  # "RoleBinding" or "ClusterRoleBinding"
    namespace: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["RoleBinding"]

    def _natural_key_props(self) -> dict:
        return {"kind": self.kind, "namespace": self.namespace, "name": self.name}


@dataclass
class ServiceAccount(WorkspaceScopedNode):
    """A Kubernetes ServiceAccount within a workspace.

    Identity: (workspace, env, namespace, name).
    """

    namespace: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["ServiceAccount"]

    def _natural_key_props(self) -> dict:
        return {"namespace": self.namespace, "name": self.name}


# ---------------------------------------------------------------------------
# Global hub node types
# ---------------------------------------------------------------------------


@dataclass
class ApiPermission(GlobalHubNode):
    """One exploded RBAC rule: (apiGroup, resource, verb).

    K8s subtype of Capability.  Shared across all workspaces — two repos
    granting ``secrets:create`` point at the same ApiPermission hub.

    Identity: (api_group, resource, verb).
    """

    api_group: str = ""
    resource: str = ""
    verb: str = ""

    def __post_init__(self) -> None:
        self.labels = ["ApiPermission", "Capability"]

    def _natural_key_props(self) -> dict:
        return {"api_group": self.api_group, "resource": self.resource, "verb": self.verb}


@dataclass
class ExternalEndpoint(GlobalHubNode):
    """Egress target outside the cluster (toFQDNs DNS name or toEntities: world).

    Identity: (fqdn,) or (entity,) — at least one must be set.
    """

    fqdn: str = ""
    entity: str = ""

    def __post_init__(self) -> None:
        self.labels = ["ExternalEndpoint"]

    def _natural_key_props(self) -> dict:
        props: dict = {}
        if self.fqdn:
            props["fqdn"] = self.fqdn
        if self.entity:
            props["entity"] = self.entity
        return props


@dataclass
class Capability(GlobalHubNode):
    """A risk-tiered, queryable capability that code or infra can reach.

    Examples: ``bash``, ``aws-write``, ``kubectl-apply``.
    ApiPermission is a K8s subtype of Capability.

    Identity: (name,).
    """

    name: str = ""

    def __post_init__(self) -> None:
        self.labels = ["Capability"]

    def _natural_key_props(self) -> dict:
        return {"name": self.name}


# ---------------------------------------------------------------------------
# Scheduling / topology node types
# ---------------------------------------------------------------------------


@dataclass
class NodePool(WorkspaceScopedNode):
    """A node-pool selector derived from nodeSelector or tolerations key=value pairs.

    Identity: (workspace, env, pool_key, pool_value).
    """

    pool_key: str = ""
    pool_value: str = ""

    def __post_init__(self) -> None:
        self.labels = ["NodePool"]

    def _natural_key_props(self) -> dict:
        return {"pool_key": self.pool_key, "pool_value": self.pool_value}


@dataclass
class TopologyKey(WorkspaceScopedNode):
    """A topology spread key derived from topologySpreadConstraints[].topologyKey.

    Identity: (workspace, env, topology_key).
    """

    topology_key: str = ""

    def __post_init__(self) -> None:
        self.labels = ["TopologyKey"]

    def _natural_key_props(self) -> dict:
        return {"topology_key": self.topology_key}


# ---------------------------------------------------------------------------
# Edge type
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    """A directed relationship between two nodes in the graph.

    ``from_node`` and ``to_node`` may be any Node (scoped or hub).
    ``rel_type`` is the Cypher relationship type string, e.g. ``CAN_REACH``,
    ``ALLOWS``, ``USES_SA``, ``GRANTS``.
    ``props`` carries edge-level context (e.g. workspace, env on hub edges).
    """

    from_node: WorkspaceScopedNode | GlobalHubNode
    to_node: WorkspaceScopedNode | GlobalHubNode
    rel_type: str
    props: dict = field(default_factory=dict)
