"""Pricing defaults based on Russian IaaS market (VK Cloud / Selectel, 2026).

Sources / anchors (₽ / month, VAT incl. where published):
- VK Cloud: vCPU Ice Lake ~849 ₽, RAM ~223 ₽/Gi, SSD ~13 ₽/Gi
  https://cloud.vk.com/pricelist/
- Selectel: network SSD from ~9 ₽/Gi, local SSD ~11 ₽/Gi
  https://selectel.ru/services/cloud/servers/

Cloud DWH defaults sit slightly below mid-market private-IaaS rates.
Admins can change them in the superuser console.
"""

from __future__ import annotations

DEFAULT_PRICES = {
    "currency": "RUB",
    "vcpu_month": 780.0,
    "ram_gb_month": 210.0,
    "storage_gb_month": 12.0,
    # Flat managed fee per enabled platform service (PaaS markup)
    "service_month": 490.0,
    # When stack is stopped/frozen: charge storage (+ optional service fee factor)
    "stopped_compute_factor": 0.0,
    "stopped_storage_factor": 1.0,
    "blocked_compute_factor": 0.0,
    "blocked_storage_factor": 1.0,
    "source_note": (
        "Ориентиры 2026: VK Cloud (~849₽/vCPU, ~223₽/Gi RAM, ~13₽/Gi SSD), "
        "Selectel SSD ~9–11₽/Gi. Прайс Cloud DWH редактируется суперпользователем."
    ),
}


def parse_cpu(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().lower()
    if s.endswith("m"):
        return float(s[:-1]) / 1000.0
    return float(s or 0)


def parse_gi(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s.endswith("Ti"):
        return float(s[:-2]) * 1024
    if s.endswith("Gi"):
        return float(s[:-2])
    if s.endswith("Mi"):
        return float(s[:-2]) / 1024
    if s.endswith("G"):
        return float(s[:-1])
    return float(s or 0)


def spec_resources(spec: dict) -> dict:
    """Aggregate billed resources from a stack spec."""
    cpu = 0.0
    ram = 0.0
    storage = 0.0
    services = 0
    breakdown = []

    for key, cfg in (spec or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        services += 1
        res = cfg.get("resources") or {}
        svc_cpu = parse_cpu(res.get("cpu", 0))
        svc_ram = parse_gi(res.get("memory", 0))
        svc_storage = parse_gi(res.get("storage", 0))

        multiplier = 1
        if key == "clickhouse":
            multiplier = max(1, int(cfg.get("replicas") or 1))
        elif key == "kafka":
            multiplier = max(1, int(cfg.get("brokers") or 1))
        elif key == "airflow":
            # workers consume extra CPU/RAM beyond the webserver defaults in billing
            workers = max(1, int(cfg.get("workers") or 1))
            multiplier = workers

        total_cpu = svc_cpu * multiplier
        total_ram = svc_ram * multiplier
        total_storage = svc_storage * (multiplier if key in ("clickhouse", "kafka", "postgres") else 1)

        cpu += total_cpu
        ram += total_ram
        storage += total_storage
        breakdown.append(
            {
                "service": key,
                "cpu": round(total_cpu, 2),
                "memory_gb": round(total_ram, 2),
                "storage_gb": round(total_storage, 2),
                "units": multiplier,
            }
        )

    return {
        "cpu": round(cpu, 2),
        "memory_gb": round(ram, 2),
        "storage_gb": round(storage, 2),
        "services": services,
        "breakdown": breakdown,
    }


def estimate_cost(spec: dict, prices: dict, status: str = "running") -> dict:
    """Return monthly cost estimate for a stack."""
    p = {**DEFAULT_PRICES, **(prices or {})}
    res = spec_resources(spec)

    compute = res["cpu"] * float(p["vcpu_month"]) + res["memory_gb"] * float(p["ram_gb_month"])
    storage = res["storage_gb"] * float(p["storage_gb_month"])
    services = res["services"] * float(p["service_month"])

    status = (status or "running").lower()
    if status in ("stopped", "blocked"):
        cf = float(p.get(f"{status}_compute_factor", 0.0))
        sf = float(p.get(f"{status}_storage_factor", 1.0))
        compute *= cf
        storage *= sf
        services *= cf

    monthly = round(compute + storage + services, 2)
    hourly = round(monthly / (30 * 24), 4)

    return {
        "currency": p.get("currency", "RUB"),
        "status": status,
        "resources": res,
        "lines": {
            "compute": round(compute, 2),
            "storage": round(storage, 2),
            "services": round(services, 2),
        },
        "monthly": monthly,
        "hourly": hourly,
        "unit_prices": {
            "vcpu_month": float(p["vcpu_month"]),
            "ram_gb_month": float(p["ram_gb_month"]),
            "storage_gb_month": float(p["storage_gb_month"]),
            "service_month": float(p["service_month"]),
        },
    }
