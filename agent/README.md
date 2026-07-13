# Agent

Полное ТЗ: [../docs/TZ-4-agent.md](../docs/TZ-4-agent.md).
Контракт: [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto) (не редактируется здесь — см. [../contracts/README.md](../contracts/README.md)).

Кратко: Python 3.12+, control-plane gRPC-клиент к Platform по контракту
`agent_control.proto` (agent dial-out, mTLS), нативные gRPC-стабы к самому
Xray-core (`HandlerService`/`RoutingService`/`StatsService`, из реально
завендоренных `.proto` — см. `vendor/xray-proto/`) для девяти из одиннадцати
`Command`-операций (все, кроме `RunTest`/`RotateSecret`), и `docker-py` для
управления локальным Xray-core-контейнером.

## Текущий срез: `agent/grpc-client-skeleton`

Предыдущий под-срез реализовал только control-plane канал Agent↔Platform
(`AgentControl.Session`) с "not implemented"-заглушкой на каждую `Command`.
Этот под-срез добавляет реальное управление локальным Xray-core:
`xray_client.py` (нативные gRPC-вызовы к Xray) и `docker_manager.py`
(жизненный цикл контейнера), подключённые к тому же control-plane клиенту.
`RunTest` и `RotateSecret` по-прежнему не реализованы предметно (см. "Scope
of this slice" ниже — это осознанное решение, не недосмотр).

## Структура

```
agent/
  pyproject.toml            зависимости, src-layout, пакет dist_vpn_agent
  Dockerfile                двухстадийная сборка (регенерация стабов + wheel, затем рантайм)
  scripts/
    gen_proto.sh                  регенерация Python gRPC-стабов: agent_control.proto И vendor/xray-proto/
    _fix_xray_proto_imports.py    пост-обработка импортов для завендоренного (многофайлового) Xray proto-дерева
  vendor/xray-proto/         реальные .proto из XTLS/Xray-core (закреплённый tag/commit) -- см. его README.md
  src/dist_vpn_agent/
    __init__.py
    config.py                AgentSettings (pydantic-settings) + mask_secret()
    logging_setup.py         структурированное JSON-логирование
    control_client.py        gRPC-клиент control-plane + реальный диспатч Command -> xray_client.py
    xray_client.py            gRPC-клиент к локальному Xray-core (HandlerService/RoutingService/StatsService)
    docker_manager.py         жизненный цикл Docker-контейнера Xray-core (docker-py)
    main.py                  точка входа процесса (console-script `dist-vpn-agent`)
    generated/                сгенерированные protobuf/gRPC стабы (agent_control + xray/) — НЕ коммитится (см. .gitignore)
  tests/
    mock_platform_server.py  мок AgentControl-сервера для тестов
    mock_xray_server.py       мок HandlerService/RoutingService/StatsService для тестов
    test_control_client.py   end-to-end: enrollment -> heartbeat -> ping -> real command dispatch
    test_xray_client.py       все 9 реализованных операций против мок-Xray + разбор ошибок,
                               плюс отдельный ярус против настоящего Xray-core-контейнера
    test_docker_manager.py    юнит-тесты (мок docker-клиент) + реальный интеграционный тест против Docker Desktop
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
(ставится вместе с остальными зависимостями выше). Компилирует **оба**
источника за один запуск:

1. `contracts/proto/agent_control.proto` → `src/dist_vpn_agent/generated/*_pb2*.py` (как раньше).
2. Всё дерево `vendor/xray-proto/**/*.proto` → `src/dist_vpn_agent/generated/xray/` (новое в этом срезе).

Оба каталога целиком в `.gitignore` (`**/generated/`, `**/*_pb2.py`,
`**/*_pb2_grpc.py`, `**/*_pb2.pyi`); коммитятся только сами `.proto` (в
`vendor/xray-proto/`) и скрипты генерации, никогда их вывод.

Регенерировать нужно: сразу после установки зависимостей (стабов ещё нет) и
после любого изменения `agent_control.proto` или `vendor/xray-proto/**`.

Технический нюанс №1 (был и раньше): `protoc`-плагин для Python всегда
генерирует плоский top-level импорт в `*_pb2_grpc.py`
(`import agent_control_pb2 as ...`), независимо от того, куда кладётся
результат. Это ломается, как только файл оказывается внутри пакета
(`dist_vpn_agent.generated`), а не лежит прямо на `sys.path`. Для
`agent_control.proto` (один плоский файл) это по-прежнему чинится `sed`
сразу после генерации.

Технический нюанс №2 (новое в этом срезе): `vendor/xray-proto/` —
многофайловое дерево, повторяющее структуру каталогов самого Xray-core
(`app/proxyman/command/command.proto` импортирует `core/config.proto` и
т.д.), и `protoc` генерирует такой же проблемный "плоский" импорт в
**каждом** файле дерева, не только в одном `_grpc.py`. Одной строки `sed`
недостаточно — `_fix_xray_proto_imports.py` обобщает тот же приём:
пересчитывает нужное число ведущих точек (relative-импорт) для каждого
файла исходя из его глубины под корнем вывода, и заодно создаёт
отсутствующие `__init__.py` (protoc их не создаёт для вложенных
директорий). Это тот же самый обходной путь, ради которого существуют
инструменты вроде `protoletariat` — он не использован из-за конфликта
версий `protobuf` с уже установленным `grpcio-tools` (см. историю коммитов
этого среза), решение через собственный скрипт проще и без даунгрейда
рантайм-зависимости.

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
| `AGENT_XRAY_API_ADDRESS` | `127.0.0.1:10085` | `host:port` control API самого локального Xray-core (`HandlerService`/`RoutingService`/`StatsService`, `xray_client.py`). Только loopback/локальная доверенная граница — никогда не публикуется наружу |
| `AGENT_XRAY_DOCKER_IMAGE` | `ghcr.io/xtls/xray-core:latest` | Образ (с тегом), из которого `docker_manager.py` создаёт/запускает контейнер Xray-core |
| `AGENT_XRAY_CONTAINER_NAME` | `dist-vpn-xray-core` | Имя управляемого контейнера — `docker_manager.py` трогает **только** контейнер с этим именем (TZ-4 §5.4) |
| `AGENT_XRAY_CONFIG_HOST_PATH` | `/etc/dist-vpn/xray-config.json` | Путь на хосте, монтируемый read-only в контейнер как `/etc/xray/config.json` |
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

Все тесты пройдены локально (`38 passed`, Python 3.13.14 в venv на базе
`py -3.13`). Что покрыто, и — важно — **против чего именно** (мок или
реальная инфраструктура, см. "Mock vs реальный Xray/Docker — что реально
проверено" ниже):

- `tests/test_control_client.py` — поднимает лёгкий in-process мок
  `AgentControl`-сервера (`tests/mock_platform_server.py`) на сгенерированных
  стабах и гоняет `ControlClient` по нему через `pytest-asyncio`:
  - enrollment → heartbeat → ping (полный цикл; `node_id` получен от сервера
    и используется в последующих heartbeat);
  - heartbeat отражает реальный (пусть и через фейковый `DockerManager` в
    тестах) статус `xray_container_running` — раньше было захардкожено
    `False`;
  - `Command` (`add_user`, `add_inbound`) действительно диспатчится в
    `xray_client.py` (мок-Xray, см. ниже) и возвращает реальный
    `CommandResult` — как успешный, так и с настоящей ошибкой от Xray/
    трансляции, не проглоченной;
  - `RunTest` — единственная команда, которая по-прежнему получает
    "not implemented" (осознанно, см. "Scope of this slice");
  - отказ enrollment (`accepted=false`) поднимает `EnrollmentRejected`.
- `tests/test_xray_client.py` — два яруса (см. секцию ниже про mock vs
  реальный Xray):
  1. Против in-process мок-`HandlerService`/`RoutingService`/`StatsService`
     (`tests/mock_xray_server.py`) — все девять диспетчеризуемых операций,
     разбор ошибок (`grpc.RpcError` не проглатывается), трансляционные
     ошибки (невалидный JSON, неподдерживаемый протокол/транспорт).
  2. Против настоящего контейнера `ghcr.io/xtls/xray-core` (класс тестов
     `test_real_xray_core_*`) — пропускаются с понятной причиной, если
     Docker недоступен.
- `tests/test_docker_manager.py` — юнит-тесты на замоканном `docker-py`-
  клиенте (create/pull/start/no-op/health) плюс один реальный интеграционный
  тест против настоящего Docker Desktop (`test_real_docker_engine_create_
  start_stop_restart_cycle`), тоже пропускаемый при недоступном Docker.
- `tests/test_config.py` — валидация mTLS-конфигурации, включая bootstrap-
  случай (только CA, без клиентского сертификата) и проверку, что
  клиентский сертификат/ключ нельзя задать по отдельности.

### Mock vs реальный Xray/Docker — что реально проверено

Честно, без "verified" по умолчанию:

- **Docker**: в среде разработки этого среза Docker Desktop был доступен и
  запущен. `docker_manager.py` реально создавал, запускал, останавливал и
  перезапускал настоящий контейнер `ghcr.io/xtls/xray-core:latest` через
  собственный код (`test_real_docker_engine_create_start_stop_restart_cycle`,
  плюс отдельная ручная проверка вне pytest). Если в вашей среде Docker
  недоступен — этот тест и `ensure_running()` при старте (`main.py`) будут,
  соответственно, скипаться/логировать `DockerManagerError`, не падая.
- **Xray-core**: операции `xray_client.py` (все девять диспетчеризуемых:
  add/remove inbound на всех трёх используемых проектом транспортах —
  raw/xhttp/ws, add/remove user, add/remove outbound, add/remove routing
  rule, override_balancer) были реально прогнаны против настоящего
  `ghcr.io/xtls/xray-core`-контейнера (не только против мока) — как вручную
  в ходе разработки, так и теперь автоматически, в `test_real_xray_core_*`
  из `test_xray_client.py` (пропускаются, если Docker недоступен). Это и
  нашло две реальные несостыковки, которых мок в принципе не мог поймать
  (мок не декодирует `TypedMessage`-полезную нагрузку, а только записывает
  байты как есть):
  1. `stream.protocol_name` должен быть именем транспорта из реестра
     Xray (`tcp`/`splithttp`/`websocket`), а не именем из JSON-диалекта
     этого проекта (`raw`/`xhttp`/`ws`) — иначе реальный Xray падает на
     `transport/internet: <name> listener not registered` при попытке
     поднять listener.
  2. `AddRuleRequest.config` должен оборачивать `router.Config`
     (`{rule: [...]}`), а не голый `RoutingRule` — `Router.AddRule` в
     реальном Go-коде Xray делает `inst.(*Config)` и возвращает
     `"AddRule: config type error"` на что угодно ещё.

  Обе несостыковки исправлены в текущем коде `xray_client.py` (см. историю
  коммитов этого среза) и теперь покрыты и мок-, и реальным ярусом тестов.
  Что **не** проверено даже реальным Xray: полная семантика каждого
  возможного сочетания настроек (например, что REALITY-приватный ключ
  неправильной длины отклоняется так же, как это сделал бы настоящий Xray)
  — проверена только структурная трансляция и happy-path каждой операции.

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

Сборка образа и его запуск проверены вручную заново в этом под-срезе после
того, как `Dockerfile` пришлось обновить: он до сих пор копировал в
build-контекст только `agent_control.proto` и регенерировал стабы вручную
инлайн-вызовом `protoc`, не зная ни про `vendor/xray-proto/`, ни про
`scripts/gen_proto.sh` — с добавлением `xray_client.py`/`docker_manager.py`
это привело бы к падению образа на импорте (`generated.xray.*` просто не
существовал бы). Теперь стадия сборки копирует `agent/scripts` и
`agent/vendor` и зеркалит реальную структуру репозитория (`contracts/proto/`
рядом с `agent/`), так что при сборке выполняется тот же самый
`scripts/gen_proto.sh`, что используется и тестируется локально — не
отдельная, рассинхронизирующаяся копия его логики. Проверено: `docker build`
проходит, контейнер поднимается, `docker_manager.ensure_running()` честно
падает с понятной ошибкой (Docker-сокет не примонтирован в этот контейнер —
осознанно, см. `docker_manager.py`) и это не роняет процесс (см. `main.py`),
control-plane цикл логирует enrollment и переподключается с backoff
(токен замаскирован в логах), как и должен себя вести без реальной Platform
на другом конце. Продакшн-запуск — смонтировать mTLS-материалы и задать
`AGENT_MTLS_*_PATH` вместо insecure-режима, и обеспечить доступ к Docker
Engine API (см. `docker_manager.py`'s "Docker socket access decision") и к
API самого Xray-core (`AGENT_XRAY_API_ADDRESS`). Образ двухстадийный: стадия
сборки регенерирует gRPC-стабы (`agent_control.proto` и `vendor/xray-proto/`)
и собирает wheel, финальный слой — чистый рантайм под непривилегированным
пользователем `agent` (TZ-4 §5.3).

## Scope of this slice / что НЕ реализовано

Этот срез (`agent/grpc-client-skeleton`, под-срез "реальное управление
Xray-core") добавляет к предыдущему control-plane-only под-срезу:

Реализовано:

- Реальный, запускаемый Python 3.12+ проект (src-layout, пакет `dist_vpn_agent`).
- Async gRPC-клиент control-plane: agent dial-out, единственный bidi-stream
  `Session`, `Enrollment` → цикл `Heartbeat` + параллельное чтение
  `PlatformMessage`.
- Обработка `EnrollmentAck` (лог + сохранение `node_id` в памяти процесса),
  `Ping` (лог).
- **`Command` → реальное управление локальным Xray-core** (TZ-4 §3.2) для
  девяти из одиннадцати операций — `xray_client.py` транслирует
  `AddInbound`/`RemoveInbound`, `AddOutbound`/`RemoveOutbound`, `AddUser`/
  `RemoveUser` (через `AlterInbound` + `Add`/`RemoveUserOperation`),
  `AddRoutingRule`/`RemoveRoutingRule`, `OverrideBalancer` в настоящие
  gRPC-вызовы `HandlerService`/`RoutingService`, используя реально
  завендоренные `.proto` (`vendor/xray-proto/`), а не угаданные формы
  сообщений. `control_client.py`'s `_handle_command` диспетчеризует через
  `_COMMAND_DISPATCH` и возвращает настоящий `CommandResult(success=…,
  error_message=…)` — включая настоящие ошибки gRPC от Xray, ни одна не
  проглатывается молча (см. "Mock vs реальный Xray/Docker" выше про то, что
  из этого реально проверено против настоящего Xray-core, а что — только
  против мока).
- **Жизненный цикл Docker-контейнера Xray-core** (TZ-4 §5.4) —
  `docker_manager.py`, через `docker-py`: создаёт/стартует/проверяет
  здоровье контейнера `settings.xray_container_name`, никогда не трогая
  никакой другой контейнер. Решение "Agent работает на хосте напрямую, а не
  монтирует `docker.sock` внутрь себя" явно принято и обосновано в
  докстринге модуля (TZ-4 §5.4 явно требует зафиксировать этот выбор).
  `main.py` вызывает `ensure_running()` при старте процесса, не блокируясь
  на доступности Platform.
- **`Heartbeat.health.xray_container_running` — реальные данные**, а не
  захардкоженный `False`: `control_client.py` спрашивает
  `docker_manager.health()` на каждый heartbeat (в отдельном потоке —
  `docker-py` синхронный, зависший Docker Engine API не должен стопорить
  control-plane цикл). `cpu_percent`/`mem_percent`/`disk_percent`
  по-прежнему заглушки (нужен отдельный выбор способа сэмплинга ресурсов
  хоста, например `psutil` — не сделан в этом срезе).
- mTLS-канал по умолчанию (с осмысленным bootstrap-исключением для самого
  первого enrollment — см. выше), explicit insecure dev-режим только по
  явному флагу, с громким предупреждением.
- `pydantic-settings`-конфигурация, структурированное JSON-логирование без
  утечки секретов в лог (`SecretStr` + `mask_secret()`).
- Реконнект с экспоненциальным backoff вокруг одной сессии (`run_forever`)
  — минимальная версия "переживает кратковременную потерю связи" (TZ-4
  §4/§8). Теперь есть Xray-слой, который можно приводить к желаемому
  состоянию, но полная декларативная реконсиляция после реконнекта (Platform
  пере-отправляет весь желаемый стейт, а не только новые команды) всё ещё не
  реализована — см. TODO ниже.
- Тесты: 38 прошедших локально, два яруса для Xray и для Docker (мок +
  реальная инфраструктура там, где она была доступна — см. "Mock vs
  реальный Xray/Docker" выше), `Dockerfile` (Agent-контейнер, собран и
  прогнан вручную в предыдущем под-срезе), скрипт регенерации стабов
  (теперь генерирует стабы и для `agent_control.proto`, и для
  `vendor/xray-proto/`).

Сознательно НЕ реализовано в этом срезе (TODO для следующего среза, по TZ-4):

- **`RunTest` — тест-запрос через эфемерный клиент** (TZ-4 §3.4). Требует
  поднятия эфемерного Docker-контейнера с тем же образом Xray и параметрами
  конкретного пресета/узла, HTTP-запроса через полученный SOCKS-прокси к
  контрольному URL, и структурированного `TestResult` (успех/код/задержка/
  шаг отказа — не просто ok/fail, см. контракт). Это отдельный кусок работы
  поверх `docker_manager.py` (эфемерные, а не долгоживущие контейнеры) —
  сознательно не в этом срезе, который был про долгоживущий Xray-core.
- **`RotateSecret` — ротация REALITY/TLS-секретов** (TZ-4 §3.3). Двухфазная
  ротация (новый inbound рядом со старым, Platform решает момент удаления
  старого) требует более явного дизайна координации Agent↔Platform поверх
  того, что уже есть в `Command`/`CommandResult` — не просто ещё один вызов
  `xray_client.py`, а решение, как Agent сообщает Platform "новый inbound
  готов, можно переключать" и т.п. Не спроектировано в этом срезе.
- **Персист сертификата из `EnrollmentAck.mtls_client_cert_pem`** и
  последующее использование вместо токена при реконнектах — сейчас каждый
  перезапуск процесса требует валидный (одноразовый!) `enrollment_token`
  заново; `_handle_platform_message` только логирует факт получения
  сертификата. Контракт для этого уже частично готов — см. "Замеченные
  вопросы к контракту" ниже: `Enrollment.csr_pem` появился в контракте
  между разработкой предыдущего и этого под-среза, но Agent пока не
  генерирует локальный keypair/CSR и не заполняет это поле, и не персистит
  ответ на диск. Следующий срез должен закрыть весь путь: генерация
  keypair+CSR → отправка в `Enrollment.csr_pem` → сохранение
  `mtls_client_cert_pem` на диск с правами `600` (TZ-4 §5.3) → использование
  при последующих запусках вместо токена.
- **Backhaul-транспорт Hop B как pluggable-абстракция** (TZ-4 §6) — вне
  периметра и control-plane клиента, и Xray-management слоя этого среза;
  `xray_client.py` умеет добавить любой сконфигурированный inbound/outbound,
  но решение "как именно устроен Hop A↔Hop B backhaul и что в этом
  pluggable" не принято.
- Телеметрия per-user/per-inbound счётчиков (TZ-4 §3.5) — `StatsService.
  QueryStats` уже реально работает (`xray_client.py.query_stats`,
  используется тестами), но никакой `Command` его не вызывает и
  `TelemetryReport` никуда не отправляется — нужен отдельный периодический
  цикл сбора и отправки, не только реактивный ответ на команды.
- Self-diagnostics по испорченному локальному mTLS-сертификату (TZ-4 §7) —
  для недоступности Platform и недоступности/нездоровья Docker диагностика
  уже есть (структурированные ERROR-логи, `DockerManagerError` пойман и
  залогирован, а не тихо проглочен, heartbeat честно репортит нездоровье);
  для сертификата пока нечего диагностировать, т.к. персиста/ротации
  сертификата ещё нет (см. пункт выше).
- Сэмплинг реальных `cpu_percent`/`mem_percent`/`disk_percent` для
  `Heartbeat.health` (сейчас `0.0` — заглушка) — `xray_container_running`
  теперь реальный, но эти три поля ещё нет; нужен выбор способа сэмплинга
  (скорее всего `psutil`, не добавлен как зависимость в этом срезе).

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

**Обновление относительно предыдущего под-среза**: пробел, описанный выше
(CSR/публичный ключ отсутствовали в `Enrollment`), с тех пор закрыт на
уровне контракта — веткой `main` (не этой веткой) в коммите
`fix(contracts): add CSR field to Enrollment for cert-key binding`.
`Enrollment` теперь содержит `bytes csr_pem = 5` (PKCS#10 CSR, PEM), а
комментарий на `EnrollmentAck.mtls_client_cert_pem` явно ссылается на него
("подписан из `Enrollment.csr_pem`"). Этот срез слил `main` в
`agent/grpc-client-skeleton` (`git merge main`, затронуты только
`contracts/` — `agent/` этим слиянием не тронут), чтобы не остаться
позади факта: расхождение найдено сравнением с `main` напрямую
(`git log main -- contracts/...`), а не только историей этой ветки.

Поле в контракте теперь есть, но **Agent пока не использует его** — это
осталось в TODO этого среза (см. "Scope of this slice" выше, пункт про
персист сертификата): нужно (1) сгенерировать локальный keypair и CSR, (2)
заполнить `Enrollment.csr_pem` вместо оставления его пустым, (3) сохранить
полученный `EnrollmentAck.mtls_client_cert_pem` на диск с правами `600`
(TZ-4 §5.3), (4) использовать сохранённый сертификат вместо токена при
последующих запусках/реконнектах. Ничего из этого не реализовано здесь —
`_build_enrollment_message` в `control_client.py` по-прежнему не
устанавливает `csr_pem`, и `_handle_platform_message` по-прежнему только
логирует факт получения сертификата, не персистя его. Второй вариант
решения, упомянутый выше (Platform генерирует keypair и отдаёт приватный
ключ агенту), явно отклонён самим фактом появления `csr_pem` в контракте —
принят вариант 1 (агент генерирует keypair/CSR локально), так что он больше
не рассматривается как открытый вопрос.

До тех пор, пока следующий срез не реализует пункты (1)-(4) выше, в этом
срезе действует только явный fallback (см. "mTLS и bootstrap-нюанс" выше):
самый первый enrollment идёт по TLS с проверкой только серверного
сертификата, а `EnrollmentAck.mtls_client_cert_pem`, когда он приходит,
только логируется (без персиста и без попытки построить из него рабочую
клиентскую identity) — см. "Scope of this slice" выше.
