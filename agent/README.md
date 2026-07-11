# Agent

Полное ТЗ: [../docs/TZ-4-agent.md](../docs/TZ-4-agent.md).

Кратко: Python 3.12+, `docker-py` для управления локальным Xray-core, gRPC-клиент к Platform по контракту [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto) (dial-out, mTLS), нативные gRPC-стабы к самому Xray (`HandlerService`/`RoutingService`/`StatsService`) вместо шелл-обёртки над CLI.

## Старт работы

1. Сгенерировать Python-стабы из `contracts/proto/agent_control.proto` (`grpcio-tools`, команда — см. `contracts/README.md`, п.1, как только появится Makefile).
2. Структура пакета, зависимости (`pyproject.toml`), тесты — на усмотрение разработчика в рамках стека из ТЗ.
3. Критерии приёмки — [TZ-4, §8](../docs/TZ-4-agent.md#8-критерии-приёмки).
