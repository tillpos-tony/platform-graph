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
k8s_manifests = ["k8s/manifests/*"]
terraform_roots = ["terraform/"]
```

### K8s source types

graphsearch supports two ways to index Kubernetes resources:

**`k8s_overlays`** — Kustomize overlays rendered via `kustomize build`.  Each
entry must be a directory containing a `kustomization.yaml` (or `.yml` /
`Kustomization`).  Use this for any manifest tree that relies on Kustomize
patching or Helm chart inflation.

**`k8s_manifests`** — Raw manifest folders read directly, without running any
build tool.  Each entry is a directory of hand-written `*.yaml`/`*.yml` files.
graphsearch walks the tree recursively, splits multi-document files, and feeds
every resource doc through the same parse → upsert pipeline as overlays.  The
resulting graph nodes and edges are identical regardless of which source type
was used.

Both config keys accept shell-style glob wildcards (`*`, `?`, `[…]`).  A glob
entry expands to all matching subdirectories under the repo root; `env` is
derived from the last path component of each resolved directory (e.g.
`manifests/prod` → `env = "prod"`).

If a directory listed in `k8s_manifests` contains a `kustomization.yaml`,
graphsearch prints an advisory suggesting the path may belong in `k8s_overlays`
instead.  Malformed YAML files in a manifest tree are skipped with a warning;
the run still exits 0.
