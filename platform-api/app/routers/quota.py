from fastapi import APIRouter, Depends
from kubernetes import client, config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.stack import Stack, StackStatus

router = APIRouter(tags=["quota"])


def _parse_cpu(s: str) -> float:
    return float(s)


def _parse_memory_gi(s: str) -> float:
    if s.endswith("Gi"):
        return float(s[:-2])
    if s.endswith("Mi"):
        return float(s[:-2]) / 1024
    return float(s)


@router.get("/quota")
async def get_quota(db: AsyncSession = Depends(get_db)):
    total_cpu = _parse_cpu(settings.cluster_total_cpu)
    total_mem = _parse_memory_gi(settings.cluster_total_memory)

    result = await db.execute(
        select(Stack).where(Stack.status.in_([StackStatus.running, StackStatus.deploying]))
    )
    stacks = result.scalars().all()

    used_cpu = 0.0
    used_mem = 0.0
    for stack in stacks:
        for svc, cfg in stack.spec.items():
            if not cfg.get("enabled"):
                continue
            res = cfg.get("resources", {})
            used_cpu += _parse_cpu(str(res.get("cpu", 0)))
            used_mem += _parse_memory_gi(res.get("memory", "0Gi"))

    try:
        if settings.kubeconfig_in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config()
        v1 = client.CoreV1Api()
        node = v1.list_node().items[0]
        alloc_cpu = _parse_cpu(node.status.allocatable.get("cpu", "0"))
        alloc_mem = _parse_memory_gi(node.status.allocatable.get("memory", "0Gi"))
    except Exception:
        alloc_cpu = total_cpu
        alloc_mem = total_mem

    return {
        "cluster": {
            "total_cpu": total_cpu,
            "total_memory": settings.cluster_total_memory,
            "allocatable_cpu": alloc_cpu,
            "allocatable_memory": f"{alloc_mem:.0f}Gi",
        },
        "used": {"cpu": used_cpu, "memory": f"{used_mem:.1f}Gi"},
        "available": {
            "cpu": max(0, min(total_cpu, alloc_cpu) - used_cpu),
            "memory": f"{max(0, min(total_mem, alloc_mem) - used_mem):.1f}Gi",
        },
        "active_stacks": len(stacks),
    }
