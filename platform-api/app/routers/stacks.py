import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import PRESETS
from app.database import get_db
from app.models.stack import Stack, StackStatus
from app.schemas.stack import StackCreate, StackResponse
from app.services.provisioner import StackProvisioner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stacks"])


def _build_spec(body: StackCreate) -> dict:
    if body.preset:
        if body.preset not in PRESETS:
            raise HTTPException(400, f"Unknown preset: {body.preset}")
        return PRESETS[body.preset]["services"]
    if body.services:
        return body.services.model_dump()
    raise HTTPException(400, "Provide either preset or services config")


async def _deploy_stack(stack_id: str, name: str, spec: dict, db_url: str):
    from app.database import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(select(Stack).where(Stack.id == stack_id))
        stack = result.scalar_one()
        stack.status = StackStatus.deploying
        await db.commit()

        try:
            provisioner = StackProvisioner(str(stack_id), name, spec)
            endpoints = await provisioner.deploy()
            stack.status = StackStatus.running
            stack.endpoints = endpoints
            stack.status_message = None
        except Exception as e:
            logger.exception("Deploy failed for stack %s", name)
            stack.status = StackStatus.failed
            stack.status_message = str(e)
        await db.commit()


@router.post("/stacks", response_model=StackResponse, status_code=201)
async def create_stack(
    body: StackCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(Stack).where(Stack.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Stack '{body.name}' already exists")

    spec = _build_spec(body)
    if not any(cfg.get("enabled") for cfg in spec.values()):
        raise HTTPException(400, "At least one service must be enabled")

    stack = Stack(
        name=body.name,
        namespace=f"stack-pending",
        spec=spec,
        status=StackStatus.pending,
    )
    db.add(stack)
    await db.flush()
    stack.namespace = f"stack-{str(stack.id)[:8]}"
    await db.commit()
    await db.refresh(stack)

    background_tasks.add_task(_deploy_stack, stack.id, stack.name, spec, "")

    return StackResponse(
        id=str(stack.id),
        name=stack.name,
        namespace=stack.namespace,
        status=stack.status.value,
        status_message=stack.status_message,
        spec=stack.spec,
        endpoints=stack.endpoints,
        created_at=stack.created_at.isoformat(),
    )


@router.get("/stacks", response_model=list[StackResponse])
async def list_stacks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Stack).order_by(Stack.created_at.desc()))
    stacks = result.scalars().all()
    return [
        StackResponse(
            id=str(s.id),
            name=s.name,
            namespace=s.namespace,
            status=s.status.value,
            status_message=s.status_message,
            spec=s.spec,
            endpoints=s.endpoints,
            created_at=s.created_at.isoformat(),
        )
        for s in stacks
    ]


@router.get("/stacks/{stack_id}", response_model=StackResponse)
async def get_stack(stack_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    return StackResponse(
        id=str(stack.id),
        name=stack.name,
        namespace=stack.namespace,
        status=stack.status.value,
        status_message=stack.status_message,
        spec=stack.spec,
        endpoints=stack.endpoints,
        created_at=stack.created_at.isoformat(),
    )


async def _delete_stack(stack_id: str, name: str, namespace: str, spec: dict):
    from app.database import SessionLocal
    from uuid import UUID

    try:
        provisioner = StackProvisioner(str(stack_id), name, spec)
        # ensure namespace from DB is used
        provisioner.namespace = namespace
        await provisioner.delete()
    except Exception:
        logger.exception("Delete namespace failed for stack %s", name)

    async with SessionLocal() as db:
        result = await db.execute(select(Stack).where(Stack.id == UUID(str(stack_id))))
        stack = result.scalar_one_or_none()
        if stack:
            await db.delete(stack)
            await db.commit()


@router.delete("/stacks/{stack_id}", status_code=204)
async def delete_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")

    if stack.status == StackStatus.deleting:
        raise HTTPException(409, "Stack is already being deleted")

    name = stack.name
    namespace = stack.namespace
    spec = stack.spec
    stack.status = StackStatus.deleting
    await db.commit()

    background_tasks.add_task(_delete_stack, stack_id, name, namespace, spec)
