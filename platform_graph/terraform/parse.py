"""Static HCL parsing for Terraform files.

Extracts TerraformModule and Resource nodes plus three edge types:

- ``CALLS``      — TerraformModule -> child TerraformModule (from ``module`` blocks)
- ``DECLARES``   — TerraformModule -> Resource (from ``resource`` blocks)
- ``DEPENDS_ON`` — Resource -> Resource (from interpolation expressions and
                    explicit ``depends_on`` lists)

No ``terraform init``, ``plan``, or cloud credentials are required.  All
analysis is performed against the raw .tf source text via ``python-hcl2``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import hcl2

from platform_graph.model import Edge, Resource, TerraformModule

# Matches Terraform interpolation references: ${<type>.<name>.<attr>}
# We extract (resource_type, resource_name) from the first two dot-separated
# segments inside the interpolation expression.
_INTERP_RE = re.compile(r"\$\{([a-zA-Z][a-zA-Z0-9_]*)\.([a-zA-Z][a-zA-Z0-9_-]*)(?:\.[^}]*)?\}")

# Also matches bare references without ${ }: resource_type.resource_name.attr
# These appear in Terraform >=0.12 HCL2 expressions.
_BARE_REF_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9_]*)\.([a-zA-Z][a-zA-Z0-9_-]*)(?:\.[a-zA-Z][a-zA-Z0-9_]*)?\b"
)

# Keywords that look like type.name references but are NOT resource references.
_NON_RESOURCE_PREFIXES = frozenset(
    {
        "var",
        "local",
        "locals",
        "module",
        "data",
        "path",
        "self",
        "each",
        "count",
        "terraform",
        "null",
        "true",
        "false",
    }
)


def _unquote(value: str) -> str:
    """Strip surrounding double-quotes that python-hcl2 preserves for string literals.

    python-hcl2 returns HCL string literals with their surrounding quotes intact,
    e.g. ``'"aws_s3_bucket"'`` instead of ``'aws_s3_bucket'``.  This helper
    strips a single layer of surrounding double-quotes when present.
    """
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


@dataclass
class ParseResult:
    """All nodes and edges extracted from a single Terraform root directory."""

    modules: list[TerraformModule] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def parse_root(root_dir: Path, workspace: str, repo_root: Path) -> ParseResult:
    """Parse all .tf files under *root_dir* and return the extracted graph elements.

    Args:
        root_dir:   Absolute path to the Terraform root directory to parse.
        workspace:  Graphsearch workspace name (for node identity).
        repo_root:  Absolute repo root — used to compute module_path as a
                    relative path for node identity.

    Returns:
        A :class:`ParseResult` containing all nodes and edges found.
    """
    result = ParseResult()

    # Compute the module_path for this root relative to the repo root.
    try:
        root_module_path = str(root_dir.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        root_module_path = str(root_dir.resolve())

    root_module = TerraformModule(workspace=workspace, module_path=root_module_path)
    result.modules.append(root_module)

    # Collect all .tf files (recursive walk).
    tf_files = sorted(root_dir.rglob("*.tf"))
    if not tf_files:
        return result

    # Accumulate raw HCL blocks across all files for this root.
    raw_blocks: list[dict[str, Any]] = []
    for tf_file in tf_files:
        try:
            with tf_file.open("r", encoding="utf-8") as fh:
                parsed = hcl2.load(fh)
        except Exception:
            continue
        raw_blocks.append(parsed)

    # Extract module/resource/depends_on information from the merged blocks.
    for block in raw_blocks:
        _extract_module_calls(block, root_module, workspace, repo_root, root_dir, result)
        _extract_resources(block, root_module, workspace, result)

    return result


# ---------------------------------------------------------------------------
# Block extractors
# ---------------------------------------------------------------------------


def _extract_module_calls(
    block: dict[str, Any],
    parent_module: TerraformModule,
    workspace: str,
    repo_root: Path,
    root_dir: Path,
    result: ParseResult,
) -> None:
    """Extract ``module`` blocks and produce child TerraformModule nodes + CALLS edges.

    python-hcl2 represents a ``module "vpc" { source = "./modules/vpc" }`` block as::

        {"module": [{'"vpc"': {"__is_block__": True, "source": '"./modules/vpc"'}}]}

    All string keys and values must be unquoted with :func:`_unquote`.
    """
    for module_block in block.get("module", []):
        # module_block is a dict like {'"vpc"': {"__is_block__": True, "source": '"./modules/vpc"'}}
        for _module_label, module_config in module_block.items():
            if not isinstance(module_config, dict):
                continue
            source_raw = module_config.get("source", "")
            if not source_raw or not isinstance(source_raw, str):
                continue
            source = _unquote(source_raw)
            # Only handle local relative paths (./... or ../...)
            if not source.startswith("./") and not source.startswith("../"):
                continue
            child_abs = (root_dir / source).resolve()
            try:
                child_module_path = str(child_abs.relative_to(repo_root.resolve()))
            except ValueError:
                child_module_path = str(child_abs)

            child_module = TerraformModule(workspace=workspace, module_path=child_module_path)
            result.modules.append(child_module)

            result.edges.append(
                Edge(from_node=parent_module, to_node=child_module, rel_type="CALLS")
            )


def _extract_resources(
    block: dict[str, Any],
    parent_module: TerraformModule,
    workspace: str,
    result: ParseResult,
) -> None:
    """Extract ``resource`` blocks, producing Resource nodes + DECLARES edges.

    python-hcl2 represents ``resource "aws_s3_bucket" "b" {}`` as::

        {"resource": [{'"aws_s3_bucket"': {'"b"': {"__is_block__": True, ...}}}]}

    Keys are unquoted via :func:`_unquote` before use.
    """
    for resource_block in block.get("resource", []):
        # resource_block: {'"aws_s3_bucket"': {'"my_bucket"': {...}}}
        for resource_type_raw, instances in resource_block.items():
            resource_type = _unquote(resource_type_raw)
            if not isinstance(instances, dict):
                continue
            for resource_name_raw, resource_config in instances.items():
                resource_name = _unquote(resource_name_raw)
                if resource_name == "__is_block__":
                    continue
                res_node = Resource(
                    workspace=workspace,
                    resource_type=resource_type,
                    resource_name=resource_name,
                )
                result.resources.append(res_node)

                # DECLARES edge: parent module -> resource
                result.edges.append(
                    Edge(
                        from_node=parent_module,
                        to_node=res_node,
                        rel_type="DECLARES",
                    )
                )

                # DEPENDS_ON edges from explicit depends_on list
                depends_on = resource_config.get("depends_on", [])
                if isinstance(depends_on, list):
                    for dep_ref in depends_on:
                        dep_node = _resolve_ref(dep_ref, workspace, result)
                        if dep_node is not None:
                            result.edges.append(
                                Edge(
                                    from_node=res_node,
                                    to_node=dep_node,
                                    rel_type="DEPENDS_ON",
                                )
                            )

                # DEPENDS_ON edges from interpolation in string values
                _extract_interpolation_deps(resource_config, res_node, workspace, result)


def _extract_interpolation_deps(
    config: Any,
    source_resource: Resource,
    workspace: str,
    result: ParseResult,
    _visited: set[int] | None = None,
) -> None:
    """Recursively scan *config* for interpolation refs and emit DEPENDS_ON edges."""
    if _visited is None:
        _visited = set()

    obj_id = id(config)
    if obj_id in _visited:
        return
    _visited.add(obj_id)

    if isinstance(config, str):
        # Match ${type.name.attr} expressions
        for match in _INTERP_RE.finditer(config):
            ref_type, ref_name = match.group(1), match.group(2)
            if ref_type in _NON_RESOURCE_PREFIXES:
                continue
            dep_node = _find_or_create_resource(ref_type, ref_name, workspace, result)
            if dep_node is not source_resource:
                result.edges.append(
                    Edge(
                        from_node=source_resource,
                        to_node=dep_node,
                        rel_type="DEPENDS_ON",
                    )
                )
        # Match bare type.name.attr references (HCL2 style, no ${})
        for match in _BARE_REF_RE.finditer(config):
            ref_type, ref_name = match.group(1), match.group(2)
            if ref_type in _NON_RESOURCE_PREFIXES:
                continue
            # Only create a DEPENDS_ON edge if the referenced resource was already
            # declared in this root — bare refs are too broad otherwise.
            dep_node = _find_existing_resource(ref_type, ref_name, workspace, result)
            if dep_node is not None and dep_node is not source_resource:
                result.edges.append(
                    Edge(
                        from_node=source_resource,
                        to_node=dep_node,
                        rel_type="DEPENDS_ON",
                    )
                )
    elif isinstance(config, dict):
        for v in config.values():
            _extract_interpolation_deps(v, source_resource, workspace, result, _visited)
    elif isinstance(config, list):
        for item in config:
            _extract_interpolation_deps(item, source_resource, workspace, result, _visited)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_ref(ref: str, workspace: str, result: ParseResult) -> Resource | None:
    """Convert a ``depends_on`` string like ``aws_s3_bucket.my_bucket`` to a Resource.

    The *ref* value from python-hcl2 may be quoted (e.g. ``'"aws_s3_bucket.my_bucket"'``).
    It is unquoted before parsing.

    Returns an existing Resource from *result* if found, or creates a new one.
    Returns None if the ref cannot be parsed as a resource reference.
    """
    if not isinstance(ref, str):
        return None
    ref = _unquote(ref)
    parts = ref.split(".")
    if len(parts) < 2:
        return None
    ref_type, ref_name = parts[0], parts[1]
    if ref_type in _NON_RESOURCE_PREFIXES:
        return None
    return _find_or_create_resource(ref_type, ref_name, workspace, result)


def _find_existing_resource(
    resource_type: str, resource_name: str, workspace: str, result: ParseResult
) -> Resource | None:
    """Return an already-declared Resource from *result* matching type+name, or None."""
    for r in result.resources:
        if (
            r.resource_type == resource_type
            and r.resource_name == resource_name
            and r.workspace == workspace
        ):
            return r
    return None


def _find_or_create_resource(
    resource_type: str, resource_name: str, workspace: str, result: ParseResult
) -> Resource:
    """Return existing Resource from *result* or create and append a new one."""
    existing = _find_existing_resource(resource_type, resource_name, workspace, result)
    if existing is not None:
        return existing
    new_node = Resource(
        workspace=workspace, resource_type=resource_type, resource_name=resource_name
    )
    result.resources.append(new_node)
    return new_node
