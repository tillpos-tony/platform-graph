"""Tests for graphsearch.k8s.topology — scheduling/topology node and edge derivation.

Covers:
- nodeSelector → NodePool node and SCHEDULES_ON edge (one per key=value pair)
- nodeAffinity (requiredDuringScheduling, In operator) → NodePool + SCHEDULES_ON
- tolerations key=value → NodePool + SCHEDULES_ON; entries without value are skipped
- topologySpreadConstraints → TopologyKey node + SPREAD_ACROSS edge
- podAffinity labelSelector → CO_LOCATES_WITH edge between workloads
- podAntiAffinity labelSelector → ANTI_AFFINITY edge between workloads
- Non-workload resources are ignored
- NodePool identity includes workspace/env
- Integration: topology edges appear in parse_resources output
"""

from __future__ import annotations

import pytest

from graphsearch.k8s.topology import derive_topology_edges
from graphsearch.model import K8sResource, NodePool, TopologyKey

WS = "my-workspace"
ENV = "prod"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _deployment(
    name: str,
    namespace: str = "default",
    pod_labels: dict | None = None,
    node_selector: dict | None = None,
    node_affinity: dict | None = None,
    tolerations: list | None = None,
    topology_spread_constraints: list | None = None,
    pod_affinity: dict | None = None,
    pod_anti_affinity: dict | None = None,
) -> dict:
    """Build a minimal Deployment document with scheduling fields."""
    pod_spec: dict = {}
    if node_selector is not None:
        pod_spec["nodeSelector"] = node_selector
    if node_affinity is not None or pod_affinity is not None or pod_anti_affinity is not None:
        affinity: dict = {}
        if node_affinity is not None:
            affinity["nodeAffinity"] = node_affinity
        if pod_affinity is not None:
            affinity["podAffinity"] = pod_affinity
        if pod_anti_affinity is not None:
            affinity["podAntiAffinity"] = pod_anti_affinity
        pod_spec["affinity"] = affinity
    if tolerations is not None:
        pod_spec["tolerations"] = tolerations
    if topology_spread_constraints is not None:
        pod_spec["topologySpreadConstraints"] = topology_spread_constraints

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "template": {
                "metadata": {"labels": pod_labels or {"app": name}},
                "spec": pod_spec,
            }
        },
    }


def _required_node_affinity(expressions: list[dict]) -> dict:
    """Build nodeAffinity.requiredDuringScheduling from matchExpressions."""
    return {
        "requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": expressions}]
        }
    }


def _match_expr(key: str, operator: str, values: list[str]) -> dict:
    return {"key": key, "operator": operator, "values": values}


def _label_selector(match_labels: dict) -> dict:
    return {"matchLabels": match_labels}


def _edges_of_type(edges, rel_type: str):
    return [e for e in edges if e.rel_type == rel_type]


# ---------------------------------------------------------------------------
# nodeSelector → SCHEDULES_ON
# ---------------------------------------------------------------------------


class TestNodeSelector:
    def test_single_key_value_produces_one_node_and_one_edge(self) -> None:
        docs = [_deployment("app", node_selector={"node-type": "gpu"})]
        nodes, edges = derive_topology_edges(docs, WS, ENV)

        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        schedules_on = _edges_of_type(edges, "SCHEDULES_ON")

        assert len(pool_nodes) == 1
        assert pool_nodes[0].pool_key == "node-type"
        assert pool_nodes[0].pool_value == "gpu"
        assert len(schedules_on) == 1
        assert isinstance(schedules_on[0].from_node, K8sResource)
        assert schedules_on[0].from_node.name == "app"
        assert isinstance(schedules_on[0].to_node, NodePool)

    def test_multiple_key_value_pairs_produce_multiple_edges(self) -> None:
        docs = [
            _deployment(
                "app",
                node_selector={
                    "node-type": "gpu",
                    "zone": "us-east-1a",
                },
            )
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        schedules_on = _edges_of_type(edges, "SCHEDULES_ON")

        assert len(pool_nodes) == 2
        assert len(schedules_on) == 2
        keys = {n.pool_key for n in pool_nodes}
        assert keys == {"node-type", "zone"}

    def test_empty_node_selector_produces_no_edges(self) -> None:
        docs = [_deployment("app", node_selector={})]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "SCHEDULES_ON") == []

    def test_node_pool_identity_includes_workspace_and_env(self) -> None:
        docs = [_deployment("app", node_selector={"k": "v"})]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        pool = next(n for n in nodes if isinstance(n, NodePool))
        assert pool.identity_props["workspace"] == WS
        assert pool.identity_props["env"] == ENV
        assert pool.identity_props["pool_key"] == "k"
        assert pool.identity_props["pool_value"] == "v"


# ---------------------------------------------------------------------------
# nodeAffinity → SCHEDULES_ON
# ---------------------------------------------------------------------------


class TestNodeAffinity:
    def test_in_operator_produces_schedules_on_per_value(self) -> None:
        affinity = _required_node_affinity(
            [_match_expr("cloud.google.com/gke-nodepool", "In", ["high-mem", "standard"])]
        )
        docs = [_deployment("app", node_affinity=affinity)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)

        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        schedules_on = _edges_of_type(edges, "SCHEDULES_ON")

        assert len(pool_nodes) == 2
        assert len(schedules_on) == 2
        values = {n.pool_value for n in pool_nodes}
        assert values == {"high-mem", "standard"}
        assert all(n.pool_key == "cloud.google.com/gke-nodepool" for n in pool_nodes)

    def test_non_in_operator_is_skipped(self) -> None:
        affinity = _required_node_affinity(
            [_match_expr("key", "NotIn", ["value"])]
        )
        docs = [_deployment("app", node_affinity=affinity)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "SCHEDULES_ON") == []

    def test_multiple_terms_produce_all_edges(self) -> None:
        """Multiple nodeSelectorTerms (OR'd) — all produce edges."""
        affinity = {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [_match_expr("zone", "In", ["a"])]},
                    {"matchExpressions": [_match_expr("zone", "In", ["b"])]},
                ]
            }
        }
        docs = [_deployment("app", node_affinity=affinity)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        assert len(pool_nodes) == 2
        values = {n.pool_value for n in pool_nodes}
        assert values == {"a", "b"}


# ---------------------------------------------------------------------------
# Tolerations → SCHEDULES_ON
# ---------------------------------------------------------------------------


class TestTolerations:
    def test_key_value_toleration_produces_node_and_edge(self) -> None:
        tolerations = [{"key": "dedicated", "value": "gpu", "operator": "Equal", "effect": "NoSchedule"}]
        docs = [_deployment("app", tolerations=tolerations)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)

        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        schedules_on = _edges_of_type(edges, "SCHEDULES_ON")

        assert len(pool_nodes) == 1
        assert pool_nodes[0].pool_key == "dedicated"
        assert pool_nodes[0].pool_value == "gpu"
        assert len(schedules_on) == 1

    def test_toleration_without_value_is_skipped(self) -> None:
        """Exists-only tolerations (no value) produce no edge."""
        tolerations = [{"key": "dedicated", "operator": "Exists"}]
        docs = [_deployment("app", tolerations=tolerations)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "SCHEDULES_ON") == []

    def test_toleration_without_key_is_skipped(self) -> None:
        tolerations = [{"value": "some-value", "operator": "Equal"}]
        docs = [_deployment("app", tolerations=tolerations)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "SCHEDULES_ON") == []

    def test_mixed_tolerations_only_keyed_ones_produce_edges(self) -> None:
        tolerations = [
            {"key": "dedicated", "value": "gpu", "operator": "Equal"},
            {"operator": "Exists"},  # no key, skip
        ]
        docs = [_deployment("app", tolerations=tolerations)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        schedules_on = _edges_of_type(edges, "SCHEDULES_ON")
        assert len(schedules_on) == 1
        assert schedules_on[0].to_node.pool_key == "dedicated"


# ---------------------------------------------------------------------------
# topologySpreadConstraints → SPREAD_ACROSS
# ---------------------------------------------------------------------------


class TestTopologySpread:
    def test_single_constraint_produces_topology_key_and_edge(self) -> None:
        constraints = [
            {"maxSkew": 1, "topologyKey": "kubernetes.io/hostname", "whenUnsatisfiable": "DoNotSchedule"}
        ]
        docs = [_deployment("app", topology_spread_constraints=constraints)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)

        tk_nodes = [n for n in nodes if isinstance(n, TopologyKey)]
        spread = _edges_of_type(edges, "SPREAD_ACROSS")

        assert len(tk_nodes) == 1
        assert tk_nodes[0].topology_key == "kubernetes.io/hostname"
        assert len(spread) == 1
        assert isinstance(spread[0].from_node, K8sResource)
        assert spread[0].from_node.name == "app"
        assert isinstance(spread[0].to_node, TopologyKey)

    def test_multiple_different_keys_produce_separate_nodes(self) -> None:
        constraints = [
            {"topologyKey": "kubernetes.io/hostname"},
            {"topologyKey": "topology.kubernetes.io/zone"},
        ]
        docs = [_deployment("app", topology_spread_constraints=constraints)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)

        tk_nodes = [n for n in nodes if isinstance(n, TopologyKey)]
        spread = _edges_of_type(edges, "SPREAD_ACROSS")

        assert len(tk_nodes) == 2
        assert len(spread) == 2
        keys = {n.topology_key for n in tk_nodes}
        assert keys == {"kubernetes.io/hostname", "topology.kubernetes.io/zone"}

    def test_topology_key_identity_includes_workspace_env(self) -> None:
        constraints = [{"topologyKey": "kubernetes.io/hostname"}]
        docs = [_deployment("app", topology_spread_constraints=constraints)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        tk = next(n for n in nodes if isinstance(n, TopologyKey))
        assert tk.identity_props["workspace"] == WS
        assert tk.identity_props["env"] == ENV
        assert tk.identity_props["topology_key"] == "kubernetes.io/hostname"

    def test_constraint_without_topology_key_is_skipped(self) -> None:
        constraints = [{"maxSkew": 1, "whenUnsatisfiable": "DoNotSchedule"}]
        docs = [_deployment("app", topology_spread_constraints=constraints)]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "SPREAD_ACROSS") == []


# ---------------------------------------------------------------------------
# podAntiAffinity → ANTI_AFFINITY
# ---------------------------------------------------------------------------


class TestPodAntiAffinity:
    def test_required_anti_affinity_label_selector_match(self) -> None:
        docs = [
            _deployment("app-a", pod_labels={"app": "frontend"}),
            _deployment(
                "app-b",
                pod_labels={"app": "backend"},
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "frontend"}),
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        anti = _edges_of_type(edges, "ANTI_AFFINITY")

        assert len(anti) == 1
        assert anti[0].from_node.name == "app-b"
        assert anti[0].to_node.name == "app-a"

    def test_preferred_anti_affinity_label_selector_match(self) -> None:
        docs = [
            _deployment("cache", pod_labels={"role": "cache"}),
            _deployment(
                "worker",
                pod_labels={"role": "worker"},
                pod_anti_affinity={
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 100,
                            "podAffinityTerm": {
                                "topologyKey": "kubernetes.io/hostname",
                                "labelSelector": _label_selector({"role": "cache"}),
                            },
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        anti = _edges_of_type(edges, "ANTI_AFFINITY")

        assert len(anti) == 1
        assert anti[0].from_node.name == "worker"
        assert anti[0].to_node.name == "cache"

    def test_no_match_produces_no_anti_affinity_edge(self) -> None:
        docs = [
            _deployment("app-a", pod_labels={"app": "alpha"}),
            _deployment(
                "app-b",
                pod_labels={"app": "beta"},
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "gamma"}),
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "ANTI_AFFINITY") == []

    def test_workload_not_anti_affinity_with_itself(self) -> None:
        """A workload must not generate an edge to itself."""
        docs = [
            _deployment(
                "app",
                pod_labels={"app": "myapp"},
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "myapp"}),
                        }
                    ]
                },
            )
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        # No self-edge
        anti = _edges_of_type(edges, "ANTI_AFFINITY")
        assert len(anti) == 0

    def test_empty_label_selector_is_skipped(self) -> None:
        docs = [
            _deployment("app-a", pod_labels={"app": "a"}),
            _deployment(
                "app-b",
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": {},
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert _edges_of_type(edges, "ANTI_AFFINITY") == []


# ---------------------------------------------------------------------------
# podAffinity → CO_LOCATES_WITH
# ---------------------------------------------------------------------------


class TestPodAffinity:
    def test_pod_affinity_produces_co_locates_with(self) -> None:
        docs = [
            _deployment("db", pod_labels={"app": "db"}),
            _deployment(
                "cache",
                pod_labels={"app": "cache"},
                pod_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "db"}),
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        co = _edges_of_type(edges, "CO_LOCATES_WITH")

        assert len(co) == 1
        assert co[0].from_node.name == "cache"
        assert co[0].to_node.name == "db"

    def test_preferred_pod_affinity_produces_co_locates_with(self) -> None:
        docs = [
            _deployment("api", pod_labels={"tier": "api"}),
            _deployment(
                "frontend",
                pod_labels={"tier": "frontend"},
                pod_affinity={
                    "preferredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "weight": 50,
                            "podAffinityTerm": {
                                "topologyKey": "kubernetes.io/hostname",
                                "labelSelector": _label_selector({"tier": "api"}),
                            },
                        }
                    ]
                },
            ),
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        co = _edges_of_type(edges, "CO_LOCATES_WITH")

        assert len(co) == 1
        assert co[0].from_node.name == "frontend"
        assert co[0].to_node.name == "api"


# ---------------------------------------------------------------------------
# Non-workload kinds are ignored
# ---------------------------------------------------------------------------


class TestNonWorkloadIgnored:
    def test_service_is_not_parsed_for_scheduling(self) -> None:
        docs = [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "svc", "namespace": "default"},
                "spec": {"selector": {"app": "myapp"}},
            }
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert nodes == []
        assert edges == []

    def test_config_map_is_ignored(self) -> None:
        docs = [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "cfg", "namespace": "default"},
                "data": {"key": "value"},
            }
        ]
        nodes, edges = derive_topology_edges(docs, WS, ENV)
        assert nodes == []
        assert edges == []


# ---------------------------------------------------------------------------
# Integration: topology edges appear in parse_resources output
# ---------------------------------------------------------------------------


class TestIntegrationWithParseResources:
    def test_parse_resources_includes_topology_nodes_and_edges(self) -> None:
        from graphsearch.k8s.parse import parse_resources

        docs = [
            _deployment(
                "app",
                node_selector={"node-type": "gpu"},
                topology_spread_constraints=[{"topologyKey": "kubernetes.io/hostname"}],
            )
        ]
        nodes, edges = parse_resources(docs, WS, ENV)

        pool_nodes = [n for n in nodes if isinstance(n, NodePool)]
        tk_nodes = [n for n in nodes if isinstance(n, TopologyKey)]
        schedules_on = [e for e in edges if e.rel_type == "SCHEDULES_ON"]
        spread = [e for e in edges if e.rel_type == "SPREAD_ACROSS"]

        assert len(pool_nodes) == 1
        assert len(tk_nodes) == 1
        assert len(schedules_on) == 1
        assert len(spread) == 1

    def test_parse_resources_anti_affinity_and_structural_coexist(self) -> None:
        from graphsearch.k8s.parse import parse_resources

        docs = [
            _deployment("db", pod_labels={"app": "db"}),
            _deployment(
                "api",
                pod_labels={"app": "api"},
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "db"}),
                        }
                    ]
                },
            ),
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "api-svc", "namespace": "default"},
                "spec": {"selector": {"app": "api"}},
            },
        ]
        nodes, edges = parse_resources(docs, WS, ENV)
        rel_types = {e.rel_type for e in edges}
        assert "ANTI_AFFINITY" in rel_types
        assert "SELECTS" in rel_types

    def test_all_scheduling_edge_types_present(self) -> None:
        from graphsearch.k8s.parse import parse_resources

        docs = [
            _deployment("target", pod_labels={"app": "target"}),
            _deployment(
                "source",
                pod_labels={"app": "source"},
                node_selector={"pool": "general"},
                tolerations=[{"key": "dedicated", "value": "gpu", "operator": "Equal"}],
                topology_spread_constraints=[{"topologyKey": "kubernetes.io/hostname"}],
                pod_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "target"}),
                        }
                    ]
                },
                pod_anti_affinity={
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": _label_selector({"app": "target"}),
                        }
                    ]
                },
            ),
        ]
        nodes, edges = parse_resources(docs, WS, ENV)
        rel_types = {e.rel_type for e in edges}
        assert "SCHEDULES_ON" in rel_types
        assert "SPREAD_ACROSS" in rel_types
        assert "CO_LOCATES_WITH" in rel_types
        assert "ANTI_AFFINITY" in rel_types
