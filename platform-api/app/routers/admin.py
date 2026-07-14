"""Superuser administration: prices, block/unblock, all stacks, users."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_superuser, hash_password
from app.database import get_db
from app.models.stack import Stack, StackStatus
from app.models.user import PricingSettings, User
from app.routers.stacks import _delete_namespace, _load_prices, _run_lifecycle, _to_response
from app.schemas.auth import UserResponse
from app.schemas.stack import BlockRequest, ForceStatusRequest, PricingUpdate, StackResponse
from app.services.pricing import DEFAULT_PRICES

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/pricing")
async def admin_get_pricing(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    prices = await _load_prices(db)
    row = await db.get(PricingSettings, 1)
    return {
        "prices": prices,
        "defaults": DEFAULT_PRICES,
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "updated_by": row.updated_by if row else None,
    }


@router.put("/pricing")
async def admin_update_pricing(
    body: PricingUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_superuser),
):
    row = await db.get(PricingSettings, 1)
    payload = {**DEFAULT_PRICES, **body.model_dump()}
    if not row:
        row = PricingSettings(id=1, prices=payload, updated_by=admin.email)
        db.add(row)
    else:
        row.prices = payload
        row.updated_by = admin.email
    await db.commit()
    await db.refresh(row)
    return {"prices": row.prices, "updated_by": row.updated_by}


@router.get("/stacks", response_model=list[StackResponse])
async def admin_list_stacks(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(Stack).order_by(Stack.created_at.desc()))
    stacks = list(result.scalars().all())
    prices = await _load_prices(db)
    owner_ids = {s.owner_id for s in stacks if s.owner_id}
    emails = {}
    if owner_ids:
        users = await db.execute(select(User).where(User.id.in_(owner_ids)))
        emails = {u.id: u.email for u in users.scalars().all()}
    return [
        _to_response(s, prices, emails.get(s.owner_id) if s.owner_id else None)
        for s in stacks
    ]


@router.post("/stacks/{stack_id}/block", response_model=StackResponse)
async def admin_block_stack(
    stack_id: str,
    body: BlockRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    if stack.status in (StackStatus.deleting, StackStatus.pending, StackStatus.deploying):
        raise HTTPException(409, f"Cannot block stack in status {stack.status.value}")

    stack.blocked_reason = body.reason
    stack.status_message = body.reason
    prev = stack.status
    if prev == StackStatus.running:
        stack.status = StackStatus.updating
        await db.commit()
        background_tasks.add_task(_block_after_stop, stack.id, body.reason)
    else:
        stack.status = StackStatus.blocked
        await db.commit()

    await db.refresh(stack)
    prices = await _load_prices(db)
    return _to_response(stack, prices)


async def _block_after_stop(stack_id, reason: str):
    from app.database import SessionLocal

    await _run_lifecycle(stack_id, "stop")
    async with SessionLocal() as db:
        result = await db.execute(select(Stack).where(Stack.id == stack_id))
        stack = result.scalar_one_or_none()
        if not stack:
            return
        stack.status = StackStatus.blocked
        stack.blocked_reason = reason
        stack.status_message = reason
        await db.commit()


@router.post("/stacks/{stack_id}/force-status", response_model=StackResponse)
async def admin_force_status(
    stack_id: str,
    body: ForceStatusRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    """Unstick stacks stuck in updating/deploying/pending."""
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    try:
        new_status = StackStatus(body.status)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid status: {body.status}") from exc

    stack.status = new_status
    stack.status_message = body.message or f"Status forced to {body.status} by admin"
    if new_status != StackStatus.blocked:
        stack.blocked_reason = None
    await db.commit()
    await db.refresh(stack)
    prices = await _load_prices(db)
    return _to_response(stack, prices)


@router.post("/stacks/{stack_id}/redeploy", response_model=StackResponse)
async def admin_redeploy_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    stack.status = StackStatus.updating
    stack.status_message = "Admin redeploy"
    await db.commit()
    background_tasks.add_task(_run_lifecycle, stack.id, "redeploy")
    prices = await _load_prices(db)
    return _to_response(stack, prices)


@router.post("/stacks/{stack_id}/unblock", response_model=StackResponse)
async def admin_unblock_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    if stack.status != StackStatus.blocked:
        raise HTTPException(409, "Stack is not blocked")

    stack.blocked_reason = None
    stack.status_message = "Unblocked — starting"
    stack.status = StackStatus.updating
    await db.commit()
    background_tasks.add_task(_run_lifecycle, stack.id, "start")
    prices = await _load_prices(db)
    return _to_response(stack, prices)


@router.delete("/stacks/{stack_id}", status_code=204)
async def admin_delete_stack(
    stack_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    name, namespace = stack.name, stack.namespace
    await db.delete(stack)
    await db.commit()
    background_tasks.add_task(_delete_namespace, namespace, name)


@router.get("/users", response_model=list[UserResponse])
async def admin_list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [
        UserResponse(
            id=str(u.id),
            email=u.email,
            name=u.name,
            is_superuser=bool(u.is_superuser),
            is_active=bool(u.is_active),
        )
        for u in result.scalars().all()
    ]


@router.post("/users/{user_id}/disable", response_model=UserResponse)
async def admin_disable_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(get_current_superuser),
):
    if str(admin.id) == user_id:
        raise HTTPException(400, "Cannot disable yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = False
    await db.commit()
    return UserResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        is_superuser=bool(user.is_superuser),
        is_active=bool(user.is_active),
    )


@router.post("/users/{user_id}/enable", response_model=UserResponse)
async def admin_enable_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_active = True
    await db.commit()
    return UserResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        is_superuser=bool(user.is_superuser),
        is_active=bool(user.is_active),
    )


@router.post("/users/{user_id}/make-superuser", response_model=UserResponse)
async def admin_make_superuser(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    user.is_superuser = True
    await db.commit()
    return UserResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        is_superuser=True,
        is_active=bool(user.is_active),
    )


@router.post("/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_superuser),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    temporary = "TempPass1!"
    user.password_hash = hash_password(temporary)
    await db.commit()
    return {"email": user.email, "temporary_password": temporary}
