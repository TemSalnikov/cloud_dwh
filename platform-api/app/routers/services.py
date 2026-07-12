from fastapi import APIRouter

from app.catalog import PRESETS, SERVICES

router = APIRouter(tags=["services"])


@router.get("/services")
async def list_services():
    return {"services": SERVICES, "presets": PRESETS}
