"""Orchestrate K8s render → parse → upsert for all configured overlays and manifest folders.

Entry point: ``index_k8s(config, session)``

For each overlay path in ``config.k8s_overlays``:
1. Detect the render mode (helm-inflated vs raw).
2. Render the overlay with kustomize build.
3. Parse the rendered YAML into K8sResource nodes and structural edges.
4. Upsert all nodes then all edges into Memgraph via the Bolt session.

For each raw manifest path in ``config.k8s_manifests``:
1. Read all ``*.yaml``/``*.yml`` files recursively.
2. Parse and upsert using the same pipeline as overlays.

The ``env`` tag for each path is derived from the last component of the path
(e.g. ``overlays/prod`` → ``"prod"``).
"""

from __future__ import annotations

from pathlib import Path

from neo4j import Session

from platform_graph.config import PlatformGraphConfig
from platform_graph.db import upsert_edge, upsert_node
from platform_graph.k8s.manifests import read_manifests
from platform_graph.k8s.parse import parse_resources
from platform_graph.k8s.render import detect_render_mode, render_overlay

_GLOB_CHARS = frozenset("*?[")


def index_k8s(config: PlatformGraphConfig, session: Session, repo_root: Path | None = None) -> None:
    """Index all K8s overlays and raw manifest folders into Memgraph via *session*.

    Parameters
    ----------
    config:
        Parsed .platform-graph.toml config, including workspace name, overlay paths,
        and raw manifest paths.
    session:
        An open neo4j ``Session`` connected to Memgraph's Bolt endpoint.
    repo_root:
        Absolute path to the git repository root.  Relative paths are resolved
        against this directory.  Defaults to ``Path.cwd()``.

    Notes
    -----
    Both ``k8s_overlays`` and ``k8s_manifests`` paths may contain shell-style
    glob wildcards (``*``, ``?``, ``[…]``).  Wildcard paths are expanded against
    *repo_root*; only directories that exist are kept.  A "matched no directories"
    warning is printed if a glob expands to nothing.

    All upserts are idempotent: re-running ``index_k8s`` refreshes the graph
    without creating duplicates.
    """
    base = repo_root or Path.cwd()

    # --- overlays (kustomize build) ---
    for path in _resolve_paths(config.k8s_overlays, base):
        _index_overlay(str(path), config.workspace, session)

    # --- raw manifest folders ---
    for path in _resolve_paths(config.k8s_manifests, base):
        _index_manifest_dir(str(path), config.workspace, session)


def _resolve_paths(raw_paths: list[str], base: Path) -> list[Path]:
    """Expand a list of possibly-glob path strings to concrete directory paths.

    Entries with glob characters (``*``, ``?``, ``[``) are expanded against
    *base*; only matching directories are kept.  A warning is printed when a
    glob matches nothing.  Entries without glob characters are returned as-is
    (resolved against *base*) without existence checks — the caller's indexing
    logic will surface any missing-directory errors naturally.

    Returns a flat list of ``Path`` objects in sorted order per glob entry.
    """
    result: list[Path] = []
    for raw_path in raw_paths:
        pattern = raw_path.rstrip("/")
        if _GLOB_CHARS.intersection(pattern):
            resolved = sorted(p for p in base.glob(pattern) if p.is_dir())
            if not resolved:
                print(f"  [warning] glob {raw_path!r} matched no directories under {base}")
            result.extend(resolved)
        else:
            result.append(base / pattern)
    return result


def _index_docs(
    docs: list[dict],
    workspace: str,
    env: str,
    session: Session,
    source_label: str,
) -> None:
    """Parse *docs* and upsert the resulting nodes and edges into Memgraph.

    This is the shared upsert core used by both overlay and manifest indexing.

    Parameters
    ----------
    docs:
        List of parsed K8s resource dicts (same shape from both render and read).
    workspace:
        Workspace name tag.
    env:
        Environment name tag.
    session:
        Open Bolt session.
    source_label:
        Human-readable label for log messages (e.g. ``"overlay"`` or ``"manifest"``).
    """
    nodes, edges = parse_resources(docs, workspace, env)
    print(
        f"  [{workspace}/{env}] Parsed {len(nodes)} node(s), {len(edges)} edge(s) from {source_label}"
    )

    # Upsert nodes first so that edge MERGE can always find both endpoints.
    for node in nodes:
        upsert_node(session, node)

    for edge in edges:
        upsert_edge(session, edge)

    print(f"  [{workspace}/{env}] Upserted {len(nodes)} node(s) and {len(edges)} edge(s)")


def _index_overlay(overlay_path: str, workspace: str, session: Session) -> None:
    """Render, parse, and upsert a single Kustomize overlay."""
    env = _env_from_path(overlay_path)

    print(f"  [{workspace}/{env}] Detecting render mode for {overlay_path!r}…")
    mode = detect_render_mode(overlay_path)
    print(f"  [{workspace}/{env}] Render mode: {mode}")

    print(f"  [{workspace}/{env}] Running kustomize build…")
    docs = render_overlay(overlay_path, mode)
    print(f"  [{workspace}/{env}] Rendered {len(docs)} resource(s)")

    _index_docs(docs, workspace, env, session, source_label="overlay")


def _index_manifest_dir(manifest_path: str, workspace: str, session: Session) -> None:
    """Read raw manifest files, parse, and upsert a single manifest directory."""
    env = _env_from_path(manifest_path)

    print(f"  [{workspace}/{env}] Reading raw manifests from {manifest_path!r}…")
    docs = read_manifests(manifest_path)
    print(f"  [{workspace}/{env}] Read {len(docs)} resource(s)")

    _index_docs(docs, workspace, env, session, source_label="manifest")


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
