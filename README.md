# graphsearch

Index and query codebase relationships in Memgraph.

## Install

```bash
pipx install ./graphsearch
```

## Usage

```bash
# One-time setup: write .graphsearch.toml
graphsearch init

# Index the workspace into Memgraph
graphsearch index

# Query: list all indexed workspaces
graphsearch query list-workspaces

# Query: blast radius from a workload (all reachable targets via CAN_REACH)
graphsearch query blast-radius \
  --param workspace=my-repo \
  --param env=prod \
  --param kind=Deployment \
  --param namespace=default \
  --param name=api

# Query: which workloads can reach a given RBAC capability
graphsearch query who-can-reach-capability \
  --param resource=secrets \
  --param verb=get

# Query: all explicit network-reachability edges
graphsearch query network-reachability

# Query: raw Cypher
graphsearch query cypher --cypher "MATCH (n:K8sResource) RETURN n.name LIMIT 10"

# Output as JSON
graphsearch query list-workspaces --json-output
```

## Config

`graphsearch init` writes `.graphsearch.toml` at the repo root:

```toml
workspace = "my-repo"
bolt_uri = "bolt://127.0.0.1:7687"
k8s_overlays = ["k8s/overlays/prod"]
terraform_roots = ["terraform/"]
```
