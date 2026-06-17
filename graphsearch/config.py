"""Config loader for .graphsearch.toml.

Reads the per-repo config file from the repo root. A missing file is not an
error — callers receive None and should treat it as a no-op.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GraphsearchConfig:
    """Parsed contents of a .graphsearch.toml file."""

    workspace: str
    bolt_uri: str = "bolt://127.0.0.1:7687"
    k8s_overlays: list[str] = field(default_factory=list)
    terraform_roots: list[str] = field(default_factory=list)


def load_config(repo_root: Path | None = None) -> GraphsearchConfig | None:
    """Load .graphsearch.toml from *repo_root* (or the current directory).

    Returns None — silently, without raising — if the file does not exist.
    Raises tomllib.TOMLDecodeError if the file exists but is malformed.
    """
    root = repo_root or Path.cwd()
    config_path = root / ".graphsearch.toml"

    if not config_path.exists():
        return None

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    return GraphsearchConfig(
        workspace=raw["workspace"],
        bolt_uri=raw.get("bolt_uri", "bolt://127.0.0.1:7687"),
        k8s_overlays=raw.get("k8s_overlays", []),
        terraform_roots=raw.get("terraform_roots", []),
    )


def write_config(config: GraphsearchConfig, repo_root: Path | None = None) -> Path:
    """Write *config* to .graphsearch.toml in *repo_root*.

    Returns the path that was written.
    """
    root = repo_root or Path.cwd()
    config_path = root / ".graphsearch.toml"

    lines = [
        f'workspace = "{config.workspace}"\n',
        f'bolt_uri = "{config.bolt_uri}"\n',
    ]

    if config.k8s_overlays:
        overlays = ", ".join(f'"{p}"' for p in config.k8s_overlays)
        lines.append(f"k8s_overlays = [{overlays}]\n")
    else:
        lines.append("k8s_overlays = []\n")

    if config.terraform_roots:
        roots = ", ".join(f'"{p}"' for p in config.terraform_roots)
        lines.append(f"terraform_roots = [{roots}]\n")
    else:
        lines.append("terraform_roots = []\n")

    config_path.write_text("".join(lines))
    return config_path
