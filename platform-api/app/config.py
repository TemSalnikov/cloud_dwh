from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://platform:platform@localhost:5432/platform"
    redis_url: str = "redis://localhost:6379/0"
    kubeconfig_in_cluster: bool = True
    helm_charts_path: str = "/charts/stacks"
    cluster_total_cpu: str = "42"
    cluster_total_memory: str = "116Gi"
    ingress_base_domain: str = "192.168.31.195.nip.io"
    server_ip: str = "192.168.31.195"
    auth_secret: str = "cloud-dwh-dev-secret-change-me"
    bootstrap_admin_email: str = "admin@cloud-dwh.local"
    bootstrap_admin_password: str = "ChangeMeAdmin1!"
    bootstrap_admin_name: str = "Superuser"

    class Config:
        env_file = ".env"


settings = Settings()
