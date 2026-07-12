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

Кратко:

```bash
# NODE1 — всё на сервере (проект: /home/user/dev/cloud_dwh)
ssh user@192.168.31.195
cd /home/user/dev/cloud_dwh
chmod +x scripts/*.sh

# Offline-пакеты (обязательно при медленном интернете / timeout GitHub)
ls deploy/vendor/charts/*.tgz || bash scripts/download-vendor.sh

sudo bash scripts/setup-node1.sh      # kubectl, helm, docker, registry
sudo bash scripts/build-images.sh    # образы platform-api/ui
sudo bash scripts/bootstrap.sh       # ingress, operators, platform (из vendor/)
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
