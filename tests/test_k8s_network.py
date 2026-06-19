"""Tests for graphsearch.k8s.network — CAN_REACH edge derivation.

Covers:
- NetworkPolicy ingress allow rules → CAN_REACH between workloads
- CiliumNetworkPolicy toFQDNs egress → ExternalEndpoint + CAN_REACH
- CiliumNetworkPolicy toEntities: world → ExternalEndpoint + CAN_REACH
- Default-deny (no ingress/egress rules) → no edges produced
"""

from __future__ import annotations

from graphsearch.k8s.network import derive_network_edges
from graphsearch.model import ExternalEndpoint

WS = "my-workspace"
ENV = "prod"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


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


def _network_policy(
    name: str,
    namespace: str = "default",
    pod_selector_labels: dict | None = None,
    ingress_from_pod_selector: dict | None = None,
) -> dict:
    """Build a minimal NetworkPolicy document.

    Parameters
    ----------
    name:
        Policy name.
    namespace:
        Policy namespace.
    pod_selector_labels:
        matchLabels for spec.podSelector (the target).  None/empty → select all pods.
    ingress_from_pod_selector:
        matchLabels for spec.ingress[0].from[0].podSelector (the source).
        None → no ingress rules (pure default-deny).
    """
    doc: dict = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": ({"matchLabels": pod_selector_labels} if pod_selector_labels else {}),
        },
    }
    if ingress_from_pod_selector is not None:
        doc["spec"]["ingress"] = [
            {"from": [{"podSelector": {"matchLabels": ingress_from_pod_selector}}]}
        ]
    return doc


def _cilium_policy(
    name: str,
    namespace: str = "default",
    endpoint_selector_labels: dict | None = None,
    to_fqdns: list[str] | None = None,
    to_entities: list[str] | None = None,
) -> dict:
    """Build a minimal CiliumNetworkPolicy document."""
    egress_rule: dict = {}
    if to_fqdns:
        egress_rule["toFQDNs"] = [{"matchName": fqdn} for fqdn in to_fqdns]
    if to_entities:
        egress_rule["toEntities"] = to_entities

    return {
        "apiVersion": "cilium.io/v2",
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "endpointSelector": {"matchLabels": endpoint_selector_labels or {}},
            "egress": [egress_rule] if (to_fqdns or to_entities) else [],
        },
    }


def _can_reach_edges(edges):
    return [e for e in edges if e.rel_type == "CAN_REACH"]


# ---------------------------------------------------------------------------
# NetworkPolicy — ingress allow rules → workload-to-workload CAN_REACH
# ---------------------------------------------------------------------------


class TestNetworkPolicyIngressAllow:
    def test_produces_can_reach_from_source_to_target(self) -> None:
        """Source workload matching ingress.from[] → CAN_REACH → target workload."""
        docs = [
            _deployment("frontend", labels={"app": "frontend"}),
            _deployment("backend", labels={"app": "backend"}),
            _network_policy(
                "allow-frontend",
                pod_selector_labels={"app": "backend"},
                ingress_from_pod_selector={"app": "frontend"},
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert len(can_reach) == 1
        assert can_reach[0].from_node.name == "frontend"
        assert can_reach[0].to_node.name == "backend"

    def test_edge_carries_policy_type_and_source_namespace(self) -> None:
        docs = [
            _deployment("frontend", labels={"app": "frontend"}),
            _deployment("backend", labels={"app": "backend"}),
            _network_policy(
                "allow-frontend",
                pod_selector_labels={"app": "backend"},
                ingress_from_pod_selector={"app": "frontend"},
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert can_reach[0].props["policy_type"] == "NetworkPolicy"
        assert can_reach[0].props["source_namespace"] == "default"

    def test_empty_pod_selector_on_policy_matches_all_workloads_in_namespace(self) -> None:
        """podSelector: {} means all pods in namespace are targets."""
        docs = [
            _deployment("frontend", labels={"app": "frontend"}),
            _deployment("backend", labels={"app": "backend"}),
            _network_policy(
                "allow-all-targets",
                pod_selector_labels=None,  # {} → select all
                ingress_from_pod_selector={"app": "frontend"},
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        # frontend → backend (not frontend → frontend self-loop)
        target_names = {e.to_node.name for e in can_reach}
        assert "backend" in target_names
        # Self-loop should NOT appear
        self_loops = [e for e in can_reach if e.from_node.name == e.to_node.name]
        assert self_loops == []

    def test_no_edges_when_source_selector_matches_nothing(self) -> None:
        """If no workload matches ingress.from podSelector, no edge is produced."""
        docs = [
            _deployment("backend", labels={"app": "backend"}),
            _network_policy(
                "allow-frontend",
                pod_selector_labels={"app": "backend"},
                ingress_from_pod_selector={"app": "frontend"},  # no such workload
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert _can_reach_edges(edges) == []

    def test_no_edges_from_default_deny_policy(self) -> None:
        """A NetworkPolicy with no ingress rules (default-deny) produces no CAN_REACH edges."""
        docs = [
            _deployment("backend", labels={"app": "backend"}),
            # No ingress rules → pure default-deny
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": "default"},
                "spec": {"podSelector": {}},
            },
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert _can_reach_edges(edges) == []

    def test_no_cross_namespace_edges(self) -> None:
        """Workloads in different namespaces do not match same-namespace policy."""
        docs = [
            _deployment("frontend", namespace="ns-a", labels={"app": "frontend"}),
            _deployment("backend", namespace="ns-b", labels={"app": "backend"}),
            _network_policy(
                "allow-frontend",
                namespace="ns-b",
                pod_selector_labels={"app": "backend"},
                ingress_from_pod_selector={"app": "frontend"},
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        # frontend is in ns-a, policy is in ns-b — no cross-namespace match
        assert _can_reach_edges(edges) == []

    def test_no_edges_for_non_policy_resources(self) -> None:
        """Documents that are not NetworkPolicy/CiliumNetworkPolicy are ignored."""
        docs = [
            _deployment("app", labels={"app": "app"}),
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "svc", "namespace": "default"},
            },
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert edges == []

    def test_multiple_ingress_from_peers(self) -> None:
        """Multiple peers in from[] each produce their own CAN_REACH edge."""
        policy: dict = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-multi", "namespace": "default"},
            "spec": {
                "podSelector": {"matchLabels": {"app": "backend"}},
                "ingress": [
                    {
                        "from": [
                            {"podSelector": {"matchLabels": {"app": "frontend"}}},
                            {"podSelector": {"matchLabels": {"app": "worker"}}},
                        ]
                    }
                ],
            },
        }
        docs = [
            _deployment("frontend", labels={"app": "frontend"}),
            _deployment("worker", labels={"app": "worker"}),
            _deployment("backend", labels={"app": "backend"}),
            policy,
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        source_names = {e.from_node.name for e in can_reach}
        assert source_names == {"frontend", "worker"}
        for e in can_reach:
            assert e.to_node.name == "backend"


# ---------------------------------------------------------------------------
# CiliumNetworkPolicy — toFQDNs egress → ExternalEndpoint
# ---------------------------------------------------------------------------


class TestCiliumToFQDNs:
    def test_to_fqdns_creates_external_endpoint_and_can_reach_edge(self) -> None:
        docs = [
            _deployment("payments", labels={"app": "payments"}),
            _cilium_policy(
                "payments-egress",
                endpoint_selector_labels={"app": "payments"},
                to_fqdns=["api.stripe.com"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert len(can_reach) == 1
        assert can_reach[0].from_node.name == "payments"
        to_node = can_reach[0].to_node
        assert isinstance(to_node, ExternalEndpoint)
        assert to_node.fqdn == "api.stripe.com"

    def test_external_endpoint_is_global_hub_no_workspace(self) -> None:
        """ExternalEndpoint identity_props must NOT include workspace."""
        docs = [
            _deployment("payments", labels={"app": "payments"}),
            _cilium_policy(
                "payments-egress",
                endpoint_selector_labels={"app": "payments"},
                to_fqdns=["api.stripe.com"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        to_node = can_reach[0].to_node
        assert "workspace" not in to_node.identity_props
        assert to_node.identity_props == {"fqdn": "api.stripe.com"}

    def test_multiple_fqdns_produce_multiple_edges(self) -> None:
        docs = [
            _deployment("payments", labels={"app": "payments"}),
            _cilium_policy(
                "payments-egress",
                endpoint_selector_labels={"app": "payments"},
                to_fqdns=["api.stripe.com", "hooks.stripe.com"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        fqdns = {e.to_node.fqdn for e in can_reach}
        assert fqdns == {"api.stripe.com", "hooks.stripe.com"}

    def test_edge_carries_cilium_policy_type(self) -> None:
        docs = [
            _deployment("payments", labels={"app": "payments"}),
            _cilium_policy(
                "payments-egress",
                endpoint_selector_labels={"app": "payments"},
                to_fqdns=["api.stripe.com"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert can_reach[0].props["policy_type"] == "CiliumNetworkPolicy"

    def test_no_edges_when_endpoint_selector_matches_nothing(self) -> None:
        """CiliumNetworkPolicy with endpointSelector that matches no workload → no edges."""
        docs = [
            _deployment("backend", labels={"app": "backend"}),
            _cilium_policy(
                "payments-egress",
                endpoint_selector_labels={"app": "payments"},  # no such workload
                to_fqdns=["api.stripe.com"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert _can_reach_edges(edges) == []


# ---------------------------------------------------------------------------
# CiliumNetworkPolicy — toEntities: world → ExternalEndpoint
# ---------------------------------------------------------------------------


class TestCiliumToEntities:
    def test_to_entities_world_creates_external_endpoint(self) -> None:
        docs = [
            _deployment("scraper", labels={"app": "scraper"}),
            _cilium_policy(
                "scraper-egress",
                endpoint_selector_labels={"app": "scraper"},
                to_entities=["world"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert len(can_reach) == 1
        to_node = can_reach[0].to_node
        assert isinstance(to_node, ExternalEndpoint)
        assert to_node.entity == "world"
        assert "workspace" not in to_node.identity_props

    def test_combined_to_fqdns_and_to_entities_in_same_rule(self) -> None:
        docs = [
            _deployment("scraper", labels={"app": "scraper"}),
            _cilium_policy(
                "scraper-egress",
                endpoint_selector_labels={"app": "scraper"},
                to_fqdns=["api.example.com"],
                to_entities=["world"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert len(can_reach) == 2
        endpoint_types = {
            ("fqdn", e.to_node.fqdn) if e.to_node.fqdn else ("entity", e.to_node.entity)
            for e in can_reach
        }
        assert ("fqdn", "api.example.com") in endpoint_types
        assert ("entity", "world") in endpoint_types

    def test_to_entities_edge_carries_cilium_policy_type(self) -> None:
        docs = [
            _deployment("scraper", labels={"app": "scraper"}),
            _cilium_policy(
                "scraper-egress",
                endpoint_selector_labels={"app": "scraper"},
                to_entities=["world"],
            ),
        ]
        edges = derive_network_edges(docs, WS, ENV)
        can_reach = _can_reach_edges(edges)
        assert can_reach[0].props["policy_type"] == "CiliumNetworkPolicy"


# ---------------------------------------------------------------------------
# No default-deny edges
# ---------------------------------------------------------------------------


class TestNoDefaultDeny:
    def test_cilium_policy_with_no_egress_rules_produces_no_edges(self) -> None:
        docs = [
            _deployment("backend", labels={"app": "backend"}),
            {
                "apiVersion": "cilium.io/v2",
                "kind": "CiliumNetworkPolicy",
                "metadata": {"name": "deny-all", "namespace": "default"},
                "spec": {
                    "endpointSelector": {"matchLabels": {"app": "backend"}},
                    "egress": [],
                },
            },
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert _can_reach_edges(edges) == []

    def test_network_policy_ingress_deny_produces_no_edges(self) -> None:
        docs = [
            _deployment("backend", labels={"app": "backend"}),
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "deny-ingress", "namespace": "default"},
                "spec": {
                    "podSelector": {"matchLabels": {"app": "backend"}},
                    # No ingress key → no ingress rules → default deny
                },
            },
        ]
        edges = derive_network_edges(docs, WS, ENV)
        assert _can_reach_edges(edges) == []


# ---------------------------------------------------------------------------
# Integration: derive_network_edges called via parse_resources
# ---------------------------------------------------------------------------


class TestIntegrationWithParseResources:
    def test_parse_resources_includes_can_reach_edges(self) -> None:
        """CAN_REACH edges appear in the output of parse_resources."""
        from graphsearch.k8s.parse import parse_resources

        docs = [
            _deployment("frontend", labels={"app": "frontend"}),
            _deployment("backend", labels={"app": "backend"}),
            _network_policy(
                "allow-frontend",
                pod_selector_labels={"app": "backend"},
                ingress_from_pod_selector={"app": "frontend"},
            ),
        ]
        _, edges = parse_resources(docs, WS, ENV)
        can_reach = [e for e in edges if e.rel_type == "CAN_REACH"]
        assert len(can_reach) == 1
        assert can_reach[0].from_node.name == "frontend"
        assert can_reach[0].to_node.name == "backend"
