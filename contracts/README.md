# Контракты

Границы между тремя независимо разрабатываемыми программами ([Agent](../docs/TZ-4-agent.md), [Platform](../docs/TZ-5-platform.md), [Admin Client](../docs/TZ-6-admin-client.md)). Меняются только через PR — от каждого файла здесь зависит минимум две стороны одновременно.

- **`proto/agent_control.proto`** — control-plane канал Agent↔Platform (gRPC, bidi-streaming, agent dial-out — см. [TZ-4, §5.1](../docs/TZ-4-agent.md)). Проверено компиляцией через `grpcio-tools` (см. `Makefile`/CI, когда появится).
- **`rest/openapi.yaml`** — REST API Platform↔Admin Client (OpenAPI 3.1). WebSocket-канал `/events` описан текстом внизу файла — формального AsyncAPI-описания пока нет, добавить при первой реализации.

## Как менять

1. Если меняется `agent_control.proto` — сначала обновить эту схему, затем регенерировать стабы в `agent/` и `platform/` (команда генерации — появится в соответствующих `Makefile`/`pyproject.toml` при первой реализации).
2. Если меняется `rest/openapi.yaml` — Admin Client может сгенерировать типизированный клиент прямо из схемы (стандартные генераторы OpenAPI→TypeScript), не писать типы вручную.
3. Ломающие изменения — только с версионированием (`v1`→`v2` в пакете proto / версия в пути REST), не молча менять поведение уже реализованной v1.
