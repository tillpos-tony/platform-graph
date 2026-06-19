"""platform-graph CLI entry point.

Subcommands
-----------
init    Write .platform-graph.toml with prompts. Does not clobber an existing file.
index   Read config and index the workspace into Memgraph.
query   Read config and query the graph.

Query modes
-----------
platform-graph query <named-query> [--param key=value ...] [--json-output]
platform-graph query cypher --cypher "MATCH ... RETURN ..." [--json-output]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from platform_graph.config import PlatformGraphConfig, load_config, write_config
from platform_graph.db import connect
from platform_graph.k8s.index import index_k8s
from platform_graph.terraform.index import index_terraform

# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Interactively write .platform-graph.toml, refusing to clobber an existing file."""
    repo_root = _git_root()
    config_path = repo_root / ".platform-graph.toml"

    if config_path.exists():
        print(
            f"platform-graph init: {config_path} already exists. "
            "Delete it first if you want to reinitialise.",
            file=sys.stderr,
        )
        return 1

    print("platform-graph init — setting up this workspace.")
    print()

    workspace = input(f"Workspace name [{repo_root.name}]: ").strip() or repo_root.name
    bolt_uri = input("Bolt URI [bolt://127.0.0.1:7687]: ").strip() or "bolt://127.0.0.1:7687"

    print(
        "K8s overlay paths (comma-separated, relative to repo root, leave blank to skip):"
    )
    k8s_raw = input("> ").strip()
    k8s_overlays = [p.strip() for p in k8s_raw.split(",") if p.strip()] if k8s_raw else []

    print(
        "Terraform root paths (comma-separated, relative to repo root, leave blank to skip):"
    )
    tf_raw = input("> ").strip()
    terraform_roots = [p.strip() for p in tf_raw.split(",") if p.strip()] if tf_raw else []

    config = PlatformGraphConfig(
        workspace=workspace,
        bolt_uri=bolt_uri,
        k8s_overlays=k8s_overlays,
        terraform_roots=terraform_roots,
    )
    written = write_config(config, repo_root)

    print()
    print(f"platform-graph init: config written to {written}")
    print(f"  workspace      = {workspace}")
    print(f"  bolt_uri       = {bolt_uri}")
    print(f"  k8s_overlays   = {k8s_overlays}")
    print(f"  terraform_roots = {terraform_roots}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: index
# ---------------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    """Index the workspace into Memgraph (Terraform + K8s)."""
    repo_root = _git_root()
    config = load_config(repo_root)

    if config is None:
        print(
            "platform-graph index: no .platform-graph.toml found. "
            "Run `platform-graph init` first.",
            file=sys.stderr,
        )
        return 1

    print(f"platform-graph index: workspace={config.workspace!r}")

    if not config.terraform_roots and not config.k8s_overlays:
        print("platform-graph index: nothing to index (no terraform_roots or k8s_overlays configured).")
        return 0

    driver = connect(config.bolt_uri)
    try:
        with driver.session() as session:
            if config.terraform_roots:
                index_terraform(config, session, repo_root)
            if config.k8s_overlays:
                index_k8s(config, session, repo_root)
    finally:
        driver.close()

    print("platform-graph index: done.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: query
# ---------------------------------------------------------------------------


def _parse_params(raw: list[str]) -> dict[str, str]:
    """Parse a list of 'key=value' strings into a dict."""
    params: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise ValueError(f"Invalid --param format {item!r}. Expected key=value.")
        k, _, v = item.partition("=")
        params[k.strip()] = v
    return params


def _print_results(rows: list[dict], json_output: bool) -> None:
    """Print *rows* either as JSON or as a plain-text table."""
    if not rows:
        print("(no results)")
        return

    if json_output:
        print(json.dumps(rows, indent=2, default=str))
        return

    # Simple column-aligned table — no external dependency needed
    columns = list(rows[0].keys())
    col_widths = {col: max(len(col), *(len(str(row.get(col, ""))) for row in rows))
                  for col in columns}

    # Header
    header = "  ".join(col.ljust(col_widths[col]) for col in columns)
    separator = "  ".join("-" * col_widths[col] for col in columns)
    print(header)
    print(separator)
    for row in rows:
        print("  ".join(str(row.get(col, "")).ljust(col_widths[col]) for col in columns))


def cmd_query(args: argparse.Namespace) -> int:
    """Query the graph: named query or raw Cypher."""
    from platform_graph.queries import REGISTRY, run_named

    repo_root = _git_root()
    config = load_config(repo_root)

    if config is None:
        print(
            "platform-graph query: no .platform-graph.toml found. "
            "Run `platform-graph init` first.",
            file=sys.stderr,
        )
        return 1

    query_name: str = args.query_name
    json_output: bool = getattr(args, "json_output", False)

    # ------ mode: raw Cypher ------
    if query_name == "cypher":
        cypher_stmt: str | None = getattr(args, "cypher", None)
        if not cypher_stmt:
            print(
                "platform-graph query cypher: --cypher <statement> is required.",
                file=sys.stderr,
            )
            return 1
        try:
            driver = connect(config.bolt_uri)
        except Exception as exc:  # noqa: BLE001
            print(
                f"platform-graph query: cannot connect to Memgraph at {config.bolt_uri!r}: {exc}",
                file=sys.stderr,
            )
            return 1
        try:
            with driver.session() as session:
                result = session.run(cypher_stmt)
                rows = [dict(record) for record in result]
        finally:
            driver.close()
        _print_results(rows, json_output)
        return 0

    # ------ mode: named query ------
    if query_name not in REGISTRY:
        available = ", ".join(sorted(REGISTRY))
        print(
            f"platform-graph query: unknown query {query_name!r}.\n"
            f"Available named queries: {available}\n"
            f"Or use: platform-graph query cypher --cypher '...'",
            file=sys.stderr,
        )
        return 1

    try:
        params = _parse_params(getattr(args, "param", None) or [])
    except ValueError as exc:
        print(f"platform-graph query: {exc}", file=sys.stderr)
        return 1

    try:
        driver = connect(config.bolt_uri)
    except Exception as exc:  # noqa: BLE001
        print(
            f"platform-graph query: cannot connect to Memgraph at {config.bolt_uri!r}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        with driver.session() as session:
            try:
                rows = run_named(session, query_name, params)
            except ValueError as exc:
                print(f"platform-graph query: {exc}", file=sys.stderr)
                return 1
    finally:
        driver.close()

    _print_results(rows, json_output)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_root() -> Path:
    """Return the git repo root, or cwd if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError:
        return Path.cwd()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="platform-graph",
        description="Index and query codebase relationships in Memgraph.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # init
    init_p = sub.add_parser("init", help="Write .platform-graph.toml for this workspace.")
    init_p.set_defaults(func=cmd_init)

    # index
    index_p = sub.add_parser("index", help="Index this workspace into Memgraph.")
    index_p.set_defaults(func=cmd_index)

    # query
    query_p = sub.add_parser(
        "query",
        help="Query the graph.",
        description=(
            "Run a named query or raw Cypher against the indexed graph.\n\n"
            "Named queries: list-workspaces, blast-radius, who-can-reach-capability, "
            "network-reachability\n\n"
            "Raw Cypher: platform-graph query cypher --cypher 'MATCH (n) RETURN n LIMIT 5'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    query_p.add_argument(
        "query_name",
        metavar="<query-name>",
        help=(
            "Named query to run, or 'cypher' for raw Cypher. "
            "Named queries: list-workspaces, blast-radius, "
            "who-can-reach-capability, network-reachability."
        ),
    )
    query_p.add_argument(
        "--param",
        metavar="key=value",
        action="append",
        dest="param",
        help="Query parameter (repeatable). E.g. --param workspace=my-repo.",
    )
    query_p.add_argument(
        "--cypher",
        metavar="<cypher-statement>",
        help="Raw Cypher statement (only used when query-name is 'cypher').",
    )
    query_p.add_argument(
        "--json-output",
        action="store_true",
        default=False,
        help="Emit results as a JSON array of objects instead of a table.",
    )
    query_p.set_defaults(func=cmd_query)

    parsed = parser.parse_args(argv)
    sys.exit(parsed.func(parsed))


if __name__ == "__main__":
    main()
