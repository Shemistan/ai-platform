# Developer News Sources v1

Подборка источников для сервиса персональных новостных дайджестов с фокусом на Go/backend-разработчика.

Основная идея: сервис следит за большим числом источников, но пользователю показывает только действительно важные изменения — major-релизы, security-события, breaking changes, сильные engineering-материалы, значимые AI-инструменты и лучшие видео/обсуждения.

---

## 1. Мой стек: языки, базы данных, брокеры, контейнеры и инфраструктура

### Go

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| The Go Blog | https://go.dev/blog/ | 5/5 | Major/minor releases, runtime, language changes, security, tooling |
| Go Releases | https://go.dev/dl/ | 5/5 | Новые версии Go |
| Go GitHub | https://github.com/golang/go | 4/5 | Важные issues, proposals, runtime/compiler changes |
| GopherCon | https://www.youtube.com/@GopherAcademy | 4/5 | Сильные доклады по Go |

### PostgreSQL

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| PostgreSQL News | https://www.postgresql.org/about/news/ | 5/5 | Releases, security, major features |
| PostgreSQL Release Notes | https://www.postgresql.org/docs/release/ | 5/5 | Breaking changes, fixes |
| Postgres Professional | https://postgrespro.com/blog | 4/5 | PostgreSQL engineering и production cases |

### Kafka

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| Apache Kafka Blog | https://kafka.apache.org/blog | 5/5 | Releases, KRaft, Streams, consumer/producer changes |
| Apache Kafka GitHub | https://github.com/apache/kafka | 4/5 | Release activity, major KIPs |

### RabbitMQ

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| RabbitMQ Blog | https://www.rabbitmq.com/blog | 4/5 | Releases, quorum queues, performance, breaking changes |
| RabbitMQ Releases | https://github.com/rabbitmq/rabbitmq-server/releases | 4/5 | Новые версии и fixes |

### Redis

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| Redis Blog | https://redis.io/blog/ | 4/5 | Releases, architecture, licensing/ecosystem |
| Redis GitHub | https://github.com/redis/redis | 4/5 | Major releases |

### ClickHouse

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| ClickHouse Blog | https://clickhouse.com/blog | 4/5 | Releases, performance, storage, production patterns |
| ClickHouse GitHub | https://github.com/ClickHouse/ClickHouse | 4/5 | Major releases |

### Kubernetes

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| Kubernetes Blog | https://kubernetes.io/blog/ | 5/5 | Releases, security, API changes, deprecations |
| Kubernetes GitHub | https://github.com/kubernetes/kubernetes | 4/5 | Releases, major changes |
| CNCF Blog | https://www.cncf.io/blog/ | 3/5 | Cloud-native ecosystem |

### Docker

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| Docker Blog | https://www.docker.com/blog/ | 4/5 | Engine, Compose, BuildKit, security |
| Docker Engine Releases | https://docs.docker.com/engine/release-notes/ | 5/5 | Breaking changes и важные релизы |

### Observability

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| OpenTelemetry Blog | https://opentelemetry.io/blog/ | 4/5 | SDK, Collector, semantic conventions |
| Grafana Labs Blog | https://grafana.com/blog/ | 4/5 | Metrics, logs, traces, observability architecture |
| Prometheus Blog | https://prometheus.io/blog/ | 4/5 | Releases, Prometheus ecosystem |

### GitHub / Developer Platform

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| GitHub Changelog | https://github.blog/changelog/ | 5/5 | Actions, Copilot, platform changes |
| GitHub Engineering | https://github.blog/engineering/ | 4/5 | Infrastructure, scaling, Git, CI/CD |

---

## 2. Backend / Infrastructure / Engineering Blogs

Эти источники нужны не столько для новостей, сколько для сильных production-разборов и инженерных идей.

| Источник | URL | Приоритет | Темы |
|---|---|---:|---|
| Cloudflare Blog | https://blog.cloudflare.com/ | 5/5 | Networking, DNS, HTTP, security, performance |
| Uber Engineering | https://www.uber.com/blog/engineering/ | 5/5 | Go, distributed systems, databases, performance |
| Stripe Engineering | https://stripe.dev/blog | 4/5 | APIs, infrastructure, reliability, AI tooling |
| Netflix TechBlog | https://netflixtechblog.com/ | 4/5 | Distributed systems, scaling, streaming |
| Discord Engineering | https://discord.com/category/engineering | 4/5 | Realtime systems, Go/Rust/Elixir, infrastructure |
| Dropbox Tech | https://dropbox.tech/ | 4/5 | Storage, databases, infrastructure |
| GitHub Engineering | https://github.blog/engineering/ | 4/5 | Git, CI/CD, infra, developer tooling |
| Grafana Labs | https://grafana.com/blog/ | 4/5 | Observability |
| Cockroach Labs Blog | https://www.cockroachlabs.com/blog/ | 4/5 | Distributed databases, consistency, SQL |

### Как обрабатывать

Категория материала:

```text
deep_dive
```

Показывать пользователю только если статья содержит сильную инженерную идею, новый production pattern, необычный инцидент или измеримый результат.

Пример:

> Cloudflare изменил архитектуру кеширования и сократил расход памяти на X%.
>
> Почему важно: подход можно применить к высоконагруженным backend-системам.

---

## 3. AI для разработчика

Отдельно отслеживать не просто AI-новости, а именно AI, который меняет software engineering.

### Primary sources

| Источник | URL | Приоритет | Что отслеживать |
|---|---|---:|---|
| OpenAI Developer Blog | https://developers.openai.com/blog | 5/5 | Agents, Codex, API, tools, engineering |
| OpenAI API Changelog | https://developers.openai.com/api/docs/changelog | 5/5 | Models, APIs, tools |
| Anthropic Engineering | https://www.anthropic.com/engineering | 5/5 | Claude Code, agents, context engineering |
| Google DeepMind Blog | https://deepmind.google/blog/ | 4/5 | Gemini, models, agents, research |
| Hugging Face Blog | https://huggingface.co/blog | 4/5 | Open models, inference, agents |
| GitHub Copilot Changelog | https://github.blog/changelog/label/copilot/ | 5/5 | Agentic coding, Copilot changes |
| LangChain Blog | https://blog.langchain.com/ | 4/5 | Agents, LangGraph, orchestration |
| LlamaIndex Blog | https://www.llamaindex.ai/blog | 4/5 | RAG, agents, knowledge systems |

### Темы с высоким весом

```text
coding_agents
agent_orchestration
mcp
tool_use
context_engineering
agent_memory
agent_evals
long_running_agents
ai_code_review
ai_testing
ai_devops
local_agents
model_api_changes
```

### Что считать важным

Высокий приоритет:

- новый coding agent;
- существенное изменение Claude Code / Codex / Copilot;
- новый стандарт взаимодействия агентов с инструментами;
- MCP ecosystem changes;
- long-running agent patterns;
- новые API для tool use;
- agent memory/context;
- большие изменения стоимости или latency API;
- security issues в agentic workflows.

Низкий приоритет для Go/backend-профиля:

- генерация картинок;
- consumer AI apps;
- небольшие UI-фичи AI-сервисов;
- очередная модель без заметного влияния на разработку.

---

## 4. YouTube — зарубежные каналы

YouTube используется как discovery/analysis слой, а не как первичный источник фактов.

| Канал | URL | Приоритет | Для чего |
|---|---|---:|---|
| Fireship | https://www.youtube.com/@Fireship | 4/5 | Быстрый tech radar |
| ThePrimeagen | https://www.youtube.com/@ThePrimeTimeagen | 4/5 | Programming, Go/Rust, AI coding, industry |
| ThePrimeTime | https://www.youtube.com/@ThePrimeTime | 4/5 | Разборы engineering-новостей |
| Hussein Nasser | https://www.youtube.com/@hnasr | 5/5 | Backend, DB, networking, proxies, protocols |
| ByteByteGo | https://www.youtube.com/@ByteByteGo | 4/5 | System design |
| Theo / t3.gg | https://www.youtube.com/@t3dotgg | 4/5 | AI coding, dev tools, web ecosystem |
| TechWorld with Nana | https://www.youtube.com/@TechWorldwithNana | 4/5 | Kubernetes, Docker, DevOps |
| Ardan Labs | https://www.youtube.com/@ardanlabs | 5/5 | Go, Kubernetes, production engineering |
| GopherCon / Gopher Academy | https://www.youtube.com/@GopherAcademy | 5/5 | Go talks |
| Andrej Karpathy | https://www.youtube.com/@AndrejKarpathy | 5/5 | LLM fundamentals, AI |
| Gaurav Sen | https://www.youtube.com/@gkcs | 4/5 | System design |

### Формат результата для видео

Не просто:

> Вышло новое видео.

А:

```text
🎬 Hussein Nasser — <название>

Длительность: 34 мин
Важность: 4/5

Суть:
- ...
- ...
- ...

Что нового:
- ...

Почему полезно backend-разработчику:
- ...

Стоит смотреть полностью: да / нет
```

---

## 5. YouTube — RU / CIS

Основной фокус — реальные production cases, backend, highload, Go и архитектура.

| Канал | URL | Приоритет | Темы |
|---|---|---:|---|
| Yandex for Backend | https://www.youtube.com/@YandexforBackend | 5/5 | Backend, Go, Python, Java, distributed systems |
| AvitoTech | https://www.youtube.com/@AvitoTech | 5/5 | Go, backend, architecture, highload |
| Golang Channel / GolangConf | https://www.youtube.com/@GolangChannel | 5/5 | Go |
| HighLoad++ | https://www.youtube.com/@HighLoadChannel | 5/5 | Highload, DB, infrastructure, architecture |
| Podlodka | https://www.youtube.com/@PodlodkaPodcast | 4/5 | Engineering deep dives, AI engineering |
| MoscowPython | https://www.youtube.com/@MoscowPython | 3/5 | Python ecosystem |
| АйТиБорода | https://www.youtube.com/@АйТиБорода | 3/5 | Interviews, industry, engineering |

---

## 6. Telegram — RU / CIS

Telegram делится на два типа источников:

```text
telegram_channel
telegram_community
```

### Каналы

| Источник | URL | Приоритет | Темы |
|---|---|---:|---|
| AvitoTech | https://t.me/avitotech | 5/5 | Backend, Go, architecture |
| Yandex for Backend | https://t.me/yandexforbackend | 5/5 | Backend, distributed systems |
| HighLoad++ | https://t.me/highloadchannel | 4/5 | Highload, DB, infrastructure |
| Postgres Professional | https://t.me/postgres_pro | 4/5 | PostgreSQL |
| MoscowPython | https://t.me/moscow_python | 3/5 | Python |

### Сообщества / чаты

| Источник | URL | Приоритет | Темы |
|---|---|---:|---|
| Golang RU | https://t.me/golang_ru | 5/5 | Go community |
| Go Proglib | https://t.me/goproglib | 3/5 | Go articles |
| DevOps RU | https://t.me/devops_ru | 4/5 | DevOps |
| Kubernetes RU | https://t.me/kubernetes_ru | 4/5 | Kubernetes |
| Docker RU | https://t.me/docker_ru | 4/5 | Docker |

### Как использовать community-чаты

Не пытаться делать summary всех сообщений.

Искать:

```text
discussion_spike
repeated_error
breaking_change_reports
production_incident
new_tool_adoption
controversial_change
workaround
```

Пример результата:

> В kubernetes_ru за сутки резко выросло обсуждение проблемы X после обновления Y.
>
> 37 сообщений, 11 участников.
>
> Возможный сигнал: regression.
>
> Требуется подтверждение официальным источником.

---

## 7. Radar / Discovery Sources

Эти источники помогают находить темы, которые ещё не попали в официальный поток.

| Источник | URL | Приоритет | Назначение |
|---|---|---:|---|
| Hacker News | https://news.ycombinator.com/ | 4/5 | Emerging topics |
| InfoQ | https://www.infoq.com/ | 4/5 | Architecture, releases, trends |
| Lobsters | https://lobste.rs/ | 3/5 | Engineering community |
| Reddit / r/golang | https://www.reddit.com/r/golang/ | 3/5 | Go community signal |
| Reddit / r/programming | https://www.reddit.com/r/programming/ | 2/5 | Broad tech radar |

Эти источники не должны считаться подтверждением новости.

Их задача:

```text
discover -> verify -> enrich
```

---

# 8. Иерархия доверия

Источники не должны иметь одинаковый вес.

Пример базовой модели:

```yaml
source_types:
  official:
    authority_weight: 1.00

  official_changelog:
    authority_weight: 1.00

  engineering_blog:
    authority_weight: 0.90

  conference:
    authority_weight: 0.80

  youtube_expert:
    authority_weight: 0.75

  telegram_channel:
    authority_weight: 0.65

  telegram_community:
    authority_weight: 0.55

  aggregator:
    authority_weight: 0.45
```

Пример отдельных источников:

```yaml
sources:
  go_dev:
    authority_weight: 1.00

  postgresql_org:
    authority_weight: 1.00

  kubernetes_io:
    authority_weight: 1.00

  openai_changelog:
    authority_weight: 1.00

  cloudflare_blog:
    authority_weight: 0.90

  uber_engineering:
    authority_weight: 0.90

  hussein_nasser:
    authority_weight: 0.75

  fireship:
    authority_weight: 0.70

  avitotech:
    authority_weight: 0.80

  highload:
    authority_weight: 0.75

  golang_ru:
    authority_weight: 0.55
```

---

# 9. Правило подтверждения новости

Если Telegram, YouTube, Hacker News или другой secondary source сообщает:

> В Go нашли критический баг.

это ещё не должно автоматически становиться новостью 5/5.

Pipeline:

```text
secondary source
      ↓
extract claim
      ↓
search primary source
      ↓
verify
      ↓
calculate importance
      ↓
publish / ignore
```

Возможные primary sources:

- официальный сайт проекта;
- changelog;
- GitHub release;
- GitHub security advisory;
- официальный engineering blog;
- CVE/NVD;
- release notes.

---

# 10. Предлагаемая структура записи Source

```yaml
id: go_blog

name: The Go Blog

url: https://go.dev/blog/

source_type: official

category: stack
subcategory: golang

language: en

priority: 5
authority_weight: 1.0

relevance:
  golang: 1.0
  backend: 0.9
  databases: 0.1
  infrastructure: 0.5
  ai: 0.1

formats:
  - article
  - release

fetch:
  rss: true
  scrape: true
  youtube: false
  telegram: false
```

---

# 11. Предлагаемые категории

```text
stack
backend
infrastructure
databases
messaging
cloud_native
observability
security

ai
ai_agents
ai_coding
ai_infrastructure

engineering_blog

youtube_en
youtube_ru

telegram_channel
telegram_community

radar
```

---

# 12. Классификация материалов

Каждый полученный материал можно классифицировать:

```text
release
security
breaking_change
deprecation
new_feature

incident
postmortem
engineering_deep_dive
benchmark
architecture

ai_model
ai_agent
ai_tool
ai_research

video
conference_talk
tutorial

community_signal
discussion
rumor
```

---

# 13. Пример расчёта важности

Можно хранить несколько независимых факторов:

```yaml
importance:
  global_impact: 0.0
  stack_relevance: 0.0
  novelty: 0.0
  authority: 0.0
  production_impact: 0.0
  community_signal: 0.0
```

Условная итоговая формула (первая версия; действующая — в спеке
`2026-09-23-news-digest-design.md` и `WEIGHTS` в `digest.py`):

```text
score =
    global_impact       * 0.20 +
    stack_relevance     * 0.25 +
    novelty             * 0.15 +
    authority           * 0.15 +
    production_impact   * 0.20 +
    community_signal    * 0.05
```

Затем:

```text
0.00 - 0.20 -> importance 1
0.20 - 0.40 -> importance 2
0.40 - 0.60 -> importance 3
0.60 - 0.80 -> importance 4
0.80 - 1.00 -> importance 5
```

---

# 14. Размер initial source pool

Для первой версии нет необходимости собирать сотни источников.

Рекомендуемый seed:

```text
Official tech / releases       15
Engineering blogs               8
AI primary sources              8
YouTube EN                     10
YouTube RU/CIS                  7
Telegram                       10
Radar / aggregators             5
---------------------------------
Total                          ~63
```

После запуска можно автоматически оценивать полезность каждого источника:

```text
source_value =
    important_items_found /
    total_items_processed
```

И постепенно отключать шумные источники.

---

# 15. Пример дневного pipeline

```text
~650 collected items
        ↓
deduplication
        ↓
~300 unique items
        ↓
classification
        ↓
~180 technical items
        ↓
user relevance filtering
        ↓
~55 relevant items
        ↓
importance scoring
        ↓
~15-20 interesting items
        ↓
cross-source clustering
        ↓
~6 important events
        ↓
daily digest
        ↓
3-5 items for the user
```

---

# 16. Главный принцип продукта

Сервис не должен отвечать на вопрос:

> Что сегодня опубликовано?

Он должен отвечать на вопрос:

> Что сегодня произошло в мире разработки, что мне действительно нужно знать?

Для Go/backend-разработчика приоритет примерно такой:

```text
1. Critical changes in my stack
2. Security / breaking changes
3. Important backend / infrastructure developments
4. AI changes affecting software development
5. Strong engineering deep dives
6. High-signal conference talks / YouTube videos
7. Community anomalies and emerging discussions
8. Everything else
```
