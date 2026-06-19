# platform-graph

Index and query codebase relationships in Memgraph.

## Install

```bash
pipx install .
```

## Usage

```bash
# One-time setup: write .platform-graph.toml
platform-graph init

# Index the workspace into Memgraph
platform-graph index

# Query: list all indexed workspaces
platform-graph query list-workspaces

# Query: blast radius from a workload (all reachable targets via CAN_REACH)
platform-graph query blast-radius \
  --param workspace=my-repo \
  --param env=prod \
  --param kind=Deployment \
  --param namespace=default \
  --param name=api

# Query: which workloads can reach a given RBAC capability
platform-graph query who-can-reach-capability \
  --param resource=secrets \
  --param verb=get

# Query: all explicit network-reachability edges
platform-graph query network-reachability

# Query: raw Cypher
platform-graph query cypher --cypher "MATCH (n:K8sResource) RETURN n.name LIMIT 10"

# Output as JSON
platform-graph query list-workspaces --json-output
```

## Config

`platform-graph init` writes `.platform-graph.toml` at the repo root:

```toml
workspace = "my-repo"
bolt_uri = "bolt://127.0.0.1:7687"
k8s_overlays = ["k8s/overlays/prod"]
terraform_roots = ["terraform/"]
```
