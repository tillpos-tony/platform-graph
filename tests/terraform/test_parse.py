"""Tests for platform_graph.terraform.parse — static HCL extraction."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from platform_graph.model import Resource, TerraformModule
from platform_graph.terraform.parse import parse_root

# ---------------------------------------------------------------------------
# Fixtures: write minimal .tf files into tmp directories
# ---------------------------------------------------------------------------


def write_tf(dir_: Path, filename: str, content: str) -> Path:
    """Write *content* to *dir_*/*filename* and return the path."""
    path = dir_ / filename
    path.write_text(textwrap.dedent(content))
    return path


# ---------------------------------------------------------------------------
# Test: root module node is always produced
# ---------------------------------------------------------------------------


def test_root_module_node_is_created(tmp_path: Path) -> None:
    """parse_root always yields the root TerraformModule node."""
    write_tf(tmp_path, "main.tf", """\
        resource "null_resource" "example" {}
    """)
    result = parse_root(tmp_path, workspace="test-ws", repo_root=tmp_path.parent)
    assert any(isinstance(n, TerraformModule) for n in result.modules)
    root_mod = result.modules[0]
    assert root_mod.workspace == "test-ws"
    assert root_mod.module_path != ""


# ---------------------------------------------------------------------------
# Test: resource block -> Resource node + DECLARES edge
# ---------------------------------------------------------------------------


def test_resource_block_produces_resource_node_and_declares_edge(tmp_path: Path) -> None:
    """A resource block produces a Resource node and a DECLARES edge."""
    write_tf(tmp_path, "main.tf", """\
        resource "aws_s3_bucket" "my_bucket" {
          bucket = "my-unique-bucket-name"
        }
    """)
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)

    # Resource node
    resources = [n for n in result.resources if isinstance(n, Resource)]
    assert len(resources) == 1
    r = resources[0]
    assert r.resource_type == "aws_s3_bucket"
    assert r.resource_name == "my_bucket"
    assert r.workspace == "ws"
    assert "Resource" in r.labels
    assert "aws_s3_bucket" in r.labels

    # DECLARES edge
    declares_edges = [e for e in result.edges if e.rel_type == "DECLARES"]
    assert len(declares_edges) == 1
    assert isinstance(declares_edges[0].from_node, TerraformModule)
    assert declares_edges[0].to_node is r


# ---------------------------------------------------------------------------
# Test: module block -> child TerraformModule + CALLS edge
# ---------------------------------------------------------------------------


def test_module_block_produces_child_module_and_calls_edge(tmp_path: Path) -> None:
    """A module block produces a child TerraformModule node and a CALLS edge."""
    # Create child module directory
    child_dir = tmp_path / "modules" / "vpc"
    child_dir.mkdir(parents=True)
    write_tf(child_dir, "main.tf", "# child module")

    write_tf(tmp_path, "main.tf", """\
        module "vpc" {
          source = "./modules/vpc"
        }
    """)
    repo_root = tmp_path.parent
    result = parse_root(tmp_path, workspace="ws", repo_root=repo_root)

    modules = result.modules
    assert len(modules) == 2  # root + child

    calls_edges = [e for e in result.edges if e.rel_type == "CALLS"]
    assert len(calls_edges) == 1
    parent = calls_edges[0].from_node
    child = calls_edges[0].to_node
    assert isinstance(parent, TerraformModule)
    assert isinstance(child, TerraformModule)
    assert "modules/vpc" in child.module_path or child.module_path.endswith("modules/vpc")


# ---------------------------------------------------------------------------
# Test: interpolation expression -> DEPENDS_ON edge
# ---------------------------------------------------------------------------


def test_interpolation_creates_depends_on_edge(tmp_path: Path) -> None:
    """String interpolation like ${aws_security_group.sg.id} creates a DEPENDS_ON edge."""
    write_tf(tmp_path, "main.tf", """\
        resource "aws_security_group" "sg" {
          name = "my-sg"
        }

        resource "aws_instance" "web" {
          ami           = "ami-12345"
          instance_type = "t3.micro"
          vpc_security_group_ids = ["${aws_security_group.sg.id}"]
        }
    """)
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)

    depends_on_edges = [e for e in result.edges if e.rel_type == "DEPENDS_ON"]
    assert len(depends_on_edges) >= 1

    from_names = {e.from_node.resource_name for e in depends_on_edges}  # type: ignore[union-attr]
    to_names = {e.to_node.resource_name for e in depends_on_edges}  # type: ignore[union-attr]
    assert "web" in from_names
    assert "sg" in to_names


# ---------------------------------------------------------------------------
# Test: explicit depends_on list -> DEPENDS_ON edge
# ---------------------------------------------------------------------------


def test_explicit_depends_on_creates_edge(tmp_path: Path) -> None:
    """Explicit depends_on = [resource_type.resource_name] creates a DEPENDS_ON edge."""
    write_tf(tmp_path, "main.tf", """\
        resource "aws_iam_role" "role" {
          name = "my-role"
        }

        resource "aws_instance" "app" {
          ami           = "ami-12345"
          instance_type = "t3.micro"
          depends_on    = ["aws_iam_role.role"]
        }
    """)
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)

    depends_on_edges = [e for e in result.edges if e.rel_type == "DEPENDS_ON"]
    assert len(depends_on_edges) >= 1
    to_types = {e.to_node.resource_type for e in depends_on_edges}  # type: ignore[union-attr]
    assert "aws_iam_role" in to_types


# ---------------------------------------------------------------------------
# Test: empty directory produces only root module
# ---------------------------------------------------------------------------


def test_empty_root_produces_only_root_module(tmp_path: Path) -> None:
    """A directory with no .tf files still yields the root TerraformModule."""
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)
    assert len(result.modules) == 1
    assert len(result.resources) == 0
    assert len(result.edges) == 0


# ---------------------------------------------------------------------------
# Test: multiple resources in one file
# ---------------------------------------------------------------------------


def test_multiple_resources_all_extracted(tmp_path: Path) -> None:
    """All resource blocks in a file are extracted as separate Resource nodes."""
    write_tf(tmp_path, "main.tf", """\
        resource "aws_s3_bucket" "bucket_a" {
          bucket = "bucket-a"
        }

        resource "aws_s3_bucket" "bucket_b" {
          bucket = "bucket-b"
        }

        resource "aws_dynamodb_table" "table" {
          name = "my-table"
        }
    """)
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)

    resources = result.resources
    assert len(resources) == 3

    types = {r.resource_type for r in resources}
    assert types == {"aws_s3_bucket", "aws_dynamodb_table"}

    names = {r.resource_name for r in resources}
    assert names == {"bucket_a", "bucket_b", "table"}

    # Three DECLARES edges
    declares_edges = [e for e in result.edges if e.rel_type == "DECLARES"]
    assert len(declares_edges) == 3


# ---------------------------------------------------------------------------
# Test: parsing is fully static (no terraform binary needed)
# ---------------------------------------------------------------------------


def test_parsing_requires_no_terraform_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """parse_root must not invoke any subprocess (no terraform init/plan)."""
    import subprocess

    original_run = subprocess.run

    def fail_if_terraform(args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(args, (list, tuple)) and args and "terraform" in str(args[0]):
            raise AssertionError(f"terraform subprocess invoked unexpectedly: {args}")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_if_terraform)

    write_tf(tmp_path, "main.tf", """\
        resource "aws_s3_bucket" "test" {
          bucket = "static-test"
        }
    """)
    # This must not raise — i.e., no terraform subprocess is called.
    result = parse_root(tmp_path, workspace="ws", repo_root=tmp_path.parent)
    assert len(result.resources) == 1
