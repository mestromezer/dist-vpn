# Agent

Полное ТЗ: [../docs/TZ-4-agent.md](../docs/TZ-4-agent.md).
Контракт: [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto) (не редактируется здесь — см. [../contracts/README.md](../contracts/README.md)).

Кратко: Python 3.12+, control-plane gRPC-клиент к Platform по контракту
`agent_control.proto` (agent dial-out, mTLS с реальным локальным CSR/
keypair и персистом выданного сертификата — см. "mTLS-идентичность" ниже),
нативные gRPC-стабы к самому Xray-core (`HandlerService`/`RoutingService`/
`StatsService`, из реально завендоренных `.proto` — см. `vendor/xray-proto/`)
для девяти из одиннадцати `Command`-операций плюс `RunTest` (реальный
эфемерный тест-клиент подключения — см. "RunTest" ниже), и `docker-py` для
управления и локальным долгоживущим Xray-core-контейнером, и эфемерными
тест-контейнерами. Только `RotateSecret` остаётся нереализованным (см.
"Scope of this slice" ниже).

## Текущий срез: `agent/grpc-client-skeleton`

Предыдущий под-срез реализовал только control-plane канал Agent↔Platform
(`AgentControl.Session`) с "not implemented"-заглушкой на каждую `Command`.
Под-срез после него добавил реальное управление локальным Xray-core
(`xray_client.py`, `docker_manager.py`). **Этот под-срез** закрывает два
оставшихся куска TZ-4, кроме `RotateSecret`:

1. **mTLS-идентичность**: генерация локального EC/P-256 keypair + PKCS#10
   CSR при bootstrap-enrollment (`identity.py`), персист выданного
   Platform'ом сертификата на диск, и переиспользование этой identity при
   переподключениях вместо повторного enrollment по токену — см.
   "mTLS-идентичность: CSR при bootstrap, персист identity" ниже. Начато в
   предыдущей (прерванной инфраструктурной ошибкой, не связанной с кодом)
   сессии как `identity.py` — модуль был уже полным, рабочим куском (CSR-
   генерация + персист + чтение персиста), этот срез не переписывал его, а
   подключил (`control_client.py`) и покрыл тестами.
2. **`RunTest`**: реальное поднятие эфемерного тест-клиента Xray-core в
   Docker-контейнере, HTTP-проба через его SOCKS5-прокси, разбор результата
   в один из пяти `TestOutcome` — см. "RunTest: эфемерный тест-клиент
   подключения" ниже.

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
    identity.py               локальный EC/P-256 keypair + CSR (bootstrap), персист/чтение выданной identity
    connectivity_test.py      RunTest: эфемерный тест-Xray-контейнер + HTTP-проба через SOCKS5 + outcome-mapping
    control_client.py        gRPC-клиент control-plane: enrollment/reconnect-с-identity, диспатч Command -> xray_client.py/connectivity_test.py
    xray_client.py            gRPC-клиент к локальному Xray-core (HandlerService/RoutingService/StatsService)
    docker_manager.py         жизненный цикл долгоживущего Docker-контейнера Xray-core (docker-py)
    main.py                  точка входа процесса (console-script `dist-vpn-agent`)
    generated/                сгенерированные protobuf/gRPC стабы (agent_control + xray/) — НЕ коммитится (см. .gitignore)
  tests/
    mock_platform_server.py  мок AgentControl-сервера для тестов; теперь реально подписывает CSR (throwaway test-CA)
    mock_xray_server.py       мок HandlerService/RoutingService/StatsService для тестов
    test_control_client.py   end-to-end: enrollment/reconnect -> heartbeat -> ping -> real command/run_test dispatch
    test_identity.py          юнит-тесты identity.py: реальная CSR-генерация, персист/чтение, all-or-nothing
    test_connectivity_test.py RunTest: юнит-тесты (без Docker) + пять исходов против настоящего Xray-core-контейнера
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
| `AGENT_STATE_DIR` | `/var/lib/dist-vpn-agent` | Каталог для персиста локальной identity (`identity.py`: клиентский ключ+сертификат+node_id) — см. "mTLS-идентичность" ниже. Должен быть доступен на запись процессу Agent'а; создаётся сам при первом использовании |
| `AGENT_MTLS_CA_CERT_PATH` | *(нет)* | Путь к CA, которым подписан серверный сертификат Platform. Обязателен, если не включён `insecure_dev_mode` |
| `AGENT_MTLS_CLIENT_CERT_PATH` | *(нет)* | Путь к клиентскому mTLS-сертификату ноды, выданному **не** через CSR-flow этого Agent'а (полностью внешняя, out-of-band identity). Опционален и используется только когда в `AGENT_STATE_DIR` ещё нет персистнутой identity — см. "mTLS-идентичность" ниже |
| `AGENT_MTLS_CLIENT_KEY_PATH` | *(нет)* | Путь к приватному ключу клиента для той же out-of-band identity. Должен быть задан вместе с `..._CERT_PATH` (либо оба, либо ни одного) |
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
- Клиентская identity выбирается в таком порядке (см. следующий раздел для
  полного дизайна): (1) персистнутая в `AGENT_STATE_DIR` identity, если она
  там уже есть; иначе (2) `AGENT_MTLS_CLIENT_CERT_PATH`/`_KEY_PATH`, если
  заданы (out-of-band identity, вне CSR-flow); иначе (3) обычный TLS с
  проверкой только сервера, без клиентского сертификата — bootstrap-
  enrollment по токену с локально сгенерированным CSR (см. ниже).
  `AGENT_MTLS_CLIENT_CERT_PATH`/`_KEY_PATH` можно задавать либо обе сразу,
  либо ни одной — конфигурация с одной из двух отклоняется на старте с
  понятной ошибкой.

### Небезопасный dev-режим

`AGENT_INSECURE_DEV_MODE=true` открывает нешифрованный, неаутентифицированный
канал к Platform. Это единственный способ обойти обязательный TLS/mTLS, он
явно и громко логируется (`WARNING`) при каждом использовании
(`control_client.py`, `_build_channel`) и никогда не является поведением по
умолчанию (валидация конфигурации падает без mTLS-путей, если этот флаг не
установлен явно). Используется только для локальной разработки и в тестах
(в т.ч. в `tests/`) — никогда в production (TZ-4 §5.2, §5.5 категория A02).

## mTLS-идентичность: CSR при bootstrap, персист identity

**Этот раздел — то, что должна прочитать sibling-сессия на Platform-треке,
реализующая CA/подпись сертификатов по тому же контракту: она не видит код
этого Agent'а, только этот файл и `agent_control.proto`.**

### Что уже было (`identity.py`)

`identity.py` был начат в прерванной предыдущей сессии этого же среза и
оказался полным, рабочим куском — этот срез не переписывал его, только
подключил к `control_client.py` и добавил тесты (`tests/test_identity.py`).
Модуль целиком реализует:

- `generate_csr(common_name=...)` — генерирует EC/P-256 keypair (через
  `cryptography`) и PKCS#10 CSR, подписанный этим ключом (Subject
  CN = hostname ноды). Приватный ключ возвращается только в памяти
  (`GeneratedCsr.private_key_pem`, PKCS8, без шифрования) — никогда не
  пишется на диск, пока не придёт подписанный сертификат.
- `persist_identity(state_dir, node_id, private_key_pem, cert_pem)` —
  пишет три файла в `state_dir` (создаёт каталог при необходимости):
  `client.key.pem` (права `0600` — секрет), `client.cert.pem` (права
  `0644` — не секрет), `node_id` (права `0644`, простой текст). Права
  реально применяются только на POSIX (`Path.chmod` — no-op на Windows,
  см. докстринг модуля) — целевая среда развёртывания Agent'а Linux-
  контейнер (TZ-4 §5.3), так что для прода это работает.
- `load_persisted_identity(state_dir)` — читает те же три файла, all-or-
  nothing: если не хватает хотя бы одного файла или `node_id` пустой,
  возвращает `None` (трактуется как "identity ещё нет", а не ошибка) —
  безопасный fallback на bootstrap-enrollment в любом неполном состоянии.

### Как это подключено (`control_client.py`)

1. **Bootstrap** (`load_persisted_identity` вернул `None`): перед отправкой
   `Enrollment` вызывается `identity.generate_csr(common_name=hostname)`.
   Приватный ключ остаётся в памяти на `ControlClient` (`self._generated_csr`)
   до ответа. CSR PEM кладётся в уже существующее в контракте поле
   `Enrollment.csr_pem`. Канал в этот момент — обычная TLS с проверкой
   только серверного сертификата Platform (токен — application-layer
   credential, см. предыдущий раздел).
2. **Получен `EnrollmentAck` с непустым `mtls_client_cert_pem`**: вызывается
   `identity.persist_identity(...)` с ровно тем приватным ключом, что был
   сгенерирован на шаге 1, и байтами сертификата из ответа — пишутся все
   три файла в `AGENT_STATE_DIR`. Если `_generated_csr` неожиданно пуст
   (не должно случаться при нормальном протоколе — `EnrollmentAck` всегда
   следует за отправленным `Enrollment`), персист пропускается с warning в
   лог, а не падает: без совпадающего приватного ключа персистить
   сертификат бессмысленно и небезопасно.
3. **Каждый вызов `run_session()`** (и первый старт процесса, и
   реконнект после обрыва стрима через `run_forever()`) сначала проверяет
   `identity.load_persisted_identity(state_dir)`. Если результат не `None`:
   - `self.node_id` выставляется немедленно из персистнутого файла —
     Platform не нужно заново спрашивать, кто это.
   - Канал строится с персистнутыми `cert_path`/`key_path` как клиентской
     mTLS-identity (**не** `AGENT_MTLS_CLIENT_CERT_PATH`/`_KEY_PATH` —
     персистнутая identity всегда имеет приоритет над этими двумя
     операторскими путями, которые остаются только для identity,
     выданной полностью вне этого CSR-flow).
   - **`Enrollment` в этой сессии не отправляется вообще.** Вместо этого
     первым сообщением в поток кладётся `Heartbeat` (с уже известным
     `node_id`) — см. следующий подраздел, это и есть сигнал реконнекта.

### Сигнал "уже есть identity" (design note для Platform-стороны)

Контракт (`agent_control.proto`) не получил новых полей для этого — сигнал
чисто структурный: **какое сообщение открывает стрим**.

- Стрим начинается с `AgentMessage.enrollment` → это bootstrap или
  осознанный повторный enrollment (у Agent'а нет валидной персистнутой
  identity, ожидается `enrollment_token`).
- Стрим начинается с `AgentMessage.heartbeat` (никакого `enrollment` в этой
  сессии вообще не было) → это реконнект уже известной, ранее
  заенроллленной ноды. `Heartbeat.node_id` говорит Platform, какая именно
  это нода.

Настоящий якорь доверия здесь — не `Heartbeat.node_id` сам по себе, а сам
факт, что канал вообще смог подняться как mTLS с клиентским сертификатом,
подписанным CA Platform: `grpc.ssl_channel_credentials`/сервер Platform
проверяют клиентский сертификат на уровне транспорта раньше, чем Agent
успевает отправить хоть один `AgentMessage`. **Рекомендация для реализации
Platform-стороны**: `Session`-хендлер должен сверять `Heartbeat.node_id`
(и вообще любой node_id, заявленный по установленному mTLS-соединению) с
тем node_id, который Platform сама связала с этим клиентским сертификатом
при его выдаче — не доверять полю `node_id` вслепую просто потому, что оно
пришло по TLS-каналу. Это тот же принцип, которому Platform обязана
следовать для *любого* сообщения на уже установленном mTLS-соединении, не
специфика именно этого reconnect-сценария.

### Что не проверено (честно)

Реальное взаимодействие с настоящим Platform CA/подписывающим кодом
непроверяемо в этой песочнице — это независимая реализация sibling-сессии,
недоступная здесь. Что реально проверено (`tests/test_identity.py`,
`tests/test_control_client.py`):

- `identity.generate_csr` создаёт настоящий EC/P-256 keypair и CSR,
  реально проходящий self-signature verification (`cryptography`'s
  `csr.is_signature_valid`) — не заглушка.
- `tests/mock_platform_server.py` теперь реально подписывает полученный
  CSR настоящим (одноразовым, тестовым) CA через `cryptography`
  (`x509.CertificateBuilder`, EC/P-256, `not_valid_after` = 90 дней —
  то же значение валидности, что было озвучено для настоящего Platform CA
  в контексте этой задачи, но это никак не гарантирует, что настоящий
  Platform CA сделает то же самое — это только для правдоподобия мок-теста).
  Персистнутый на диск сертификат в тесте — настоящий, парсящийся X.509 с
  тем же public key, что и у персистнутого приватного ключа (проверено
  `cryptography.x509.load_pem_x509_certificate`).
- Полный цикл реконнекта проверен по-настоящему: первая сессия
  bootstrap-enrolls и персистит identity на диск; вторая сессия — **новый**
  `ControlClient` и **новый** экземпляр мок-сервера (имитация реального
  перезапуска процесса, не просто повторный вызов на том же объекте),
  читающий тот же `state_dir` — подтверждено, что `Enrollment` в этой
  сессии не отправляется вовсе, `Heartbeat` уходит первым с правильным
  `node_id`, и обычный `Command`-раунд-трип на этой reconnect-сессии всё
  ещё работает.
- Отдельный юнит-тест (`test_build_channel_prefers_persisted_identity_over_configured_paths`)
  проверяет именно приоритет выбора credentials (персистнутая identity
  побеждает `AGENT_MTLS_CLIENT_CERT_PATH`/`_KEY_PATH`, даже если обе
  заданы) — через перехват аргументов `grpc.ssl_channel_credentials`
  (единственный способ проверить это без настоящего TLS-хендшейка, т.к.
  этот объект непрозрачен и не даёт geттеров).
- **Не проверено**: настоящий TLS/mTLS-хендшейк с этими сертификатами
  (все тесты выше используют `insecure_dev_mode=True` для транспорта,
  как и остальные тесты этого проекта, — см. "Тесты" ниже) и, самое
  главное, совместимость с настоящим Platform CA/его конкретным выбором
  алгоритма подписи, срока действия, набора extensions в сертификате и
  т.п. — эта проверка возможна только на интеграционном этапе, когда обе
  стороны (Agent и Platform) будут разворачиваться вместе.

## RunTest: эфемерный тест-клиент подключения

TZ-4 §3.4 (требование пользователя №2 — проверка канала). По команде
`Command.run_test` (`RunTest{test_id, preset_client_config_json, probe_url,
timeout_seconds}`) Agent теперь реально:

1. Поднимает эфемерный Docker-контейнер того же образа Xray-core
   (`settings.xray_docker_image`), с уникальным именем на каждый запуск,
   `remove=True` (auto-remove) + явный `remove(force=True)` в `finally`
   как подстраховка — контейнер никогда не переживает один вызов.
2. Через его SOCKS5-прокси (`requests` + PySocks, `socks5h://` — DNS тоже
   резолвится через туннель, не локально) делает HTTP GET на `probe_url` с
   таймаутом `timeout_seconds`.
3. Возвращает `TestResult{test_id, outcome, latency_ms, detail}` — **не**
   `CommandResult`, см. design note ниже.
4. Контейнер снимается всегда, успех или неудача (`connectivity_test.py`,
   `ConnectivityTester._teardown`).

### `preset_client_config_json`: интерпретация (design decision, не факт контракта)

Комментарий в `agent_control.proto` на этом поле — только "тестовый
клиентский конфиг, см. TZ-4 §3.4"; сам TZ-4 §3.4 говорит "с параметрами
конкретного пресета/узла". Реализовано так: это **один Xray outbound**
(в нативном static-config JSON-диалекте Xray, `{"protocol":...,
"settings":{...},"streamSettings":{...}}` — тот же формат ключей, что и у
`AddOutbound.outbound_json` в `xray_client.py`, для единообразия, хотя
здесь это идёт в файл конфигурации для `xray run`, а не в protobuf RPC).
Agent сам оборачивает этот outbound локальным SOCKS5 inbound'ом и одним
routing-правилом "всё из SOCKS5 в этот outbound" — см. `connectivity_test.py`
module docstring для полного обоснования (в т.ч. почему альтернативная
трактовка — "это весь файл конфига целиком" — была рассмотрена и отклонена).
**Открытый вопрос для согласования с Platform-стороной**, как и с
`csr_pem` в прошлом под-срезе.

### Design note: `TestResult`, а не `CommandResult`, для `run_test`

Контракт определяет `TestResult` как отдельный вариант `AgentMessage.payload`
(не вложенный в `CommandResult`) с собственным `test_id` (отдельным от
`Command.command_id`). Реализовано так: `Command` с `op = run_test`
получает **только** `AgentMessage.test_result`, никогда
`AgentMessage.command_result` для этого `command_id`. **Platform-сторона не
должна ждать `CommandResult` для команд с `run_test`** — она никогда не
придёт. Полное обоснование — докстринг `ControlClient._handle_run_test` в
`control_client.py`.

### Разбор исхода в `TestOutcome` — что точно, что best-effort

- `TEST_OUTCOME_SUCCESS` / `TEST_OUTCOME_CONTAINER_START_FAILED` /
  `TEST_OUTCOME_TIMEOUT` — точные: старт контейнера не удался, если Docker
  не сообщил `running` с опубликованным портом в течение ограниченного
  времени (не входит в бюджет `timeout_seconds` — TZ-4 §4 явно говорит, что
  бюджет теста — "сверх времени на поднятие эфемерного контейнера"); таймаут
  — это ровно `requests.exceptions.Timeout` (проба реально не уложилась в
  `timeout_seconds`, независимо от точной причины).
- `TEST_OUTCOME_TLS_HANDSHAKE_FAILED` — **явно best-effort**. У SOCKS5 нет
  кода ошибки "TLS handshake failed" (RFC 1928 §6 — только общие
  failure/refused/unreachable). Реально удалось различить это только через
  grep реальных логов эфемерного Xray-контейнера (`_looks_like_tls_failure`
  ищет `"tls:"`, `"reality"`, `"certificate"`, `"x509"`, `"handshake"`) —
  подтверждено на настоящем Xray-core: vless+tls outbound, направленный на
  чистый HTTP-эндпоинт, реально даёт в логе `"tls: first record does not
  look like a TLS handshake"`. **Важный нюанс, найденный только реальным
  тестированием**: даже `requests.exceptions.SSLError` — не надёжный
  самостоятельный сигнал! SOCKS-туннель может оборваться в середине TLS-
  хендшейка `requests` совсем по другой причине (например, `freedom`
  outbound с `redirect` на отказанный адрес — Xray сначала принимает SOCKS
  CONNECT, потом обнаруживает, что его *собственный* outbound отказан, и
  рвёт туннель), и это тоже всплывает как `SSLError(SSLEOFError(...))`, хотя
  к TLS/сертификатам никакого отношения не имеет. Поэтому `SSLError`
  разбирается тем же best-effort log-grep'ом, что и любой другой
  `ConnectionError`, а не получает автоматический "проход" — см. подробный
  комментарий в `connectivity_test.py::_probe_through_socks`. Известный,
  принятый (не решённый в этом срезе) пробел: настоящий отказ TLS-
  сертификата именно `probe_url` (не туннеля) на non-TLS outbound (напр.
  `freedom`) не оставит TLS-related текста в логах Xray (Xray просто
  ретранслирует байты, ни о чём не зная) — будет классифицирован как
  `PROXY_NO_RESPONSE`, не `TLS_HANDSHAKE_FAILED`. Более узкий, но тоже
  реально найденный нюанс: `log.loglevel` в эфемерном тест-конфиге —
  `"info"`, не `"warning"` (в отличие от того, что было бы разумно для
  настоящего клиентского пресета) — без этого именно та строка лога, от
  которой зависит классификатор, тихо не пишется вообще, и любой реальный
  handshake-фейл вырождался в `PROXY_NO_RESPONSE`. `log.access: "none"`
  при этом остаётся безусловным (TZ-4 §3.5) — обоснование этого
  осознанного, узкого исключения именно для эфемерного диагностического
  контейнера см. в докстринге `_build_ephemeral_config`.
- `TEST_OUTCOME_PROXY_NO_RESPONSE` — остаточный бакет: быстрый,
  не-таймаутный `ConnectionError`, не распознанный как TLS-related.

### Что реально проверено, что замокано (`tests/test_connectivity_test.py`)

Реальный Docker в этой песочнице был доступен (образ `ghcr.io/xtls/
xray-core:latest` уже был локально) — **все пять** `TestOutcome` проверены
против настоящего Docker + настоящего Xray-core-процесса, ни один не
симулирован моком `requests`/docker-py:

- `SUCCESS` — freedom outbound, реальный HTTP GET на `https://example.com`
  через настоящий SOCKS5.
- `CONTAINER_START_FAILED` — пресет с несуществующим протоколом, реальный
  контейнер реально падает почти сразу (в разработке этого среза —
  exit code 23, `"unknown config id"` в логе Xray).
- `TLS_HANDSHAKE_FAILED` — vless+tls outbound на чистый HTTP-эндпоинт (см.
  выше).
- `PROXY_NO_RESPONSE` — freedom outbound с `redirect` на отказанный
  loopback-порт (`127.0.0.1:1`) — быстрый реальный `ConnectionError`,
  без TLS-related текста в логах.
- `TIMEOUT` — freedom outbound, проба на non-routable-адрес
  (`10.255.255.1`) с коротким `timeout_seconds`.

Плюс юнит-тесты без Docker вообще: `_looks_like_tls_failure` на реальных
строках лога, и невалидный/неполный `preset_client_config_json` (никогда
не доходит до Docker).

**Честная оговорка про сеть**: тесты `SUCCESS`/`TIMEOUT`/
`PROXY_NO_RESPONSE`/`TLS_HANDSHAKE_FAILED` дёргают настоящие внешние хосты
(`example.com`, `neverssl.com`, `10.255.255.1`) и реально требуют
работающего исходящего интернета из среды, где гоняются тесты. В отличие
от недоступности Docker (чисто и понятно скипается), недоступность/
деградация именно интернета в момент прогона обнаружена не заранее, а как
настоящий, наблюдаемый в разработке этого среза flaky-фактор — при
интенсивном последовательном прогоне (несколько контейнеров подряд бьют в
один и тот же реальный хост) один из пяти изредка падает по таймауту/
отказу на уровне самой сети, а не по вине кода (перезапуск теста в
изоляции или чуть погодя проходит). Не решено попыткой retry/mock в тесте
— это исказило бы то, что тест реально проверяет; если это станет проблемой
в CI, естественное решение — direct outbound egress с других хостов или
собственный контролируемый HTTP-эхо-сервис вместо публичных `example.com`/
`neverssl.com`, не сделано в этом срезе.

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

Все тесты пройдены локально (`63 passed, 1 skipped` — единственный skip —
Unix-only проверка прав `0600`/`0644` на `identity.py`'s файлах, см.
`tests/test_identity.py`, реально no-op-нится на Windows; Python 3.13.14 в
venv на базе `py -3.13`). Что покрыто, и — важно — **против чего именно**
(мок или реальная инфраструктура, см. "Mock vs реальный Xray/Docker — что
реально проверено" ниже, и "RunTest"'s собственную секцию про то же для
`connectivity_test.py`):

- `tests/test_control_client.py` — поднимает лёгкий in-process мок
  `AgentControl`-сервера (`tests/mock_platform_server.py`, теперь с реальной
  CSR-подписью через throwaway test-CA) на сгенерированных стабах и гоняет
  `ControlClient` по нему через `pytest-asyncio`:
  - enrollment → heartbeat → ping (полный цикл; `node_id` получен от сервера
    и используется в последующих heartbeat);
  - heartbeat отражает реальный (пусть и через фейковый `DockerManager` в
    тестах) статус `xray_container_running` — раньше было захардкожено
    `False`;
  - `Command` (`add_user`, `add_inbound`) действительно диспатчится в
    `xray_client.py` (мок-Xray, см. ниже) и возвращает реальный
    `CommandResult` — как успешный, так и с настоящей ошибкой от Xray/
    трансляции, не проглоченной;
  - `run_test` реально диспатчится в (фейковый в этих тестах, см.
    `tests/test_connectivity_test.py` для реального Docker-яруса)
    `ConnectivityTester` и возвращает `TestResult`, **не**
    `CommandResult` — включая последний рубеж на неожиданное исключение;
  - bootstrap-enrollment реально генерирует CSR, персистит выданный (реально
    подписанный мок-CA) сертификат на диск; второй, отдельный `ControlClient`
    против **нового** экземпляра мок-сервера (имитация перезапуска процесса)
    переиспользует персистнутую identity: `Enrollment` не отправляется,
    первым сообщением уходит `Heartbeat` с уже известным `node_id`, обычный
    `Command`-раунд-трип на этой сессии всё ещё работает;
  - отдельный юнит-тест на приоритет выбора credentials в `_build_channel`
    (персистнутая identity против `AGENT_MTLS_CLIENT_CERT_PATH`/`_KEY_PATH`);
  - сбой персиста identity на диск (папка состояния занята файлом) после
    **успешного** enrollment не роняет сессию — heartbeat всё равно уходит
    с правильным `node_id` (см. фикс + design note в "Scope of this slice");
  - отказ enrollment (`accepted=false`) поднимает `EnrollmentRejected`.
- `tests/test_identity.py` — юнит-тесты `identity.py` без gRPC вообще:
  реальная EC/P-256 CSR-генерация (self-signature проверяется через
  `cryptography`), персист/чтение (round-trip, all-or-nothing на неполном
  состоянии, перезапись), права доступа на POSIX (скип на Windows).
- `tests/test_connectivity_test.py` — `RunTest`/`connectivity_test.py`, см.
  "RunTest" выше для полного разбора: юнит-тесты без Docker
  (`_looks_like_tls_failure`, невалидный `preset_client_config_json`) плюс
  все пять `TestOutcome` против настоящего Docker + настоящего Xray-core
  (пропускаются с понятной причиной при недоступном Docker; **не**
  пропускаются, а падают по-настоящему при недоступном исходящем интернете —
  честная оговорка в той же секции).
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

Этот срез (`agent/grpc-client-skeleton`) состоит из трёх под-срезов:
control-plane-only (самый первый), затем реальное управление Xray-core
(`xray_client.py`/`docker_manager.py`), и **этот, последний под-срез** —
mTLS-идентичность (CSR + персист, см. выше) и `RunTest` (эфемерный
тест-клиент, см. выше).

Реализовано в этом под-срезе (кроме перечисленного в предыдущих под-срезах
ниже):

- **CSR при bootstrap + персист identity + reconnect без токена** — см.
  "mTLS-идентичность" выше для полного дизайна. Закрывает TODO предыдущего
  под-среза целиком (генерация keypair+CSR → отправка в
  `Enrollment.csr_pem` → сохранение `mtls_client_cert_pem` на диск с правами
  `600` → использование при последующих запусках вместо токена — все четыре
  пункта сделаны).
- **`RunTest`** — см. "RunTest" выше. Закрывает соответствующий TODO
  предыдущего под-среза целиком: эфемерный контейнер, HTTP-проба через
  SOCKS5, структурированный `TestResult` с разбором по шагу отказа (все пять
  `TestOutcome`, не просто ok/fail).
- Два found-and-fixed попутных гэпа (не отдельные TODO, потому что оба
  закрыты в этом же срезе, а не просто обнаружены): (1) `Dockerfile` не
  провижинил `AGENT_STATE_DIR` для непривилегированного пользователя
  `agent` — первый успешный enrollment не смог бы персистить выданную
  identity на реальном образе; (2) сбой персиста identity на диск (после
  уже успешного, серверно принятого enrollment) не должен ронять всю
  сессию — теперь перехватывается и логируется, не проглатывается и не
  фатален.

Реализовано в предыдущих под-срезах (для контекста, без изменений в этом):

- Реальный, запускаемый Python 3.12+ проект (src-layout, пакет `dist_vpn_agent`).
- Async gRPC-клиент control-plane: agent dial-out, единственный bidi-stream
  `Session`, `Enrollment` → цикл `Heartbeat` + параллельное чтение
  `PlatformMessage`.
- Обработка `EnrollmentAck` (лог; `node_id` теперь и в памяти процесса, и
  персистится на диск вместе с ключом/сертификатом — см. "mTLS-идентичность"
  выше, это уже не то же самое "только в памяти", что было на момент, когда
  этот пункт впервые был написан), `Ping` (лог).
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
- Тесты (на момент завершения предыдущего под-среза: 38 прошедших локально;
  этот под-срез добавил идентичность/CSR/reconnect- и `RunTest`-тесты
  сверху, см. "Тесты" выше для актуального числа), два яруса для Xray и для
  Docker (мок + реальная инфраструктура там, где она была доступна — см.
  "Mock vs реальный Xray/Docker" выше), `Dockerfile` (Agent-контейнер,
  собран и прогнан вручную в предыдущем под-срезе — этот под-срез добавил
  `state_dir`-провижининг, см. выше), скрипт регенерации стабов (теперь
  генерирует стабы и для `agent_control.proto`, и для `vendor/xray-proto/`).

Сознательно НЕ реализовано (TODO для следующего среза, по TZ-4) — и **что
из этого списка появилось или изменилось именно в этом под-срезе**:

- **`RotateSecret` — ротация REALITY/TLS-секретов и сертификатов** (TZ-4
  §3.3). Всё ещё не спроектировано. Двухфазная ротация клиентских пресетов
  (`ROTATION_KIND_HOPA_PRESET`/`ROTATION_KIND_HOPB_KEY`, новый inbound рядом
  со старым, Platform решает момент удаления старого) требует более явного
  дизайна координации Agent↔Platform поверх того, что уже есть в
  `Command`/`CommandResult` — не просто ещё один вызов `xray_client.py`, а
  решение, как Agent сообщает Platform "новый inbound готов, можно
  переключать" и т.п. **Новое в этом под-срезе**: `ROTATION_KIND_P4_CERT`
  (сигнал "обнови сертификат", хот-релоад делает сам Xray) теперь пересекается
  с mTLS-identity этого Agent'а не напрямую (`ROTATION_KIND_P4_CERT` — про
  сертификат P4-пресета для конечных пользователей, не про mTLS-сертификат
  самого Agent'а к Platform), но следующему срезу стоит явно решить, нужна
  ли **отдельная** ротация именно mTLS client-сертификата Agent'а (истечёт
  ли выданный Platform'ом сертификат когда-нибудь, и как Agent должен на
  это реагировать) — см. следующий пункт.
- **Истечение/отзыв персистнутой mTLS-identity — не обрабатывается**
  (найдено при реализации CSR/персиста в этом под-срезе, не было
  актуально до него, т.к. персиста не было вообще). `identity.py`
  персистит сертификат, но ничего не проверяет его срок действия при
  чтении (`load_persisted_identity`) — просроченный или отозванный
  Platform'ом сертификат сейчас обнаружится только когда реальный mTLS-
  хендшейк к Platform реально провалится (генерическая ошибка,
  подхватывается `run_forever`'s backoff-циклом), причём **с тем же
  просроченным сертификатом на каждой попытке** — нет логики "если
  reconnect с персистнутой identity стабильно проваливается, удалить
  персист и упасть обратно на bootstrap-enrollment токеном". Это и есть
  TZ-4 §7's "self-diagnostics по испорченному локальному mTLS-сертификату"
  — раньше в этом README числилось как "нечего диагностировать, персиста
  ещё нет"; теперь персист есть, а диагностики/восстановления всё ещё нет
  — статус пункта не "не начат", а "стал actionable и не сделан".
- **Backhaul-транспорт Hop B как pluggable-абстракция** (TZ-4 §6) — без
  изменений в этом под-срезе; вне периметра и control-plane клиента, и
  Xray-management слоя. `xray_client.py` умеет добавить любой
  сконфигурированный inbound/outbound, но решение "как именно устроен
  Hop A↔Hop B backhaul и что в этом pluggable" не принято.
- Телеметрия per-user/per-inbound счётчиков (TZ-4 §3.5) — без изменений в
  этом под-срезе; `StatsService.QueryStats` уже реально работает
  (`xray_client.py.query_stats`, используется тестами), но никакой
  `Command` его не вызывает и `TelemetryReport` никуда не отправляется —
  нужен отдельный периодический цикл сбора и отправки, не только
  реактивный ответ на команды.
- Сэмплинг реальных `cpu_percent`/`mem_percent`/`disk_percent` для
  `Heartbeat.health` (сейчас `0.0` — заглушка) — без изменений в этом
  под-срезе; нужен выбор способа сэмплинга (скорее всего `psutil`, не
  добавлен как зависимость).
- **`RunTest`'s `TLS_HANDSHAKE_FAILED`-эвристика имеет известный пробел**
  (найдено в этом под-срезе реальным тестированием, не гипотетически): на
  non-TLS outbound'е (например `freedom`) настоящий отказ TLS-сертификата
  именно `probe_url` (не туннеля) классифицируется как `PROXY_NO_RESPONSE`,
  не `TLS_HANDSHAKE_FAILED`, потому что Xray в этом случае просто
  ретранслирует байты и ничего TLS-related не логирует — см. "RunTest"
  выше для полного разбора. Не решено; вариант для следующего среза —
  различать эту ситуацию через сам факт, что security outbound'а — не
  `"tls"`/`"reality"` (доступно этому Agent'у из `preset_client_config_json`
  до старта контейнера, не только постфактум через grep логов).
- **`RunTest`'s тесты против реальной инфраструктуры зависят от реального
  исходящего интернета** (`example.com`/`neverssl.com`/`10.255.255.1`) —
  найдено флаки при интенсивном последовательном прогоне в этом под-срезе
  (см. "RunTest" выше). Не решено retry/mock'ом (исказило бы, что реально
  проверяется) — естественное решение (собственный контролируемый
  HTTP-эхо-сервис вместо публичных хостов) не сделано в этом срезе.
- Полная декларативная реконсиляция после реконнекта (Platform
  пере-отправляет весь желаемый Xray-стейт целиком, а не только новые
  команды с момента разрыва) — без изменений в этом под-срезе, всё ещё не
  реализована, `run_forever`'s backoff остаётся только транспортным
  восстановлением связи.

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

**Обновление в этом под-срезе: закрыто полностью, Agent-сторона.** Все
четыре пункта, оставшиеся TODO с прошлого раза, сделаны: (1) локальный
keypair+CSR генерируется (`identity.generate_csr`), (2) `Enrollment.csr_pem`
реально заполняется (`_build_enrollment_message`), (3)
`EnrollmentAck.mtls_client_cert_pem` персистится на диск с правами `600`
(`identity.persist_identity`, `_handle_platform_message`), (4) персистнутая
identity используется вместо токена на последующих запусках/реконнектах
(`run_session`, "Heartbeat-first reconnect" — см. "mTLS-идентичность"
выше для полного дизайна, специально написанного так, чтобы Platform-
сторона могла свериться с ним, не видя код этого Agent'а). Второй вариант
решения, упомянутый выше (Platform генерирует keypair и отдаёт приватный
ключ агенту), остаётся отклонённым.

**Новые открытые вопросы к контракту, найденные в этом под-срезе** (не
реализовано в контракте, требуют согласования через PR, если/когда
Platform-сторона будет реализовывать свою часть `RunTest`):

1. **`RunTest.preset_client_config_json` — форма не зафиксирована
   контрактом.** Комментарий в `.proto` — только "тестовый клиентский
   конфиг, см. TZ-4 §3.4"; TZ-4 §3.4 говорит "с параметрами конкретного
   пресета/узла". Agent реализовал это как один Xray outbound-объект
   (нативный static-config JSON-диалект Xray) — см. "RunTest" выше,
   раздел про интерпретацию, для полного обоснования и рассмотренной-и-
   отклонённой альтернативы (весь файл конфига целиком). Если Platform-
   сторона, готовящая `preset_client_config_json` для отправки в `RunTest`,
   исходит из другого предположения о форме этого поля — несовпадение
   обнаружится только на интеграционном этапе (эта проверка недоступна в
   этой песочнице, нет второй стороны для сверки).
2. **`Command.run_test` отвечает `TestResult`, а не `CommandResult`** —
   контракт это не говорит явно (просто определяет оба типа сообщений как
   параллельные варианты `AgentMessage.payload`, с разными полями-
   идентификаторами: `command_id` у одного, `test_id` у другого), но и не
   исключает. Agent принял это как дизайн-решение (см. "RunTest" выше) —
   **Platform-сторона не должна ждать `CommandResult` для команд с
   `op = run_test`**, только `TestResult`, коррелированный по `test_id`
   (который отдельная величина, а не эквивалентен `Command.command_id`).
   Если Platform-реализация этого не ожидает, `Session`-хендлер там может
   зависнуть в ожидании `CommandResult`, который никогда не придёт.
