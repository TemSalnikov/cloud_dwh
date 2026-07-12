import asyncio
import logging
import secrets
import string
from pathlib import Path

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
        self.core, self.rbac, self.net, self.custom = _get_k8s_clients()

    async def deploy(self) -> dict:
        await asyncio.to_thread(self._create_namespace)
        await asyncio.to_thread(self._create_resource_quota)
        endpoints = {}

        if self._enabled("postgres") or self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_postgres)
            endpoints["postgres"] = f"postgres.{self.namespace}.svc.cluster.local:5432"

        if self._enabled("airflow") or self._enabled("superset"):
            await asyncio.to_thread(self._deploy_redis)

        if self._enabled("clickhouse"):
            await asyncio.to_thread(self._deploy_clickhouse)
            endpoints["clickhouse"] = f"https://{self.name}-ch.{self.base_domain}"

        if self._enabled("kafka"):
            await asyncio.to_thread(self._deploy_kafka)
            endpoints["kafka"] = f"{self.name}-kafka.{self.namespace}.svc.cluster.local:9092"

        if self._enabled("airflow"):
            await asyncio.to_thread(self._deploy_airflow)
            endpoints["airflow"] = f"https://{self.name}-airflow.{self.base_domain}"

        if self._enabled("superset"):
            await asyncio.to_thread(self._deploy_superset)
            endpoints["superset"] = f"https://{self.name}-superset.{self.base_domain}"

        return endpoints

    async def delete(self):
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
        total_cpu = sum(
            float(self.spec.get(s, {}).get("resources", {}).get("cpu", 0))
            for s in self.spec
            if self._enabled(s)
        )
        quota = client.V1ResourceQuota(
            metadata=client.V1ObjectMeta(name="stack-quota", namespace=self.namespace),
            spec=client.V1ResourceQuotaSpec(
                hard={
                    "requests.cpu": str(int(total_cpu) + 2),
                    "requests.memory": self._total_memory(),
                    "persistentvolumeclaims": "20",
                }
            ),
        )
        try:
            self.core.create_namespaced_resource_quota(self.namespace, quota)
        except ApiException as e:
            if e.status != 409:
                raise

    def _total_memory(self) -> str:
        total_gi = 0
        for svc in self.spec:
            if not self._enabled(svc):
                continue
            mem = self.spec[svc].get("resources", {}).get("memory", "0Gi")
            total_gi += int(mem.replace("Gi", "").replace("Mi", ""))
        return f"{total_gi + 4}Gi"

    def _deploy_postgres(self):
        cfg = self.spec.get("postgres", {})
        res = cfg.get("resources", {})
        password = _generate_password()
        cluster = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Cluster",
            "metadata": {"name": "postgres", "namespace": self.namespace},
            "spec": {
                "instances": 1,
                "storage": {"size": res.get("storage", "10Gi")},
                "resources": {
                    "requests": {"cpu": res.get("cpu", "1"), "memory": res.get("memory", "2Gi")},
                },
                "bootstrap": {
                    "initdb": {
                        "database": "dwh",
                        "owner": "dwh",
                        "secret": {"name": "postgres-credentials"},
                    }
                },
            },
        }
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name="postgres-credentials", namespace=self.namespace),
            string_data={"username": "dwh", "password": password},
        )
        self.core.create_namespaced_secret(self.namespace, secret)
        self.custom.create_namespaced_custom_object(
            "postgresql.cnpg.io", "v1", self.namespace, "clusters", cluster
        )

    def _deploy_redis(self):
        # Minimal Redis deployment for Airflow/Superset
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
                                    requests={"cpu": "100m", "memory": "128Mi"}
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
        self.core.create_namespaced_deployment(self.namespace, dep)
        self.core.create_namespaced_service(self.namespace, svc)

    def _deploy_clickhouse(self):
        cfg = self.spec.get("clickhouse", {})
        res = cfg.get("resources", {})
        chi = {
            "apiVersion": "clickhouse.altinity.com/v1",
            "kind": "ClickHouseInstallation",
            "metadata": {"name": "clickhouse", "namespace": self.namespace},
            "spec": {
                "configuration": {
                    "clusters": [
                        {
                            "name": "dwh",
                            "layout": {
                                "shardsCount": 1,
                                "replicasCount": cfg.get("replicas", 1),
                            },
                        }
                    ],
                    "users": {
                        "dwh/password": _generate_password(),
                        "dwh/networks/ip": ["0.0.0.0/0"],
                    },
                },
                "templates": {
                    "podTemplates": [
                        {
                            "name": "default",
                            "spec": {
                                "containers": [
                                    {
                                        "name": "clickhouse",
                                        "image": "clickhouse/clickhouse-server:24.6",
                                        "resources": {
                                            "requests": {
                                                "cpu": res.get("cpu", "2"),
                                                "memory": res.get("memory", "8Gi"),
                                            }
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
                },
            },
        }
        self.custom.create_namespaced_custom_object(
            "clickhouse.altinity.com", "v1", self.namespace, "clickhouseinstallations", chi
        )

    def _deploy_kafka(self):
        cfg = self.spec.get("kafka", {})
        res = cfg.get("resources", {})
        brokers = cfg.get("brokers", 1)
        kafka = {
            "apiVersion": "kafka.strimzi.io/v1beta2",
            "kind": "Kafka",
            "metadata": {"name": "kafka", "namespace": self.namespace},
            "spec": {
                "kafka": {
                    "version": "3.7.1",
                    "replicas": brokers,
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
                    "storage": {
                        "type": "persistent-claim",
                        "size": res.get("storage", "20Gi"),
                        "deleteClaim": True,
                    },
                    "resources": {
                        "requests": {"cpu": res.get("cpu", "2"), "memory": res.get("memory", "4Gi")}
                    },
                },
                "entityOperator": {
                    "topicOperator": {},
                    "userOperator": {},
                },
            },
        }
        self.custom.create_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", self.namespace, "kafkas", kafka
        )

    def _deploy_airflow(self):
        cfg = self.spec.get("airflow", {})
        res = cfg.get("resources", {})
        password = _generate_password()
        labels = {"app": "airflow-webserver"}
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
                                command=["airflow", "webserver"],
                                ports=[client.V1ContainerPort(container_port=8080)],
                                env=[
                                    client.V1EnvVar(
                                        name="AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
                                        value=f"postgresql+psycopg2://dwh:{password}@postgres-rw.{self.namespace}.svc:5432/airflow",
                                    ),
                                    client.V1EnvVar(
                                        name="AIRFLOW__CELERY__BROKER_URL",
                                        value=f"redis://redis.{self.namespace}.svc:6379/0",
                                    ),
                                    client.V1EnvVar(name="AIRFLOW__CORE__EXECUTOR", value=cfg.get("executor", "CeleryExecutor")),
                                    client.V1EnvVar(name="AIRFLOW__CORE__LOAD_EXAMPLES", value="false"),
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": res.get("cpu", "2"), "memory": res.get("memory", "4Gi")}
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
        ingress = client.V1Ingress(
            metadata=client.V1ObjectMeta(
                name="airflow",
                namespace=self.namespace,
                annotations={"cert-manager.io/cluster-issuer": "selfsigned-issuer"},
            ),
            spec=client.V1IngressSpec(
                ingress_class_name="nginx",
                tls=[client.V1IngressTLS(
                    hosts=[f"{self.name}-airflow.{self.base_domain}"],
                    secret_name="airflow-tls",
                )],
                rules=[
                    client.V1IngressRule(
                        host=f"{self.name}-airflow.{self.base_domain}",
                        http=client.V1HTTPIngressRuleValue(
                            paths=[
                                client.V1HTTPIngressPath(
                                    path="/",
                                    path_type="Prefix",
                                    backend=client.V1IngressBackend(
                                        service=client.V1IngressServiceBackend(
                                            name="airflow-webserver",
                                            port=client.V1ServiceBackendPort(number=8080),
                                        )
                                    ),
                                )
                            ]
                        ),
                    )
                ],
            ),
        )
        self.core.create_namespaced_deployment(self.namespace, dep)
        self.core.create_namespaced_service(self.namespace, svc)
        self.net.create_namespaced_ingress(self.namespace, ingress)

    def _deploy_superset(self):
        cfg = self.spec.get("superset", {})
        res = cfg.get("resources", {})
        labels = {"app": "superset"}
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
                                image="apache/superset:latest",
                                command=["/usr/bin/run-server.sh"],
                                ports=[client.V1ContainerPort(container_port=8088)],
                                env=[
                                    client.V1EnvVar(
                                        name="SUPERSET_SECRET_KEY",
                                        value=_generate_password(32),
                                    ),
                                ],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": res.get("cpu", "1"), "memory": res.get("memory", "2Gi")}
                                ),
                            )
                        ]
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
        self.core.create_namespaced_deployment(self.namespace, dep)
        self.core.create_namespaced_service(self.namespace, svc)
