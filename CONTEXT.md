# graphsearch

A CLI that indexes infrastructure-as-code relationships (Kubernetes, Terraform) into a Memgraph graph and answers reachability/blast-radius queries over it.

## Language

### K8s indexing sources

**K8s source**:
Any configured path that yields Kubernetes resource documents for indexing. Has exactly two subtypes — **Kustomize overlay** and **Raw manifest folder** — declared in separate config keys.

**Kustomize overlay**:
A directory containing a `kustomization.yaml` that is rendered via `kustomize build` to produce resource documents. Declared in the `k8s_overlays` config key.
_Avoid_: overlay (when ambiguous), kustomization.

**Raw manifest folder**:
A directory tree of hand-written, checked-in Kubernetes manifest files (`*.yaml`/`*.yml`) read directly — no `kustomization.yaml`, no `kustomize build`. Declared in the `k8s_manifests` config key.
_Avoid_: raw overlay, plain folder, manifest dump.

**Resource document** (or **doc**):
A single parsed Kubernetes YAML object (one `---`-separated document) with a `kind` and `metadata.name`. The shared unit both source subtypes produce and feed into parsing.

**env**:
The environment tag attached to every indexed resource, derived from the last path component of a configured K8s source (e.g. `manifests/prod` → `prod`). Identical derivation rule for both source subtypes.

**workspace**:
The named repository/codebase being indexed, set in `.graphsearch.toml`. Scopes every node so multiple repos can share one Memgraph instance.

## Relationships

- A **K8s source** is either a **Kustomize overlay** or a **Raw manifest folder** — never both for the same path.
- A **Kustomize overlay** produces **Resource documents** via `kustomize build`; a **Raw manifest folder** produces them by reading files directly.
- Both subtypes feed the *same* parse → upsert pipeline, so the resulting graph is indistinguishable by source subtype.
- Every **Resource document** is indexed under exactly one **workspace** and one **env**.

## Example dialogue

> **Dev:** "If a folder in `k8s_manifests` has a `kustomization.yaml` in it, do we render it?"
> **Domain expert:** "No. A **Raw manifest folder** is read directly — the `kustomization.yaml` is skipped and we warn that the path might belong in `k8s_overlays`. Rendering only happens for a **Kustomize overlay**."

## Flagged ambiguities

- "overlay" was used loosely for any K8s source — resolved: an **overlay** is specifically a **Kustomize overlay**; a **Raw manifest folder** is not an overlay.
