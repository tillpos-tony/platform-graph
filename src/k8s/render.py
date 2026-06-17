"""Kustomize overlay rendering for graphsearch K8s indexing.

Two responsibilities
--------------------
1. ``detect_render_mode`` — inspect a kustomization.yaml and decide whether
   the overlay needs ``--enable-helm`` (helm-inflated) or can be rendered
   as plain Kustomize (raw).
2. ``render_overlay`` — shell out to ``kustomize build`` and parse the
   multi-document YAML stream into a list of resource dicts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml  # PyYAML — pulled in transitively; add to deps if needed

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Render mode detection
# ---------------------------------------------------------------------------

_KUSTOMIZATION_FILENAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")


def detect_render_mode(overlay_path: str) -> Literal["helm-inflated", "raw"]:
    """Return the render mode for *overlay_path*.

    Reads the kustomization file in the overlay directory.  If it contains a
    top-level ``helmCharts`` key the overlay requires Helm inflation; otherwise
    it is treated as plain Kustomize.

    Parameters
    ----------
    overlay_path:
        Absolute or relative path to a kustomization overlay directory.

    Returns
    -------
    "helm-inflated" | "raw"

    Raises
    ------
    FileNotFoundError
        If no kustomization file is found in *overlay_path*.
    yaml.YAMLError
        If the kustomization file cannot be parsed.
    """
    overlay = Path(overlay_path)

    kustomization_file: Path | None = None
    for name in _KUSTOMIZATION_FILENAMES:
        candidate = overlay / name
        if candidate.exists():
            kustomization_file = candidate
            break

    if kustomization_file is None:
        raise FileNotFoundError(
            f"No kustomization file found in {overlay_path!r}. "
            f"Looked for: {', '.join(_KUSTOMIZATION_FILENAMES)}"
        )

    with kustomization_file.open() as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict) and data.get("helmCharts"):
        return "helm-inflated"

    return "raw"


# ---------------------------------------------------------------------------
# Overlay rendering
# ---------------------------------------------------------------------------


def render_overlay(overlay_path: str, mode: str) -> list[dict]:
    """Run ``kustomize build`` and return parsed resource dicts.

    Parameters
    ----------
    overlay_path:
        Path to the kustomization overlay directory.
    mode:
        ``"helm-inflated"`` to pass ``--enable-helm``, or ``"raw"`` for
        plain kustomize build.

    Returns
    -------
    list[dict]
        One dict per K8s resource in the rendered YAML stream (multi-doc).
        Empty documents (YAML separators with no content) are skipped.

    Raises
    ------
    RuntimeError
        If ``kustomize build`` exits with a non-zero status.
    FileNotFoundError
        If the ``kustomize`` binary is not on PATH.
    """
    cmd = ["kustomize", "build", overlay_path]
    if mode == "helm-inflated":
        cmd.append("--enable-helm")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "kustomize binary not found on PATH. "
            "Install it via https://kubectl.docs.kubernetes.io/installation/kustomize/."
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"kustomize build failed for {overlay_path!r} (exit {result.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr:\n{stderr}"
        )

    resources: list[dict] = []
    for doc in yaml.safe_load_all(result.stdout):
        if doc is not None:
            resources.append(doc)

    return resources
