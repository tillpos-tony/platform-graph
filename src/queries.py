"""Named canned queries for graphsearch.

Each named query is a function that accepts a neo4j Session and optional
keyword parameters, executes a Cypher statement, and returns a list of dicts.

Usage from CLI::

    graphsearch query list-workspaces
    graphsearch query blast-radius --param workspace=my-repo --param env=prod \
        --param kind=Deployment --param namespace=default --param name=api
    graphsearch query who-can-reach-capability --param resource=secrets --param verb=get
    graphsearch query network-reachability
"""

from __future__ import annotations

from typing import Any

from neo4j import Session

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Maps query name → (function, required_param_names)
REGISTRY: dict[str, tuple[Any, list[str]]] = {}


def _register(name: str, required: list[str]):
    """Decorator that registers a query function under *name*."""

    def decorator(fn):
        REGISTRY[name] = (fn, required)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Named queries
# ---------------------------------------------------------------------------


@_register("list-workspaces", required=[])
def query_list_workspaces(session: Session, **_params) -> list[dict]:
    """Return all distinct workspace values across all nodes."""
    result = session.run(
        "MATCH (n) WHERE n.workspace IS NOT NULL "
        "RETURN DISTINCT n.workspace AS workspace "
        "ORDER BY workspace"
    )
    return [dict(record) for record in result]


@_register(
    "blast-radius",
    required=["workspace", "env", "kind", "namespace", "name"],
)
def query_blast_radius(session: Session, **params) -> list[dict]:
    """Return all workloads reachable from the given workload via CAN_REACH edges.

    Required params: workspace, env, kind, namespace, name.
    """
    result = session.run(
        "MATCH p = (src:K8sResource {workspace: $workspace, env: $env, "
        "kind: $kind, namespace: $namespace, name: $name})"
        "-[:`CAN_REACH`*1..5]->(dst) "
        "RETURN DISTINCT dst.kind AS kind, dst.namespace AS namespace, "
        "dst.name AS name, dst.workspace AS workspace",
        **params,
    )
    return [dict(record) for record in result]


@_register("who-can-reach-capability", required=["resource", "verb"])
def query_who_can_reach_capability(session: Session, **params) -> list[dict]:
    """Return all workloads that hold the given ApiPermission via RBAC chain.

    Required params: resource, verb.
    """
    result = session.run(
        "MATCH (w:K8sResource)-[:`USES_SA`]->(sa:ServiceAccount)"
        "<-[:`SUBJECT`]-(rb:RoleBinding)-[:`GRANTS`]->(r:Role)"
        "-[:`ALLOWS`]->(p:ApiPermission {resource: $resource, verb: $verb}) "
        "RETURN DISTINCT w.workspace AS workspace, w.env AS env, "
        "w.namespace AS namespace, w.name AS name",
        **params,
    )
    return [dict(record) for record in result]


@_register("network-reachability", required=[])
def query_network_reachability(session: Session, **_params) -> list[dict]:
    """Return all explicit CAN_REACH edges."""
    result = session.run(
        "MATCH (src)-[e:`CAN_REACH`]->(dst) "
        "RETURN src.workspace AS src_workspace, src.name AS src_name, "
        "dst.name AS dst_name, type(dst) AS dst_type"
    )
    return [dict(record) for record in result]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_named(session: Session, name: str, params: dict[str, str]) -> list[dict]:
    """Execute the named query with *params* and return results.

    Raises KeyError if the query name is not registered.
    Raises ValueError if any required parameter is missing.
    """
    if name not in REGISTRY:
        raise KeyError(
            f"Unknown query {name!r}. Available: {', '.join(sorted(REGISTRY))}"
        )
    fn, required = REGISTRY[name]
    missing = [k for k in required if k not in params]
    if missing:
        raise ValueError(
            f"Query {name!r} requires params: {', '.join(missing)}. "
            f"Pass them with --param key=value."
        )
    return fn(session, **params)
