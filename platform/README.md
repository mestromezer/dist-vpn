# Platform

Полное ТЗ: [../docs/TZ-5-platform.md](../docs/TZ-5-platform.md).

Кратко: Python 3.12+, FastAPI + SQLAlchemy 2.0 (async) + Alembic + PostgreSQL, ARQ для фоновых задач, gRPC-сервер к Agent'ам по контракту [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto), REST/WebSocket API для Admin Client по контракту [`../contracts/rest/openapi.yaml`](../contracts/rest/openapi.yaml).

## Старт работы

1. Сгенерировать Python-стабы из `contracts/proto/agent_control.proto`.
2. Модель данных — [TZ-5, §3](../docs/TZ-5-platform.md#3-модель-данных-минимальный-набор-сущностей) (Node, Cascade, User, UsageStat, HealthEvent, LinkTest, RotationRecord, AdminAccount).
3. Разделы безопасности — [TZ-5, §6](../docs/TZ-5-platform.md#6-требования-к-безопасности) обязательны к реализации с первой версии, не "потом добавим".
4. Критерии приёмки — [TZ-5, §9](../docs/TZ-5-platform.md#9-критерии-приёмки).
