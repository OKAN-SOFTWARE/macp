"""Cross-project dependency graph for MaestroACP.

Some projects are platforms/libraries that others consume — e.g. **advote**'s
feedback SDK is used by DripCoffee. When such an upstream project changes, the
downstream projects must be re-checked so an API/contract change doesn't silently
break them. A consumer declares its upstreams in `<root>/.macp/config.toml`:

    name       = "dripcoffee"
    depends_on = ["advote"]          # this project consumes advote

    # and advote's own config can advertise what it exposes:
    name     = "advote"
    provides = "advote-feedback-sdk"

This module scans .macp-managed projects, builds the graph, computes the impact
(transitive downstream closure) of a change, and emits follow-up integration
tasks the planner/orchestrator can dispatch to each affected project.
"""
from __future__ import annotations

import datetime as dt
import glob
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from . import config as C

# Where managed projects live (each has a `.macp/`). Override via scan(roots=...).
DEFAULT_ROOTS = [Path.home() / "Projects", Path.home() / "Worker"]
# Vault whose Todo/items propagate tasks land in. start= is
# paths.vault_search_start(): the engine root in a checkout (the repo this
# package lives in, byte-identical to the former parents[2] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
VAULT_ROOT = paths.vault_root(start=paths.vault_search_start())


@dataclass
class ProjectNode:
    name: str
    root: str
    provides: str = ""
    depends_on: list[str] = field(default_factory=list)


def project_name(root: Path | str, cfg: "C.ProjectConfig | None") -> str:
    """Canonical id: the config's `name`, else the directory basename."""
    if cfg and cfg.name:
        return cfg.name
    return Path(root).name


def scan(roots: list[Path | str] | None = None, max_depth: int = 2) -> dict[str, ProjectNode]:
    """Discover .macp-managed projects under `roots` → {name: ProjectNode}.
    Looks for `<root>[/*[/*]]/.macp/config.toml` (depth 0..max_depth)."""
    roots = roots if roots is not None else DEFAULT_ROOTS
    nodes: dict[str, ProjectNode] = {}
    seen_roots: set[str] = set()
    for r in roots:
        base = Path(r)
        patterns = [base / C.PROJECT_DIR / "config.toml"]
        for d in range(1, max_depth + 1):
            patterns.append(base.joinpath(*(["*"] * d)) / C.PROJECT_DIR / "config.toml")
        for pat in patterns:
            for cfgfile in glob.glob(str(pat)):
                proot = Path(cfgfile).resolve().parent.parent   # strip .macp/config.toml
                key = str(proot)
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                cfg = C.load_project(proot)
                if cfg is None:
                    continue
                name = project_name(proot, cfg)
                nodes[name] = ProjectNode(name=name, root=str(proot),
                                          provides=cfg.provides,
                                          depends_on=list(cfg.depends_on))
    return nodes


def downstream_edges(nodes: dict[str, ProjectNode]) -> dict[str, set[str]]:
    """{upstream_name: {consumers that depend on it}} — the reverse of depends_on."""
    down: dict[str, set[str]] = {n: set() for n in nodes}
    for name, node in nodes.items():
        for up in node.depends_on:
            down.setdefault(up, set()).add(name)
    return down


def direct_downstream(changed: str, nodes: dict[str, ProjectNode]) -> list[str]:
    return sorted(downstream_edges(nodes).get(changed, set()))


def impacted(changed: str, nodes: dict[str, ProjectNode]) -> list[str]:
    """Every project transitively affected by a change to `changed` (BFS, cycle-safe,
    excludes `changed` itself), in propagation order (nearest consumers first)."""
    down = downstream_edges(nodes)
    order: list[str] = []
    seen: set[str] = {changed}
    q: deque[str] = deque([changed])
    while q:
        cur = q.popleft()
        for consumer in sorted(down.get(cur, set())):
            if consumer not in seen:
                seen.add(consumer)
                order.append(consumer)
                q.append(consumer)
    return order


def upstream_of(name: str, nodes: dict[str, ProjectNode]) -> list[str]:
    n = nodes.get(name)
    return list(n.depends_on) if n else []


def integration_task_specs(changed: str, nodes: dict[str, ProjectNode],
                           reason: str = "") -> list[dict]:
    """Follow-up integration-check task specs, one per DIRECT downstream consumer of
    `changed`. The planner/orchestrator turns these into real tasks so an upstream
    change is verified against each consumer instead of silently breaking it."""
    why = reason or f"upstream '{changed}' 변경"
    specs = []
    for consumer in direct_downstream(changed, nodes):
        node = nodes.get(consumer)
        specs.append({
            "slug": f"{consumer}-integcheck-{changed}",
            "title": f"{consumer}: {changed} 변경 통합 검증",
            "project": consumer,
            "root": node.root if node else "",
            "worker_mode": "delegate",
            "outcome": (f"{changed}의 최신 변경이 {consumer}에 호환되는지 검증 — "
                        f"의존 버전/계약(API·SDK) 확인, 빌드·테스트 그린, 필요 시 어댑트"),
            "reason": why,
        })
    return specs


def git_remote_url(root: str | Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def write_integration_task(spec: dict, items_dir: Path | str | None = None) -> Path:
    """Materialize one integration-check spec as a vault Todo item (git-ignored, then
    imported to todo.db). Routes to the consumer's repo so the pipeline can build/test
    it. auto_merge:false — an upstream-driven change should be reviewed."""
    items = Path(items_dir) if items_dir else (VAULT_ROOT / "Todo" / "items")
    items.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    repo = git_remote_url(spec.get("root", "")) if spec.get("root") else ""
    fm = ["---", "tags:", "  - todo", "  - integration", "  - macp-propagated",
          "status: open", f"updated: {today}",
          f'outcome: "{spec["outcome"]}"',
          f'next_action: "{spec["reason"]} → {spec["project"]}에서 의존 버전 갱신 + 빌드·테스트로 호환성 확인"',
          'if_then: "-"', f'worker_mode: {spec.get("worker_mode", "delegate")}',
          "auto_merge: false"]
    if repo:
        fm.append(f'repo: "{repo}"')
    fm.append("---")
    body = (f"\n# {spec['title']}\n\n{spec['reason']}로 자동 생성된 통합검증 태스크 "
            f"(macp propagate). 상류 변경이 이 프로젝트를 깨뜨리지 않는지 확인.\n\n"
            f"## Progress Log\n\n- {today}: macp propagate 자동생성\n")
    path = items / f"{spec['slug']}.md"
    path.write_text("\n".join(fm) + body, encoding="utf-8")
    return path
