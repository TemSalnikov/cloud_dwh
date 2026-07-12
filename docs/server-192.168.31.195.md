# Инструкция по установке Cloud DWH

Пошаговое руководство: **что**, **куда** и **зачем** устанавливать.

---

## 1. Две машины — кто за что отвечает

```
┌─────────────────────────┐         SSH (один раз)        ┌──────────────────────────────────┐
│  Локальная машина       │  ─────────────────────────►   │  node1 — 192.168.31.195          │
│  (ваш ПК / ноутбук)      │                               │  128 GB RAM · 48 vCPU            │
│                         │                               │                                  │
│  Роль: разработка,      │                               │  Роль: ВСЁ работает здесь        │
│  копирование проекта    │                               │  Kubernetes + платформа + стеки  │
└─────────────────────────┘                               └──────────────────────────────────┘
         │                                                            │
         │  браузер (постоянно)                                       │
         └──────────────────────────────────────────────────────────►│
                    https://platform.192.168.31.195.nip.io
```

| Машина | IP | Роль | Нужна постоянно? |
|--------|-----|------|------------------|
| **Локальная** | любой | Разработка, редактирование кода, однократная доставка проекта на сервер | **Нет** — после установки можно отключить |
| **node1** | `192.168.31.195` | Kubernetes-кластер, все сервисы DWH, platform-api, platform-ui | **Да** — это production-сервер |
| **Клиент (браузер)** | любой в сети `192.168.31.0/24` | Открывает веб-UI, создаёт стеки | Только для доступа к UI |

**Цель:** node1 работает **автономно**. Локальная машина не участвует в работе платформы — только в первоначальной настройке и обновлениях кода.

---

## 2. Что устанавливать на каждую машину

### 2.1 Локальная машина (однократно)

| Компонент | Куда ставится | Зачем | Обязательно? |
|-----------|---------------|-------|--------------|
| Git / исходники `cloud_dwh` | `/home/user/dev/cloud_dwh` (node1) | Код проекта, скрипты, helm charts | Да |
| `rsync`, `ssh` | системные пакеты | Копирование проекта на node1 | Да |
| `kubectl` | `~/.local/bin` или snap | Опционально: удалённая диагностика кластера | Нет |
| `helm` | `~/.local/bin` | Опционально: удалённый деплой с локальной машины | **Нет** — на node1 ставится свой |

> **Важно:** Helm на локальной машине (`~/.local/bin/helm`) **не нужен** для работы сервера.  
> Он был установлен для отладки. Все деплои должны выполняться **на node1**.

**Если проект уже склонирован на node1** (ваш случай):

```bash
# NODE1 — проект уже здесь:
# /home/user/dev/cloud_dwh
cd /home/user/dev/cloud_dwh
chmod +x scripts/*.sh
```

**Если нужно скопировать с другой машины:**

```bash
# ЛОКАЛЬНАЯ МАШИНА
cd /path/to/cloud_dwh
chmod +x scripts/*.sh
./scripts/sync-to-node1.sh
```

**Если нужно клонировать на node1 напрямую:**

```bash
# NODE1
git clone https://github.com/TemSalnikov/cloud_dwh.git /home/user/dev/cloud_dwh
```

---

### 2.2 node1 — сервер (192.168.31.195)

#### A. Системные инструменты (на самой ОС node1)

| Компонент | Путь после установки | Зачем |
|-----------|---------------------|-------|
| **kubectl** | `/usr/local/bin/kubectl` | Управление Kubernetes-кластером |
| **helm** | `/usr/local/bin/helm` | Установка ingress, operators, monitoring, platform |
| **Docker** | `/usr/bin/docker` | Сборка образов platform-api и platform-ui |
| **Docker Registry** | `192.168.31.195:5000` | Хранение образов на сервере (без Docker Hub) |
| **kubeconfig** | `/etc/kubernetes/admin.conf` | Доступ к API Kubernetes (создаётся при `kubeadm init`) |

**Установка:** один скрипт на node1

```bash
ssh user@192.168.31.195
sudo bash /home/user/dev/cloud_dwh/scripts/setup-node1.sh
```

---

#### B. Инфраструктура Kubernetes (pods на node1, namespace `platform` и др.)

Устанавливается скриптом `bootstrap.sh`. Всё работает **внутри K8s** на node1:

| Компонент | Namespace | Зачем |
|-----------|-----------|-------|
| **local-path-provisioner** | `local-path-storage` | Локальные диски → PersistentVolume для ClickHouse, Postgres, Kafka |
| **nginx-ingress** | `ingress-nginx` | Единая точка входа: все URL через `*.192.168.31.195.nip.io` |
| **cert-manager** | `cert-manager` | TLS-сертификаты (self-signed для внутренней сети) |
| **ClickHouse Operator** | `kube-system` | Автоматическое создание ClickHouse-кластеров по запросу из UI |
| **Strimzi (Kafka Operator)** | `strimzi` | Автоматическое создание Kafka-брокеров |
| **CloudNativePG Operator** | `cnpg-system` | Автоматическое создание PostgreSQL |
| **kube-prometheus-stack** | `monitoring` | Prometheus + Grafana — мониторинг ресурсов node1 |
| **platform-api** | `platform` | Backend: создаёт/удаляет стеки, проверяет квоты |
| **platform-ui** | `platform` | Web-интерфейс для пользователя |
| **PostgreSQL (platform)** | `platform` | БД метаданных: список стеков, их конфиги, статусы |
| **Redis (platform)** | `platform` | Кэш для platform-api |

**Установка образов + bootstrap на node1:**

```bash
# 1. Собрать и загрузить образы в локальный registry
sudo bash /home/user/dev/cloud_dwh/scripts/build-images.sh

# 2. Настроить containerd для локального registry (если ImagePullBackOff)
sudo bash /home/user/dev/cloud_dwh/scripts/configure-registry.sh

# 3. Развернуть всю инфраструктуру (~15 мин)
sudo bash /home/user/dev/cloud_dwh/scripts/bootstrap.sh
```

---

#### C. Пользовательские стеки (создаются через Web UI, pods на node1)

Когда пользователь нажимает «Развернуть стек» в UI, platform-api создаёт namespace `stack-{id}` и разворачивает:

| Сервис | Зачем |
|--------|-------|
| **ClickHouse** | OLAP-хранилище, аналитика |
| **Kafka** | Потоковая шина данных |
| **PostgreSQL** | Метаданные Airflow / Superset |
| **Redis** | Брокер Celery (Airflow), кэш Superset |
| **Airflow** | ETL-оркестрация, DAG-и |
| **Superset** | BI-дашборды |

---

### 2.3 Клиентская машина (браузер)

| Компонент | Зачем | Обязательно? |
|-----------|-------|--------------|
| Браузер | Platform UI, Grafana, Airflow, Superset | Да |
| `/etc/hosts` | Только если `nip.io` недоступен из вашей сети | Нет |

**URL после установки:**

| Сервис | URL |
|--------|-----|
| Platform UI | https://platform.192.168.31.195.nip.io |
| Grafana | https://grafana.192.168.31.195.nip.io |
| API | https://platform.192.168.31.195.nip.io/api/v1/services |

---

## 3. Полная последовательность установки

### Фаза 0 — Подготовка (node1, ~1 мин)

```bash
# NODE1 — проект уже в /home/user/dev/cloud_dwh
cd /home/user/dev/cloud_dwh
chmod +x scripts/*.sh
```

**Зачем:** убедиться, что скрипты исполняемые. Клонирование уже выполнено.

---

### Фаза 1.5 — Offline-пакеты (ОБЯЗАТЕЛЬНО — на node1 нет VPN)

На node1 **нет доступа** к GitHub / strimzi.io → helm падает с `context deadline exceeded`.  
Все charts скачиваются **на локальной машине с VPN** и передаются на node1.

```
ЛОКАЛЬНАЯ (VPN)                         NODE1 (без VPN)
─────────────────                        ─────────────────
pack-offline-bundle.sh  ──rsync──►      deploy/vendor/
  ├─ ingress-nginx.tgz                   helm/platform/charts/
  ├─ cert-manager.tgz                    scripts/bootstrap.sh
  ├─ strimzi-kafka-operator.tgz
  ├─ cloudnative-pg.tgz
  ├─ kube-prometheus-stack.tgz
  ├─ postgresql + redis (bitnami)
  └─ K8s manifests (yaml)
```

**На ЛОКАЛЬНОЙ машине (с VPN):**

```bash
cd ~/dev/cloud_dwh          # или /home/ubuntu/dev/cloud_dwh
bash scripts/pack-offline-bundle.sh    # скачать + проверить (~2 мин)
bash scripts/sync-offline-bundle.sh    # передать на node1 (~1 мин)
```

**Проверка на node1:**

```bash
cd /home/user/dev/cloud_dwh
bash scripts/verify-offline-bundle.sh   # должно быть OK
ls deploy/vendor/charts/                # 5 файлов .tgz
ls helm/platform/charts/                # postgresql + redis .tgz
```

**Зачем:** bootstrap на node1 **не обращается к интернету** — только локальные файлы.

---

### Фаза 2 — Инструменты (node1, ~5 мин)

```bash
# NODE1
ssh user@192.168.31.195
sudo bash /home/user/dev/cloud_dwh/scripts/setup-node1.sh
```

**Зачем:** установить kubectl, helm, docker, registry — всё необходимое для автономной работы сервера.

**Проверка:**

```bash
# NODE1
kubectl get nodes
helm version
docker ps | grep registry
```

---

### Фаза 3 — Образы platform (node1, ~5 мин)

```bash
# NODE1
sudo bash /home/user/dev/cloud_dwh/scripts/build-images.sh
sudo bash /home/user/dev/cloud_dwh/scripts/configure-registry.sh
```

**Зачем:** platform-api и platform-ui — custom-образы. Они собираются на node1 и хранятся в локальном registry, чтобы K8s мог их скачать без интернета.

---

### Фаза 4 — Bootstrap платформы (node1, ~15 мин)

```bash
# NODE1
sudo bash /home/user/dev/cloud_dwh/scripts/bootstrap.sh
```

**Зачем:** развернуть ingress, operators, monitoring и control plane — всё, что нужно для self-service создания DWH-стеков.

**Проверка:**

```bash
# NODE1
kubectl get pods -A | grep -v Running | grep -v Completed
# (пустой вывод = всё OK)

kubectl get ingress -A
```

---

### Фаза 5 — Первый стек (браузер на любой машине)

1. Открыть https://platform.192.168.31.195.nip.io
2. Имя стека: `analytics-dev`
3. Preset: **Minimal** (~32 GB RAM)
4. Нажать **Развернуть стек**
5. Дождаться статуса `running`

**Зачем:** проверить, что platform-api корректно создаёт DWH-стек через operators.

---

## 4. Карта файлов проекта на node1

```
/home/user/dev/cloud_dwh/                  ← весь проект (с локальной машины или git clone)
├── deploy/server.env            ← IP, домен, пути (конфиг node1)
├── scripts/
│   ├── setup-node1.sh           ← NODE1: kubectl + helm + docker + registry
│   ├── build-images.sh          ← NODE1: сборка образов platform-api/ui
│   ├── configure-registry.sh    ← NODE1: настройка containerd
│   ├── bootstrap.sh             ← NODE1: деплой инфраструктуры K8s
│   └── check-cluster.sh         ← NODE1: проверка кластера
├── helm/platform/               ← Helm chart control plane
├── platform-api/                ← исходники API
└── platform-ui/                 ← исходники Web UI
```

---

## 5. Что где работает после установки

```
node1 (192.168.31.195)
│
├── ОС (Ubuntu)
│   ├── /usr/local/bin/kubectl, helm
│   ├── /usr/bin/docker
│   └── registry :5000
│
└── Kubernetes
    ├── ingress-nginx          ← маршрутизация HTTP(S)
    ├── cert-manager           ← TLS
    ├── clickhouse-operator    ← управляет ClickHouse
    ├── strimzi-operator       ← управляет Kafka
    ├── cnpg-operator          ← управляет Postgres
    ├── monitoring             ← Grafana + Prometheus
    ├── platform/              ← control plane (UI + API)
    └── stack-analytics-dev/   ← пользовательский стек (пример)
        ├── clickhouse
        ├── kafka
        ├── postgres
        ├── airflow
        └── superset
```

---

## 6. Обновление проекта (когда меняется код)

```bash
# 1. ЛОКАЛЬНАЯ МАШИНА — доставить новый код (если редактировали локально)
./scripts/sync-to-node1.sh

# 2. NODE1 — пересобрать образы (если менялись platform-api/ui)
cd /home/user/dev/cloud_dwh
sudo bash scripts/build-images.sh

# 3. NODE1 — обновить деплой
sudo bash scripts/bootstrap.sh
```

---

## 7. Troubleshooting

| Симптом | Где смотреть | Решение (на node1) |
|---------|--------------|-------------------|
| `Missing: helm` | локальная машина | Не запускайте bootstrap локально — только на node1 |
| `context deadline exceeded` (strimzi/GitHub) | node1 без VPN | **ЛОКАЛЬНАЯ:** `pack-offline-bundle.sh` → `sync-offline-bundle.sh` |
| `Offline-пакет неполный` | node1 | Сначала sync-offline-bundle с локальной машины |
| `Unable to connect to server` | node1 | `export KUBECONFIG=/etc/kubernetes/admin.conf` |
| `ImagePullBackOff` | node1 | `sudo bash /home/user/dev/cloud_dwh/scripts/configure-registry.sh` |
| `Pending` PVC | node1 | `kubectl get sc` — нужен StorageClass `local-path` |
| UI не открывается | браузер | Проверить `kubectl get ingress -n platform` на node1 |
| nip.io не резолвится | браузер | Добавить в `/etc/hosts`: `192.168.31.195 platform.192.168.31.195.nip.io` |

---

## 8. Полезные команды (node1)

```bash
# Статус всего кластера
kubectl get pods -A

# Ресурсы ноды
kubectl top node
kubectl describe node | grep -A10 "Allocated resources"

# Логи platform-api
kubectl logs -n platform -l app=platform-api -f

# Port-forward (если ingress не работает)
kubectl port-forward -n platform svc/platform-ui 3000:80
```

---

## 9. Конфигурация

Все параметры node1 — в файле `deploy/server.env`:

```bash
SERVER_IP=192.168.31.195
BASE_DOMAIN=192.168.31.195.nip.io
REPO_DIR=/home/user/dev/cloud_dwh
REGISTRY=192.168.31.195:5000
KUBECONFIG=/etc/kubernetes/admin.conf
```

Архитектура платформы: [architecture.md](architecture.md)
