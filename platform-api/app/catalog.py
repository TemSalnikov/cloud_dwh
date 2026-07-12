"""Service catalog with resource presets."""

SERVICES = {
    "clickhouse": {
        "name": "ClickHouse",
        "description": "OLAP columnar database for analytics",
        "icon": "database",
        "dependencies": [],
        "defaults": {"replicas": 1, "cpu": "2", "memory": "8Gi", "storage": "50Gi"},
        "limits": {"replicas": {"min": 1, "max": 4}, "cpu": {"min": 1, "max": 16}, "memory": {"min": "4Gi", "max": "32Gi"}},
    },
    "kafka": {
        "name": "Apache Kafka",
        "description": "Distributed event streaming + Kafka UI (web console)",
        "icon": "stream",
        "dependencies": [],
        "includes": ["kafka-ui"],
        "defaults": {"brokers": 1, "ui": True, "cpu": "2", "memory": "4Gi", "storage": "20Gi"},
        "limits": {"brokers": {"min": 1, "max": 3}, "cpu": {"min": 1, "max": 8}, "memory": {"min": "2Gi", "max": "16Gi"}},
    },
    "postgres": {
        "name": "PostgreSQL",
        "description": "Relational database (CloudNativePG)",
        "icon": "database",
        "dependencies": [],
        "defaults": {"cpu": "1", "memory": "2Gi", "storage": "10Gi"},
        "limits": {"cpu": {"min": 0.5, "max": 4}, "memory": {"min": "1Gi", "max": "8Gi"}},
    },
    "airflow": {
        "name": "Apache Airflow",
        "description": "Workflow orchestration for ETL pipelines",
        "icon": "workflow",
        "dependencies": ["postgres", "redis"],
        "defaults": {"executor": "CeleryExecutor", "workers": 2, "cpu": "2", "memory": "4Gi"},
        "limits": {"workers": {"min": 1, "max": 8}, "cpu": {"min": 1, "max": 8}, "memory": {"min": "2Gi", "max": "16Gi"}},
    },
    "superset": {
        "name": "Apache Superset",
        "description": "Business intelligence and visualization",
        "icon": "chart",
        "dependencies": ["postgres", "redis"],
        "defaults": {"cpu": "1", "memory": "2Gi"},
        "limits": {"cpu": {"min": 0.5, "max": 4}, "memory": {"min": "1Gi", "max": "8Gi"}},
    },
}

PRESETS = {
    "minimal": {
        "name": "Minimal (Dev)",
        "description": "Single-node dev stack, ~32 GB RAM",
        "services": {
            "clickhouse": {"enabled": True, "replicas": 1, "resources": {"cpu": "2", "memory": "8Gi", "storage": "30Gi"}},
            "kafka": {"enabled": True, "brokers": 1, "resources": {"cpu": "1", "memory": "2Gi", "storage": "10Gi"}},
            "postgres": {"enabled": True, "resources": {"cpu": "1", "memory": "2Gi", "storage": "10Gi"}},
            "airflow": {"enabled": True, "workers": 1, "resources": {"cpu": "2", "memory": "4Gi"}},
            "superset": {"enabled": True, "resources": {"cpu": "1", "memory": "2Gi"}},
        },
    },
    "standard": {
        "name": "Standard",
        "description": "Balanced stack for staging, ~64 GB RAM",
        "services": {
            "clickhouse": {"enabled": True, "replicas": 2, "resources": {"cpu": "4", "memory": "16Gi", "storage": "100Gi"}},
            "kafka": {"enabled": True, "brokers": 1, "resources": {"cpu": "2", "memory": "4Gi", "storage": "20Gi"}},
            "postgres": {"enabled": True, "resources": {"cpu": "2", "memory": "4Gi", "storage": "20Gi"}},
            "airflow": {"enabled": True, "workers": 2, "resources": {"cpu": "4", "memory": "8Gi"}},
            "superset": {"enabled": True, "resources": {"cpu": "2", "memory": "4Gi"}},
        },
    },
    "full": {
        "name": "Full Production-like",
        "description": "Maximum on single node, ~96 GB RAM",
        "services": {
            "clickhouse": {"enabled": True, "replicas": 4, "resources": {"cpu": "4", "memory": "16Gi", "storage": "200Gi"}},
            "kafka": {"enabled": True, "brokers": 3, "resources": {"cpu": "2", "memory": "4Gi", "storage": "30Gi"}},
            "postgres": {"enabled": True, "resources": {"cpu": "2", "memory": "4Gi", "storage": "30Gi"}},
            "airflow": {"enabled": True, "workers": 4, "resources": {"cpu": "4", "memory": "8Gi"}},
            "superset": {"enabled": True, "resources": {"cpu": "2", "memory": "4Gi"}},
        },
    },
}
