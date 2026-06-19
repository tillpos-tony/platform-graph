"""Raw K8s manifest folder reader for graphsearch.

Reads a directory tree of hand-written Kubernetes YAML directly — no kustomize
build, no subprocess.  The caller receives the same list-of-dicts shape that
``render_overlay`` returns, so the parse → upsert pipeline is identical.

Entry point: ``read_manifests(directory)``
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Filenames that are kustomize build directives, not K8s resources.
_KUSTOMIZATION_NAMES = frozenset({"kustomization.yaml", "kustomization.yml", "Kustomization"})


def read_manifests(directory: str | Path) -> list[dict]:
    """Recursively read all ``*.yaml``/``*.yml`` files under *directory*.

    Files named ``kustomization.yaml``, ``kustomization.yml``, or
    ``Kustomization`` are skipped.  If any such file is present in a directory,
    a one-line advisory warning is printed suggesting the path may belong in
    ``k8s_overlays``.

    Multi-document YAML files are split and each document is returned
    individually.  Empty documents (``null`` / bare ``---`` separators) are
    discarded.  The returned list is in sorted filesystem order (by absolute
    path), making the result deterministic across runs.

    A file that fails YAML parsing is reported as a warning and skipped; other
    files continue to be indexed and the function returns normally (exit 0).

    Parameters
    ----------
    directory:
        Path to the root of the manifest tree (must be a directory).

    Returns
    -------
    list[dict]
        One dict per K8s resource found across all matching files.
    """
    root = Path(directory)

    # Collect all yaml/yml files recursively, sorted for determinism.
    yaml_files = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    # Re-sort the combined list for overall determinism.
    yaml_files = sorted(set(yaml_files))

    # Per-directory advisory: warn once per directory that contains a kustomization file.
    _warn_kustomization_dirs(root)

    docs: list[dict] = []
    for filepath in yaml_files:
        if filepath.name in _KUSTOMIZATION_NAMES:
            continue

        try:
            text = filepath.read_text(encoding="utf-8")
            for doc in yaml.safe_load_all(text):
                if doc is not None:
                    docs.append(doc)
        except yaml.YAMLError as exc:
            print(f"  [warning] skipping {filepath}: malformed YAML — {exc}")

    return docs


def _warn_kustomization_dirs(root: Path) -> None:
    """Emit a one-line advisory for each directory under *root* that contains
    a kustomization file, suggesting the path may belong in ``k8s_overlays``."""
    warned: set[Path] = set()
    for name in _KUSTOMIZATION_NAMES:
        for kfile in root.rglob(name):
            parent = kfile.parent
            if parent not in warned:
                warned.add(parent)
                print(
                    f"  [advisory] {parent} contains {kfile.name!r} — "
                    "this path may belong in k8s_overlays instead of k8s_manifests"
                )
