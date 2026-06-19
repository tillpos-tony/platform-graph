# Separate config key for raw manifest folders

To index hand-written Kubernetes manifests that have no `kustomization.yaml`, we add a dedicated `k8s_manifests` config key alongside the existing `k8s_overlays`, rather than auto-detecting render mode from the absence of a `kustomization.yaml` in an `k8s_overlays` entry. A separate key makes intent explicit at config time, keeps the `k8s_overlays` name accurate (overlays are always kustomize-rendered), and lets the indexer warn when a path is in the wrong list (e.g. a `kustomization.yaml` found under `k8s_manifests`).

## Considered Options

- **Auto-detect (single `k8s_overlays` key):** treat any entry lacking a `kustomization.yaml` as raw. Rejected — least config but silently indexes mistakenly-listed folders, and makes `k8s_overlays` a misnomer for non-overlay paths.
- **Typed entries** (`{path, mode}` objects): most flexible but breaks the simple string-list TOML format and complicates config parsing. Rejected as over-engineered for two well-defined subtypes.

## Consequences

- `GraphsearchConfig` gains a `k8s_manifests: list[str]` field defaulting to `[]`, so existing `.graphsearch.toml` files keep working untouched.
- Both source subtypes feed the same parse → upsert pipeline, so the resulting graph is indistinguishable by source — only the ingestion path differs.
