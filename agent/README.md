# Agent

Полное ТЗ: [../docs/TZ-4-agent.md](../docs/TZ-4-agent.md).
Контракт: [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto) (не редактируется здесь — см. [../contracts/README.md](../contracts/README.md)).

Кратко: Python 3.12+, `docker-py` для управления локальным Xray-core (в этом
срезе — только объявленная зависимость, не используется), gRPC-клиент к
Platform по контракту `agent_control.proto` (agent dial-out, mTLS), нативные
gRPC-стабы к самому Xray (`HandlerService`/`RoutingService`/`StatsService`) —
следующий срез.

## Текущий срез: `agent/grpc-client-skeleton`

Этот срез реализует **только control-plane gRPC-клиент** — канал
Agent↔Platform (`AgentControl.Session`, единственный bidi-streaming RPC).
Управление Xray/Docker сюда не входит (см. "Scope of this slice" ниже).

## Структура

```
agent/
  pyproject.toml            зависимости, src-layout, пакет dist_vpn_agent
  Dockerfile                двухстадийная сборка (регенерация стабов + wheel, затем рантайм)
  scripts/
    gen_proto.sh             регенерация Python gRPC-стабов из contracts/proto/agent_control.proto
  src/dist_vpn_agent/
    __init__.py
    config.py                AgentSettings (pydantic-settings) + mask_secret()
    logging_setup.py         структурированное JSON-логирование
    control_client.py        gRPC-клиент control-plane (ядро этого среза)
    main.py                  точка входа процесса (console-script `dist-vpn-agent`)
    generated/                сгенерированные protobuf/gRPC стабы — НЕ коммитится (см. .gitignore)
  tests/
    mock_platform_server.py  мок AgentControl-сервера для тестов
    test_control_client.py   end-to-end: enrollment -> heartbeat -> ping -> command
    test_config.py           валидация mTLS-конфигурации (в т.ч. bootstrap-случай)
```

## Установка зависимостей

Требуется Python 3.12+.

```bash
cd agent
python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

(В разработке этого среза системный `python` на хосте был 3.10 — использовался
`py -3.13` через Python launcher; подходит любой интерпретатор 3.12+.)

## Регенерация gRPC-стабов

```bash
bash scripts/gen_proto.sh
```

Скрипт сам резолвит пути относительно своего расположения, так что его можно
запускать и из `agent/`, и из корня репозитория. Требует `grpcio-tools`
(ставится вместе с остальными зависимостями выше). Результат кладётся в
`src/dist_vpn_agent/generated/` — каталог целиком в `.gitignore`
(`**/generated/`, `**/*_pb2.py`, `**/*_pb2_grpc.py`); коммитится только сам
скрипт генерации, никогда — его вывод.

Регенерировать нужно: сразу после установки зависимостей (стабов ещё нет) и
после любого изменения `agent_control.proto` (изменение — обязанность
Agent- и Platform-команд одновременно, см. `contracts/README.md`).

Технический нюанс, на который стоит обратить внимание при отладке скрипта:
`protoc`-плагин для Python всегда генерирует плоский top-level импорт в
`*_pb2_grpc.py` (`import agent_control_pb2 as ...`), независимо от того, куда
кладётся результат. Это ломается, как только файл оказывается внутри пакета
(`dist_vpn_agent.generated`), а не лежит прямо на `sys.path`. `gen_proto.sh`
патчит эту строку на relative-импорт (`from . import agent_control_pb2 as
...`) через `sed` сразу после генерации — это тот же самый обходной путь,
ради которого существуют инструменты вроде `protoletariat`.

## Конфигурация (переменные окружения)

Все настройки — через `pydantic-settings`, префикс `AGENT_`; также
подхватывается файл `.env` в рабочей директории (никогда не коммитить `.env`
— см. корневой `.gitignore`).

| Переменная | По умолчанию | Описание |
|---|---|---|
| `AGENT_PLATFORM_ADDRESS` | `localhost:50051` | `host:port` Platform gRPC-эндпоинта. Agent всегда сам инициирует соединение (dial-out, TZ-4 §5.1), никогда не принимает входящие control-plane соединения |
| `AGENT_ENROLLMENT_TOKEN` | *(пусто)* | Одноразовый TTL-токен для enrollment (TZ-4 §5.2) |
| `AGENT_HOSTNAME` | `socket.gethostname()` | Имя ноды, отправляется в `Enrollment` |
| `AGENT_AGENT_VERSION` | `0.1.0` | Версия агента, отправляется в `Enrollment` |
| `AGENT_REQUESTED_ROLE` | `unspecified` | `entry` \| `exit` \| `unspecified` — см. `NodeRole` в контракте |
| `AGENT_HEARTBEAT_INTERVAL_SECONDS` | `15.0` | Интервал отправки `Heartbeat` (TZ-4 §3.1 default) |
| `AGENT_RECONNECT_BACKOFF_INITIAL_SECONDS` | `1.0` | Начальная задержка перед реконнектом после обрыва сессии |
| `AGENT_RECONNECT_BACKOFF_MAX_SECONDS` | `30.0` | Потолок экспоненциального backoff реконнекта |
| `AGENT_MTLS_CA_CERT_PATH` | *(нет)* | Путь к CA, которым подписан серверный сертификат Platform. Обязателен, если не включён `insecure_dev_mode` |
| `AGENT_MTLS_CLIENT_CERT_PATH` | *(нет)* | Путь к клиентскому mTLS-сертификату ноды. Опционален — см. ниже про bootstrap |
| `AGENT_MTLS_CLIENT_KEY_PATH` | *(нет)* | Путь к приватному ключу клиента. Должен быть задан вместе с `..._CERT_PATH` (либо оба, либо ни одного) |
| `AGENT_INSECURE_DEV_MODE` | `false` | Только для локальной разработки: небезопасный (без TLS) канал. **Никогда не включать в production** — см. предупреждение ниже |
| `AGENT_LOG_LEVEL` | `INFO` | Уровень логирования |

### mTLS и bootstrap-нюанс

Обычный (установившийся) канал — mTLS: и клиентский, и серверный сертификат
проверяются обеими сторонами. Но на **самом первом** запуске у Agent'а ещё
нет собственного, специфичного для ноды клиентского сертификата — вся идея
одноразового enrollment-токена (TZ-4 §3.1/§5.2) в том, что именно он и
аутентифицирует агента при самом первом подключении. Поэтому:

- `AGENT_MTLS_CA_CERT_PATH` обязателен всегда (кроме `insecure_dev_mode`) —
  Agent обязан суметь проверить серверный сертификат Platform в любом случае.
- `AGENT_MTLS_CLIENT_CERT_PATH` / `AGENT_MTLS_CLIENT_KEY_PATH` — опциональны.
  Если не заданы, канал поднимается как обычный TLS с проверкой сервера, но
  без клиентского сертификата (агент аутентифицируется на уровне сообщения
  `Enrollment` через токен). Если заданы — используется полноценный mTLS.
  Задавать можно либо обе переменные сразу, либо ни одной — конфигурация с
  одной из двух отклоняется на старте с понятной ошибкой.

Это осознанное архитектурное решение, а не недосмотр — см. раздел "Замеченные
вопросы к контракту" ниже про то, почему сертификат, выданный в
`EnrollmentAck`, пока не привязывается автоматически к локальному keypair.

### Небезопасный dev-режим

`AGENT_INSECURE_DEV_MODE=true` открывает нешифрованный, неаутентифицированный
канал к Platform. Это единственный способ обойти обязательный TLS/mTLS, он
явно и громко логируется (`WARNING`) при каждом использовании
(`control_client.py`, `_build_channel`) и никогда не является поведением по
умолчанию (валидация конфигурации падает без mTLS-путей, если этот флаг не
установлен явно). Используется только для локальной разработки и в тестах
(в т.ч. в `tests/`) — никогда в production (TZ-4 §5.2, §5.5 категория A02).

## Запуск локально

```bash
cd agent
pip install -e .
bash scripts/gen_proto.sh

AGENT_PLATFORM_ADDRESS=localhost:50051 \
AGENT_ENROLLMENT_TOKEN=my-one-time-token \
AGENT_INSECURE_DEV_MODE=true \
dist-vpn-agent
```

(эквивалентно `python -m dist_vpn_agent.main`). Без реальной Platform на
другом конце соединение будет переподключаться с экспоненциальным backoff
(`AGENT_RECONNECT_BACKOFF_*`), с понятным сообщением об ошибке на уровне
`ERROR` при каждой неудачной попытке — не тихое зависание (TZ-4 §7).

## Тесты

```bash
cd agent
pip install -e .
bash scripts/gen_proto.sh   # нужно сделать до запуска тестов — стабы не коммитятся
pytest -v
```

Все тесты пройдены локально (`8 passed`, Python 3.13.14 в venv на базе
`py -3.13`). Что покрыто:

- `tests/test_control_client.py` — поднимает лёгкий in-process мок
  `AgentControl`-сервера (`tests/mock_platform_server.py`) на сгенерированных
  стабах и гоняет `ControlClient` по нему через `pytest-asyncio`:
  - enrollment → heartbeat → ping (полный цикл; `node_id` получен от сервера
    и используется в последующих heartbeat);
  - `Command` → `CommandResult(success=false, error_message="not implemented
    in this slice")` — контракт исполняется сквозным образом, без реальной
    логики Xray (это явный, намеренный предмет данного среза);
  - отказ enrollment (`accepted=false`) поднимает `EnrollmentRejected`.
- `tests/test_config.py` — валидация mTLS-конфигурации, включая bootstrap-
  случай (только CA, без клиентского сертификата) и проверку, что
  клиентский сертификат/ключ нельзя задать по отдельности.

## Docker

```bash
# Собирать из КОРНЯ репозитория — контексту нужен доступ к
# contracts/proto/agent_control.proto, который лежит вне agent/
# (см. комментарий вверху Dockerfile).
docker build -f agent/Dockerfile -t dist-vpn-agent:dev .

docker run --rm \
  -e AGENT_PLATFORM_ADDRESS=platform.example:50051 \
  -e AGENT_ENROLLMENT_TOKEN=... \
  -e AGENT_INSECURE_DEV_MODE=true \
  dist-vpn-agent:dev
```

Сборка образа и его запуск проверены вручную в рамках этого среза (`docker
build` проходит, контейнер поднимается, логирует enrollment/переподключение
структурированным JSON, токен маскируется в логах). Продакшн-запуск —
смонтировать mTLS-материалы и задать `AGENT_MTLS_*_PATH` вместо
insecure-режима. Образ двухстадийный: стадия сборки регенерирует gRPC-стабы
и собирает wheel, финальный слой — чистый рантайм под непривилегированным
пользователем `agent` (TZ-4 §5.3).

## Scope of this slice / что НЕ реализовано

Реализовано в `agent/grpc-client-skeleton`:

- Реальный, запускаемый Python 3.12+ проект (src-layout, пакет `dist_vpn_agent`).
- Async gRPC-клиент control-plane: agent dial-out, единственный bidi-stream
  `Session`, `Enrollment` → цикл `Heartbeat` + параллельное чтение
  `PlatformMessage`.
- Обработка `EnrollmentAck` (лог + сохранение `node_id` в памяти процесса),
  `Ping` (лог), `Command` (лог "не реализовано в этом срезе" + отправка
  `CommandResult(success=false, error_message="not implemented in this
  slice")`, так что контракт проходит сквозной цикл целиком).
- mTLS-канал по умолчанию (с осмысленным bootstrap-исключением для самого
  первого enrollment — см. выше), explicit insecure dev-режим только по
  явному флагу, с громким предупреждением.
- `pydantic-settings`-конфигурация, структурированное JSON-логирование без
  утечки секретов в лог (`SecretStr` + `mask_secret()`).
- Реконнект с экспоненциальным backoff вокруг одной сессии (`run_forever`)
  — минимальная версия "переживает кратковременную потерю связи" (TZ-4
  §4/§8), но БЕЗ полной декларативной реконсиляции состояния (для неё нет
  ещё Xray-слоя, который нужно приводить к желаемому состоянию).
- Тесты (мок `AgentControl`-сервера, `pytest-asyncio`), `Dockerfile`
  (собран и прогнан вручную), скрипт регенерации стабов.

Сознательно НЕ реализовано в этом срезе (следующие срезы, по TZ-4):

- Управление Xray-core (`HandlerService`/`RoutingService`/`StatsService`,
  TZ-4 §3.2) — `Command` в этом срезе только логируется и получает ответ
  "not implemented", реальной трансляции в вызовы к Xray нет.
- Управление жизненным циклом Docker-контейнера Xray. `docker-py` в этом
  срезе — только объявленная зависимость в `pyproject.toml`, нигде не
  вызывается (решение по TZ-4 §5.4 — как именно Agent получает доступ к
  Docker Engine API — сознательно отложено до появления кода, который
  реально этим управляет).
- Пресеты P1–P4 и ротация REALITY/TLS-секретов (TZ-4 §3.3) — `RotateSecret`
  приходит по контракту, но предметно не обрабатывается.
- Тест-запрос через эфемерный клиент (TZ-4 §3.4) — `RunTest` не
  обрабатывается предметно (эфемерный Docker-контейнер не поднимается).
- Телеметрия per-user/per-inbound счётчиков (TZ-4 §3.5) — `TelemetryReport`
  не отправляется, т.к. считать пока нечего без слоя управления Xray.
- Персист сертификата, полученного в `EnrollmentAck.mtls_client_cert_pem`,
  на диск и его последующее использование вместо токена при реконнектах —
  сейчас каждый перезапуск процесса требует валидный (одноразовый!)
  `enrollment_token` заново. Это важно закрыть в следующем срезе вместе с
  решением вопроса о CSR/keypair (см. "Замеченные вопросы к контракту").
- Backhaul-транспорт Hop B как pluggable-абстракция (TZ-4 §6) — вне
  периметра control-plane клиента.
- Self-diagnostics по недоступности Docker/испорченному локальному
  сертификату (TZ-4 §7) — в этом срезе есть только диагностика по
  недоступности Platform (структурированные ERROR-логи с причиной и
  реконнектом, без тихого зависания); Docker/сертификат пока нечем
  диагностировать, т.к. они не используются.

## Замеченные вопросы к контракту (не тронуты, только отчёт)

`contracts/proto/agent_control.proto` не редактировался — по правилам
контракта (`contracts/README.md`) изменения там требуют PR и согласования с
Platform-стороной. Но при реализации bootstrap-сценария (TZ-4 §3.1: "Agent
генерирует локальный keypair, инициирует enrollment ... получает mTLS-
сертификат клиента") обнаружился вероятный пробел: сообщение `Enrollment`
не содержит поля ни для публичного ключа/CSR, ни для какого-либо
идентификатора локально сгенерированного keypair, а `EnrollmentAck`
возвращает только `mtls_client_cert_pem` (без приватного ключа — что и
правильно, приватный ключ никогда не должен покидать ноду). Без CSR/
публичного ключа в `Enrollment` непонятно, каким именно публичным ключом
Platform-CA должен подписывать выдаваемый сертификат, и как агенту потом
доказать владение соответствующим приватным ключом. Варианты решения (не
реализованы, требуют согласования с Platform-командой через PR к контракту):

1. Добавить в `Enrollment` поле `bytes csr_der` (или отдельный ранний
   RPC/сообщение для CSR) — агент генерирует keypair и CSR локально, шлёт
   CSR вместе с токеном, получает подписанный сертификат обратно.
2. Либо явно принять, что для самого первого enrollment Platform генерирует
   keypair *и* сертификат сама и должна вернуть в `EnrollmentAck` не только
   сертификат, но и приватный ключ (менее предпочтительно с точки зрения
   PKI-гигиены, но проще и, возможно, было тем, что имелось в виду).

В этом срезе решение не принято — реализован только явный fallback (см.
"mTLS и bootstrap-нюанс" выше): самый первый enrollment идёт по TLS с
проверкой только серверного сертификата, а `EnrollmentAck.mtls_client_cert_pem`,
когда он приходит, только логируется (без персиста и без попытки построить
из него рабочую клиентскую identity) — см. "Scope of this slice" выше.
