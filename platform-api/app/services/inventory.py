"""Owner-facing service inventory: connection credentials + admin access."""

from __future__ import annotations

from app.config import settings
from app.services.pricing import DEFAULT_PRICES, parse_cpu, parse_gi

SERVICE_META = {
    "clickhouse": {"name": "ClickHouse"},
    "kafka": {"name": "Apache Kafka"},
    "postgres": {"name": "PostgreSQL"},
    "airflow": {"name": "Apache Airflow"},
    "superset": {"name": "Apache Superset"},
}


def _unit_count(key: str, cfg: dict) -> int:
    if key == "clickhouse":
        return max(1, int(cfg.get("replicas") or 1))
    if key == "kafka":
        return max(1, int(cfg.get("brokers") or 1))
    if key == "airflow":
        return max(1, int(cfg.get("workers") or 1))
    return 1


def _specs_text(key: str, cfg: dict, units: int) -> str:
    res = cfg.get("resources") or {}
    cpu, mem, storage = res.get("cpu", "—"), res.get("memory", "—"), res.get("storage")
    base = f"CPU {cpu}, RAM {mem}" + (f", диск {storage}" if storage else "")
    if key == "clickhouse":
        return f"{units} реплик(и): {base} на каждую"
    if key == "kafka":
        return f"{units} брокер(а): {base} на каждый"
    if key == "airflow":
        return f"Web + Scheduler, ёмкость {units} worker: {base}"
    return base


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
    return {
        "cpu": round(cpu, 2),
        "memory_gb": round(ram, 2),
        "storage_gb": round(storage, 2),
        "units": units,
        "monthly": round(compute + stor + managed, 2),
        "currency": p.get("currency", "RUB"),
        "lines": {
            "compute": round(compute, 2),
            "storage": round(stor, 2),
            "managed": round(managed, 2),
        },
    }


def _creds(endpoints: dict | None) -> dict:
    return (endpoints or {}).get("credentials") or {}


def _host() -> str:
    return settings.server_ip


def _domain() -> str:
    return settings.ingress_base_domain


def _svc_block(
    *,
    host: str,
    port: int | str,
    protocol: str,
    username: str | None = None,
    password: str | None = None,
    database: str | None = None,
    url: str | None = None,
    example: str | None = None,
) -> dict:
    return {
        "host": host,
        "port": str(port),
        "protocol": protocol,
        "username": username,
        "password": password,
        "database": database,
        "url": url or f"{host}:{port}",
        "example": example,
    }


def _console_admin(*, stack_id: str, pod_hint: str, web_url: str | None = None, web_user: str | None = None, web_password: str | None = None, web_note: str | None = None) -> dict:
    """Admin block for owner: web UI + in-browser bash console (no host SSH)."""
    console_path = f"/console.html?stack={stack_id}&pod={pod_hint}"
    return {
        "console_url": console_path,
        "console_label": "Открыть bash-консоль ноды",
        "web_url": web_url,
        "web_user": web_user,
        "web_password": web_password,
        "web_note": web_note,
        "hint": "Консоль открывается в браузере (shell внутри контейнера ноды). Доступ только владельцу стека и админу.",
    }


def _nodes_from_spec(
    key: str,
    cfg: dict,
    *,
    stack_id: str,
    stack_name: str,
    namespace: str,
    endpoints: dict | None,
) -> list[dict]:
    units = _unit_count(key, cfg)
    res = cfg.get("resources") or {}
    endpoints = endpoints or {}
    creds = _creds(endpoints)
    host = _host()
    domain = _domain()
    nodes: list[dict] = []
    specs_unit = f"CPU {res.get('cpu', '—')}, RAM {res.get('memory', '—')}" + (
        f", диск {res.get('storage')}" if res.get("storage") else ""
    )

    if key == "clickhouse":
        user = creds.get("clickhouse_user") or "dwh"
        password = creds.get("clickhouse_password")
        web = endpoints.get("clickhouse_web") or f"https://{stack_name}-ch.{domain}"
        for i in range(units):
            http_port = 8123
            native_port = 9000
            pod_hint = f"clickhouse-replica-{i}"
            nodes.append(
                {
                    "title": f"ClickHouse · реплика {i + 1}",
                    "role": f"replica-{i}",
                    "status": "planned",
                    "specs": specs_unit,
                    "service": _svc_block(
                        host=host,
                        port=http_port,
                        protocol="HTTP (ClickHouse)",
                        username=user,
                        password=password,
                        database="default",
                        url=f"http://{host}:{http_port}",
                        example=(
                            f"curl 'http://{host}:{http_port}/?user={user}&password={password or '***'}' -d 'SELECT 1'"
                        ),
                    ),
                    "service_native": _svc_block(
                        host=host,
                        port=native_port,
                        protocol="Native TCP",
                        username=user,
                        password=password,
                        database="default",
                        url=f"clickhouse://{user}@{host}:{native_port}/default",
                        example=f"clickhouse-client -h {host} --port {native_port} -u {user}",
                    ),
                    "admin": _console_admin(
                        stack_id=stack_id,
                        pod_hint=pod_hint,
                        web_url=web,
                        web_user=user,
                        web_password=password,
                        web_note="Веб/HTTP API ClickHouse",
                    ),
                }
            )

    elif key == "kafka":
        ui = endpoints.get("kafka_ui") or f"https://{stack_name}-kafka-ui.{domain}"
        for i in range(units):
            port = 30993 + i
            pod_hint = f"kafka-broker-{i}"
            nodes.append(
                {
                    "title": f"Kafka · брокер {i + 1}",
                    "role": f"broker-{i}",
                    "status": "planned",
                    "specs": specs_unit,
                    "service": _svc_block(
                        host=host,
                        port=port,
                        protocol="Kafka PLAINTEXT",
                        url=f"{host}:{port}",
                        example=f"kcat -b {host}:{port} -L",
                    ),
                    "admin": _console_admin(
                        stack_id=stack_id,
                        pod_hint=pod_hint,
                        web_url=ui,
                        web_note="Kafka UI — топики и сообщения",
                    ),
                }
            )

    elif key == "postgres":
        user = creds.get("postgres_user") or "dwh"
        password = creds.get("postgres_password")
        port = 5432
        nodes.append(
            {
                "title": "PostgreSQL · primary",
                "role": "primary",
                "status": "planned",
                "specs": specs_unit,
                "service": _svc_block(
                    host=host,
                    port=port,
                    protocol="PostgreSQL",
                    username=user,
                    password=password,
                    database="dwh",
                    url=f"postgresql://{user}@{host}:{port}/dwh",
                    example=f"psql -h {host} -p {port} -U {user} -d dwh",
                ),
                "admin": _console_admin(
                    stack_id=stack_id,
                    pod_hint="postgres",
                    web_note="SQL: psql / DBeaver по реквизитам слева; bash — через консоль",
                ),
            }
        )

    elif key == "airflow":
        web = endpoints.get("airflow") or f"https://{stack_name}-airflow.{domain}"
        af_user = creds.get("airflow_user") or "admin"
        af_pass = creds.get("airflow_password") or "admin"
        nodes.append(
            {
                "title": "Airflow · Webserver",
                "role": "webserver",
                "status": "planned",
                "specs": "CPU 0.5, RAM 1Gi",
                "service": _svc_block(
                    host=f"{stack_name}-airflow.{domain}",
                    port=443,
                    protocol="HTTPS",
                    username=af_user,
                    password=af_pass,
                    url=web,
                    example=web,
                ),
                "admin": _console_admin(
                    stack_id=stack_id,
                    pod_hint="airflow-webserver",
                    web_url=web,
                    web_user=af_user,
                    web_password=af_pass,
                    web_note="UI: DAG, логи, переменные",
                ),
            }
        )
        nodes.append(
            {
                "title": "Airflow · Scheduler",
                "role": "scheduler",
                "status": "planned",
                "specs": specs_unit,
                "service": _svc_block(
                    host=host,
                    port="—",
                    protocol="внутренний компонент",
                    url="scheduler (без внешнего порта)",
                    example="Управляется через Airflow UI",
                ),
                "admin": _console_admin(
                    stack_id=stack_id,
                    pod_hint="airflow-scheduler",
                    web_url=web,
                    web_user=af_user,
                    web_password=af_pass,
                    web_note="Планировщик задач",
                ),
            }
        )
        for i in range(units):
            nodes.append(
                {
                    "title": f"Airflow · worker {i + 1}",
                    "role": f"worker-{i}",
                    "status": "logical",
                    "specs": specs_unit,
                    "service": _svc_block(
                        host=host,
                        port="—",
                        protocol="ёмкость / биллинг",
                        example="Слот под параллельные задачи (LocalExecutor)",
                    ),
                    "admin": _console_admin(
                        stack_id=stack_id,
                        pod_hint="airflow-scheduler",
                        web_url=web,
                        web_user=af_user,
                        web_password=af_pass,
                        web_note="Отдельные worker-поды — при CeleryExecutor",
                    ),
                }
            )

    elif key == "superset":
        web = endpoints.get("superset") or f"https://{stack_name}-superset.{domain}"
        nodes.append(
            {
                "title": "Superset · Web",
                "role": "web",
                "status": "planned",
                "specs": specs_unit,
                "service": _svc_block(
                    host=f"{stack_name}-superset.{domain}",
                    port=443,
                    protocol="HTTPS",
                    url=web,
                    example=web,
                ),
                "admin": _console_admin(
                    stack_id=stack_id,
                    pod_hint="superset",
                    web_url=web,
                    web_note="BI-консоль",
                ),
            }
        )

    return nodes


def enrich_nodes_with_pods(nodes: list[dict], pods: list[dict]) -> list[dict]:
    by_name = {p.get("name"): p for p in pods}
    out = []
    for node in nodes:
        n = dict(node)
        role = (n.get("role") or "").lower()
        live = None
        for pname, pdata in by_name.items():
            pl = pname.lower()
            if role.startswith("replica") and "clickhouse" in pl:
                # match index if present
                idx = role.split("-")[-1]
                if idx.isdigit() and pl.endswith(f"-{idx}") or f"-{idx}-" in pl or pl.endswith(idx):
                    live = pdata
                    break
                if live is None and "clickhouse" in pl:
                    live = pdata
            elif role.startswith("broker") and "kafka" in pl and "ui" not in pl:
                idx = role.split("-")[-1]
                if idx.isdigit() and (pl.endswith(f"-{idx}") or f"-{idx}." in pl):
                    live = pdata
                    break
            elif role == "primary" and "postgres" in pl:
                live = pdata
                break
            elif role == "webserver" and "airflow-web" in pl:
                live = pdata
                break
            elif role == "scheduler" and "airflow-sched" in pl:
                live = pdata
                break
            elif role == "web" and "superset" in pl:
                live = pdata
                break
        if live:
            n["status"] = live.get("phase", n.get("status"))
            n["pod_ip"] = live.get("pod_ip")
            n["pod_name"] = live.get("name")
            admin = dict(n.get("admin") or {})
            # Prefer exact pod name in console URL once live pods are known.
            url = admin.get("console_url") or ""
            if "stack=" in url and live.get("name"):
                stack_q = url.split("stack=", 1)[1].split("&", 1)[0]
                admin["console_url"] = f"/console.html?stack={stack_q}&pod={live['name']}"
                n["admin"] = admin
        out.append(n)
    return out


def build_services_inventory(
    spec: dict,
    *,
    stack_id: str,
    stack_name: str,
    namespace: str,
    endpoints: dict | None,
    prices: dict | None,
    live_pods: list[dict] | None = None,
) -> list[dict]:
    endpoints = dict(endpoints or {})
    endpoints["credentials"] = load_stack_credentials(namespace, endpoints)
    services = []
    for key, cfg in (spec or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        if key not in SERVICE_META:
            continue
        units = _unit_count(key, cfg)
        nodes = _nodes_from_spec(
            key,
            cfg,
            stack_id=stack_id,
            stack_name=stack_name,
            namespace=namespace,
            endpoints=endpoints,
        )
        if live_pods:
            related = [
                p
                for p in live_pods
                if key in (p.get("name") or "")
                or (key == "clickhouse" and "clickhouse" in (p.get("name") or ""))
                or (key == "kafka" and "kafka" in (p.get("name") or ""))
                or (key == "airflow" and "airflow" in (p.get("name") or ""))
                or (key == "postgres" and "postgres" in (p.get("name") or ""))
                or (key == "superset" and "superset" in (p.get("name") or ""))
            ]
            nodes = enrich_nodes_with_pods(nodes, related)

        services.append(
            {
                "key": key,
                "name": SERVICE_META[key]["name"],
                "units": units,
                "specs": _specs_text(key, cfg, units),
                "resources": cfg.get("resources") or {},
                "cost": _cost_for_service(key, cfg, prices or {}),
                "nodes": nodes,
            }
        )
    return services


def list_namespace_pods(namespace: str) -> list[dict]:
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


def load_stack_credentials(namespace: str, endpoints: dict | None) -> dict:
    """Prefer endpoints.credentials; fall back to K8s secrets for older stacks."""
    creds = dict((endpoints or {}).get("credentials") or {})
    if creds.get("postgres_password") and creds.get("clickhouse_password"):
        return creds
    try:
        from app.services.provisioner import _get_k8s_clients

        core, *_ = _get_k8s_clients()
        if not creds.get("postgres_password"):
            try:
                sec = core.read_namespaced_secret("postgres-credentials", namespace)
                data = sec.data or {}
                import base64

                if "password" in data:
                    creds["postgres_user"] = creds.get("postgres_user") or "dwh"
                    creds["postgres_password"] = base64.b64decode(data["password"]).decode()
            except Exception:
                pass
        if not creds.get("clickhouse_password"):
            try:
                sec = core.read_namespaced_secret("clickhouse-credentials", namespace)
                data = sec.data or {}
                import base64

                if "password" in data:
                    creds["clickhouse_user"] = creds.get("clickhouse_user") or "dwh"
                    creds["clickhouse_password"] = base64.b64decode(data["password"]).decode()
            except Exception:
                pass
    except Exception:
        pass
    creds.setdefault("airflow_user", "admin")
    creds.setdefault("airflow_password", "admin")
    return creds
