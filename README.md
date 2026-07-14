# Cloud DWH Platform

Self-service платформа для развёртывания DWH-стека на Kubernetes.

Пользователь через веб-интерфейс выбирает сервисы (ClickHouse, Postgres, Kafka, Airflow, Superset), задаёт ресурсы — platform-api создаёт изолированный namespace и разворачивает стек.

## Машины

| Машина | Роль |
|--------|------|
| **node1** (`192.168.31.195`) | Kubernetes + вся платформа + DWH-стеки — **работает автономно** |
| **Локальная машина** | Разработка, однократная доставка кода на node1 |
| **Браузер** | Web UI: https://platform.192.168.31.195.nip.io |

## Установка

**Полная инструкция:** [docs/server-192.168.31.195.md](docs/server-192.168.31.195.md)

### Обновление platform-api / UI после правок кода (node1)

```bash
# с локальной машины (синхронизация кода)
bash scripts/sync-to-node1.sh

# на node1 — сборка + загрузка в containerd + rollout
ssh user@192.168.31.195
cd /home/user/dev/cloud_dwh
sudo bash scripts/update-platform.sh
```

Скрипт сам: `docker build` → `ctr import` → `helm upgrade` → `rollout restart`.
Registry `:5000` опционален (TLS-проблемы обходятся через containerd import).

### ЛОКАЛЬНАЯ машина (с VPN) — offline bootstrap

```bash
cd ~/dev/cloud_dwh
bash scripts/pack-offline-bundle.sh     # скачать charts + manifests
bash scripts/sync-offline-bundle.sh     # передать на node1
```

### NODE1 (без VPN) — установка

```bash
ssh user@192.168.31.195
cd /home/user/dev/cloud_dwh
bash scripts/verify-offline-bundle.sh   # проверка пакета
sudo bash scripts/setup-node1.sh
sudo bash scripts/build-images.sh
sudo bash scripts/bootstrap.sh          # 100% offline
```

## Архитектура

```
node1 (128 GB / 48 vCPU)
├── Platform (~10 GB)     ingress · operators · platform-api/ui · monitoring
└── User Stacks           ClickHouse · Kafka · Postgres · Airflow · Superset
```

Подробнее: [docs/architecture.md](docs/architecture.md)

## Структура репозитория

```
cloud_dwh/
├── docs/              # Инструкции, архитектура
├── deploy/            # server.env — конфиг node1
├── helm/platform/     # Helm chart control plane
├── platform-api/      # REST API provisioning
├── platform-ui/       # Web UI
└── scripts/           # setup-node1, bootstrap, sync-to-node1
```
