"""Build per-service inventory: nodes, specs, connection, cost."""

from __future__ import annotations

from app.config import settings
from app.services.pricing import DEFAULT_PRICES, estimate_cost, parse_cpu, parse_gi

SERVICE_META = {
    "clickhouse": {"name": "ClickHouse", "ports": {"http": 8123, "native": 9000}},
    "kafka": {"name": "Apache Kafka", "ports": {"bootstrap": 9094}},
    "postgres": {"name": "PostgreSQL", "ports": {"sql": 5432}},
    "airflow": {"name": "Apache Airflow", "ports": {"web": 8080}},
    "superset": {"name": "Apache Superset", "ports": {"web": 8088}},
    "redis": {"name": "Redis", "ports": {"redis": 6379}},
}


def _unit_count(key: str, cfg: dict) -> int:
    if key == "clickhouse":
        return max(1, int(cfg.get("replicas") or 1))
    if key == "kafka":
        return max(1, int(cfg.get("brokers") or 1))
    if key == "airflow":
        # webserver + scheduler + N workers (workers billed; LocalExecutor runs tasks on scheduler)
        return max(1, int(cfg.get("workers") or 1))
    return 1


def _specs_text(key: str, cfg: dict, units: int) -> str:
    res = cfg.get("resources") or {}
    cpu = res.get("cpu", "—")
    mem = res.get("memory", "—")
    storage = res.get("storage")
    parts = [f"{units}×", f"CPU {cpu}", f"RAM {mem}"]
    if storage:
        parts.append(f"disk {storage}")
    if key == "clickhouse":
        return f"{units} replica(s): " + ", ".join(parts[1:])
    if key == "kafka":
        return f"{units} broker(s): " + ", ".join(parts[1:])
    if key == "airflow":
        return f"web+scheduler + {units} worker budget: " + ", ".join(parts[1:])
    return ", ".join(parts[1:])


def _ssh_hints(namespace: str, pod_name: str) -> dict:
    server_ip = settings.server_ip
    return {
        "node": f"ssh user@{server_ip}",
        "pod": f"kubectl --kubeconfig=/etc/kubernetes/admin.conf -n {namespace} exec -it {pod_name} -- /bin/sh",
        "note": (
            "SSH в приложение обычно недоступен (containers без sshd). "
            "Используйте SSH на ноду или kubectl exec в pod."
        ),
    }


def _cost_for_service(key: str, cfg: dict, prices: dict) -> dict:
    p = {**DEFAULT_PRICES, **(prices or {})}
    res = cfg.get("resources") or {}
    units = _unit_count(key, cfg)
    cpu = parse_cpu(res.get("cpu", 0)) * units
    ram = parse_gi(res.get("memory", 0)) * units
    storage = parse_gi(res.get("storage", 0)) * (
        units if key in ("clickhouse", "kafka", "postgres") else (1 if res.get("storage") else 0)
    )
    compute = cpu * float(p["vcpu_month"]) + ram * float(p["ram_gb_month"])
    stor = storage * float(p["storage_gb_month"])
    managed = float(p["service_month"])
    monthly = round(compute + stor + managed, 2)
    return {
        "cpu": round(cpu, 2),
        "memory_gb": round(ram, 2),
        "storage_gb": round(storage, 2),
        "units": units,
        "monthly": monthly,
        "currency": p.get("currency", "RUB"),
        "lines": {
            "compute": round(compute, 2),
            "storage": round(stor, 2),
            "managed": round(managed, 2),
        },
    }


def _nodes_from_spec(
    key: str,
    cfg: dict,
    *,
    stack_name: str,
    namespace: str,
    endpoints: dict | None,
) -> list[dict]:
    units = _unit_count(key, cfg)
    res = cfg.get("resources") or {}
    endpoints = endpoints or {}
    base = settings.server_ip
    domain = settings.ingress_base_domain
    nodes = []

    if key == "clickhouse":
        for i in range(units):
            pod = f"chi-clickhouse-dwh-0-{i}"
            nodes.append(
                {
                    "name": pod,
                    "role": f"replica-{i}",
                    "status": "planned",
                    "resources": dict(res),
                    "connect": {
                        "http_lb": endpoints.get("clickhouse") or f"{base}:8123",
                        "native_lb": f"{base}:9000",
                        "web": endpoints.get("clickhouse_web")
                        or f"https://{stack_name}-ch.{domain}",
                        "internal_http": f"clickhouse-clickhouse.{namespace}.svc:8123",
                        "internal_native": f"clickhouse-clickhouse.{namespace}.svc:9000",
                        "pod_hint": f"{pod}.{namespace} (operator may use slightly different name)",
                    },
                    "ssh": _ssh_hints(namespace, pod),
                }
            )
    elif key == "kafka":
        for i in range(units):
            pod = f"{stack_name}-kafka-{i}"
            # Strimzi advertised external often SERVER_IP:30993+i style in this project
            nodes.append(
                {
                    "name": pod,
                    "role": f"broker-{i}",
                    "status": "planned",
                    "resources": dict(res),
                    "connect": {
                        "bootstrap_lb": endpoints.get("kafka") or f"{base}:30993",
                        "broker_external": f"{base}:{30993 + i}",
                        "internal": f"{stack_name}-kafka-bootstrap.{namespace}.svc:9092",
                        "ui": endpoints.get("kafka_ui")
                        or f"https://{stack_name}-kafka-ui.{domain}",
                    },
                    "ssh": _ssh_hints(namespace, pod),
                }
            )
    elif key == "postgres":
        pod = "postgres-1"
        nodes.append(
            {
                "name": pod,
                "role": "primary",
                "status": "planned",
                "resources": dict(res),
                "connect": {
                    "sql": endpoints.get("postgres") or f"{base}:5432",
                    "internal": f"postgres-rw.{namespace}.svc:5432",
                    "user": "dwh",
                },
                "ssh": _ssh_hints(namespace, pod),
            }
        )
    elif key == "airflow":
        nodes.append(
            {
                "name": "airflow-webserver",
                "role": "webserver",
                "status": "planned",
                "resources": {"cpu": "500m", "memory": "1Gi"},
                "connect": {
                    "web": endpoints.get("airflow")
                    or f"https://{stack_name}-airflow.{domain}",
                    "login": "admin / admin",
                },
                "ssh": _ssh_hints(namespace, "deploy/airflow-webserver"),
            }
        )
        nodes.append(
            {
                "name": "airflow-scheduler",
                "role": "scheduler",
                "status": "planned",
                "resources": dict(res),
                "connect": {
                    "note": "LocalExecutor: DAG tasks run inside scheduler",
                    "internal": f"airflow-scheduler.{namespace}.svc",
                },
                "ssh": _ssh_hints(namespace, "deploy/airflow-scheduler"),
            }
        )
        for i in range(units):
            nodes.append(
                {
                    "name": f"airflow-worker-{i}",
                    "role": f"worker-slot-{i}",
                    "status": "logical",
                    "resources": dict(res),
                    "connect": {
                        "note": (
                            "Слот биллинга/ёмкости. Текущий деплой — LocalExecutor; "
                            "отдельные worker-поды появятся при CeleryExecutor."
                        ),
                    },
                    "ssh": _ssh_hints(namespace, "airflow-scheduler"),
                }
            )
    elif key == "superset":
        nodes.append(
            {
                "name": "superset",
                "role": "web",
                "status": "planned",
                "resources": dict(res),
                "connect": {
                    "web": endpoints.get("superset")
                    or f"https://{stack_name}-superset.{domain}",
                },
                "ssh": _ssh_hints(namespace, "deploy/superset"),
            }
        )
    return nodes


def enrich_nodes_with_pods(nodes: list[dict], pods: list[dict]) -> list[dict]:
    """Merge live pod phase/IP into planned nodes when names overlap."""
    by_name = {p.get("name"): p for p in pods}
    out = []
    for node in nodes:
        n = dict(node)
        live = by_name.get(n["name"])
        if not live:
            # fuzzy: startswith
            for pname, pdata in by_name.items():
                if pname.startswith(n["name"]) or n["name"] in pname:
                    live = pdata
                    break
        if live:
            n["status"] = live.get("phase", n.get("status"))
            n["pod_ip"] = live.get("pod_ip")
            n["name"] = live.get("name", n["name"])
            if live.get("name"):
                n["ssh"] = _ssh_hints(live.get("namespace", ""), live["name"])
        out.append(n)
    # append unknown running pods related to service prefix
    return out


def build_services_inventory(
    spec: dict,
    *,
    stack_name: str,
    namespace: str,
    endpoints: dict | None,
    prices: dict | None,
    live_pods: list[dict] | None = None,
) -> list[dict]:
    services = []
    for key, cfg in (spec or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        if key not in SERVICE_META:
            continue
        units = _unit_count(key, cfg)
        nodes = _nodes_from_spec(
            key, cfg, stack_name=stack_name, namespace=namespace, endpoints=endpoints
        )
        if live_pods:
            # filter live pods roughly by service
            related = [
                p
                for p in live_pods
                if key in (p.get("name") or "")
                or key.replace("postgres", "postgres") in (p.get("name") or "")
                or (key == "clickhouse" and "clickhouse" in (p.get("name") or ""))
                or (key == "kafka" and "kafka" in (p.get("name") or ""))
                or (key == "airflow" and "airflow" in (p.get("name") or ""))
                or (key == "postgres" and "postgres" in (p.get("name") or ""))
                or (key == "superset" and "superset" in (p.get("name") or ""))
            ]
            nodes = enrich_nodes_with_pods(nodes, related)

        cost = _cost_for_service(key, cfg, prices or {})
        services.append(
            {
                "key": key,
                "name": SERVICE_META[key]["name"],
                "units": units,
                "specs": _specs_text(key, cfg, units),
                "resources": cfg.get("resources") or {},
                "cost": cost,
                "endpoint": (endpoints or {}).get(key),
                "nodes": nodes,
            }
        )
    return services


def list_namespace_pods(namespace: str) -> list[dict]:
    """Best-effort live pod list; empty if kube unreachable."""
    try:
        from app.services.provisioner import _get_k8s_clients

        core, *_ = _get_k8s_clients()
        items = core.list_namespaced_pod(namespace).items
        return [
            {
                "name": p.metadata.name,
                "namespace": namespace,
                "phase": p.status.phase,
                "pod_ip": p.status.pod_ip,
                "labels": dict(p.metadata.labels or {}),
            }
            for p in items
        ]
    except Exception:
        return []
