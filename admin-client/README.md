# Admin Client

Полное ТЗ: [../docs/TZ-6-admin-client.md](../docs/TZ-6-admin-client.md).

Кратко: React + TypeScript + Vite, [react-admin](https://github.com/marmelab/react-admin) как основа (минимум кастомного кода/CSS — MUI из коробки), [React Flow](https://reactflow.dev/) для графа топологии каскадов. Данные — по контракту [`../contracts/rest/openapi.yaml`](../contracts/rest/openapi.yaml) (можно сгенерировать типизированный клиент прямо из схемы).

## Старт работы

1. Сгенерировать TypeScript-типы/клиент из `contracts/rest/openapi.yaml` (стандартный генератор OpenAPI→TS — не писать типы вручную).
2. Экраны — по [TZ-6, §3](../docs/TZ-6-admin-client.md#3-функциональные-требования): ноды/каскады (граф со статусом), тест-запрос (человекочитаемый результат), пользователи, ротация секретов, логин.
3. Чек-лист безопасности перед сдачей — [TZ-6, §5](../docs/TZ-6-admin-client.md#5-требования-к-безопасности).
4. Критерии приёмки — [TZ-6, §7](../docs/TZ-6-admin-client.md#7-критерии-приёмки).
