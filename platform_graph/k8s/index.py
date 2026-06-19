"""Orchestrate K8s render → parse → upsert for all configured overlays.

Entry point: ``index_k8s(config, session)``

For each overlay path in ``config.k8s_overlays``:
1. Detect the render mode (helm-inflated vs raw).
2. Render the overlay with kustomize build.
3. Parse the rendered YAML into K8sResource nodes and structural edges.
4. Upsert all nodes then all edges into Memgraph via the Bolt session.

The ``env`` tag for each overlay is derived from the last component of the
overlay path (e.g. ``overlays/prod`` → ``"prod"``).
"""

from __future__ import annotations

from pathlib import Path

from neo4j import Session

from platform_graph.config import PlatformGraphConfig
from platform_graph.db import upsert_edge, upsert_node
from platform_graph.k8s.parse import parse_resources
from platform_graph.k8s.render import detect_render_mode, render_overlay

_GLOB_CHARS = frozenset("*?[")


def index_k8s(config: PlatformGraphConfig, session: Session, repo_root: Path | None = None) -> None:
    """Index all K8s overlays listed in *config* into Memgraph via *session*.

    Parameters
    ----------
    config:
        Parsed .platform-graph.toml config, including workspace name and the list
        of overlay paths.
    session:
        An open neo4j ``Session`` connected to Memgraph's Bolt endpoint.
    repo_root:
        Absolute path to the git repository root.  Relative overlay paths are
        resolved against this directory.  Defaults to ``Path.cwd()``.

    Notes
    -----
    Overlay paths may contain shell-style glob wildcards (``*``, ``?``, ``[…]``).
    Wildcard paths are expanded against *repo_root*; only directories that exist
    are kept.  Paths without wildcards are resolved to absolute paths directly.

    All upserts are idempotent: re-running ``index_k8s`` refreshes the graph
    without creating duplicates.
    """
    base = repo_root or Path.cwd()
    for raw_path in config.k8s_overlays:
        pattern = raw_path.rstrip("/")
        if _GLOB_CHARS.intersection(pattern):
            resolved = sorted(p for p in base.glob(pattern) if p.is_dir())
            if not resolved:
                print(f"  [warning] glob {raw_path!r} matched no directories under {base}")
        else:
            resolved = [base / pattern]
        for path in resolved:
            _index_overlay(str(path), config.workspace, session)


def _index_overlay(overlay_path: str, workspace: str, session: Session) -> None:
    """Render, parse, and upsert a single Kustomize overlay."""
    env = _env_from_path(overlay_path)

    print(f"  [{workspace}/{env}] Detecting render mode for {overlay_path!r}…")
    mode = detect_render_mode(overlay_path)
    print(f"  [{workspace}/{env}] Render mode: {mode}")

    print(f"  [{workspace}/{env}] Running kustomize build…")
    docs = render_overlay(overlay_path, mode)
    print(f"  [{workspace}/{env}] Rendered {len(docs)} resource(s)")

    nodes, edges = parse_resources(docs, workspace, env)
    print(f"  [{workspace}/{env}] Parsed {len(nodes)} node(s), {len(edges)} edge(s)")

    # Upsert nodes first so that edge MERGE can always find both endpoints
    for node in nodes:
        upsert_node(session, node)

    for edge in edges:
        upsert_edge(session, edge)

    print(f"  [{workspace}/{env}] Upserted {len(nodes)} node(s) and {len(edges)} edge(s)")


def _env_from_path(overlay_path: str) -> str:
    """Derive the env tag from the last component of the overlay path.

    Examples
    --------
    >>> _env_from_path("overlays/prod")
    'prod'
    >>> _env_from_path("/k8s/base")
    'base'
    >>> _env_from_path(".")
    ''
    """
    last = Path(overlay_path).name
    return last if last != "." else ""
