"""Tests for graphsearch.k8s.render — render mode detection and overlay rendering."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graphsearch.k8s.render import detect_render_mode, render_overlay

# ---------------------------------------------------------------------------
# detect_render_mode
# ---------------------------------------------------------------------------


class TestDetectRenderMode:
    def test_returns_raw_for_plain_kustomization(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yaml").write_text(
            textwrap.dedent("""\
                resources:
                  - deployment.yaml
                  - service.yaml
            """)
        )
        assert detect_render_mode(str(tmp_path)) == "raw"

    def test_returns_helm_inflated_when_helmCharts_present(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yaml").write_text(
            textwrap.dedent("""\
                helmCharts:
                  - name: my-chart
                    repo: https://charts.example.com
                    version: "1.2.3"
            """)
        )
        assert detect_render_mode(str(tmp_path)) == "helm-inflated"

    def test_returns_raw_when_helmCharts_empty_list(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yaml").write_text("helmCharts: []\n")
        assert detect_render_mode(str(tmp_path)) == "raw"

    def test_accepts_kustomization_yml_filename(self, tmp_path: Path) -> None:
        (tmp_path / "kustomization.yml").write_text("resources:\n  - svc.yaml\n")
        assert detect_render_mode(str(tmp_path)) == "raw"

    def test_raises_file_not_found_when_no_kustomization_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="No kustomization file found"):
            detect_render_mode(str(tmp_path))


# ---------------------------------------------------------------------------
# render_overlay
# ---------------------------------------------------------------------------

_SIMPLE_YAML = textwrap.dedent("""\
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: my-cm
      namespace: default
    data:
      key: value
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: my-app
      namespace: default
    spec:
      replicas: 1
""")


class TestRenderOverlay:
    def test_raw_mode_returns_parsed_docs(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SIMPLE_YAML

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            docs = render_overlay("/some/overlay", "raw")

        mock_run.assert_called_once_with(
            ["kustomize", "build", "/some/overlay"],
            capture_output=True,
            text=True,
        )
        assert len(docs) == 2
        assert docs[0]["kind"] == "ConfigMap"
        assert docs[1]["kind"] == "Deployment"

    def test_helm_inflated_mode_passes_enable_helm_flag(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _SIMPLE_YAML

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            render_overlay("/some/overlay", "helm-inflated")

        called_cmd = mock_run.call_args[0][0]
        assert "--enable-helm" in called_cmd

    def test_raises_runtime_error_on_nonzero_exit(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error: kustomize build failed"

        with (
            patch("subprocess.run", return_value=mock_result),
            pytest.raises(RuntimeError, match="kustomize build failed"),
        ):
            render_overlay("/bad/overlay", "raw")

    def test_skips_empty_yaml_documents(self) -> None:
        yaml_with_empty = "---\n---\napiVersion: v1\nkind: Namespace\nmetadata:\n  name: ns\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = yaml_with_empty

        with patch("subprocess.run", return_value=mock_result):
            docs = render_overlay("/some/overlay", "raw")

        assert len(docs) == 1
        assert docs[0]["kind"] == "Namespace"

    def test_raises_file_not_found_when_kustomize_missing(self) -> None:
        with (
            patch("subprocess.run", side_effect=FileNotFoundError("kustomize not found")),
            pytest.raises(FileNotFoundError, match="kustomize binary not found"),
        ):
            render_overlay("/some/overlay", "raw")
