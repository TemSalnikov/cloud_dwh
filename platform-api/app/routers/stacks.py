import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.catalog import PRESETS
from app.database import get_db
from app.models.stack import Stack, StackStatus
from app.models.user import PricingSettings, User
from app.schemas.stack import (
    CostEstimate,
    EstimateRequest,
    StackCreate,
    StackResponse,
    StackUpdate,
)
from app.services.inventory import build_services_inventory, list_namespace_pods
from app.services.pricing import DEFAULT_PRICES, estimate_cost
from app.services.provisioner import StackProvisioner, _get_k8s_clients

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stacks"])

_RECONCILE_STATUSES = frozenset(
    {StackStatus.running, StackStatus.stopped, StackStatus.blocked, StackStatus.failed, StackStatus.deleting}
)


async def _namespace_exists(namespace: str) -> bool:
    def _check() -> bool:
        core, *_ = _get_k8s_clients()
        try:
            core.read_namespace(namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            raise

    return await asyncio.to_thread(_check)


async def _reconcile_orphan_stacks(stacks: list[Stack], db: AsyncSession) -> list[Stack]:
    alive: list[Stack] = []
    removed = 0
    for stack in stacks:
        if stack.status not in _RECONCILE_STATUSES:
            alive.append(stack)
            continue
        if await _namespace_exists(stack.namespace):
            alive.append(stack)
            continue
        await db.delete(stack)
        removed += 1
        logger.info("Removed orphan stack %s (%s)", stack.name, stack.id)
    if removed:
        await db.commit()
    return alive


async def _load_prices(db: AsyncSession) -> dict:
    row = await db.get(PricingSettings, 1)
    if not row:
        return dict(DEFAULT_PRICES)
    return {**DEFAULT_PRICES, **(row.prices or {})}


def _build_spec(body: StackCreate | StackUpdate | EstimateRequest) -> dict:
    if body.preset:
        if body.preset not in PRESETS:
            raise HTTPException(400, f"Unknown preset: {body.preset}")
        return PRESETS[body.preset]["services"]
    if body.services:
        return body.services.model_dump()
    raise HTTPException(400, "Provide either preset or services config")


def _to_response(
    stack: Stack,
    prices: dict,
    owner_email: str | None = None,
    *,
    with_live_pods: bool = False,
) -> StackResponse:
    cost = estimate_cost(stack.spec, prices, stack.status.value)
    live = list_namespace_pods(stack.namespace) if with_live_pods else None
    services = build_services_inventory(
        stack.spec,
        stack_name=stack.name,
        namespace=stack.namespace,
        endpoints=stack.endpoints,
        prices=prices,
        live_pods=live,
    )
    return StackResponse(
        id=str(stack.id),
        name=stack.name,
        namespace=stack.namespace,
        status=stack.status.value,
        status_message=stack.status_message,
        blocked_reason=stack.blocked_reason,
        owner_id=str(stack.owner_id) if stack.owner_id else None,
        owner_email=owner_email,
        spec=stack.spec,
        endpoints=stack.endpoints,
        services=services,
        cost=CostEstimate(**cost),
        created_at=stack.created_at.isoformat() if stack.created_at else "",
        updated_at=stack.updated_at.isoformat() if stack.updated_at else None,
    )


async def _get_owned_stack(stack_id: str, user: User, db: AsyncSession, *, admin: bool = False) -> Stack:
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    if not admin and stack.owner_id and stack.owner_id != user.id and not user.is_superuser:
        raise HTTPException(404, "Stack not found")
    return stack


def _assert_mutable(stack: Stack, *, allow_blocked_admin: bool = False):
    if stack.status == StackStatus.blocked and not allow_blocked_admin:
        raise HTTPException(403, f"Stack is blocked: {stack.blocked_reason or 'contact admin'}")
    if stack.status in (StackStatus.deploying, StackStatus.deleting, StackStatus.updating, StackStatus.pending):
        raise HTTPException(409, f"Stack is busy ({stack.status.value})")


async def _deploy_stack(stack_id, name: str, spec: dict):
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


async def _run_lifecycle(stack_id, action: str):
    from app.database import SessionLocal

    async with SessionLocal() as db:
        result = await db.execute(select(Stack).where(Stack.id == stack_id))
        stack = result.scalar_one_or_none()
        if not stack:
            return
        try:
            provisioner = StackProvisioner(str(stack.id), stack.name, stack.spec)
            provisioner.namespace = stack.namespace
            if action == "stop":
                await provisioner.stop()
                stack.status = StackStatus.stopped
            elif action == "start":
                await provisioner.start()
                stack.status = StackStatus.running
            elif action == "restart":
                await provisioner.restart()
                stack.status = StackStatus.running
            elif action == "redeploy":
                stack.status = StackStatus.updating
                await db.commit()
                endpoints = await provisioner.deploy()
                stack.endpoints = endpoints
                stack.status = StackStatus.running
            stack.status_message = None
        except Exception as e:
            logger.exception("Lifecycle %s failed for %s", action, stack.name)
            stack.status = StackStatus.failed
            stack.status_message = str(e)
        await db.commit()


@router.get("/pricing")
async def get_pricing(db: AsyncSession = Depends(get_db)):
    prices = await _load_prices(db)
    return {"prices": prices, "defaults": DEFAULT_PRICES}


@router.post("/pricing/estimate", response_model=CostEstimate)
async def estimate_pricing(body: EstimateRequest, db: AsyncSession = Depends(get_db)):
    spec = _build_spec(body)
    prices = await _load_prices(db)
    return CostEstimate(**estimate_cost(spec, prices, body.status))


@router.post("/stacks", response_model=StackResponse, status_code=201)
async def create_stack(
    body: StackCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = await db.execute(select(Stack).where(Stack.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Stack '{body.name}' already exists")

    spec = _build_spec(body)
    if not any(cfg.get("enabled") for cfg in spec.values()):
        raise HTTPException(400, "At least one service must be enabled")

    stack = Stack(
        name=body.name,
        owner_id=user.id,
        namespace="stack-pending",
        spec=spec,
        status=StackStatus.pending,
    )
    db.add(stack)
    await db.flush()
    stack.namespace = f"stack-{str(stack.id)[:8]}"
    await db.commit()
    await db.refresh(stack)

    background_tasks.add_task(_deploy_stack, stack.id, stack.name, spec)
    prices = await _load_prices(db)
    return _to_response(stack, prices, user.email)


@router.get("/stacks", response_model=list[StackResponse])
async def list_stacks(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.is_superuser:
        result = await db.execute(select(Stack).order_by(Stack.created_at.desc()))
    else:
        result = await db.execute(
            select(Stack)
            .where((Stack.owner_id == user.id) | (Stack.owner_id.is_(None)))
            .order_by(Stack.created_at.desc())
        )
    stacks = await _reconcile_orphan_stacks(list(result.scalars().all()), db)
    prices = await _load_prices(db)

    emails: dict = {}
    owner_ids = {s.owner_id for s in stacks if s.owner_id}
    if owner_ids:
        users = await db.execute(select(User).where(User.id.in_(owner_ids)))
        emails = {u.id: u.email for u in users.scalars().all()}

    return [
        _to_response(s, prices, emails.get(s.owner_id) if s.owner_id else None, with_live_pods=True)
        for s in stacks
    ]


@router.get("/stacks/{stack_id}", response_model=StackResponse)
async def get_stack(
    stack_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    prices = await _load_prices(db)
    return _to_response(stack, prices, with_live_pods=True)


@router.patch("/stacks/{stack_id}", response_model=StackResponse)
async def update_stack(
    stack_id: str,
    body: StackUpdate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    _assert_mutable(stack)
    if stack.status == StackStatus.stopped:
        raise HTTPException(409, "Start the stack before editing configuration")

    spec = _build_spec(body)
    if not any(cfg.get("enabled") for cfg in spec.values()):
        raise HTTPException(400, "At least one service must be enabled")

    stack.spec = spec
    stack.status = StackStatus.updating
    stack.status_message = "Applying new configuration"
    await db.commit()
    await db.refresh(stack)

    background_tasks.add_task(_run_lifecycle, stack.id, "redeploy")
    prices = await _load_prices(db)
    return _to_response(stack, prices, user.email)


@router.post("/stacks/{stack_id}/stop", response_model=StackResponse)
async def stop_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    _assert_mutable(stack)
    if stack.status != StackStatus.running:
        raise HTTPException(409, "Only running stacks can be stopped")
    stack.status = StackStatus.updating
    stack.status_message = "Stopping"
    await db.commit()
    background_tasks.add_task(_run_lifecycle, stack.id, "stop")
    prices = await _load_prices(db)
    return _to_response(stack, prices)


@router.post("/stacks/{stack_id}/start", response_model=StackResponse)
async def start_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    _assert_mutable(stack)
    if stack.status != StackStatus.stopped:
        raise HTTPException(409, "Only stopped stacks can be started")
    stack.status = StackStatus.updating
    stack.status_message = "Starting"
    await db.commit()
    background_tasks.add_task(_run_lifecycle, stack.id, "start")
    prices = await _load_prices(db)
    return _to_response(stack, prices)


@router.post("/stacks/{stack_id}/restart", response_model=StackResponse)
async def restart_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    _assert_mutable(stack)
    if stack.status not in (StackStatus.running, StackStatus.failed):
        raise HTTPException(409, "Restart is available for running or failed stacks")
    stack.status = StackStatus.updating
    stack.status_message = "Restarting pods"
    await db.commit()
    background_tasks.add_task(_run_lifecycle, stack.id, "restart")
    prices = await _load_prices(db)
    return _to_response(stack, prices)


async def _delete_namespace(namespace: str, name: str):
    try:
        provisioner = StackProvisioner("00000000-0000-0000-0000-000000000000", name, {})
        provisioner.namespace = namespace
        await provisioner.delete()
    except Exception:
        logger.exception("Delete namespace failed for stack %s", name)


@router.delete("/stacks/{stack_id}", status_code=204)
async def delete_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stack = await _get_owned_stack(stack_id, user, db)
    if stack.status == StackStatus.blocked and not user.is_superuser:
        raise HTTPException(403, "Blocked stacks can only be deleted by an administrator")
    name = stack.name
    namespace = stack.namespace
    await db.delete(stack)
    await db.commit()
    background_tasks.add_task(_delete_namespace, namespace, name)
