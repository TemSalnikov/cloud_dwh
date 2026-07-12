from pydantic import BaseModel, Field, field_validator


class ServiceResources(BaseModel):
    cpu: str = Field(default="1", pattern=r"^\d+(\.\d+)?$")
    memory: str = Field(default="2Gi", pattern=r"^\d+(Mi|Gi)$")
    storage: str | None = Field(default=None, pattern=r"^\d+(Gi|Ti)$")


class ClickHouseConfig(BaseModel):
    enabled: bool = False
    replicas: int = Field(default=1, ge=1, le=4)
    resources: ServiceResources = ServiceResources(cpu="2", memory="8Gi", storage="50Gi")


class KafkaConfig(BaseModel):
    enabled: bool = False
    brokers: int = Field(default=1, ge=1, le=3)
    ui: bool = True  # Kafka UI (provectuslabs) — включается вместе с Kafka
    resources: ServiceResources = ServiceResources(cpu="2", memory="4Gi", storage="20Gi")


class PostgresConfig(BaseModel):
    enabled: bool = False
    resources: ServiceResources = ServiceResources(cpu="1", memory="2Gi", storage="10Gi")


class AirflowConfig(BaseModel):
    enabled: bool = False
    executor: str = Field(default="CeleryExecutor", pattern=r"^(CeleryExecutor|KubernetesExecutor)$")
    workers: int = Field(default=1, ge=1, le=8)
    resources: ServiceResources = ServiceResources(cpu="2", memory="4Gi")


class SupersetConfig(BaseModel):
    enabled: bool = False
    resources: ServiceResources = ServiceResources(cpu="1", memory="2Gi")


class StackSpec(BaseModel):
    clickhouse: ClickHouseConfig = ClickHouseConfig()
    kafka: KafkaConfig = KafkaConfig()
    postgres: PostgresConfig = PostgresConfig()
    airflow: AirflowConfig = AirflowConfig()
    superset: SupersetConfig = SupersetConfig()

    @field_validator("airflow", "superset", mode="after")
    @classmethod
    def resolve_dependencies(cls, v, info):
        return v


class StackCreate(BaseModel):
    name: str = Field(min_length=3, max_length=32, pattern=r"^[a-z][a-z0-9-]*$")
    preset: str | None = Field(default=None, pattern=r"^(minimal|standard|full)$")
    services: StackSpec | None = None


class StackResponse(BaseModel):
    id: str
    name: str
    namespace: str
    status: str
    status_message: str | None
    spec: dict
    endpoints: dict | None
    created_at: str

    class Config:
        from_attributes = True
