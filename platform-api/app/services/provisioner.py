"""Deploys user DWH stacks to Kubernetes."""

import logging
import secrets
import string
import time
from urllib.parse import quote

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from app.config import settings

logger = logging.getLogger(__name__)


def _get_k8s_clients():
    if settings.kubeconfig_in_cluster:
        config.load_incluster_config()
    else:
        config.load_kube_config()
    return (
        client.CoreV1Api(),
        client.AppsV1Api(),
        client.RbacAuthorizationV1Api(),
        client.NetworkingV1Api(),
        client.CustomObjectsApi(),
    )


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class StackProvisioner:
    """Deploys user DWH stacks to Kubernetes."""

    def __init__(self, stack_id: str, name: str, spec: dict):
        self.stack_id = stack_id
        self.name = name
        self.namespace = f"stack-{stack_id[:8]}"
        self.spec = spec
        self.base_domain = settings.ingress_base_domain
        self.server_ip = settings.server_ip
        self.pg_password = _generate_password()
        self.ch_password = _generate_password()
        self.core, self.apps, self.rbac, self.net, self.custom = _get_k8s_clients()

    def _apply_external_service(self, name: str, selector: dict, ports: list[dict]):
        """Expose service on node IP (externalIPs) for clients outside the cluster."""
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name=name, namespace=self.namespace),
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                external_i_ps=[self.server_ip],
                selector=selector,
                ports=[
                    client.V1ServicePort(
                        name=p["name"],
                        port=p["port"],
                        target_port=p.get("target_port", p["port"]),
                    )
                    for p in ports
                ],
            ),
        )
        try:
            self.core.create_namespaced_service(self.namespace, svc)
        except ApiException as e:
            if e.status != 409:
                raise

    async def deploy(self) -> dict:
        import asyncio

        await asyncio.to_thread(self._create_namespace)
        await asyncio.to_thread(self._create_limit_range)
        await asyncio.to_thread(self._create_resource_quota)
        endpoints = {}

        if self._enabled("postgres") or self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_postgres)
            # External: host IP:5432 (not cluster-local DNS)
            endpoints["postgres"] = f"{self.server_ip}:5432"
            if self._enabled("airflow") or self._enabled("superset"):
                await asyncio.to_thread(self._wait_postgres_ready)

        if self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_redis)

        if self._enabled("clickhouse"):
            await asyncio.to_thread(self._deploy_clickhouse)
            # Client HTTP API (8123) + optional HTTPS web via ingress
            endpoints["clickhouse"] = f"{self.server_ip}:8123"
            endpoints["clickhouse_web"] = f"https://{self.name}-ch.{self.base_domain}"

        if self._enabled("kafka"):
            await asyncio.to_thread(self._deploy_kafka)
            # External bootstrap NodePort (advertised to clients outside the cluster)
            endpoints["kafka"] = f"{self.server_ip}:30993"
            if self.spec.get("kafka", {}).get("ui", True):
                await asyncio.to_thread(self._deploy_kafka_ui)
                endpoints["kafka_ui"] = f"https://{self.name}-kafka-ui.{self.base_domain}"

        if self._enabled("airflow"):
            await asyncio.to_thread(self._deploy_airflow)
            endpoints["airflow"] = f"https://{self.name}-airflow.{self.base_domain}"

        if self._enabled("superset"):
            await asyncio.to_thread(self._deploy_superset)
            endpoints["superset"] = f"https://{self.name}-superset.{self.base_domain}"

        endpoints["credentials"] = {
            "postgres_user": "dwh",
            "postgres_password": self.pg_password,
            "clickhouse_user": "dwh",
            "clickhouse_password": self.ch_password,
            "airflow_user": "admin",
            "airflow_password": "admin",
        }
        return endpoints

    async def delete(self):
        import asyncio

        try:
            await asyncio.to_thread(self.core.delete_namespace, self.namespace)
        except ApiException as e:
            if e.status != 404:
                raise

    def _enabled(self, service: str) -> bool:
        return self.spec.get(service, {}).get("enabled", False)

    def _create_namespace(self):
        ns = client.V1Namespace(
            metadata=client.V1ObjectMeta(
                name=self.namespace,
                labels={"app.kubernetes.io/managed-by": "cloud-dwh", "stack": self.name},
            )
        )
        try:
            self.core.create_namespace(ns)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_limit_range(self):
        """Default container limits so operator-managed pods pass ResourceQuota."""
        lr = client.V1LimitRange(
            metadata=client.V1ObjectMeta(name="stack-limits", namespace=self.namespace),
            spec=client.V1LimitRangeSpec(
                limits=[
                    client.V1LimitRangeItem(
                        type="Container",
                        default={"cpu": "4", "memory": "8Gi"},
                        default_request={"cpu": "100m", "memory": "256Mi"},
                        max={"cpu": "32", "memory": "64Gi"},
                    )
                ]
            ),
        )
        try:
            self.core.create_namespaced_limit_range(self.namespace, lr)
        except ApiException as e:
            if e.status != 409:
                raise

    def _create_resource_quota(self):
        from app.services.clickhouse_topology import unit_count as ch_units

        # Generous buffer: redis, kafka-ui, init jobs, operators sidecars
        total_cpu = 4.0
        total_gi = 8
        for svc in self.spec:
            if not self._enabled(svc):
                continue
            res = self.spec[svc].get("resources", {})
            cpu = float(res.get("cpu", 1))
            mem = res.get("memory", "2Gi")
            mem_gi = int(str(mem).replace("Gi", "").replace("Mi", "") or "0")
            units = 1
            if svc == "clickhouse":
                units = ch_units(self.spec[svc])
            elif svc == "kafka":
                units = int(self.spec[svc].get("brokers", 1))
            total_cpu += cpu * units
            total_gi += mem_gi * units
            if svc == "kafka":
                total_cpu += 0.5  # kafka-ui
                total_gi += 1

        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="stack-quota", namespace=self.namespace),
            spec=client.V1ResourceQuotaSpec(
                hard={
                    "requests.cpu": str(int(total_cpu) + 6),
                    "requests.memory": f"{total_gi + 12}Gi",
                    "persistentvolumeclaims": "30",
                    "pods": "50",
                }
            ),
        )
        try:
            self.core.create_namespaced_resource_quota(self.namespace, quota)
        except ApiException as e:
            if e.status != 409:
                raise

    def _ingress(self, name: str, service: str, port: int, host: str, tls_secret: str):
        return client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.namespace,
                annotations={"cert-manager.io/cluster-issuer": "selfsigned-issuer"},
            ),
            spec=client.V1IngressSpec(
                ingress_class_name="nginx",
                tls=[client.V1IngressTLS(hosts=[host], secret_name=tls_secret)],
                rules=[
                    client.V1IngressRule(
                        host=host,
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path="/",
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name=service,
                                            port=client.V1ServiceBackendPort(number=port),
                                        )
                                    ),
                                )
                            ]
                        ),
                    )
                ],
            ),
        )

    def _apply_dep_svc_ing(self, dep=None, svc=None, ing=None):
        if dep is not None:
            try:
                self.apps.create_namespaced_deployment(self.namespace, dep)
            except ApiException as e:
                if e.status != 409:
                    raise
        if svc is not None:
            try:
                self.core.create_namespaced_service(self.namespace, svc)
            except ApiException as e:
                if e.status != 409:
                    raise
        if ing is not None:
            try:
                self.net.create_namespaced_ingress(self.namespace, ing)
            except ApiException as e:
                if e.status != 409:
                    raise

    def _wait_postgres_ready(self, timeout_sec: int = 300):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            pods = self.core.list_namespaced_pod(
                self.namespace, label_selector="cnpg.io/cluster=postgres"
            )
            for pod in pods.items:
                if pod.status.phase != "Running":
                    continue
                for cs in pod.status.container_statuses or []:
                    if cs.ready:
                        return
            time.sleep(5)
        raise TimeoutError(f"Postgres not ready in {self.namespace} after {timeout_sec}s")

    def _deploy_postgres(self):
        cfg = self.spec.get("postgres", {})
        res = cfg.get("resources", {})
        pg_host = f"postgres-rw.{self.namespace}.svc"
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name="postgres-credentials", namespace=self.namespace),
            string_data={
                "username": "dwh",
                "password": self.pg_password,
                "airflow-db-url": (
                    f"postgresql+psycopg2://dwh:{self.pg_password}@{pg_host}:5432/airflow"
                ),
                "superset-db-url": (
                    f"postgresql+psycopg2://dwh:{self.pg_password}@{pg_host}:5432/superset"
                ),
            },
        )
        try:
            self.core.create_namespaced_secret(self.namespace, secret)
        except ApiException as e:
            if e.status != 409:
                raise

        cluster = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": "postgres", "namespace": self.namespace},
            "spec": {
                "instances": 1,
                "imageName": "ghcr.io/cloudnative-pg/postgresql:16.6",
                "storage": {"size": res.get("storage", "10Gi")},
                "resources": {
                    "requests": {
                        "cpu": str(res.get("cpu", "1")),
                        "memory": res.get("memory", "2Gi"),
                    },
                    "limits": {
                        "cpu": str(res.get("cpu", "1")),
                        "memory": res.get("memory", "2Gi"),
                    },
                },
                "bootstrap": {
                    "initdb": {
                        "database": "dwh",
                        "owner": "dwh",
                        "secret": {"name": "postgres-credentials"},
                        "postInitSQL": [
                            "CREATE DATABASE airflow OWNER dwh;",
                            "CREATE DATABASE superset OWNER dwh;",
                        ],
                    }
                },
            },
        }
        try:
            self.custom.create_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", self.namespace, "clusters", cluster
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # External access: 192.168.x.x:5432 → postgres pods
        self._apply_external_service(
            "postgres-external",
            {"cnpg.io/cluster": "postgres", "role": "primary"},
            [{"name": "postgresql", "port": 5432, "target_port": 5432}],
        )

    def _deploy_redis(self):
        labels = {"app": "redis"}
        dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="redis", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="redis",
                                image="redis:7-alpine",
                                ports=[client.V1ContainerPort(container_port=6379)],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "100m", "memory": "128Mi"},
                                    limits={"cpu": "500m", "memory": "256Mi"},
                                ),
                            )
                        ]
                    ),
                ),
            ),
        )
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="redis", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector=labels, ports=[client.V1ServicePort(port=6379, target_port=6379)]
            ),
        )
        self._apply_dep_svc_ing(dep=dep, svc=svc)

    def _deploy_clickhouse(self):
        from app.services.clickhouse_topology import layout_for_chi

        cfg = self.spec.get("clickhouse", {})
        res = cfg.get("resources", {})
        layout = layout_for_chi(cfg)

        ch_secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name="clickhouse-credentials", namespace=self.namespace),
            string_data={
                "username": "dwh",
                "password": self.ch_password,
                # HTTP interface — used by clickhouse-connect / Superset
                "sqlalchemy-uri": (
                    f"clickhousedb://dwh:{quote(self.ch_password, safe='')}"
                    f"@clickhouse-clickhouse.{self.namespace}.svc:8123/default"
                ),
            },
        )
        try:
            self.core.create_namespaced_secret(self.namespace, ch_secret)
        except ApiException as e:
            if e.status != 409:
                raise

        chi = {
            "apiVersion": "clickhouse.altinity.com/v1",
            "kind": "ClickHouseInstallation",
            "metadata": {"name": "clickhouse", "namespace": self.namespace},
            "spec": {
                "defaults": {
                    "templates": {
                        "podTemplate": "clickhouse-pod",
                        "dataVolumeClaimTemplate": "data",
                        "serviceTemplate": "ch-service",
                    }
                },
                "configuration": {
                    "clusters": [
                        {
                            "name": "dwh",
                            "layout": layout,
                        }
                    ],
                    "users": {
                        "dwh/password": self.ch_password,
                        "dwh/networks/ip": ["0.0.0.0/0"],
                        "dwh/profile": "default",
                    },
                },
                "templates": {
                    "podTemplates": [
                        {
                            "name": "clickhouse-pod",
                            "spec": {
                                "containers": [
                                    {
                                        "name": "clickhouse",
                                        "image": "clickhouse/clickhouse-server:24.8",
                                        "resources": {
                                            "requests": {
                                                "cpu": str(res.get("cpu", "2")),
                                                "memory": res.get("memory", "8Gi"),
                                            },
                                            "limits": {
                                                "cpu": str(res.get("cpu", "2")),
                                                "memory": res.get("memory", "8Gi"),
                                            },
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                    "volumeClaimTemplates": [
                        {
                            "name": "data",
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {
                                    "requests": {"storage": res.get("storage", "50Gi")}
                                },
                            },
                        }
                    ],
                    "serviceTemplates": [
                        {
                            "name": "ch-service",
                            "spec": {
                                "ports": [
                                    {"name": "http", "port": 8123},
                                    {"name": "native", "port": 9000},
                                ],
                                "type": "ClusterIP",
                            },
                        }
                    ],
                },
            },
        }
        try:
            self.custom.create_namespaced_custom_object(
                "clickhouse.altinity.com", "v1", self.namespace, "clickhouseinstallations", chi
            )
        except ApiException as e:
            if e.status != 409:
                raise

        # Ingress → ClickHouse HTTP (browser / HTTPS)
        host = f"{self.name}-ch.{self.base_domain}"
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="clickhouse-http", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector={"clickhouse.altinity.com/chi": "clickhouse"},
                ports=[client.V1ServicePort(name="http", port=8123, target_port=8123)],
            ),
        )
        ing = self._ingress("clickhouse", "clickhouse-http", 8123, host, "clickhouse-tls")
        self._apply_dep_svc_ing(svc=svc, ing=ing)

        # External clients: IP:8123 (HTTP) and IP:9000 (native)
        self._apply_external_service(
            "clickhouse-external",
            {"clickhouse.altinity.com/chi": "clickhouse"},
            [
                {"name": "http", "port": 8123, "target_port": 8123},
                {"name": "native", "port": 9000, "target_port": 9000},
            ],
        )

    def _deploy_kafka(self):
        """Strimzi 0.48+: KRaft + KafkaNodePool, Kafka 4.0."""
        cfg = self.spec.get("kafka", {})
        res = cfg.get("resources", {})
        brokers = cfg.get("brokers", 1)

        node_pool = {
            "apiVersion": "kafka.strimzi.io/v1beta2",
            "kind": "KafkaNodePool",
            "metadata": {
                "name": "dual-role",
                "namespace": self.namespace,
                "labels": {"strimzi.io/cluster": "kafka"},
            },
            "spec": {
                "replicas": brokers,
                "roles": ["controller", "broker"],
                "storage": {
                    "type": "persistent-claim",
                    "size": res.get("storage", "20Gi"),
                    "deleteClaim": True,
                },
                "resources": {
                    "requests": {
                        "cpu": str(res.get("cpu", "2")),
                        "memory": res.get("memory", "4Gi"),
                    },
                    "limits": {
                        "cpu": str(res.get("cpu", "2")),
                        "memory": res.get("memory", "4Gi"),
                    },
                },
            },
        }
        kafka = {
            "apiVersion": "kafka.strimzi.io/v1beta2",
            "kind": "Kafka",
            "metadata": {
                "name": "kafka",
                "namespace": self.namespace,
                "annotations": {
                    "strimzi.io/node-pools": "enabled",
                    "strimzi.io/kraft": "enabled",
                },
            },
            "spec": {
                "kafka": {
                    "version": "4.0.0",
                    "metadataVersion": "4.0-IV3",
                    "listeners": [
                        {"name": "plain", "port": 9092, "type": "internal", "tls": False},
                        {"name": "tls", "port": 9093, "type": "internal", "tls": True},
                        {
                            "name": "external",
                            "port": 9094,
                            "type": "nodeport",
                            "tls": False,
                            "configuration": {
                                "bootstrap": {"nodePort": 30993},
                                "brokers": [
                                    {
                                        "broker": i,
                                        "advertisedHost": self.server_ip,
                                        "nodePort": 30994 + i,
                                    }
                                    for i in range(brokers)
                                ],
                            },
                        },
                    ],
                    "config": {
                        "offsets.topic.replication.factor": min(brokers, 3),
                        "transaction.state.log.replication.factor": min(brokers, 3),
                        "transaction.state.log.min.isr": 1,
                        "default.replication.factor": min(brokers, 3),
                        "min.insync.replicas": 1,
                    },
                },
                "entityOperator": {"topicOperator": {}, "userOperator": {}},
            },
        }
        try:
            self.custom.create_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", self.namespace, "kafkanodepools", node_pool
            )
        except ApiException as e:
            if e.status != 409:
                raise
        try:
            self.custom.create_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", self.namespace, "kafkas", kafka
            )
        except ApiException as e:
            if e.status != 409:
                raise

    def _deploy_kafka_ui(self):
        labels = {"app": "kafka-ui"}
        bootstrap = f"kafka-kafka-bootstrap.{self.namespace}.svc.cluster.local:9092"
        dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="kafka-ui", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="kafka-ui",
                                image="provectuslabs/kafka-ui:v0.7.2",
                                ports=[client.V1ContainerPort(container_port=8080)],
                                env=[
                                    client.V1EnvVar(name="KAFKA_CLUSTERS_0_NAME", value=self.name),
                                    client.V1EnvVar(
                                        name="KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS", value=bootstrap
                                    ),
                                    client.V1EnvVar(name="DYNAMIC_CONFIG_ENABLED", value="true"),
                                ],
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/", port=8080),
                                    initial_delay_seconds=45,
                                    period_seconds=15,
                                    timeout_seconds=5,
                                    failure_threshold=10,
                                ),
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "200m", "memory": "512Mi"},
                                    limits={"cpu": "1", "memory": "1Gi"},
                                ),
                            )
                        ]
                    ),
                ),
            ),
        )
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="kafka-ui", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector=labels, ports=[client.V1ServicePort(port=8080, target_port=8080)]
            ),
        )
        host = f"{self.name}-kafka-ui.{self.base_domain}"
        ing = self._ingress("kafka-ui", "kafka-ui", 8080, host, "kafka-ui-tls")
        self._apply_dep_svc_ing(dep=dep, svc=svc, ing=ing)

    def _airflow_env(self) -> list:
        return [
            client.V1EnvVar(
                name="AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name="postgres-credentials",
                        key="airflow-db-url",
                    )
                ),
            ),
            client.V1EnvVar(name="AIRFLOW__CORE__EXECUTOR", value="LocalExecutor"),
            client.V1EnvVar(name="AIRFLOW__CORE__LOAD_EXAMPLES", value="false"),
            client.V1EnvVar(name="AIRFLOW__WEBSERVER__EXPOSE_CONFIG", value="true"),
        ]

    def _deploy_airflow(self):
        cfg = self.spec.get("airflow", {})
        res = cfg.get("resources", {})
        # LocalExecutor: webserver + scheduler. Tasks run in the scheduler process
        # (no Celery workers). workers in preset is reserved for future CeleryExecutor.
        cpu_req = str(res.get("cpu", "1"))
        mem_req = res.get("memory", "2Gi")
        # Split budget roughly: webserver lighter, scheduler does the work
        ws_cpu, sch_cpu = "500m", cpu_req
        ws_mem = "1Gi" if str(mem_req).endswith("Gi") else mem_req
        sch_mem = mem_req

        ws_labels = {"app": "airflow-webserver"}
        ws_cmd = (
            "airflow db migrate && "
            "airflow users create --username admin --password admin "
            "--firstname Admin --lastname User --role Admin --email admin@example.com || true; "
            "exec airflow webserver"
        )
        ws_dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="airflow-webserver", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=ws_labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=ws_labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="webserver",
                                image="apache/airflow:2.7.3-python3.10",
                                command=["bash", "-c", ws_cmd],
                                ports=[client.V1ContainerPort(container_port=8080)],
                                env=self._airflow_env(),
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/health", port=8080),
                                    initial_delay_seconds=60,
                                    period_seconds=15,
                                    failure_threshold=20,
                                ),
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": ws_cpu, "memory": ws_mem},
                                    limits={"cpu": "1", "memory": "2Gi"},
                                ),
                            )
                        ]
                    ),
                ),
            ),
        )
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="airflow-webserver", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector=ws_labels, ports=[client.V1ServicePort(port=8080, target_port=8080)]
            ),
        )
        host = f"{self.name}-airflow.{self.base_domain}"
        ing = self._ingress("airflow", "airflow-webserver", 8080, host, "airflow-tls")
        self._apply_dep_svc_ing(dep=ws_dep, svc=svc, ing=ing)

        sch_labels = {"app": "airflow-scheduler"}
        sch_dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="airflow-scheduler", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=sch_labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=sch_labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="scheduler",
                                image="apache/airflow:2.7.3-python3.10",
                                command=["bash", "-c", "exec airflow scheduler"],
                                env=self._airflow_env(),
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": sch_cpu, "memory": sch_mem},
                                    limits={
                                        "cpu": str(res.get("cpu", "2")),
                                        "memory": res.get("memory", "4Gi"),
                                    },
                                ),
                            )
                        ]
                    ),
                ),
            ),
        )
        self._apply_dep_svc_ing(dep=sch_dep)

    def _deploy_superset(self):
        cfg = self.spec.get("superset", {})
        res = cfg.get("resources", {})
        labels = {"app": "superset"}
        config_py = (
            "import os\n"
            "SQLALCHEMY_DATABASE_URI = os.environ['DATABASE_URL']\n"
            "SECRET_KEY = os.environ['SUPERSET_SECRET_KEY']\n"
            "WTF_CSRF_ENABLED = True\n"
            "# clickhouse-connect registers dialect clickhousedb+connect\n"
            "try:\n"
            "    import clickhouse_connect  # noqa: F401\n"
            "except ImportError:\n"
            "    pass\n"
        )
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(name="superset-config", namespace=self.namespace),
            data={"superset_config.py": config_py},
        )
        try:
            self.core.create_namespaced_config_map(self.namespace, cm)
        except ApiException as e:
            if e.status != 409:
                raise

        # Install drivers + optionally register ClickHouse after init
        register_ch = ""
        if self._enabled("clickhouse"):
            register_ch = (
                "if [ -n \"$CLICKHOUSE_SQLALCHEMY_URI\" ]; then "
                "python - <<'PY'\n"
                "import os\n"
                "from superset.app import create_app\n"
                "app = create_app()\n"
                "with app.app_context():\n"
                "    from superset import db\n"
                "    from superset.models.core import Database\n"
                "    uri = os.environ['CLICKHOUSE_SQLALCHEMY_URI']\n"
                "    existing = db.session.query(Database).filter_by(database_name='ClickHouse').first()\n"
                "    if existing:\n"
                "        existing.sqlalchemy_uri = uri\n"
                "        existing.expose_in_sqllab = True\n"
                "    else:\n"
                "        db.session.add(Database(\n"
                "            database_name='ClickHouse', sqlalchemy_uri=uri, expose_in_sqllab=True))\n"
                "    db.session.commit()\n"
                "    print('ClickHouse database registered')\n"
                "PY\n"
                "fi && "
            )

        start_cmd = (
            "pip install -q psycopg2-binary 'clickhouse-connect>=0.7.0' && "
            "superset db upgrade && "
            "superset fab create-admin --username admin --firstname Admin "
            "--lastname User --email admin@example.com --password admin || true && "
            "superset init && "
            f"{register_ch}"
            "exec gunicorn --bind 0.0.0.0:8088 --workers 1 --timeout 120 "
            "'superset.app:create_app()'"
        )

        env = [
            client.V1EnvVar(
                name="SUPERSET_SECRET_KEY",
                value=_generate_password(32),
            ),
            client.V1EnvVar(
                name="DATABASE_URL",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name="postgres-credentials",
                        key="superset-db-url",
                    )
                ),
            ),
            client.V1EnvVar(
                name="SUPERSET_CONFIG_PATH",
                value="/app/pythonpath/superset_config.py",
            ),
            client.V1EnvVar(name="PYTHONPATH", value="/app/pythonpath"),
        ]
        if self._enabled("clickhouse"):
            env.append(
                client.V1EnvVar(
                    name="CLICKHOUSE_SQLALCHEMY_URI",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name="clickhouse-credentials",
                            key="sqlalchemy-uri",
                        )
                    ),
                )
            )

        dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="superset", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="superset",
                                image="apache/superset:4.0.2",
                                command=["bash", "-c", start_cmd],
                                ports=[client.V1ContainerPort(container_port=8088)],
                                env=env,
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="config", mount_path="/app/pythonpath"
                                    )
                                ],
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/health", port=8088),
                                    initial_delay_seconds=120,
                                    period_seconds=20,
                                    failure_threshold=30,
                                ),
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "cpu": str(res.get("cpu", "1")),
                                        "memory": res.get("memory", "2Gi"),
                                    },
                                    limits={
                                        "cpu": str(res.get("cpu", "2")),
                                        "memory": res.get("memory", "2Gi"),
                                    },
                                ),
                            )
                        ],
                        volumes=[
                            client.V1Volume(
                                name="config",
                                config_map=client.V1ConfigMapVolumeSource(name="superset-config"),
                            )
                        ],
                    ),
                ),
            ),
        )
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="superset", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector=labels, ports=[client.V1ServicePort(port=8088, target_port=8088)]
            ),
        )
        host = f"{self.name}-superset.{self.base_domain}"
        ing = self._ingress("superset", "superset", 8088, host, "superset-tls")
        self._apply_dep_svc_ing(dep=dep, svc=svc, ing=ing)

    async def stop(self):
        import asyncio

        await asyncio.to_thread(self._scale_namespace_workloads, target=0, save_prev=True, delete_pods=True)

    async def start(self):
        import asyncio

        await asyncio.to_thread(self._scale_namespace_workloads, target=None, save_prev=False, delete_pods=False)

    async def restart(self, progress=None, hard: bool = False, wait_ready_sec: int = 180):
        """Normal restart = scale off → on. Hard = delete pods (emergency)."""
        import asyncio

        if hard:
            await asyncio.to_thread(
                self._restart_pods,
                False,
                0,
                progress,
                wait_ready_sec,
                True,
            )
        else:
            await asyncio.to_thread(self._power_cycle, progress, wait_ready_sec)

    def _power_cycle(self, progress=None, wait_ready_sec: int = 180):
        """Restart by powering workloads off then on (no pod delete)."""
        import time

        def report(msg: str):
            logger.info("power-cycle %s: %s", self.namespace, msg)
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        before = [
            p
            for p in self.core.list_namespaced_pod(self.namespace).items
            if (p.status.phase or "") not in ("Succeeded", "Failed")
        ]
        expected = max(1, len(before))
        report(f"Перезагрузка выкл/вкл: сейчас {len(before)} под(ов)")

        report("Выключение: scale workloads → 0…")
        self._scale_namespace_workloads(target=0, save_prev=True, delete_pods=False)

        off_deadline = time.time() + min(90, max(30, wait_ready_sec // 2))
        while time.time() < off_deadline:
            items = self.core.list_namespaced_pod(self.namespace).items
            running = [
                p
                for p in items
                if (p.status.phase or "") == "Running" and not p.metadata.deletion_timestamp
            ]
            terminating = [p for p in items if p.metadata.deletion_timestamp]
            report(f"Выключение… Running {len(running)}, terminating {len(terminating)}")
            if len(running) == 0:
                break
            time.sleep(3)
        else:
            report("Выключение неполное — продолжаем включение")

        # Brief settle so controllers accept scale-up after scale-down
        time.sleep(2)
        report("Включение: восстановление replica counts…")
        self._scale_namespace_workloads(target=None, save_prev=False, delete_pods=False)

        report("Ожидание готовности подов…")
        deadline = time.time() + max(30, int(wait_ready_sec))
        last_msg = ""
        while time.time() < deadline:
            items = self.core.list_namespaced_pod(self.namespace).items
            active = [
                p
                for p in items
                if (p.status.phase or "") not in ("Succeeded", "Failed")
            ]
            terminating = [p for p in active if p.metadata.deletion_timestamp]
            ready = [p for p in active if self._pod_ready(p) and not p.metadata.deletion_timestamp]
            pending = [p for p in active if not p.metadata.deletion_timestamp and p not in ready]
            msg = (
                f"Готово {len(ready)}/{max(expected, len(active))}"
                f" · terminating {len(terminating)} · pending {len(pending)}"
            )
            if msg != last_msg:
                report(msg)
                last_msg = msg
            if not terminating and len(ready) >= expected:
                report(f"Перезагрузка завершена: {len(ready)} под(ов) Ready")
                return
            if not terminating and ready and len(ready) == len(active) and len(ready) >= max(1, expected // 2):
                report(f"Перезагрузка завершена: {len(ready)} Ready из {len(active)}")
                return
            time.sleep(3)

        items = self.core.list_namespaced_pod(self.namespace).items
        ready_n = sum(1 for p in items if self._pod_ready(p))
        raise TimeoutError(
            f"Таймаут перезагрузки выкл/вкл ({wait_ready_sec}с): Ready {ready_n}/{expected}. "
            "Можно выполнить жёсткую перезагрузку (удаление подов)."
        )

    def _scale_namespace_workloads(self, target: int | None, save_prev: bool, delete_pods: bool = True):
        """Scale Deployments/StatefulSets. target=None restores previous replica counts."""
        deps = self.apps.list_namespaced_deployment(self.namespace).items
        for dep in deps:
            name = dep.metadata.name
            annotations = dict(dep.metadata.annotations or {})
            if save_prev and target == 0:
                annotations["cloud-dwh/prev-replicas"] = str(dep.spec.replicas or 1)
                body = {"metadata": {"annotations": annotations}, "spec": {"replicas": 0}}
            else:
                prev = int(annotations.get("cloud-dwh/prev-replicas", "1") or "1")
                replicas = prev if target is None else target
                body = {"spec": {"replicas": replicas}}
            self.apps.patch_namespaced_deployment(name, self.namespace, body)

        stss = self.apps.list_namespaced_stateful_set(self.namespace).items
        for sts in stss:
            name = sts.metadata.name
            annotations = dict(sts.metadata.annotations or {})
            if save_prev and target == 0:
                annotations["cloud-dwh/prev-replicas"] = str(sts.spec.replicas or 1)
                body = {"metadata": {"annotations": annotations}, "spec": {"replicas": 0}}
            else:
                prev = int(annotations.get("cloud-dwh/prev-replicas", "1") or "1")
                replicas = prev if target is None else target
                body = {"spec": {"replicas": replicas}}
            self.apps.patch_namespaced_stateful_set(name, self.namespace, body)

        # CloudNativePG hibernation
        try:
            clusters = self.custom.list_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", self.namespace, "clusters"
            ).get("items", [])
            for cluster in clusters:
                cname = cluster["metadata"]["name"]
                annotations = dict(cluster["metadata"].get("annotations") or {})
                if save_prev and target == 0:
                    annotations["cnpg.io/hibernation"] = "on"
                else:
                    annotations.pop("cnpg.io/hibernation", None)
                self.custom.patch_namespaced_custom_object(
                    "postgresql.cnpg.io",
                    "v1",
                    self.namespace,
                    "clusters",
                    cname,
                    {"metadata": {"annotations": annotations}},
                )
        except ApiException as e:
            if e.status not in (404, 403):
                logger.warning("CNPG hibernation patch failed: %s", e)

        # ClickHouse scale (preserve explicit shards layout when stopping/starting)
        try:
            import json

            chis = self.custom.list_namespaced_custom_object(
                "clickhouse.altinity.com", "v1", self.namespace, "clickhouseinstallations"
            ).get("items", [])
            for chi in chis:
                cname = chi["metadata"]["name"]
                annotations = dict(chi["metadata"].get("annotations") or {})
                clusters = chi.get("spec", {}).get("configuration", {}).get("clusters", [])
                if not clusters:
                    continue
                layout = clusters[0].get("layout") or {}
                if save_prev and target == 0:
                    annotations["cloud-dwh/prev-ch-layout"] = json.dumps(layout)
                    # Scale each shard to 0 replicas (operator may keep CR valid with empty pods)
                    stopped_shards = []
                    if layout.get("shards"):
                        for shard in layout["shards"]:
                            stopped_shards.append({"replicasCount": 0})
                        new_layout = {"shards": stopped_shards}
                    else:
                        new_layout = {"shardsCount": layout.get("shardsCount", 1), "replicasCount": 0}
                else:
                    prev = annotations.get("cloud-dwh/prev-ch-layout")
                    if prev:
                        try:
                            new_layout = json.loads(prev)
                        except Exception:
                            new_layout = layout
                    else:
                        new_layout = layout
                        if target is not None and "replicasCount" in new_layout:
                            new_layout = {**new_layout, "replicasCount": target}
                patch = {
                    "metadata": {"annotations": annotations},
                    "spec": {
                        "configuration": {
                            "clusters": [{"layout": new_layout}]
                        }
                    },
                }
                self.custom.patch_namespaced_custom_object(
                    "clickhouse.altinity.com",
                    "v1",
                    self.namespace,
                    "clickhouseinstallations",
                    cname,
                    patch,
                )
        except ApiException as e:
            if e.status not in (404, 403):
                logger.warning("ClickHouse scale patch failed: %s", e)

        # Kafka replicas (Strimzi)
        try:
            kafkas = self.custom.list_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", self.namespace, "kafkas"
            ).get("items", [])
            for kafka in kafkas:
                kname = kafka["metadata"]["name"]
                annotations = dict(kafka["metadata"].get("annotations") or {})
                current = kafka.get("spec", {}).get("kafka", {}).get("replicas", 1)
                if save_prev and target == 0:
                    annotations["cloud-dwh/prev-replicas"] = str(current)
                    replicas = 0
                else:
                    replicas = int(annotations.get("cloud-dwh/prev-replicas", str(current)) or "1")
                    if target is not None:
                        replicas = target
                # Strimzi often requires replicas >= 1; on stop delete pods via deployment scale
                if replicas == 0:
                    replicas = 1  # keep CR valid; hibernation approximated by scaling sts above
                patch = {
                    "metadata": {"annotations": annotations},
                    "spec": {"kafka": {"replicas": replicas}},
                }
                self.custom.patch_namespaced_custom_object(
                    "kafka.strimzi.io", "v1beta2", self.namespace, "kafkas", kname, patch
                )
        except ApiException as e:
            if e.status not in (404, 403):
                logger.warning("Kafka scale patch failed: %s", e)

        if delete_pods and save_prev and target == 0:
            # Ensure compute is down even if CR controllers fight back briefly
            self._restart_pods(delete_only=True, grace_period_seconds=0)

    def _pod_ready(self, pod) -> bool:
        if pod.status.phase != "Running":
            return False
        statuses = pod.status.container_statuses or []
        if not statuses:
            return False
        return all(cs.ready for cs in statuses)

    def _restart_pods(
        self,
        delete_only: bool = False,
        grace_period_seconds: int = 15,
        progress=None,
        wait_ready_sec: int = 180,
        hard: bool = False,
    ):
        import time

        def report(msg: str):
            logger.info("restart %s: %s", self.namespace, msg)
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        pods = self.core.list_namespaced_pod(self.namespace).items
        # Skip finished jobs
        pods = [
            p
            for p in pods
            if (p.status.phase or "") not in ("Succeeded", "Failed")
        ]
        expected = len(pods)
        mode = "жёсткая (grace=0)" if hard or grace_period_seconds == 0 else f"grace={grace_period_seconds}s"
        report(f"Перезагрузка: найдено {expected} под(ов), режим {mode}")

        for i, pod in enumerate(pods, start=1):
            name = pod.metadata.name
            report(f"Удаление пода {i}/{expected}: {name}")
            try:
                self.core.delete_namespaced_pod(
                    name,
                    self.namespace,
                    grace_period_seconds=grace_period_seconds,
                    propagation_policy="Background",
                    _request_timeout=30,
                )
            except ApiException as e:
                if e.status != 404:
                    logger.warning("Pod delete failed %s: %s", name, e)
                    report(f"Ошибка удаления {name}: {e.reason or e.status}")

        if delete_only:
            report("Поды удалены (режим stop)")
            return

        report("Ожидание пересоздания и готовности подов…")
        deadline = time.time() + max(30, int(wait_ready_sec))
        last_msg = ""
        while time.time() < deadline:
            items = self.core.list_namespaced_pod(self.namespace).items
            active = [
                p
                for p in items
                if (p.status.phase or "") not in ("Succeeded", "Failed")
            ]
            terminating = [p for p in active if p.metadata.deletion_timestamp]
            ready = [p for p in active if self._pod_ready(p) and not p.metadata.deletion_timestamp]
            pending = [
                p
                for p in active
                if not p.metadata.deletion_timestamp and p not in ready
            ]
            msg = (
                f"Готово {len(ready)}/{max(expected, len(active))}"
                f" · terminating {len(terminating)} · pending {len(pending)}"
            )
            if msg != last_msg:
                report(msg)
                last_msg = msg
            # Success: no terminating leftovers and enough ready pods
            if not terminating and len(ready) >= max(1, expected):
                report(f"Перезагрузка завершена: {len(ready)} под(ов) Ready")
                return
            # Soft success: no terminating, all current pods ready, at least one
            if not terminating and ready and len(ready) == len(active) and len(ready) >= max(1, expected // 2):
                report(f"Перезагрузка завершена (частично): {len(ready)} Ready из {len(active)}")
                return
            time.sleep(3)

        # Timeout — leave controllers to finish; surface clear error for lifecycle
        items = self.core.list_namespaced_pod(self.namespace).items
        ready_n = sum(1 for p in items if self._pod_ready(p))
        raise TimeoutError(
            f"Таймаут перезагрузки ({wait_ready_sec}с): Ready {ready_n}/{expected}. "
            "Можно выполнить жёсткую перезагрузку подов."
        )

