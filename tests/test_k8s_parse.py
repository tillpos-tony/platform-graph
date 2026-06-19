"""Tests for graphsearch.k8s.parse — node and edge extraction from K8s YAML."""

from __future__ import annotations

from graphsearch.k8s.parse import parse_resources, resource_key

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

WS = "my-workspace"
ENV = "prod"


def _deployment(name: str, namespace: str = "default", labels: dict | None = None) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "metadata": {"labels": labels or {"app": name}},
                "spec": {"containers": [{"name": "main", "image": f"{name}:latest"}]},
            }
        },
    }


def _service(name: str, namespace: str = "default", selector: dict | None = None) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": selector or {"app": name}},
    }


def _ingress(name: str, namespace: str = "default", backend_svc: str = "my-svc") -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "backend": {
                                    "service": {
                                        "name": backend_svc,
                                        "port": {"number": 80},
                                    }
                                },
                            }
                        ]
                    }
                }
            ]
        },
    }


def _configmap(name: str, namespace: str = "default") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": {"key": "value"},
    }


def _secret(name: str, namespace: str = "default") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
    }


def _serviceaccount(name: str, namespace: str = "default") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": name, "namespace": namespace},
    }


def _edges_of_type(edges, rel_type: str):
    return [e for e in edges if e.rel_type == rel_type]


# ---------------------------------------------------------------------------
# resource_key
# ---------------------------------------------------------------------------


class TestResourceKey:
    def test_returns_namespace_name_kind_tuple(self) -> None:
        doc = {"kind": "Deployment", "metadata": {"name": "app", "namespace": "prod"}}
        assert resource_key(doc) == ("prod", "app", "Deployment")

    def test_cluster_scoped_has_empty_namespace(self) -> None:
        doc = {"kind": "ClusterRole", "metadata": {"name": "admin"}}
        assert resource_key(doc) == ("", "admin", "ClusterRole")


# ---------------------------------------------------------------------------
# parse_resources — nodes
# ---------------------------------------------------------------------------


class TestParseNodes:
    def test_creates_k8s_resource_node_per_doc(self) -> None:
        docs = [_deployment("app"), _service("app-svc")]
        nodes, _ = parse_resources(docs, WS, ENV)
        assert len(nodes) == 2

    def test_node_carries_workspace_and_env(self) -> None:
        docs = [_deployment("app")]
        nodes, _ = parse_resources(docs, WS, ENV)
        node = nodes[0]
        assert node.workspace == WS
        assert node.env == ENV

    def test_node_identity_includes_kind_namespace_name(self) -> None:
        docs = [_deployment("my-app", namespace="ns-a")]
        nodes, _ = parse_resources(docs, WS, ENV)
        node = nodes[0]
        identity = node.identity_props
        assert identity["kind"] == "Deployment"
        assert identity["namespace"] == "ns-a"
        assert identity["name"] == "my-app"

    def test_node_labels_include_kind(self) -> None:
        docs = [_deployment("app")]
        nodes, _ = parse_resources(docs, WS, ENV)
        node = nodes[0]
        assert "K8sResource" in node.labels
        assert "Deployment" in node.labels

    def test_skips_docs_without_kind_or_name(self) -> None:
        docs = [
            {"apiVersion": "v1", "metadata": {"name": "x"}},  # no kind
            {"apiVersion": "v1", "kind": "Service"},  # no name
        ]
        nodes, _ = parse_resources(docs, WS, ENV)
        assert nodes == []


# ---------------------------------------------------------------------------
# parse_resources — SELECTS edges
# ---------------------------------------------------------------------------


class TestSelectsEdges:
    def test_service_selects_matching_deployment(self) -> None:
        docs = [
            _deployment("app", labels={"app": "app"}),
            _service("app-svc", selector={"app": "app"}),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        selects = _edges_of_type(edges, "SELECTS")
        assert len(selects) == 1
        assert selects[0].from_node.name == "app-svc"
        assert selects[0].to_node.name == "app"

    def test_selector_must_be_subset_of_pod_labels(self) -> None:
        docs = [
            _deployment("app", labels={"app": "app", "version": "v1"}),
            _service("app-svc", selector={"app": "app"}),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        selects = _edges_of_type(edges, "SELECTS")
        assert len(selects) == 1

    def test_no_selects_edge_when_labels_do_not_match(self) -> None:
        docs = [
            _deployment("app", labels={"app": "other"}),
            _service("app-svc", selector={"app": "app"}),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        selects = _edges_of_type(edges, "SELECTS")
        assert len(selects) == 0

    def test_no_selects_edge_when_selector_empty(self) -> None:
        svc = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "svc", "namespace": "default"},
            "spec": {},  # no selector
        }
        docs = [_deployment("app"), svc]
        _, edges = parse_resources(docs, WS, ENV)
        selects = _edges_of_type(edges, "SELECTS")
        assert len(selects) == 0

    def test_no_cross_namespace_selects(self) -> None:
        docs = [
            _deployment("app", namespace="ns-a"),
            _service("app-svc", namespace="ns-b", selector={"app": "app"}),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        selects = _edges_of_type(edges, "SELECTS")
        assert len(selects) == 0


# ---------------------------------------------------------------------------
# parse_resources — ROUTES_TO edges
# ---------------------------------------------------------------------------


class TestRoutesToEdges:
    def test_ingress_routes_to_backend_service(self) -> None:
        docs = [
            _service("my-svc"),
            _ingress("my-ing", backend_svc="my-svc"),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        routes = _edges_of_type(edges, "ROUTES_TO")
        assert len(routes) == 1
        assert routes[0].from_node.name == "my-ing"
        assert routes[0].to_node.name == "my-svc"

    def test_creates_placeholder_node_for_unknown_service(self) -> None:
        docs = [_ingress("my-ing", backend_svc="ghost-svc")]
        _, edges = parse_resources(docs, WS, ENV)
        routes = _edges_of_type(edges, "ROUTES_TO")
        assert len(routes) == 1
        assert routes[0].to_node.name == "ghost-svc"
        assert routes[0].to_node.kind == "Service"

    def test_deduplicates_same_backend_service_across_rules(self) -> None:
        ing = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": "ing", "namespace": "default"},
            "spec": {
                "rules": [
                    {
                        "http": {
                            "paths": [
                                {
                                    "path": "/a",
                                    "backend": {"service": {"name": "svc", "port": {"number": 80}}},
                                },
                                {
                                    "path": "/b",
                                    "backend": {"service": {"name": "svc", "port": {"number": 80}}},
                                },
                            ]
                        }
                    }
                ]
            },
        }
        _, edges = parse_resources([ing], WS, ENV)
        routes = _edges_of_type(edges, "ROUTES_TO")
        assert len(routes) == 1  # deduplicated


# ---------------------------------------------------------------------------
# parse_resources — MOUNTS edges
# ---------------------------------------------------------------------------


class TestMountsEdges:
    def test_mounts_configmap_from_volumes(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["volumes"] = [
            {"name": "config-vol", "configMap": {"name": "my-cm"}}
        ]
        docs = [deploy, _configmap("my-cm")]
        _, edges = parse_resources(docs, WS, ENV)
        mounts = _edges_of_type(edges, "MOUNTS")
        assert len(mounts) == 1
        assert mounts[0].to_node.name == "my-cm"
        assert mounts[0].to_node.kind == "ConfigMap"

    def test_mounts_secret_from_volumes(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["volumes"] = [
            {"name": "sec-vol", "secret": {"secretName": "my-secret"}}
        ]
        docs = [deploy, _secret("my-secret")]
        _, edges = parse_resources(docs, WS, ENV)
        mounts = _edges_of_type(edges, "MOUNTS")
        assert len(mounts) == 1
        assert mounts[0].to_node.name == "my-secret"
        assert mounts[0].to_node.kind == "Secret"

    def test_mounts_configmap_from_env_from(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["envFrom"] = [{"configMapRef": {"name": "app-config"}}]
        docs = [deploy]
        _, edges = parse_resources(docs, WS, ENV)
        mounts = _edges_of_type(edges, "MOUNTS")
        assert len(mounts) == 1
        assert mounts[0].to_node.name == "app-config"
        assert mounts[0].to_node.kind == "ConfigMap"

    def test_mounts_secret_from_env_from(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["envFrom"] = [{"secretRef": {"name": "app-secrets"}}]
        docs = [deploy]
        _, edges = parse_resources(docs, WS, ENV)
        mounts = _edges_of_type(edges, "MOUNTS")
        assert len(mounts) == 1
        assert mounts[0].to_node.name == "app-secrets"
        assert mounts[0].to_node.kind == "Secret"

    def test_deduplicates_same_configmap_across_volumes_and_envfrom(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["volumes"] = [
            {"name": "v", "configMap": {"name": "shared-cm"}}
        ]
        deploy["spec"]["template"]["spec"]["envFrom"] = [{"configMapRef": {"name": "shared-cm"}}]
        docs = [deploy]
        _, edges = parse_resources(docs, WS, ENV)
        mounts = _edges_of_type(edges, "MOUNTS")
        assert len(mounts) == 1  # deduplicated


# ---------------------------------------------------------------------------
# parse_resources — USES_SA edges
# ---------------------------------------------------------------------------


class TestUsesSaEdges:
    def test_workload_uses_service_account(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["serviceAccountName"] = "my-sa"
        docs = [deploy, _serviceaccount("my-sa")]
        _, edges = parse_resources(docs, WS, ENV)
        uses_sa = _edges_of_type(edges, "USES_SA")
        assert len(uses_sa) == 1
        assert uses_sa[0].from_node.name == "app"
        assert uses_sa[0].to_node.name == "my-sa"

    def test_no_uses_sa_when_no_service_account_name(self) -> None:
        docs = [_deployment("app")]
        _, edges = parse_resources(docs, WS, ENV)
        uses_sa = _edges_of_type(edges, "USES_SA")
        assert len(uses_sa) == 0

    def test_creates_placeholder_sa_node_when_not_in_docs(self) -> None:
        deploy = _deployment("app")
        deploy["spec"]["template"]["spec"]["serviceAccountName"] = "implicit-sa"
        docs = [deploy]
        _, edges = parse_resources(docs, WS, ENV)
        uses_sa = _edges_of_type(edges, "USES_SA")
        assert len(uses_sa) == 1
        assert uses_sa[0].to_node.name == "implicit-sa"
        assert uses_sa[0].to_node.kind == "ServiceAccount"


# ---------------------------------------------------------------------------
# StatefulSet and DaemonSet also produce edges
# ---------------------------------------------------------------------------


class TestWorkloadKindCoverage:
    def _make_workload(self, kind: str, name: str = "wl") -> dict:
        return {
            "apiVersion": "apps/v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": "default"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "serviceAccountName": "my-sa",
                        "envFrom": [{"configMapRef": {"name": "cfg"}}],
                    },
                }
            },
        }

    def test_statefulset_produces_uses_sa_and_mounts(self) -> None:
        docs = [self._make_workload("StatefulSet")]
        _, edges = parse_resources(docs, WS, ENV)
        assert _edges_of_type(edges, "USES_SA")
        assert _edges_of_type(edges, "MOUNTS")

    def test_daemonset_produces_uses_sa_and_mounts(self) -> None:
        docs = [self._make_workload("DaemonSet")]
        _, edges = parse_resources(docs, WS, ENV)
        assert _edges_of_type(edges, "USES_SA")
        assert _edges_of_type(edges, "MOUNTS")
