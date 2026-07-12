"""Deploys user DWH stacks to Kubernetes."""

import logging
import secrets
import string

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
        self.pg_password = _generate_password()
        self.core, self.apps, self.rbac, self.net, self.custom = _get_k8s_clients()

    async def deploy(self) -> dict:
        import asyncio

        await asyncio.to_thread(self._create_namespace)
        await asyncio.to_thread(self._create_resource_quota)
        endpoints = {}

        if self._enabled("postgres") or self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_postgres)
            endpoints["postgres"] = f"postgres-rw.{self.namespace}.svc.cluster.local:5432"

        if self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_redis)

        if self._enabled("clickhouse"):
            await asyncio.to_thread(self._deploy_clickhouse)
            endpoints["clickhouse"] = f"https://{self.name}-ch.{self.base_domain}"

        if self._enabled("kafka"):
            await asyncio.to_thread(self._deploy_kafka)
            endpoints["kafka"] = f"kafka-kafka-bootstrap.{self.namespace}.svc.cluster.local:9092"
            if self.spec.get("kafka", {}).get("ui", True):
                await asyncio.to_thread(self._deploy_kafka_ui)
                endpoints["kafka_ui"] = f"https://{self.name}-kafka-ui.{self.base_domain}"

        if self._enabled("airflow"):
            await asyncio.to_thread(self._deploy_airflow)
            endpoints["airflow"] = f"https://{self.name}-airflow.{self.base_domain}"

        if self._enabled("superset"):
            await asyncio.to_thread(self._deploy_superset)
            endpoints["superset"] = f"https://{self.name}-superset.{self.base_domain}"

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

    def _create_resource_quota(self):
        # Generous buffer: redis, kafka-ui, init jobs, operators sidecars
        total_cpu = 4.0
        total_gi = 8
        for svc in self.spec:
            if not self._enabled(svc):
                continue
            res = self.spec[svc].get("resources", {})
            total_cpu += float(res.get("cpu", 1))
            mem = res.get("memory", "2Gi")
            total_gi += int(str(mem).replace("Gi", "").replace("Mi", "") or "0")
            if svc == "clickhouse":
                total_cpu *= max(1, int(self.spec[svc].get("replicas", 1)))
                total_gi *= max(1, int(self.spec[svc].get("replicas", 1)))
            if svc == "kafka":
                brokers = int(self.spec[svc].get("brokers", 1))
                total_cpu += float(res.get("cpu", 1)) * (brokers - 1)
                total_gi += int(str(res.get("memory", "2Gi")).replace("Gi", "") or "0") * (brokers - 1)
                total_cpu += 0.5  # kafka-ui
                total_gi += 1

        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="stack-quota", namespace=self.namespace),
            spec=client.V1ResourceQuotaSpec(
                hard={
                    "requests.cpu": str(int(total_cpu) + 4),
                    "requests.memory": f"{total_gi + 8}Gi",
                    "limits.cpu": str(int(total_cpu) + 8),
                    "limits.memory": f"{total_gi + 16}Gi",
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

    def _deploy_postgres(self):
        cfg = self.spec.get("postgres", {})
        res = cfg.get("resources", {})
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name="postgres-credentials", namespace=self.namespace),
            string_data={"username": "dwh", "password": self.pg_password},
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
                    }
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
        cfg = self.spec.get("clickhouse", {})
        res = cfg.get("resources", {})
        replicas = cfg.get("replicas", 1)
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
                            "layout": {"shardsCount": 1, "replicasCount": replicas},
                        }
                    ],
                    "users": {
                        "dwh/password": _generate_password(),
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
                                    {"name": "tcp", "port": 9000},
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

        # Ingress → ClickHouse HTTP (8123) via CHI service clickhouse-clickhouse
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
                                readinessProbe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/", port=8080),
                                    initial_delay_seconds=20,
                                    period_seconds=10,
                                ),
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "100m", "memory": "256Mi"},
                                    limits={"cpu": "500m", "memory": "512Mi"},
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

    def _deploy_airflow(self):
        cfg = self.spec.get("airflow", {})
        res = cfg.get("resources", {})
        labels = {"app": "airflow-webserver"}
        db_url = (
            f"postgresql+psycopg2://dwh:{self.pg_password}"
            f"@postgres-rw.{self.namespace}.svc:5432/airflow"
        )
        # Init DB then start webserver (LocalExecutor — no celery workers needed for MVP)
        start_cmd = (
            "airflow db migrate && "
            "airflow users create --username admin --password admin "
            "--firstname Admin --lastname User --role Admin --email admin@example.com || true; "
            "exec airflow webserver"
        )
        dep = client.V1Deployment(
            metadata=client.V1ObjectMeta(name="airflow-webserver", namespace=self.namespace),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="webserver",
                                image="apache/airflow:2.7.3-python3.10",
                                command=["bash", "-c", start_cmd],
                                ports=[client.V1ContainerPort(container_port=8080)],
                                env=[
                                    client.V1EnvVar(
                                        name="AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", value=db_url
                                    ),
                                    client.V1EnvVar(
                                        name="AIRFLOW__CORE__EXECUTOR", value="LocalExecutor"
                                    ),
                                    client.V1EnvVar(
                                        name="AIRFLOW__CORE__LOAD_EXAMPLES", value="false"
                                    ),
                                    client.V1EnvVar(
                                        name="AIRFLOW__WEBSERVER__EXPOSE_CONFIG", value="true"
                                    ),
                                ],
                                readinessProbe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/health", port=8080),
                                    initial_delay_seconds=60,
                                    period_seconds=15,
                                    failure_threshold=20,
                                ),
                                resources=client.V1ResourceRequirements(
                                    requests={
                                        "cpu": str(res.get("cpu", "1")),
                                        "memory": res.get("memory", "2Gi"),
                                    },
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
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(name="airflow-webserver", namespace=self.namespace),
            spec=client.V1ServiceSpec(
                selector=labels, ports=[client.V1ServicePort(port=8080, target_port=8080)]
            ),
        )
        host = f"{self.name}-airflow.{self.base_domain}"
        ing = self._ingress("airflow", "airflow-webserver", 8080, host, "airflow-tls")
        self._apply_dep_svc_ing(dep=dep, svc=svc, ing=ing)

    def _deploy_superset(self):
        cfg = self.spec.get("superset", {})
        res = cfg.get("resources", {})
        labels = {"app": "superset"}
        db_url = (
            f"postgresql+psycopg2://dwh:{self.pg_password}"
            f"@postgres-rw.{self.namespace}.svc:5432/superset"
        )
        config_py = (
            "import os\n"
            "SQLALCHEMY_DATABASE_URI = os.environ['DATABASE_URL']\n"
            "SECRET_KEY = os.environ['SUPERSET_SECRET_KEY']\n"
            "WTF_CSRF_ENABLED = True\n"
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

        start_cmd = (
            "pip install -q psycopg2-binary && "
            "superset db upgrade && "
            "superset fab create-admin --username admin --firstname Admin "
            "--lastname User --email admin@example.com --password admin || true && "
            "superset init && "
            "exec gunicorn --bind 0.0.0.0:8088 --workers 1 --timeout 120 "
            "'superset.app:create_app()'"
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
                                env=[
                                    client.V1EnvVar(
                                        name="SUPERSET_SECRET_KEY",
                                        value=_generate_password(32),
                                    ),
                                    client.V1EnvVar(name="DATABASE_URL", value=db_url),
                                    client.V1EnvVar(
                                        name="SUPERSET_CONFIG_PATH",
                                        value="/app/pythonpath/superset_config.py",
                                    ),
                                    client.V1EnvVar(name="PYTHONPATH", value="/app/pythonpath"),
                                ],
                                volume_mounts=[
                                    client.V1VolumeMount(
                                        name="config", mount_path="/app/pythonpath"
                                    )
                                ],
                                readinessProbe=client.V1Probe(
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
