"""Terraform indexing orchestration — parse HCL and upsert into Memgraph.

Entry point: :func:`index_terraform`.
"""

from __future__ import annotations

from pathlib import Path

from neo4j import Session

from platform_graph.config import PlatformGraphConfig
from platform_graph.db import upsert_edge, upsert_node
from platform_graph.terraform.parse import ParseResult, parse_root


def index_terraform(config: PlatformGraphConfig, session: Session, repo_root: Path) -> None:
    """Index all Terraform roots declared in *config* into Memgraph.

    For each root in ``config.terraform_roots``:

    1. Walk all ``.tf`` files in the root directory (recursively).
    2. Parse HCL statically — no ``terraform init`` or plan required.
    3. Extract :class:`~platform_graph.model.TerraformModule` and
       :class:`~platform_graph.model.Resource` nodes plus ``CALLS``,
       ``DECLARES``, and ``DEPENDS_ON`` edges.
    4. Upsert every node and edge idempotently via the Bolt session.

    Args:
        config:     Loaded ``.platform-graph.toml`` configuration.
        session:    Open neo4j/Memgraph Bolt session.
        repo_root:  Absolute path to the repository root (used to resolve
                    terraform_roots relative paths and compute node
                    ``module_path`` values).
    """
    for root_rel in config.terraform_roots:
        root_dir = (repo_root / root_rel).resolve()
        if not root_dir.is_dir():
            print(f"platform-graph index: terraform root not found, skipping: {root_rel}")
            continue

        print(f"platform-graph index: parsing terraform root {root_rel!r} ...")
        result: ParseResult = parse_root(root_dir, config.workspace, repo_root)

        # Upsert all TerraformModule nodes
        for module_node in result.modules:
            upsert_node(session, module_node)

        # Upsert all Resource nodes
        for resource_node in result.resources:
            upsert_node(session, resource_node)

        # Upsert all edges (CALLS, DECLARES, DEPENDS_ON)
        for edge in result.edges:
            upsert_edge(session, edge)

        print(
            f"platform-graph index: {root_rel!r} — "
            f"{len(result.modules)} module(s), "
            f"{len(result.resources)} resource(s), "
            f"{len(result.edges)} edge(s) upserted."
        )
