"""ClickHouse node topology: per-node shard vs replica roles."""

from __future__ import annotations

from typing import Literal

Role = Literal["shard", "replica"]


def normalize_nodes(cfg: dict | None) -> list[dict]:
    """Return ordered nodes with roles from explicit list or legacy `replicas`."""
    cfg = cfg or {}
    raw = cfg.get("nodes")
    if isinstance(raw, list) and raw:
        nodes = []
        for item in raw[:8]:
            if isinstance(item, str):
                role = item.strip().lower()
            elif isinstance(item, dict):
                role = str(item.get("role") or "replica").strip().lower()
            else:
                continue
            if role not in ("shard", "replica"):
                role = "replica"
            nodes.append({"role": role})
        if nodes:
            # First node always opens shard 0.
            nodes[0]["role"] = "shard"
            return nodes

    n = max(1, min(8, int(cfg.get("replicas") or 1)))
    return [{"role": "shard"}] + [{"role": "replica"} for _ in range(n - 1)]


def expand_topology(cfg: dict | None) -> list[dict]:
    """Assign shard/replica indices for inventory and titles."""
    nodes = normalize_nodes(cfg)
    out: list[dict] = []
    shard = -1
    replica = 0
    for i, node in enumerate(nodes):
        role = node["role"]
        if i == 0 or role == "shard":
            shard += 1
            replica = 0
            role = "shard"
        else:
            replica += 1
            role = "replica"
        out.append(
            {
                "index": i,
                "role": role,
                "shard": shard,
                "replica": replica,
                "title": f"ClickHouse · шард {shard} · реплика {replica}",
                "pod_hint": f"clickhouse-{shard}-{replica}",
            }
        )
    return out


def unit_count(cfg: dict | None) -> int:
    return max(1, len(normalize_nodes(cfg)))


def layout_for_chi(cfg: dict | None) -> dict:
    """Altinity CHI layout from node roles (explicit shards list)."""
    topo = expand_topology(cfg)
    shards: list[dict] = []
    for node in topo:
        if node["replica"] == 0:
            shards.append({"replicasCount": 1})
        else:
            shards[-1]["replicasCount"] = int(shards[-1]["replicasCount"]) + 1
    if not shards:
        shards = [{"replicasCount": 1}]
    return {"shards": shards}


def legacy_replicas(cfg: dict | None) -> int:
    """Uniform replicasCount if all shards equal; else total pods (compat)."""
    layout = layout_for_chi(cfg)
    counts = [int(s.get("replicasCount") or 1) for s in layout["shards"]]
    if len(set(counts)) == 1 and len(counts) == 1:
        return counts[0]
    return sum(counts)
