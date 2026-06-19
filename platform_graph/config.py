"""Config loader for .platform-graph.toml.

Reads the per-repo config file from the repo root. A missing file is not an
error — callers receive None and should treat it as a no-op.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PlatformGraphConfig:
    """Parsed contents of a .platform-graph.toml file."""

    workspace: str
    bolt_uri: str = "bolt://127.0.0.1:7687"
    k8s_overlays: list[str] = field(default_factory=list)
    k8s_manifests: list[str] = field(default_factory=list)
    terraform_roots: list[str] = field(default_factory=list)


def load_config(repo_root: Path | None = None) -> PlatformGraphConfig | None:
    """Load .platform-graph.toml from *repo_root* (or the current directory).

    Returns None — silently, without raising — if the file does not exist.
    Raises tomllib.TOMLDecodeError if the file exists but is malformed.
    """
    root = repo_root or Path.cwd()
    config_path = root / ".platform-graph.toml"

    if not config_path.exists():
        return None

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    return PlatformGraphConfig(
        workspace=raw["workspace"],
        bolt_uri=raw.get("bolt_uri", "bolt://127.0.0.1:7687"),
        k8s_overlays=raw.get("k8s_overlays", []),
        k8s_manifests=raw.get("k8s_manifests", []),
        terraform_roots=raw.get("terraform_roots", []),
    )


def write_config(config: PlatformGraphConfig, repo_root: Path | None = None) -> Path:
    """Write *config* to .platform-graph.toml in *repo_root*.

    Returns the path that was written.
    """
    root = repo_root or Path.cwd()
    config_path = root / ".platform-graph.toml"

    lines = [
        f'workspace = "{config.workspace}"\n',
        f'bolt_uri = "{config.bolt_uri}"\n',
    ]

    if config.k8s_overlays:
        overlays = ", ".join(f'"{p}"' for p in config.k8s_overlays)
        lines.append(f"k8s_overlays = [{overlays}]\n")
    else:
        lines.append("k8s_overlays = []\n")

    if config.k8s_manifests:
        manifests = ", ".join(f'"{p}"' for p in config.k8s_manifests)
        lines.append(f"k8s_manifests = [{manifests}]\n")
    else:
        lines.append("k8s_manifests = []\n")

    if config.terraform_roots:
        roots = ", ".join(f'"{p}"' for p in config.terraform_roots)
        lines.append(f"terraform_roots = [{roots}]\n")
    else:
        lines.append("terraform_roots = []\n")

    config_path.write_text("".join(lines))
    return config_path
