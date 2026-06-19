"""Bolt connection and idempotent MERGE upsert helpers for Memgraph.

Uses the official ``neo4j`` Python driver (neo4j>=5), which speaks Bolt and
is compatible with Memgraph's Bolt endpoint.

All upserts are idempotent: calling upsert_node or upsert_edge multiple times
with the same identity produces exactly one node / one relationship.
"""

from __future__ import annotations

from neo4j import Driver, GraphDatabase, Session

from platform_graph.model import Edge, GlobalHubNode, WorkspaceScopedNode

# Type alias for nodes accepted by upsert_node
Node = WorkspaceScopedNode | GlobalHubNode


def connect(bolt_uri: str) -> Driver:
    """Return an authenticated Driver for *bolt_uri*.

    Memgraph runs without auth by default; the driver is configured with no
    credentials.  Call ``driver.close()`` when done, or use as a context
    manager.
    """
    return GraphDatabase.driver(bolt_uri, auth=None)


def _inline_props(alias: str, props: dict) -> tuple[str, dict]:
    """Return a Cypher property pattern and its parameter dict.

    Memgraph does not support map-parameter matching (``MERGE (n:L $map)``),
    so each key must appear inline.  We namespace each param with the node
    alias to avoid collisions when multiple nodes share a key name.

    Example
    -------
    _inline_props("a", {"workspace": "x", "kind": "Deploy"})
    → ("{workspace: $a_workspace, kind: $a_kind}",
       {"a_workspace": "x", "a_kind": "Deploy"})
    """
    parts: list[str] = []
    params: dict = {}
    for key, value in props.items():
        param = f"{alias}_{key}"
        parts.append(f"{key}: ${param}")
        params[param] = value
    return "{" + ", ".join(parts) + "}", params


def upsert_node(session: Session, node: Node) -> None:
    """Idempotently MERGE *node* into Memgraph and SET all its properties.

    Uses a single Cypher statement of the form::

        MERGE (n:Label {<identity_props>})
        SET n += {<identity_props + extra_props>}

    Calling this twice with the same node yields exactly one graph node.
    """
    if not node.labels:
        raise ValueError(f"Node {node!r} has no labels — cannot upsert.")

    label_str = ":".join(node.labels)
    all_props = {**node.identity_props, **node.extra_props}
    pattern, params = _inline_props("n", node.identity_props)

    cypher = f"MERGE (n:{label_str} {pattern}) SET n += $all_props"
    session.run(cypher, **params, all_props=all_props)


def upsert_edge(session: Session, edge: Edge) -> None:
    """Idempotently MERGE *edge* between its two nodes.

    Both endpoint nodes are MERGEd first (so this is safe to call without
    pre-existing nodes), then the relationship is MERGEd with its props SET.

    Example generated Cypher::

        MERGE (a:K8sResource {workspace: "x", env: "prod", ...})
        MERGE (b:ExternalEndpoint {fqdn: "api.example.com"})
        MERGE (a)-[r:CAN_REACH]->(b)
        SET r += {workspace: "x", env: "prod"}
    """
    from_node = edge.from_node
    to_node = edge.to_node

    from_label_str = ":".join(from_node.labels)
    to_label_str = ":".join(to_node.labels)
    from_pattern, from_params = _inline_props("a", from_node.identity_props)
    to_pattern, to_params = _inline_props("b", to_node.identity_props)

    cypher = (
        f"MERGE (a:{from_label_str} {from_pattern}) "
        f"MERGE (b:{to_label_str} {to_pattern}) "
        f"MERGE (a)-[r:`{edge.rel_type}`]->(b)"
    )
    session.run(cypher, **from_params, **to_params)
