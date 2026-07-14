"""Interactive bash console into stack pods via Kubernetes exec + WebSocket."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from sqlalchemy import select

from app.auth import decode_access_token, get_current_user
from app.database import SessionLocal, get_db
from app.models.stack import Stack
from app.models.user import User
from app.services.provisioner import _get_k8s_clients

logger = logging.getLogger(__name__)
router = APIRouter(tags=["console"])


async def _load_stack_for_user(stack_id: str, user_id: str, is_superuser: bool) -> Stack:
    async with SessionLocal() as db:
        result = await db.execute(select(Stack).where(Stack.id == stack_id))
        stack = result.scalar_one_or_none()
        if not stack:
            raise HTTPException(404, "Stack not found")
        if not is_superuser and stack.owner_id and str(stack.owner_id) != user_id:
            raise HTTPException(403, "Access denied")
        return stack


def _pick_container(pod) -> str | None:
    containers = [c.name for c in (pod.spec.containers or [])]
    if not containers:
        return None
    preferred = ("clickhouse", "kafka", "postgres", "webserver", "scheduler", "superset", "redis")
    for name in preferred:
        for c in containers:
            if name in c.lower():
                return c
    return containers[0]


def _resolve_pod_name(namespace: str, hint: str) -> tuple[str, str]:
    """Return (pod_name, container_name) from exact name or role hint."""
    core, *_ = _get_k8s_clients()
    pods = core.list_namespaced_pod(namespace).items
    if not pods:
        raise HTTPException(404, "No pods in stack namespace")

    hint_l = (hint or "").lower()
    for p in pods:
        if p.metadata.name == hint:
            return p.metadata.name, _pick_container(p) or "main"

    scored = []
    for p in pods:
        name = p.metadata.name.lower()
        score = 0
        if hint_l and hint_l in name:
            score += 10
        for token in hint_l.replace(":", "-").replace("_", "-").split("-"):
            if token and token in name:
                score += 2
        if "clickhouse" in hint_l and "clickhouse" in name:
            score += 5
        if "kafka" in hint_l and "kafka" in name and "ui" not in name:
            score += 5
        if "postgres" in hint_l and "postgres" in name:
            score += 5
        if "airflow" in hint_l and "airflow" in name:
            score += 5
        if "webserver" in hint_l and "web" in name:
            score += 3
        if "scheduler" in hint_l and "sched" in name:
            score += 3
        if "superset" in hint_l and "superset" in name:
            score += 5
        if "broker" in hint_l and "kafka" in name:
            idx = hint_l.split("-")[-1]
            if idx.isdigit() and name.endswith(idx):
                score += 4
            score += 2
        if "replica" in hint_l and "clickhouse" in name:
            idx = hint_l.split("-")[-1]
            if idx.isdigit() and idx in name:
                score += 4
            score += 2
        # clickhouse-{shard}-{replica}
        if hint_l.startswith("clickhouse-") and "clickhouse" in name:
            parts = hint_l.split("-")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                s_idx, r_idx = parts[1], parts[2]
                if f"-{s_idx}-{r_idx}-" in name or name.endswith(f"-{s_idx}-{r_idx}-0"):
                    score += 12
                score += 3
        if p.status.phase == "Running":
            score += 1
        scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    if scored and scored[0][0] > 0:
        p = scored[0][1]
        return p.metadata.name, _pick_container(p) or "main"

    for p in pods:
        if p.status.phase == "Running":
            return p.metadata.name, _pick_container(p) or "main"
    p = pods[0]
    return p.metadata.name, _pick_container(p) or "main"


@router.get("/stacks/{stack_id}/console-targets")
async def console_targets(
    stack_id: str,
    db=Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await db.execute(select(Stack).where(Stack.id == stack_id))
    stack = result.scalar_one_or_none()
    if not stack:
        raise HTTPException(404, "Stack not found")
    if not user.is_superuser and stack.owner_id and stack.owner_id != user.id:
        raise HTTPException(403, "Access denied")

    try:
        core, *_ = _get_k8s_clients()
        pods = core.list_namespaced_pod(stack.namespace).items
    except Exception as e:
        raise HTTPException(502, f"Cannot list pods: {e}") from e

    out = []
    for p in pods:
        if p.status.phase in ("Succeeded", "Failed"):
            continue
        out.append(
            {
                "pod": p.metadata.name,
                "phase": p.status.phase,
                "pod_ip": p.status.pod_ip,
                "container": _pick_container(p),
                "console_url": f"/console.html?stack={stack.id}&pod={p.metadata.name}",
            }
        )
    return {"stack_id": str(stack.id), "namespace": stack.namespace, "targets": out}


@router.websocket("/console/ws")
async def console_ws(websocket: WebSocket):
    await websocket.accept()
    params = parse_qs(websocket.url.query or "")
    token = (params.get("token") or [None])[0]
    stack_id = (params.get("stack") or [None])[0]
    pod_hint = (params.get("pod") or params.get("target") or [None])[0]

    if not token or not stack_id or not pod_hint:
        await websocket.send_text("\r\n[error] Missing token, stack or pod\r\n")
        await websocket.close()
        return

    try:
        payload = decode_access_token(token)
        user_id = payload["sub"]
        async with SessionLocal() as db:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(401, "Unauthorized")
        stack = await _load_stack_for_user(stack_id, str(user.id), bool(user.is_superuser))
    except Exception as e:
        await websocket.send_text(f"\r\n[error] Auth failed: {e}\r\n")
        await websocket.close()
        return

    try:
        pod_name, container = await asyncio.to_thread(_resolve_pod_name, stack.namespace, pod_hint)
    except HTTPException as e:
        await websocket.send_text(f"\r\n[error] {e.detail}\r\n")
        await websocket.close()
        return
    except Exception as e:
        await websocket.send_text(f"\r\n[error] Resolve pod: {e}\r\n")
        await websocket.close()
        return

    await websocket.send_text(
        f"\r\n[console] namespace={stack.namespace} pod={pod_name} container={container}\r\n"
        f"[console] Connecting…\r\n\r\n"
    )

    core, *_ = _get_k8s_clients()
    command = ["/bin/bash", "-il"]
    try:
        resp = stream(
            core.connect_get_namespaced_pod_exec,
            pod_name,
            stack.namespace,
            command=command,
            container=container,
            stderr=True,
            stdin=True,
            stdout=True,
            tty=True,
            _preload_content=False,
        )
    except ApiException:
        try:
            command = ["/bin/sh", "-il"]
            resp = stream(
                core.connect_get_namespaced_pod_exec,
                pod_name,
                stack.namespace,
                command=command,
                container=container,
                stderr=True,
                stdin=True,
                stdout=True,
                tty=True,
                _preload_content=False,
            )
        except Exception as e:
            await websocket.send_text(f"\r\n[error] Exec failed: {e}\r\n")
            await websocket.close()
            return
    except Exception as e:
        await websocket.send_text(f"\r\n[error] Exec failed: {e}\r\n")
        await websocket.close()
        return

    async def k8s_to_ws():
        try:
            while resp.is_open():
                resp.update(timeout=0.1)
                if resp.peek_stdout():
                    data = resp.read_stdout()
                    if data:
                        await websocket.send_text(data)
                if resp.peek_stderr():
                    data = resp.read_stderr()
                    if data:
                        await websocket.send_text(data)
                await asyncio.sleep(0.02)
        except Exception as e:
            logger.debug("k8s_to_ws end: %s", e)

    async def ws_to_k8s():
        try:
            while True:
                msg = await websocket.receive_text()
                if msg.startswith("\x00RESIZE:"):
                    continue
                resp.write_stdin(msg)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("ws_to_k8s end: %s", e)

    try:
        await asyncio.gather(k8s_to_ws(), ws_to_k8s())
    finally:
        try:
            resp.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
