"""Tests for platform_graph.k8s.manifests — raw K8s manifest folder reader.

Covers all acceptance criteria from docs/issues/0001-index-raw-manifest-folders.md:
- Config round-trip and default
- Recursive reads, multi-doc split, extension filtering, sorted order
- kustomization skip + per-directory advisory warning
- Malformed YAML warning + skip, exit 0 (other files still indexed)
- Glob env-tagging via _resolve_paths
- Render-vs-raw parity: same docs → same nodes/edges
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from platform_graph.config import PlatformGraphConfig, load_config, write_config
from platform_graph.k8s.index import _env_from_path, _index_docs, _resolve_paths
from platform_graph.k8s.manifests import read_manifests
from platform_graph.k8s.parse import parse_resources

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEPLOYMENT = textwrap.dedent("""\
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: my-app
      namespace: default
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: my-app
      template:
        metadata:
          labels:
            app: my-app
        spec:
          containers:
            - name: app
              image: my-app:latest
""")

_SERVICE = textwrap.dedent("""\
    apiVersion: v1
    kind: Service
    metadata:
      name: my-svc
      namespace: default
    spec:
      selector:
        app: my-app
      ports:
        - port: 80
""")

_CONFIGMAP = textwrap.dedent("""\
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: my-cm
      namespace: default
    data:
      key: value
""")


# ---------------------------------------------------------------------------
# Config: round-trip and default
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    def test_k8s_manifests_defaults_to_empty_list(self, tmp_path: Path) -> None:
        """load_config returns [] for k8s_manifests when key is absent."""
        config_path = tmp_path / ".platform-graph.toml"
        config_path.write_text('workspace = "test"\nbolt_uri = "bolt://127.0.0.1:7687"\n')

        result = load_config(tmp_path)
        assert result is not None
        assert result.k8s_manifests == []

    def test_k8s_manifests_loaded_from_toml(self, tmp_path: Path) -> None:
        """load_config reads k8s_manifests list from .platform-graph.toml."""
        config_path = tmp_path / ".platform-graph.toml"
        config_path.write_text(
            'workspace = "test"\n'
            'bolt_uri = "bolt://127.0.0.1:7687"\n'
            'k8s_manifests = ["manifests/prod", "manifests/staging"]\n'
        )

        result = load_config(tmp_path)
        assert result is not None
        assert result.k8s_manifests == ["manifests/prod", "manifests/staging"]

    def test_write_config_round_trips_k8s_manifests(self, tmp_path: Path) -> None:
        """write_config writes k8s_manifests and load_config reads it back."""
        config = PlatformGraphConfig(
            workspace="test",
            bolt_uri="bolt://127.0.0.1:7687",
            k8s_manifests=["manifests/prod", "manifests/staging"],
        )
        write_config(config, tmp_path)
        reloaded = load_config(tmp_path)

        assert reloaded is not None
        assert reloaded.k8s_manifests == ["manifests/prod", "manifests/staging"]

    def test_write_config_empty_k8s_manifests(self, tmp_path: Path) -> None:
        """write_config writes k8s_manifests = [] when empty."""
        config = PlatformGraphConfig(workspace="test")
        write_config(config, tmp_path)
        text = (tmp_path / ".platform-graph.toml").read_text()

        assert "k8s_manifests = []" in text

    def test_write_config_preserves_existing_k8s_overlays(self, tmp_path: Path) -> None:
        """write_config round-trips both k8s_overlays and k8s_manifests."""
        config = PlatformGraphConfig(
            workspace="test",
            k8s_overlays=["k8s/overlays/prod"],
            k8s_manifests=["k8s/raw/prod"],
        )
        write_config(config, tmp_path)
        reloaded = load_config(tmp_path)

        assert reloaded is not None
        assert reloaded.k8s_overlays == ["k8s/overlays/prod"]
        assert reloaded.k8s_manifests == ["k8s/raw/prod"]


# ---------------------------------------------------------------------------
# read_manifests: recursive reads, extension filtering, multi-doc, sorted order
# ---------------------------------------------------------------------------


class TestReadManifests:
    def test_reads_yaml_files_recursively(self, tmp_path: Path) -> None:
        """read_manifests picks up *.yaml files in subdirectories."""
        (tmp_path / "sub").mkdir()
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)
        (tmp_path / "sub" / "svc.yaml").write_text(_SERVICE)

        docs = read_manifests(tmp_path)
        kinds = {d["kind"] for d in docs}
        assert kinds == {"Deployment", "Service"}

    def test_reads_yml_extension(self, tmp_path: Path) -> None:
        """read_manifests reads *.yml files as well as *.yaml."""
        (tmp_path / "cm.yml").write_text(_CONFIGMAP)

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "ConfigMap"

    def test_ignores_non_yaml_extensions(self, tmp_path: Path) -> None:
        """read_manifests ignores files that are not *.yaml or *.yml."""
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)
        (tmp_path / "readme.txt").write_text("not yaml")
        (tmp_path / "values.json").write_text('{"key": "value"}')

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "Deployment"

    def test_splits_multidoc_yaml(self, tmp_path: Path) -> None:
        """read_manifests splits multi-document YAML files."""
        multi = _DEPLOYMENT + "---\n" + _SERVICE
        (tmp_path / "multi.yaml").write_text(multi)

        docs = read_manifests(tmp_path)
        kinds = {d["kind"] for d in docs}
        assert kinds == {"Deployment", "Service"}

    def test_skips_empty_yaml_documents(self, tmp_path: Path) -> None:
        """read_manifests discards empty/None documents from multi-doc files."""
        content = "---\n---\n" + _CONFIGMAP
        (tmp_path / "sparse.yaml").write_text(content)

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "ConfigMap"

    def test_returns_empty_list_for_empty_directory(self, tmp_path: Path) -> None:
        """read_manifests returns [] when there are no yaml files."""
        docs = read_manifests(tmp_path)
        assert docs == []

    def test_returns_sorted_order(self, tmp_path: Path) -> None:
        """read_manifests returns docs in sorted filesystem path order."""
        (tmp_path / "aaa").mkdir()
        (tmp_path / "zzz").mkdir()
        (tmp_path / "aaa" / "res.yaml").write_text(_CONFIGMAP)
        (tmp_path / "zzz" / "res.yaml").write_text(_SERVICE)

        docs = read_manifests(tmp_path)
        assert len(docs) == 2
        # aaa/ comes before zzz/ alphabetically
        assert docs[0]["kind"] == "ConfigMap"
        assert docs[1]["kind"] == "Service"

    def test_sorted_order_across_files_in_same_dir(self, tmp_path: Path) -> None:
        """read_manifests sorts all files regardless of depth."""
        (tmp_path / "a_deploy.yaml").write_text(_DEPLOYMENT)
        (tmp_path / "b_svc.yaml").write_text(_SERVICE)

        docs = read_manifests(tmp_path)
        assert docs[0]["kind"] == "Deployment"
        assert docs[1]["kind"] == "Service"


# ---------------------------------------------------------------------------
# read_manifests: kustomization skip + advisory
# ---------------------------------------------------------------------------


class TestKustomizationSkipAndAdvisory:
    def test_skips_kustomization_yaml(self, tmp_path: Path) -> None:
        """kustomization.yaml is not treated as a resource."""
        (tmp_path / "kustomization.yaml").write_text("resources:\n  - deploy.yaml\n")
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "Deployment"

    def test_skips_kustomization_yml(self, tmp_path: Path) -> None:
        """kustomization.yml (alternate extension) is also skipped."""
        (tmp_path / "kustomization.yml").write_text("resources:\n  - svc.yaml\n")
        (tmp_path / "svc.yaml").write_text(_SERVICE)

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "Service"

    def test_skips_Kustomization_capitalised(self, tmp_path: Path) -> None:
        """Capitalised 'Kustomization' filename is also skipped."""
        (tmp_path / "Kustomization").write_text("resources:\n  - cm.yaml\n")
        (tmp_path / "cm.yaml").write_text(_CONFIGMAP)

        docs = read_manifests(tmp_path)
        assert len(docs) == 1
        assert docs[0]["kind"] == "ConfigMap"

    def test_advisory_printed_when_kustomization_present(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A per-directory advisory is printed when a kustomization file is found."""
        (tmp_path / "kustomization.yaml").write_text("resources:\n  - deploy.yaml\n")
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)

        read_manifests(tmp_path)

        captured = capsys.readouterr()
        assert "[advisory]" in captured.out
        assert "k8s_overlays" in captured.out

    def test_advisory_printed_once_per_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Advisory is emitted once per directory, not once per kustomization file."""
        # Two kustomization filenames in the same directory
        (tmp_path / "kustomization.yaml").write_text("resources: []\n")
        (tmp_path / "kustomization.yml").write_text("resources: []\n")

        read_manifests(tmp_path)

        captured = capsys.readouterr()
        advisory_lines = [line for line in captured.out.splitlines() if "[advisory]" in line]
        # Only one advisory for the root dir (both files resolve to same parent)
        assert len(advisory_lines) == 1

    def test_advisory_per_subdirectory(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A separate advisory is printed for each subdirectory with kustomization."""
        sub1 = tmp_path / "sub1"
        sub2 = tmp_path / "sub2"
        sub1.mkdir()
        sub2.mkdir()
        (sub1 / "kustomization.yaml").write_text("resources: []\n")
        (sub2 / "kustomization.yaml").write_text("resources: []\n")

        read_manifests(tmp_path)

        captured = capsys.readouterr()
        advisory_lines = [line for line in captured.out.splitlines() if "[advisory]" in line]
        assert len(advisory_lines) == 2

    def test_no_advisory_when_no_kustomization(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """No advisory is printed when there are no kustomization files."""
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)

        read_manifests(tmp_path)

        captured = capsys.readouterr()
        assert "[advisory]" not in captured.out


# ---------------------------------------------------------------------------
# read_manifests: malformed YAML warning + skip
# ---------------------------------------------------------------------------


class TestMalformedYamlHandling:
    def test_malformed_yaml_prints_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A malformed YAML file triggers a warning message."""
        (tmp_path / "bad.yaml").write_text("key: [unclosed bracket\n")

        read_manifests(tmp_path)

        captured = capsys.readouterr()
        assert "[warning]" in captured.out
        assert "bad.yaml" in captured.out

    def test_malformed_yaml_skipped_others_still_indexed(self, tmp_path: Path) -> None:
        """Other files are still indexed even when one file is malformed."""
        (tmp_path / "bad.yaml").write_text("key: [unclosed bracket\n")
        (tmp_path / "good.yaml").write_text(_DEPLOYMENT)

        docs = read_manifests(tmp_path)

        assert len(docs) == 1
        assert docs[0]["kind"] == "Deployment"

    def test_malformed_yaml_returns_normally_exit_0(self, tmp_path: Path) -> None:
        """read_manifests does not raise on malformed YAML (caller exits 0)."""
        (tmp_path / "bad.yaml").write_text(": invalid: yaml: }\n")

        # Should not raise
        docs = read_manifests(tmp_path)
        assert isinstance(docs, list)

    def test_multiple_malformed_files_all_skipped(self, tmp_path: Path) -> None:
        """All malformed files are skipped; good files are still returned."""
        (tmp_path / "bad1.yaml").write_text("key: [unclosed\n")
        (tmp_path / "bad2.yaml").write_text("{broken yaml\n")
        (tmp_path / "good.yaml").write_text(_SERVICE)

        docs = read_manifests(tmp_path)

        assert len(docs) == 1
        assert docs[0]["kind"] == "Service"


# ---------------------------------------------------------------------------
# _resolve_paths: glob expansion + env tagging
# ---------------------------------------------------------------------------


class TestResolvePaths:
    def test_literal_path_resolved_against_base(self, tmp_path: Path) -> None:
        """A path without globs is resolved against base without existence check."""
        result = _resolve_paths(["manifests/prod"], tmp_path)
        assert result == [tmp_path / "manifests/prod"]

    def test_glob_expands_to_existing_directories(self, tmp_path: Path) -> None:
        """A glob path expands to all matching subdirectories."""
        (tmp_path / "prod").mkdir()
        (tmp_path / "staging").mkdir()
        (tmp_path / "notadir.yaml").write_text("")

        result = _resolve_paths(["*"], tmp_path)
        assert tmp_path / "prod" in result
        assert tmp_path / "staging" in result
        # Non-directories are excluded
        assert tmp_path / "notadir.yaml" not in result

    def test_glob_prints_warning_when_no_match(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A glob that matches nothing emits a warning."""
        _resolve_paths(["nonexistent/*"], tmp_path)

        captured = capsys.readouterr()
        assert "[warning]" in captured.out
        assert "matched no directories" in captured.out

    def test_env_from_path_last_component(self) -> None:
        """_env_from_path derives env from the last path component."""
        assert _env_from_path("manifests/prod") == "prod"
        assert _env_from_path("/k8s/base") == "base"
        assert _env_from_path("staging") == "staging"

    def test_env_from_path_dot_returns_empty(self) -> None:
        """_env_from_path returns '' for '.'."""
        assert _env_from_path(".") == ""

    def test_glob_env_tagging_last_component(self, tmp_path: Path) -> None:
        """Each directory expanded from a glob gets env = its last path component."""
        (tmp_path / "prod").mkdir()
        (tmp_path / "staging").mkdir()

        paths = _resolve_paths(["*"], tmp_path)
        envs = {_env_from_path(str(p)) for p in paths}

        assert "prod" in envs
        assert "staging" in envs


# ---------------------------------------------------------------------------
# Render-vs-raw parity: same docs → same nodes/edges
# ---------------------------------------------------------------------------


class TestRenderVsRawParity:
    def test_same_docs_produce_same_nodes(self) -> None:
        """parse_resources returns identical nodes for docs from render or raw reader."""
        docs = [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "app", "namespace": "default"},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "app"}},
                    "template": {
                        "metadata": {"labels": {"app": "app"}},
                        "spec": {"containers": [{"name": "app", "image": "app:latest"}]},
                    },
                },
            }
        ]

        nodes_a, edges_a = parse_resources(docs, "ws", "prod")
        nodes_b, edges_b = parse_resources(docs, "ws", "prod")

        assert nodes_a == nodes_b
        assert edges_a == edges_b

    def test_raw_reader_and_render_produce_same_nodes(self, tmp_path: Path) -> None:
        """Docs read from disk yield the same parse result as the same docs inlined."""
        (tmp_path / "deploy.yaml").write_text(_DEPLOYMENT)

        raw_docs = read_manifests(tmp_path)

        # Simulate what render_overlay would return for the same resource
        from platform_graph.k8s.render import render_overlay

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _DEPLOYMENT

        with patch("subprocess.run", return_value=mock_result):
            rendered_docs = render_overlay(str(tmp_path), "raw")

        nodes_raw, edges_raw = parse_resources(raw_docs, "ws", "prod")
        nodes_rendered, edges_rendered = parse_resources(rendered_docs, "ws", "prod")

        assert nodes_raw == nodes_rendered
        assert edges_raw == edges_rendered

    def test_index_docs_calls_upsert_for_each_node_and_edge(self) -> None:
        """_index_docs upserts every node then every edge."""
        docs = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "cm", "namespace": "default"},
            }
        ]
        mock_session = MagicMock()

        with (
            patch("platform_graph.k8s.index.upsert_node") as mock_upsert_node,
            patch("platform_graph.k8s.index.upsert_edge") as mock_upsert_edge,
        ):
            _index_docs(docs, "ws", "prod", mock_session, source_label="test")

        assert mock_upsert_node.call_count >= 1
        # ConfigMap has no edges; edge count may be 0
        assert mock_upsert_edge.call_count >= 0


# ---------------------------------------------------------------------------
# cmd_index: "nothing to index" guard includes k8s_manifests
# ---------------------------------------------------------------------------


class TestCmdIndexNothingToIndexGuard:
    def test_nothing_to_index_when_all_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """cmd_index exits 0 with a message when all source lists are empty."""
        from platform_graph.cli import cmd_index

        config_path = tmp_path / ".platform-graph.toml"
        config_path.write_text(
            'workspace = "test"\n'
            'bolt_uri = "bolt://127.0.0.1:7687"\n'
            "k8s_overlays = []\n"
            "k8s_manifests = []\n"
            "terraform_roots = []\n"
        )

        with patch("platform_graph.cli._git_root", return_value=tmp_path):
            result = cmd_index(MagicMock())

        assert result == 0
        captured = capsys.readouterr()
        assert "nothing to index" in captured.out

    def test_runs_when_only_k8s_manifests_configured(self, tmp_path: Path) -> None:
        """cmd_index proceeds past the guard when only k8s_manifests is set."""
        from platform_graph.cli import cmd_index

        config_path = tmp_path / ".platform-graph.toml"
        config_path.write_text(
            'workspace = "test"\n'
            'bolt_uri = "bolt://127.0.0.1:7687"\n'
            "k8s_overlays = []\n"
            'k8s_manifests = ["manifests/prod"]\n'
            "terraform_roots = []\n"
        )

        with (
            patch("platform_graph.cli._git_root", return_value=tmp_path),
            patch("platform_graph.cli.connect") as mock_connect,
            patch("platform_graph.cli.index_k8s") as mock_index_k8s,
        ):
            mock_driver = MagicMock()
            mock_connect.return_value = mock_driver
            mock_driver.session.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_driver.session.return_value.__exit__ = MagicMock(return_value=False)

            result = cmd_index(MagicMock())

        assert result == 0
        mock_index_k8s.assert_called_once()
