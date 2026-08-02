# Platform

Полное ТЗ: [../docs/TZ-5-platform.md](../docs/TZ-5-platform.md).

Кратко: Python 3.12+, FastAPI + SQLAlchemy 2.0 (async) + Alembic + PostgreSQL, ARQ для фоновых задач (пока не подключён — см. "Scope of this slice / TODO"), gRPC-сервер к Agent'ам по контракту [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto), REST/WebSocket API для Admin Client по контракту [`../contracts/rest/openapi.yaml`](../contracts/rest/openapi.yaml).

Текущая ветка (`platform/api-skeleton`) — третий слайс: ко второму слайсу (реальный command dispatch Platform→Agent, сборка каскадов, управление пользователями VPN, приём телеметрии) добавлены настоящая mTLS между Agent↔Platform, реальные тест-запросы по связке (`POST /cascades/{id}/test` + история) и push-обновления по WebSocket (`WS /events`). Остальное — см. раздел TODO ниже.

---

## Что это делает

### Первый слайс (аутентификация администратора, реестр нод)

- REST API (`/api/v1/...`) на FastAPI: вход/обновление токена администратора, CRUD пула нод (список, выпуск enrollment-токена, детали/патч/вывод из эксплуатации).
- gRPC-сервер (`AgentControl.Session`, bidi-streaming) — принимает enrollment новых Agent'ов по одноразовому токену, обрабатывает heartbeat (перевод `ENROLLING → HEALTHY`, журналирование переходов в `HealthEvent`).
- Полная модель данных из ТЗ-5 §3 (SQLAlchemy 2.0 async) + Alembic-миграция.
- Argon2id-хеширование паролей, JWT access+refresh с ротацией и deny-list, per-IP (`slowapi`) и per-account rate limiting/lockout на `/auth/login`.
- Структурированное JSON-логирование с редактированием секретов.
- `docker-compose.yml` для локальной разработки (Postgres + сам сервис).

### Второй слайс (этот слайс)

- **Живой command dispatch Platform → Agent** (`grpc_server/servicer.py`) — `SessionRegistry` держит очередь ("outbox") на каждый подключённый bidi-стрим Agent'а, keyed по `node_id`; `dispatch_command(node_id, command)` кладёт `Command` в эту очередь и дожидается соответствующего `CommandResult`/`TestResult`, коррелируя по `command_id`/`test_id` через словарь `asyncio.Future`. Раньше это было заглушкой (`raise NotImplementedError`) — теперь реальный механизм, покрытый `tests/test_grpc_dispatch.py`.
- **`POST /cascades`** — валидирует существование entry/exit нод, сохраняет `Cascade`+`CascadeExitNode`, реально рассылает `AddInbound`/`AddOutbound`/`AddRoutingRule` затронутым Agent'ам (см. "Отклонения от ТЗ" про происхождение JSON-формы). **`DELETE /cascades/{id}`** — рассылает `Remove*` и удаляет строки. **`GET /cascades`/`GET /cascades/{id}`** — реальное состояние с полем `total`. Dispatch — best-effort (`api/command_dispatch.py`): офлайн-нода не роняет HTTP-запрос, а отражается в `Cascade.status` (`up`/`degraded`/`down`).
- **`POST /users`** — создаёт `User` (id/uuid генерируются сервером), рассылает `AddUser` на entry-ноду каждого каскада, к которому дан доступ. **`DELETE /users/{id}`** — рассылает `RemoveUser`, помечает пользователя отозванным. **`GET /users`/`GET /users/{id}`** — реальное состояние с полем `total`.
- **`POST /auth/change-password`** — проверяет `current_password` (Argon2id), задаёт новый хеш, сбрасывает `must_change_password`, отзывает все текущие refresh-токены администратора. 401 при несовпадении пароля.
- **Приём телеметрии** — `TelemetryReport.counters` реально пишется в `UsageStat` (почасовые бакеты, накопление, а не по строке на отчёт), `scope="user"` резолвится в `User.id` по `uuid`.

(На момент конца второго слайса остальные эндпоинты контракта — `PATCH /cascades/{id}`, `GET /cascades/{id}/events`, `POST /cascades/{id}/test`, `GET /tests/{id}`, `GET /usage`, `WS /events` — были объявлены в роутере, но возвращали `501 Not Implemented`. Третий слайс закрывает часть этого списка — см. ниже; актуальный список того, что всё ещё `501`, — в разделе TODO.)

### Третий слайс (этот слайс)

- **Настоящая mTLS между Agent↔Platform** (ТЗ-5 §6.2, `security/mtls_ca.py`) — Platform генерирует и хранит собственный CA (EC P-256/ECDSA-SHA256; корень — 10 лет, серверный leaf-сертификат — 825 дней) на первом запуске, персистентно (`settings.mtls_ca_dir`, Docker-том в `docker-compose.yml`). gRPC-сервер (`grpc_server/server.py`) поднимается на `ssl_server_credentials()` с `require_client_auth=True` — никакого `insecure_server_credentials` больше нет. `Enrollment.csr_pem` (PKCS#10 CSR от Agent'а) парсится, проверяется его собственная подпись, и подписывается CA (клиентский сертификат — 90 дней, "short-lived with auto-renewal" из ТЗ-5 §6.2); `EnrollmentAck.mtls_client_cert_pem` содержит реальный выпущенный сертификат, `Node.mtls_cert_fingerprint` реально заполняется. Некорректный/отсутствующий/поддельный CSR отклоняет `Enrollment` (не тратит одноразовый токен, не создаёт `Node`). Ключевые алгоритм/сроки задокументированы прямо в `security/mtls_ca.py` (не только здесь) — независимо разрабатываемому Agent-треку эти факты нужны для взаимодействия, а исходники Platform ему не видны.
- **Реальные тест-запросы по связке** (ТЗ-5 §4.3) — `POST /cascades/{id}/test` реально диспатчит `RunTest` на entry-ноду каскада через существующий `dispatch_test()`/`SessionRegistry` механизм (`grpc_server/servicer.py`), сохраняет `LinkTest` в статусе "pending" и сразу возвращает `202` с `test_id` — не дожидаясь ответа Agent'а. Когда (или если) приходит `TestResult`, коррелированный по `test_id`, — фоновая задача (`services/link_tests.py`) обновляет строку реальным исходом (`success`/`container_start_failed`/`tls_handshake_failed`/`proxy_no_response`/`timeout`) + задержкой + описанием; офлайн/не отвечающая нода тоже приводит к разрешённой строке (`timeout` + понятная причина в `detail`), а не к висящему запросу. `GET /tests/{id}` отдаёт текущее состояние строки (включая ещё не разрешённое). `GET /cascades/{id}/tests` — пагинированная история (`items`+`total`).
- **`WS /events`** — реальный push, не заглушка: `services/event_bus.py` — in-process pub/sub (по одной `asyncio.Queue` на подключение), в который публикуются `HealthEvent` (переходы статуса ноды при heartbeat, вывод ноды из эксплуатации, создание каскада) и разрешённые `LinkTest` из пункта выше. Обработчик маршрута параллельно слушает входящие ASGI-сообщения только чтобы вовремя заметить отключение клиента (иначе подписка на `event_bus` протекала бы бесконечно — соединение закрылось, а сервер всё ещё ждёт в `queue.get()`, ничего не зная об этом).

---

## Установка

Требуется Python 3.12+ (проверено на 3.13; на машине разработчика 3.12 не было в наличии, но код не использует ничего специфичного для 3.13 — только `python_requires = ">=3.12"`).

```bash
cd platform
python -m venv .venv
# Windows: .venv\Scripts\activate | Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

### Генерация gRPC-стабов

Сгенерированный код (`src/dist_vpn_platform/generated/`) **не коммитится** (см. корневой `.gitignore` — `**/generated/`, `**/*_pb2*.py`) и должен быть сгенерирован после каждого `pip install`/клонирования, и заново — при любом изменении `contracts/proto/agent_control.proto`:

```bash
python scripts/gen_proto.py
```

Скрипт сам находит корень монорепозитория (ищет `contracts/proto/agent_control.proto` вверх по дереву от своего расположения) — можно запускать из любой директории. Компиляция проверена: `grpcio-tools` собирает контракт без ошибок (см. `contracts/README.md` п.1).

---

## Миграции

Alembic настроен на асинхронный движок (`alembic/env.py` использует `dist_vpn_platform.config.settings.database_url` как единственный источник правды для строки подключения — значение в `alembic.ini` — заглушка, переопределяется в рантайме).

```bash
# применить все миграции
python -m alembic upgrade head

# откатить всё (для локальной проверки)
python -m alembic downgrade base

# сгенерировать новую миграцию после изменения моделей
python -m alembic revision --autogenerate -m "описание изменения"
```

Базовая миграция (`alembic/versions/cc06e050bd2b_baseline_schema.py`) создаёт все 12 таблиц (8 сущностей ТЗ-5 §3 + 2 служебные — `EnrollmentToken`, `RefreshToken`, см. "Отклонения от ТЗ" ниже + 2 промежуточные таблицы many-to-many `cascade_exit_nodes`/`user_cascades`). Проверена вручную:
- `upgrade head` / `downgrade base` / `upgrade head` — цикл проходит чисто против **реального PostgreSQL 16** (временный контейнер, не входит в поставку) — не только сгенерирована, но и физически применена.

---

## Запуск локально

### Вариант А — docker compose (рекомендуется, "единая команда развёртывания", ТЗ-5 §8)

```bash
docker compose -f platform/docker-compose.yml up --build
```

Поднимает Postgres + сам сервис. При старте контейнера `platform` автоматически (см. `docker-entrypoint.sh`):
1. `alembic upgrade head`;
2. сидирование дефолтного администратора (`python -m dist_vpn_platform.seed`) — **если** учётка `admin` уже существует, это no-op; если нет — создаётся со случайным одноразовым паролем, который печатается **один раз** в консоль контейнера (`docker compose logs platform`) и нигде больше не сохраняется (ТЗ-5 §6.3 — "небезопасная конфигурация по умолчанию");
3. запуск HTTP (`:8000`) + gRPC строгий mTLS (`:50051`) + gRPC enrollment-only без клиентского сертификата (`:50052`, `grpc_server/server.py` — полное обоснование, почему это два порта, а не один опциональный) в одном процессе (`python -m dist_vpn_platform.main`).

Порты хоста переопределяемы через `PLATFORM_HOST_HTTP_PORT` / `PLATFORM_HOST_GRPC_PORT` / `PLATFORM_HOST_GRPC_ENROLLMENT_PORT` / `PLATFORM_HOST_DB_PORT`, если 8000/50051/50052/5432 заняты чем-то ещё на машине.

Проверено вручную (не только `docker compose build`, но и полный `up` + сквозной curl):
- HTTP API поднимается, `/docs` отдаёт Swagger UI (200);
- `/api/v1/auth/login` с сгенерированным паролем администратора возвращает пару токенов;
- `/api/v1/nodes` (list/create-enrollment) работает через реальный Postgres в контейнере;
- gRPC: enrollment с выпущенным токеном → `EnrollmentAck.accepted=true` → heartbeat → `GET /nodes/{id}` показывает `status: "healthy"` — то есть именно тот сквозной сценарий, что зафиксирован первым пунктом критериев приёмки ТЗ-5 §9.

### Вариант Б — локально без Docker

Нужен доступный PostgreSQL (`PLATFORM_DATABASE_URL`, см. `.env.example`).

```bash
cp .env.example .env   # и отредактировать под свою БД/секреты
python -m alembic upgrade head
python -m dist_vpn_platform.seed
python -m dist_vpn_platform.main
```

---

## Тесты

```bash
pytest -q
```

**68/68 тестов проходят.** Тесты используют `aiosqlite` (файловая SQLite-БД, создаётся и удаляется на каждый тест — см. `tests/conftest.py`), а не PostgreSQL — потому что не в каждом окружении, где может запускаться этот набор тестов, гарантированно доступен Postgres, а модели используют только переносимые типы SQLAlchemy (`String`/`DateTime`/`JSON`/`Enum(..., native_enum=False)` — см. `models/base.py::sa_enum`) специально ради этого. **В production используется исключительно PostgreSQL** (ТЗ-5 §2) — сама Alembic-миграция проверена отдельно против реального Postgres (см. раздел "Миграции" выше), тесты pytest проверяют слой приложения/ORM, а не DDL.

Состав:
- `tests/test_auth.py` — успешный/неуспешный вход, per-IP rate limiting (429 после порога), per-account lockout, ротация refresh-токена + отказ при повторном использовании (theft detection), logout (с телом и без), `POST /auth/change-password` (успех + отзыв старых refresh-токенов, неверный текущий пароль → 401, требует аутентификации).
- `tests/test_nodes.py` — список нод (пустой пул, авторизация обязательна), выпуск enrollment-токена, полный цикл GET/PATCH/DELETE ноды, 404 на несуществующую ноду.
- `tests/test_grpc_servicer.py` — unit-тесты обработчика gRPC (`Enrollment`/`Heartbeat`/`TelemetryReport`) напрямую (без реального gRPC-канала — сама транспортная часть сгенерирована `grpcio-tools`): валидный/невалидный/повторно использованный токен, переход `ENROLLING → HEALTHY` по heartbeat с записью `HealthEvent`, накопление `UsageStat` по часовым бакетам с резолвом `user_id`.
- `tests/test_grpc_dispatch.py` — live command/test dispatch: открывает фейковый `Session`-стрим (`tests/grpc_test_helpers.py`), проверяет, что `Command`, отправленный через `dispatch_command()`, реально приходит в этот стрим, а присланный в ответ `CommandResult` резолвит ожидающий `asyncio.Future`; то же для `dispatch_test()`/`TestResult` (корреляция по `test_id`, а не `command_id`); отдельно — `NodeNotConnectedError` для неизвестной ноды и таймаут для неотвечающего Agent'а (обе функции).
- `tests/test_cascades.py` — `POST /cascades` реально диспатчит `AddInbound`/`AddOutbound`/`AddRoutingRule` подключённым фейковым Agent'ам (`tests/grpc_test_helpers.py::FakeAgent`) и возвращает `status: up`; офлайн exit-нода → `status: degraded`; несуществующая entry-нода → 404; `DELETE` диспатчит `Remove*` и удаляет запись; `total` в списке.
- `tests/test_users.py` — `POST /users` генерирует `id`/`uuid` на сервере и диспатчит `AddUser` на entry-ноду каскада; `DELETE` диспатчит `RemoveUser` и помечает пользователя отозванным (`cascade_ids` пустеет); 404 на несуществующий каскад/пользователя.
- `tests/test_mtls_ca.py` — генерация CA/сервер-leaf-сертификата, персистентность на диске между "перезапусками", свойства самоподписанного корня, SAN серверного сертификата, подпись CSR в клиентский сертификат (включая отклонение CSR с испорченной подписью).
- `tests/test_mtls_integration.py` — реальный `grpc.aio` secure-канал против реального `grpc.aio`-сервера (не фейковый итератор, как остальные gRPC-тесты): корректно enrolled клиент проходит `Enrollment` целиком; клиент без сертификата не может подключиться; клиент с сертификатом от чужого CA — тоже не может.
- `tests/test_link_tests.py` — `POST /cascades/{id}/test` возвращает `202`+`test_id` и реально диспатчит `RunTest` подключённому `FakeAgent`; результат резолвится в `LinkTest` с реальным исходом Agent'а (включая конкретный проваленный шаг, не просто `fail`); офлайн entry-нода → `timeout` без зависания запроса; `GET /tests/{id}`/`GET /cascades/{id}/tests` (пагинация, `total`), 404 на несуществующие `test_id`/`cascade_id`.
- `tests/test_events.py` — `WS /events`: отклоняет отсутствующий/невалидный токен; доставляет `health_event` при переходе статуса ноды по heartbeat; доставляет `link_test` при разрешении тест-запроса (сквозной сценарий `POST /cascades/{id}/test` → `FakeAgent` → WS); несколько одновременных подписчиков получают одно и то же сообщение.

---

## Переменные окружения

Полный список с описанием — `.env.example` в этой директории и `src/dist_vpn_platform/config.py`. Ключевые:

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `PLATFORM_DATABASE_URL` | Строка подключения к Postgres (asyncpg) | `postgresql+asyncpg://platform:platform@localhost:5432/platform` |
| `PLATFORM_HTTP_PORT` / `PLATFORM_GRPC_PORT` / `PLATFORM_GRPC_ENROLLMENT_PORT` | Порты REST-, строгого mTLS gRPC- и enrollment-only gRPC-серверов | `8000` / `50051` / `50052` |
| `PLATFORM_JWT_SECRET_KEY` | Секрет подписи JWT — **обязательно переопределить** вне dev | случайный при каждом запуске процесса, если не задан |
| `PLATFORM_JWT_ACCESS_TTL_MINUTES` | TTL access-токена (ТЗ-5 §6.3: 15–30 мин) | `20` |
| `PLATFORM_JWT_REFRESH_TTL_DAYS` | TTL refresh-токена | `14` |
| `PLATFORM_LOGIN_RATE_LIMIT_PER_IP` | Лимит `slowapi` на `/auth/login` по IP | `10/minute` |
| `PLATFORM_LOGIN_MAX_FAILED_ATTEMPTS` / `PLATFORM_LOGIN_LOCKOUT_MINUTES` | Порог/длительность блокировки конкретного аккаунта | `5` / `15` |
| `PLATFORM_DEFAULT_ADMIN_USERNAME` / `PLATFORM_DEFAULT_ADMIN_PASSWORD` | Логин дефолтного администратора / пароль (оставить пустым — сгенерируется случайно и напечатается один раз при сидировании) | `admin` / *(не задан)* |
| `PLATFORM_ENROLLMENT_TOKEN_TTL_MINUTES` | TTL одноразового enrollment-токена ноды | `30` |

---

## Структура

```
platform/
  pyproject.toml
  alembic.ini, alembic/            # миграции (env.py тянет URL и metadata из приложения)
  scripts/gen_proto.py             # регенерация gRPC-стабов из contracts/proto
  docker-compose.yml, Dockerfile, docker-entrypoint.sh
  src/dist_vpn_platform/
    config.py, logging_config.py, db.py, app.py, main.py, seed.py
    models/                        # SQLAlchemy 2.0 async, см. ТЗ-5 §3
    schemas/                       # Pydantic-схемы запросов/ответов (см. openapi.yaml)
    security/                      # Argon2id, JWT, TOTP, enrollment-токены, rate limit
    api/                           # роутер + маршруты REST/WS
    grpc_server/                   # AgentControl servicer + bootstrap
    generated/                     # gRPC-стабы (гитигнор, генерируется gen_proto.py)
  tests/
```

---

## Отклонения от ТЗ / контракта (задокументировано, не молчком)

1. **Две служебные сущности сверх списка ТЗ-5 §3**: `EnrollmentToken` (нужна для одноразовых токенов enrollment, ТЗ-5 §4.1/§6.2 — контракт `POST /nodes` явно требует такое поведение) и `RefreshToken` (нужна как deny-list/хранилище для ротации JWT-refresh, ТЗ-5 §6.3). Обе — инфраструктурные детали реализации существующих функциональных требований, а не новые продуктовые сущности; не заменяют и не меняют смысл ни одной из 8 сущностей ТЗ-5 §3.
2. **⚠️ Исправлено после ревью (см. коммит слияния `main` в эту ветку).** На момент реализации этого слайса `git log --oneline -- contracts/rest/openapi.yaml` на ЭТОЙ ветке показывал только исходный черновик `dd6e7f6` — из чего был сделан вывод "контракт не менялся с первого слайса". Вывод был **неверным**: ветка `platform/api-skeleton` просто ни разу не мержила `main` с момента создания, а на `main` тем временем реально прилетели два коммита (`2bac197`, `d3055d7`) от находок Agent- и Admin-Client-треков — именно те `total`/`must_change_password`/`/auth/change-password`/опциональное тело `/auth/logout`, которые задание на этот слайс описывало как уже существующие. После слияния `main` в эту ветку (см. `git log`) подтверждено: **контракт содержит всё перечисленное**, и — по факту сверки поле-в-поле — код этого слайса (`ChangePasswordRequest.current_password/new_password`, `LogoutRequest.refresh_token`, `total` в `CascadeListResponse`/`UserListResponse`) **уже полностью совпадает** с реальным контрактом, несмотря на то что писался в убеждении, что контракта для этого нет. Менять код не потребовалось — только этот раздел README. Урок на будущее (см. также `agent/README.md`/`admin-client/README.md` про ту же проблему на других треках): при параллельной разработке в отдельных ворктри **перед тем, как делать вывод "контракт не менялся"**, нужно проверять `git log -- <файл>` не только на своей ветке, а сравнивать с `origin/main`/`main` — иначе отставание ветки маскируется под "ничего не менялось".
3. **`TokenPair` в ответе `/auth/login`/`/auth/refresh` содержит поле `must_change_password`** — есть в контракте (см. п.2), реализовано и реально используется: `POST /auth/change-password` его сбрасывает.
4. ~~(объединено с п.2 после ревью)~~
5. **`CascadeListResponse`/`UserListResponse` содержат `total`** — есть в контракте (см. п.2), реализовано как реальный `COUNT(*)`-запрос, для симметрии с уже имевшимся `total` у `/nodes`.
6. **`POST /auth/change-password`** — есть в контракте (см. п.2). 401 при неверном `current_password`, 204 при успехе, отзывает все текущие refresh-токены администратора. Поля запроса (`current_password`/`new_password`) совпадают с контрактом дословно.
7. **Xray inbound/outbound/routing-rule JSON — не буквальная копия ранее проверенной конфигурации** (`services/xray_templates.py`). ТЗ-3 §0 говорит, что конкретные P1-P4 шаблоны и точная форма `balancer`/`observatory`/`fallbackTag` были "проверены ранее в проекте", но физически утрачены при сбое диска до появления этого репозитория — сохранилось только текстовое описание полей (ТЗ-3 §0: `realitySettings`, `network:"raw"/"xhttp"/"grpc"`, `password`=бывший `publicKey`; ТЗ-5 §4.2: "балансировщик с префиксным `selector`, `observatory`, `fallbackTag`"). Ни в `agent/`, ни в `provisioning/`, ни в `contracts/` нет буквального JSON — проверено. `services/xray_templates.py` — реконструкция формы полей по стандартным конвенциям Xray-core, согласованная с этим текстовым описанием, а не копия. Насчёт открытого вопроса — подхватывает ли префиксный `selector` балансировщика новый outbound автоматически без пересборки самого правила: это задокументированное поведение самого Xray (`selector: [string]` в `RoutingObject.balancers[]` — префиксное сопоставление с тегами **существующих на данный момент** outbound'ов, переоценивается на каждый выбор балансировщика, а не фиксируется один раз при создании правила), а не только предположение этого слайса — тот же факт уже был использован в более ранней части этого проекта (ротация Hop B через `rotate-hopb.sh`, где новый outbound с префиксным тегом подхватывался балансировщиком без правки `routing.balancers`). Эмпирически именно в ЭТОМ Python-кодовом слое (а не в прежнем bash-варианте) это по-прежнему не проверено — разница между "задокументированный факт про Xray" и "проверено в этой конкретной реализации" сохраняется как честная оговорка. **⚠️ Обновлено в четвёртом слайсе**: `realitySettings.privateKey`/`shortIds` были жёстко пустой строкой (`""`) с явным TODO — это был не косметический пробел, а жёсткий блокер: REALITY не может завершить handshake с пустым приватным ключом. Закрыто — см. п.11 ниже.
8. **Dispatch команд Platform→Agent — best-effort, не блокирует HTTP-запрос** (`api/command_dispatch.py`): если нода не подключена или не отвечает, `POST /cascades`/`POST /users`/`DELETE /cascades/{id}`/`DELETE /users/{id}` всё равно завершаются успешно (состояние в БД — источник истины), а `Cascade.status` отражает `degraded`/`down`. Решение принято по аналогии с уже существующим паттерном `decommission_node()` (`api/routes/nodes.py`), явного требования в задании на этот счёт не было — если желаемое поведение "выведенная из сети нода = запрос падает", это отдельное решение для следующего PR.
9. **⚠️ Обновлено: mTLS между Agent↔Platform (ТЗ-5 §6.2) теперь реализована**, in-process (`security/mtls_ca.py`, `cryptography`) вместо изначально запланированного внешнего step-ca (см. §2 ТЗ-5) — сознательный, задокументированный в самом модуле выбор: CSR-подпись выполняется синхронно прямо в обработчике `Enrollment`, отдельный сервис для этого — оверинжиниринг для текущего объёма (единственный Platform-процесс, не флот CA), а вызовы `mtls_ca.get_mtls_material()`/`sign_client_csr()` — единственная граница, которую увидит будущая замена на step-ca. gRPC-сервер — `ssl_server_credentials()` с `require_client_auth=True`, `EnrollmentAck.mtls_client_cert_pem` несёт реальный выпущенный сертификат. **Важно для Agent-трека (не видит этот код напрямую):** алгоритм — EC P-256/ECDSA-SHA256 везде; CA-корень — 3650 дней; серверный leaf — 825 дней; клиентский сертификат Agent'а — 90 дней. Подробности — прямо в докстринге `security/mtls_ca.py`. При этом канальная mTLS-аутентификация (любой клиент с сертификатом от нашего CA допускается на транспортном уровне) — это не то же самое, что identity-биндинг конкретной ноды: "переподключение по постоянному сертификату" (ТЗ-4 §5.2) по-прежнему реализовано как "первый `Heartbeat` на новом стриме с уже известным `node_id`, если `Enrollment` не отправлялся" (`grpc_server/servicer.py::Session`, ветка `kind == "heartbeat"`) — извлечение identity ноды непосредственно из клиентского сертификата (а не из тела `Heartbeat`) осталось не реализовано, отдельная задача на будущий слайс.
10. **TOTP/MFA (ТЗ-5 §6.3)**: без изменений с первого слайса — только верификация, провижининг нового секрета всё ещё не реализован.
11. **[Четвёртый слайс] REALITY-ключи теперь настоящие, плюс новый эндпоинт для готовой `vless://` ссылки.** `security/reality_keys.py` генерирует реальные X25519-keypair'ы (`cryptography`) — по одному на Hop A (клиентский, хранится на `Cascade`) и по одному на пару (каскад, exit-нода) для Hop B (хранится на `CascadeExitNode`, поскольку `build_hopb_inbound_json`/`build_hopb_outbound_json` уже диспатчились именно с этой гранулярностью). Кодировка (`base64.RawURLEncoding`, без padding) сверена напрямую с исходником Xray-core (`main/commands/all/x25519.go`: флаг `-i` документирован как принимающий `"private key (base64.RawURLEncoding)"`, `--std-encoding` — явно опциональная альтернатива) — не предположение, а сверенный факт. Добавлено **новое поле** `Node.public_address` (админ-задаваемое через `PATCH /nodes/{id}`, никогда не выводится из `Node.hostname` — тот self-reported агентом при enrollment и может быть внутренним именем, не обязательно публичным) и **новый эндпоинт** `GET /users/{user_id}/connection/{cascade_id}`, собирающий готовую `vless://` ссылку (`services/xray_templates.py::build_vless_uri`) — 400 с конкретной инструкцией, если у entry-ноды каскада не задан `public_address`. Формат query-параметров URI (`security=reality`, `pbk`, `sid`, `fp=chrome`, **`type=tcp`** — не `raw`, несмотря на то что JSON-конфиг сам называет это поле `network: "raw"`; URI-конвенция старше и не следует переименованию Xray-core) сверен с `github.com/XTLS/Xray-core/discussions/716`. `flow` в URI оставлен пустым — согласовано с `AddUser.flow` (`api/routes/users.py`, п.8 выше), намеренно, не забыто. **На этом слайсе и `contracts/rest/openapi.yaml` тоже отредактирован** (в отличие от предыдущих слайсов, см. строку ниже) — `Node.public_address`, `Cascade.hopa_reality_public_key`/`hopa_reality_short_id`, `PATCH /nodes/{id}` тело, новый путь `/users/{user_id}/connection/{cascade_id}` — тот же аддитивный, задокументированный-а-не-молчаливый паттерн, что и `/auth/change-password` (п.6). REALITY-камуфляжный `dest`/`serverName` (`REALITY_DEST`/`REALITY_SERVER_NAME` в `xray_templates.py`) — общая константа (`www.microsoft.com:443`) для Hop A и Hop B, не операторски настраиваемая в этом слайсе (см. TODO ниже) — упрощение, не требование корректности (у Hop A/Hop B независимые ключи, коллизий не создаёт).

12. **[Пятый слайс] Лимит устройств на пользователя + временной ряд ресурсов ноды.** Оба пункта требования пользователя ("ограничение количества устройств на пользователя" и "мониторинг ресурсов, чтобы соотнести с количеством пользователей") реализованы через один и тот же механизм — Xray-core `StatsService`, теперь реально дошедший до Platform через новое поле `TelemetryReport.user_online` (contracts/proto/agent_control.proto — **редактирован в этом слайсе**, аддитивно). По пути найден и закрыт **пятый по счёту в этом проекте фундаментальный пробел**: базовый конфиг Xray с секциями `api`/`stats`/`policy` нигде не генерировался вообще (см. `agent/README.md` "Телеметрия" за полной историей) — без него ни лимит устройств, ни уже существовавший (но фактически никогда не работавший) сбор трафика не могли функционировать. `security/reality_keys.py`-подобная модель применена и здесь: `User.max_devices` (`nullable=True`, `None` = без ограничения) сверяется в `grpc_server/servicer.py::_handle_user_online` с `len(online_ips)` из каждого отчёта; при превышении Platform диспатчит `RemoveUser`+`AddUser` обратно тому же узлу (принудительный реконнект всех устройств) и пишет `UserDeviceKickEvent` (новая таблица, аудит) — тем же уже существующим командным механизмом, что и `POST /users`/`DELETE /users/{id}`, ни одного нового `Command`-варианта не потребовалось. **Честная оговорка, задокументированная в докстринге `_handle_user_online`**: лимит проверяется **на ноду**, не глобально по всем каскадам пользователя — агрегация онлайн-состояния между разными нодами потребовала бы общего кэша/дополнительных запросов, которых у Platform сейчас нет; ТЗ не уточняет "на ноду vs. глобально", per-node — то, что реально измеримо без изобретения нового кросс-нодового состояния. `NodeResourceSample` (новая таблица, TZ-5's "мониторинг ресурсов") пишется по одной строке на каждый `Heartbeat` (`_handle_heartbeat`) — раньше `Node.cpu_percent`/`mem_percent`/`disk_percent` хранили только последнее значение, историю посчитать было невозможно в принципе. `provisioned_user_count` — реальный `COUNT(DISTINCT)`-запрос по `UserCascade`/`Cascade.entry_node_id`; `online_user_count`/`online_device_count` — кэш с последнего `TelemetryReport` этой ноды (`Node.last_online_user_count`/`last_online_device_count`, новые поля) — Heartbeat и TelemetryReport приходят отдельными сообщениями без гарантии порядка/синхронности, так что это "последнее известное", не строго синхронизированное с точностью до секунды значение, и это осознанно достаточно для цели "соотнести тренд ресурсов с количеством пользователей", а не для чего-то более строгого. Новые эндпоинты (`PATCH /users/{id}`, `GET /users/{id}/device-events`, `GET /nodes/{id}/resource-samples`) добавлены и в `contracts/rest/openapi.yaml` — тот же аддитивный, задокументированный-а-не-молчаливый паттерн, что и раньше.

13. **[Шестой слайс] Failover: автодетект по таймауту heartbeat + переключение по действию админа.** Ровно тот scope, который был явно согласован с пользователем (не автоматическое переключение — только автоматическое обнаружение). `services/health_scheduler.py::run_health_scheduler_forever` — periodic in-process `asyncio`-таск (интервал `PLATFORM_HEALTH_SCHEDULER_INTERVAL_SECONDS`, по умолчанию 10с), НЕ Redis/ARQ — тот же принцип "один процесс, без нового инфраструктурного слоя", что уже задокументирован для `SessionRegistry`/`event_bus` (см. TODO ниже). Помечает ноду `DOWN`, если `last_heartbeat_at` старше `PLATFORM_NODE_HEARTBEAT_TIMEOUT_SECONDS` (по умолчанию 45с = 3× дефолтного `AGENT_HEARTBEAT_INTERVAL_SECONDS`) — `ENROLLING`-ноды (`last_heartbeat_at IS NULL`) намеренно исключены: "ни разу не прислала heartbeat" — не то же самое, что "замолчала после того как была здорова". Если упавшая нода — `entry`, все её каскады со статусом `!= down` тоже помечаются `DOWN` — реализовано именно "по этой ноде", НЕ по exit-нодам каскада (Hop B failover уже нативно решён балансировщиком Xray с `leastPing`, см. п.7 выше — Platform не должно дублировать то, что Xray уже делает сам). Восстановление (`DOWN`→`HEALTHY`) по-прежнему полностью реактивное — на следующий реальный heartbeat, `_handle_heartbeat` это уже умел с первого слайса, этот срез его не менял. **`POST /cascades/{cascade_id}/migrate-users`** — сам акт переключения: админ явно указывает `to_cascade_id`, всем пользователям старой связки диспатчится `RemoveUser` на (предположительно мёртвой) старой ноде — best-effort, не блокирует — и `AddUser` на новой, `UserCascade`-связи обновляются. Никакой автоматики в выборе связки-замены нет и не запрашивалось — сценарии вроде "автоматически выбрать ближайшую по пингу здоровую связку" явно не реализованы, это отдельное решение для будущего среза, если понадобится.

14. **[Восьмой слайс] REALITY SNI-ротация, P4-пресет, клиентские routing-правила.** Три независимых пункта по мотивам стороннего разбора реальной DPI-детекции VLESS Reality (публичная статья, найдена и разобрана пользователем, не собственное открытие):
    - **REALITY-донор больше не общая константа.** `REALITY_SERVER_NAME`/`REALITY_DEST` (одна пара `www.microsoft.com`/`www.microsoft.com:443` на весь флот) заменены на `REALITY_DONOR_POOL` (список из 4 доменов) + `_select_reality_donor(seed)` — детерминированный выбор по `sha256(seed) % len(pool)`, `seed` = `cascade_id` для Hop A, `f"{cascade_id}:{exit_node_id}"` для каждой пары (каскад, exit-нода) Hop B. Детерминированность (не случайный выбор при каждом вызове) важна: `build_hopa_inbound_json`/`build_vless_uri` должны сойтись на одном и том же доноре для одного каскада независимо друг от друга, иначе SNI в клиентской ссылке не совпадёт с тем, что реально примет inbound — то же самое для пары `build_hopb_outbound_json`/`build_hopb_inbound_json`. **Честная оговорка**: список из 4 доменов — разумная стартовая точка, не проверенный для конкретного деплоя список — реальная пригодность домена как REALITY-донора (CDN-присутствие в целевом регионе, актуальность TLS 1.3+HTTP/2) зависит от живых фактов, которые проверяются инструментом вроде RealiTLScanner непосредственно на entry/exit VPS, а не константой в коде. Задокументировано прямо в докстринге `REALITY_DONOR_POOL`.
    - **P4-пресет (ТЗ-4 §3.3/§3.4, "VLESS/TLS на WebSocket") реализован на стороне Platform** — второй, одновременный inbound на entry-ноде (не замена REALITY), порт `P4_LISTEN_PORT = 8443` (один из портов, которые Cloudflare поддерживает как proxied origin — CDN-фронтинг можно добавить поверх без изменения конфига, но P4 работает и без него, как обычный HTTPS-сайт с настоящим TLS-сертификатом). Путь WebSocket (`p4_ws_path`) выводится из `cascade_id` детерминированно, не хранится отдельным полем. `POST /cascades` теперь принимает `hop_a_presets: ["p1", "p4"]` и диспатчит ОБА inbound'а — P4 best-effort, его неудача не влияет на `Cascade.status` (REALITY — обязательный путь, P4 — опциональный резерв). Новое поле `Node.p4_domain` (тот же паттерн, что `public_address` — админ-задаваемое через `PATCH /nodes/{id}`), используется только когда каскад реально запросил `"p4"`. `GET /users/{id}/connection/{cascade_id}` теперь может вернуть `vless_uri_p4` вторым полем. **Честная оговорка, задокументированная в докстринге `P4_TLS_CERT_PATH`/`P4_TLS_KEY_PATH`**: TLS-сертификат для P4 — фиксированный путь на диске Agent'а, который оператор наполняет вручную (`certbot`, Cloudflare Origin Certificate) — полноценная автоматизация через ACME-клиент, управляемый самим Agent'ом (упомянута как открытый вопрос ещё в ТЗ-4 §3.4), не реализована в этом слайсе. Provisioning-роль под размещение сертификата тоже не добавлена — отдельная задача для `provisioning/`.
    - **Клиентские routing-правила (RU bypass + блокировка IP-leak эндпоинтов)** — `build_client_routing_rules()` возвращает Xray-совместимые объекты `RoutingObject.rules[]` (не JSON-строка, а данные — в отличие от остальных `build_*_json` функций модуля, это не то, что диспатчится Agent'у, а то, что клиент сам вставляет в свой локальный конфиг): `geosite:category-ru`/`geoip:ru` → `direct` (два отдельных правила, не одно комбинированное — у Xray поля внутри одного `RuleObject` работают через AND, а не OR, так что `domain`+`ip` в одном правиле означало бы "и то, и то сразу", а не "любое из двух"), плюс список известных "узнай-свой-ip" доменов (`IP_LEAK_CHECK_DOMAINS` — `ipify.org`, `ifconfig.me`, `icanhazip.com`, `2ip.*` и т.д., список не исчерпывающий, требует ручного расширения) → `block`. Логика: если у клиента включён bypass для локальных ресурсов, а всё остальное идёт через каскад, случайное обращение к IP-эхо сервису через тоннель раскроет реальный IP exit-ноды третьей стороне — не гипотетический риск. Отдаётся новым полем `routing_rules` в `GET /users/{id}/connection/{cascade_id}`.

`contracts/proto/agent_control.proto` отредактирован в пятом слайсе (см. п.12, `TelemetryReport.user_online`) — ранее не трогался, в шестом/седьмом/восьмом не менялся. `contracts/rest/openapi.yaml` отредактирован в четвёртом (п.11), пятом (п.12), шестом (п.13, `/cascades/{id}/migrate-users`) и восьмом (п.14, `Node.p4_domain` в схеме и `PATCH /nodes/{id}`) слайсах.

15. **[Девятый слайс] Данные под раскол Platform на RU-сегмент/зарубежный-сегмент (архитектурный pivot, см. provisioning/README.md "Control-plane через REALITY" — прямой Agent↔Platform канал из РФ до Platform за рубежом убивается ТСПУ уже после хендшейка, несмотря на REALITY/фрагментацию/XHTTP).** Вместо N хардненых туннелей (по одному на каждую RU entry-ноду) — два независимых Platform-инстанса (RU-сегмент управляет только entry-нодами, зарубежный — только exit), каждый Agent общается только со своим Platform внутри своей же страны; через границу остаётся только сам каскадный data-path плюс один новый канал Platform↔Platform (следующие слайсы). Этот слайс — только фундамент данных, поведение не меняется: `Cascade.entry_node_id`/старый `CascadeExitNode.exit_node_id` — оба голые FK в одну и ту же таблицу `nodes` одной БД, что структурно невозможно, когда entry- и exit-ноды лежат в разных БД разных Platform. Новые модели: **`PeerPlatform`** (`models/peer_platform.py`) — запись о другом Platform-инстансе (pinned-сертификат, НЕ переиспользует Agent-facing mTLS CA — разный класс доверия, разный blast radius); **`RemoteExitCandidate`** (`models/remote_exit_candidate.py`) — локальная теневая запись RU-стороны о известной exit-ноде, на неё теперь ссылается `CascadeExitLink.remote_exit_candidate_id`; **`ExitLinkAllocation`** (`models/exit_link_allocation.py`) — собственная запись зарубежной стороны "какая моя exit-нода обслуживает какой удалённый каскад", туда же теперь переехал `hopb_reality_private_key` (раньше жил на `CascadeExitNode` — одна Platform держала оба приватных ключа просто потому что владела обеими нодами; теперь приватный ключ Hop B видит только та сторона, что реально диспатчит `AddInbound` на свою же exit-ноду — побочное усиление безопасности, не только следствие схемы). `CascadeExitNode`/`cascade_exit_nodes` переименованы в `CascadeExitLink`/`cascade_exit_links` (чистое переименование через `DROP TABLE`/`CREATE TABLE` в миграции — production-данных, которые нужно было бы сохранять, не было, только несколько dogfood-VDS). **Degenerate/локальный режим** (Platform ещё не расколота физически, как в этом слайсе): `peer_platform_id IS NULL` на `RemoteExitCandidate`/`ExitLinkAllocation` означает "это тот же самый узел в этой же БД" — `api/routes/cascades.py::create_cascade` теперь всегда идёт через `_get_or_create_local_remote_exit_candidate` + создаёт `ExitLinkAllocation` в этом же режиме, так что весь существующий набор тестов (`test_cascades.py`) проходит без изменения наблюдаемого поведения (тот же порядок команд Agent'ам, тот же JSON) — реальная ветка с настоящим пиром появится в 10-м слайсе (`services/peering_client.py`), когда `CascadeInput.exit_node_ids` сможет означать не только локальный `Node.id`. `CascadeOut.exit_node_ids` сохранён как есть (обратная совместимость), добавлено **аддитивное** поле `CascadeOut.exit_links: list[CascadeExitLinkOut]` (per-link `status`/`active_preset`) — понадобится, когда разные exit-кандидаты одного каскада смогут одновременно быть на разных стадиях подбора пресета (`services/link_negotiation.py`, 12-й слайс). `contracts/rest/openapi.yaml` в этом слайсе НЕ редактировался — `exit_links` пока существует только в Pydantic-схеме, синхронизация контракта отложена до момента, когда peering-протокол (10-й слайс) действительно определит форму `CascadeExitLinkOut` окончательно.

16. **[Десятый слайс] Platform↔Platform peering-протокол, без логики авто-ротации (следующий слайс).** Новый, отдельный от `agent_control.proto` контракт — `contracts/proto/platform_peering.proto`, сервис `PlatformPeering`, **4 unary RPC** (`ProvisionExitLink`/`ReleaseExitLink`/`SwitchLinkPreset`/`ListExitLinks`), не bidi-стрим — ровно два заранее известных, спровижененных один раз пира, а не большой полу-доверенный флот, так что unary/request-response проще реализовать/протестировать/ретраить по одному вызову, чем стрим. **Направление вызова зафиксировано и не настраиваемо**: RU-Platform всегда клиент (`services/peering_client.py`), зарубежный Platform всегда только сервер (`grpc_server/peering_server.py`+`peering_servicer.py`) — health/status опрашивается (pull) со стороны RU, никогда не проталкивается (push) с зарубежной; переживёт ли обратное направление (зарубеж→РФ, входящее в РФ-хост) ТСПУ — этим репозиторием НЕ проверено ни в какую сторону, и дизайн протокола специально избегает необходимости в ответе на этот вопрос, а не предполагает его. **Аутентификация — mutual cert pinning (`security/peering_identity.py`), НЕ Agent-facing mTLS CA** (`security/mtls_ca.py`): разный класс доверия для двух заранее известных операторски-заведённых сторон против большого auto-issuance флота, общий CA означал бы равное доверие скомпрометированному Agent enrollment-потоку и "второй Platform". Отдельный порт `peering_grpc_port` (по умолчанию 50053, `Settings.platform_segment`/`Settings.peering_grpc_port` — новые поля `config.py`) — свой корень доверия на свой слушающий сокет, тот же принцип, что уже применён для `grpc_enrollment_port` (50052) против `grpc_port` (50051). `grpc_server/peering_server.py` запускается только когда `platform_segment == "exit"` (см. `main.py`) — падает явной `PeeringNotConfiguredError`, если нет `PeerPlatform`-строки с заполненным `local_peering_key_pem`/`local_peering_cert_pem`/`peer_cert_pem` (тот же принцип "явная ошибка в preflight, а не тихое неверное поведение", что и везде в `provisioning/roles/*/tasks/preflight.yml`). `ProvisionExitLink` идемпотентен по `(peer_platform_id, remote_cascade_id, exit_node_id)` (см. `UniqueConstraint` на `ExitLinkAllocation`, девятый слайс) — повторный вызов не создаёт вторую аллокацию и не передиспатчит команду. **Проверка статуса ноды ослаблена до "не DECOMMISSIONED"** (не HEALTHY-only), по образцу `create_cascade`: реальный сигнал живости — открытая `SessionRegistry`-сессия, которую и проверяет `dispatch_best_effort`, а не закэшированное в БД поле `Node.status`. `OverrideBalancer`-подобной логики выбора пресета в этом слайсе ещё нет — `SwitchLinkPreset` уже перевыпускает REALITY-ключи и передиспатчит `RemoveInbound`+`AddInbound`, но использует ещё P1-only сигнатуру `build_hopb_inbound_json` (десятый слайс сознательно НЕ трогает `xray_templates.py` — это одиннадцатый), так что реальная смена транспорта по пресету заработает только после одиннадцатого слайса; плюс сама автоматическая логика "когда и на какой пресет переключаться" (`services/link_negotiation.py`) — двенадцатый слайс, здесь только протокол-плюс-обработчики. `contracts/rest/openapi.yaml` в этом слайсе не редактировался — peering-протокол не REST, отдельный `.proto`.

---

## Scope of this slice / TODO (следующие слайсы)

### Сделано во втором слайсе

- ~~Сборка каскадов и failover~~ — `POST/GET/DELETE /cascades*` реализованы (см. выше). `PATCH /cascades/{id}` — всё ещё `501` (см. ниже).
- ~~Command dispatch Platform → Agent~~ — `SessionRegistry` + `dispatch_command()` реализованы (`grpc_server/servicer.py`).
- ~~Управление пользователями VPN~~ — `POST/GET/DELETE /users*` реализованы.
- ~~Ingestion телеметрии в `UsageStat`~~ — реализовано (`_handle_telemetry`).
- ~~Смена пароля администратора~~ — `POST /auth/change-password` реализован.

### Сделано в этом (третьем) слайсе

- ~~mTLS/step-ca для Agent↔Platform~~ — реализована in-process CA (`security/mtls_ca.py`), см. "Отклонения от ТЗ" п.9. Identity-биндинг ноды напрямую из клиентского сертификата — всё ещё суррогат через heartbeat, см. там же.
- ~~Тест-запросы по связке (ТЗ-5 §4.3)~~ — `POST /cascades/{id}/test`, `GET /tests/{id}`, `GET /cascades/{id}/tests` реализованы (`services/link_tests.py`).
- ~~`WS /events`~~ — реальный push через `services/event_bus.py`, см. выше.

### Сделано в четвёртом слайсе

- ~~REALITY-ключи для Hop A/Hop B~~ — реальные X25519-keypair'ы вместо жёстко пустых `""` (был жёсткий блокер, не косметика — см. "Отклонения от ТЗ" п.11).
- ~~`port`/`listen` в Hop A/Hop B inbound JSON~~ — второй, независимый жёсткий блокер, найденный при написании пошагового деплоя на два VDS: `agent/src/dist_vpn_agent/xray_client.py::_build_inbound_handler_config` считает `"port"` опциональным полем, а не обязательным — без него инбаунд молча остаётся без `port_list`, т.е. реально ничего не слушает. Добавлена константа `HOP_LISTEN_PORT = 443` (`xray_templates.py`), используется во всех трёх местах (Hop A inbound, Hop B outbound `vnext[].port`, Hop B inbound) и в `build_vless_uri`.
- ~~Готовая `vless://` ссылка для клиента~~ — `GET /users/{user_id}/connection/{cascade_id}`.
- ~~Публичный адрес entry-ноды~~ — `Node.public_address`, админ-задаваемый через `PATCH /nodes/{id}`.

### Сделано в пятом слайсе

- ~~Базовый конфиг Xray (`api`/`stats`/`policy`)~~ — пятый по счёту фундаментальный пробел этого проекта, найден и закрыт на Agent-стороне (`agent/src/dist_vpn_agent/xray_bootstrap.py`) — без него не работала ни новая функциональность этого слайса, ни уже существовавший (но фактически мёртвый) сбор трафика.
- ~~Ограничение количества устройств на пользователя~~ — `User.max_devices`, `TelemetryReport.user_online`, `_handle_user_online`/`_kick_user_devices` в `grpc_server/servicer.py`, `UserDeviceKickEvent` (аудит), `PATCH /users/{id}`, `GET /users/{id}/device-events`. См. "Отклонения от ТЗ" п.12 за честной оговоркой про per-node vs. глобальный лимит.
- ~~Мониторинг ресурсов vs. количество пользователей~~ — `NodeResourceSample` (временной ряд, один сэмпл на `Heartbeat`), `GET /nodes/{id}/resource-samples`. См. "Отклонения от ТЗ" п.12.

### Сделано в шестом слайсе

- ~~Автоматическая деградация статуса ноды/каскада — `HEALTHY → DOWN` по таймауту heartbeat~~ — `services/health_scheduler.py`, in-process `asyncio`, не ARQ/Redis. См. "Отклонения от ТЗ" п.13 за полным дизайном. `DEGRADED` по порогам ресурсов (cpu/mem/disk) по-прежнему НЕ реализован — см. TODO ниже, отдельная, не запрошенная в этом срезе задача.
- ~~Переключение при обрыве связки~~ — `POST /cascades/{id}/migrate-users`, явное действие администратора (выбор связки-замены не автоматизирован — намеренно, см. п.13).

### Сделано в седьмом слайсе (бэклог)

- ~~`GET /cascades/{id}/events`~~ — `501` → реально работает, плоский массив + фильтр `since`, ровно та форма, что уже была в контракте изначально (не items+total, как остальные аддитивные списки в этом файле).
- ~~`GET /usage`~~ — `501` → реально работает. `UsageStat`-строки уже сами по себе агрегат (почасовые бакеты, upsert-by-add при приёме телеметрии) — эндпоинт лишь фильтрует по `user_id`/`node_id`/`from`/`to`, без дополнительной агрегации поверх.
- ~~Найден и закрыт реальный баг из шестого слайса~~: `_handle_heartbeat` восстанавливал статус НОДЫ обратно в `HEALTHY`, но никогда не восстанавливал статус связанных с ней каскадов обратно в `UP` — каскад, помеченный `DOWN` планировщиком, оставался `DOWN` навсегда, даже когда entry-нода реально приходила в себя. Не было гипотезой — воспроизведено в тесте (`test_heartbeat_recovers_down_cascade_on_entry_node`) прежде чем чинить.
- ~~`type: "cascade"` WS-событие~~ — `services/event_bus.py::publish_cascade_event`, ранее задокументированный, но не реализованный TODO самого модуля. Публикуется при создании каскада, при DOWN (планировщик) и теперь при восстановлении в UP (см. пункт про баг выше) — во всех трёх местах, где меняется `Cascade.status`. Закрывает реальный, найденный на Admin-Client-стороне баг: новая связка не появлялась на графе топологии живьём, потому что `health_event`, который публиковался раньше, не несёт `entry_node_id`/`exit_node_ids` — клиенту физически не из чего было собрать форму новой связки, только полный `Cascade`-снэпшот подходит.

### Сделано в восьмом слайсе

- ~~REALITY-донор — общая константа на весь флот~~ — `REALITY_DONOR_POOL` + детерминированный выбор по каскаду/линку. См. "Отклонения от ТЗ" п.14.
- ~~P4-пресет (ТЗ-4 §3.3/§3.4)~~ — второй inbound (VLESS+WS+TLS) рядом с REALITY, `Node.p4_domain`, второй URI в ответе connection-эндпоинта. TLS-сертификат — по-прежнему ручное размещение оператором, не ACME. См. "Отклонения от ТЗ" п.14.
- ~~Клиентские routing-правила (bypass локальных ресурсов + блокировка IP-leak эндпоинтов)~~ — `build_client_routing_rules()`, новое поле `routing_rules` в connection-эндпоинте. См. "Отклонения от ТЗ" п.14.

### Явно НЕ входит в этот слайс (объявлено в роутере/протоколе с `501`/явным TODO)

- **`PATCH /cascades/{id}`** — `501`. Нужно диффить пул exit-нод и диспатчить только инкрементальные `AddOutbound`/`RemoveOutbound`, а не пересоздавать каскад целиком.
- **ARQ / Redis-подкреплённые фоновые задачи** — по-прежнему не подключены. `services/health_scheduler.py` (шестой слайс) закрывает "нужна хоть какая-то периодическая фоновая работа" минимальным `asyncio`-таском внутри самого Platform-процесса — этого достаточно для одного процесса, но НЕ масштабируется на несколько реплик (каждая реплика гоняла бы свой независимый цикл проверки, что для health-детекта безобидно, а для чего-то вроде "ротация секретов ровно один раз" уже нет). Плановая ротация секретов по расписанию (ТЗ-5 §4.5, `RotationRecord` есть в модели, ничего её не пишет/читает — сюда же естественно ляжет плановая ротация REALITY-ключей, сейчас они генерируются один раз при создании каскада и никогда не меняются) по-прежнему требует настоящего ARQ/Redis (или как минимум блокировки "выполняется только одной репликой"), не сделано.
- **`DEGRADED` по порогам ресурсов (cpu/mem/disk)** — `health_scheduler.py` реализует только `HEALTHY → DOWN` по таймауту heartbeat (шестой слайс), не деградацию по нагрузке. `NodeResourceSample` (пятый слайс) уже собирает нужные данные, порогового правила над ними ещё нет.
- **Автоматическое продление mTLS-клиентского сертификата Agent'а** (90 дней, см. "Отклонения от ТЗ" п.9) и **identity-биндинг ноды из клиентского сертификата** (сейчас — суррогат через heartbeat, то же примечание) — без изменений в этом слайсе.
- **TOTP-провижининг** — без изменений с первого слайса, см. "Отклонения от ТЗ" п.10.
- **Keep-alive `Ping`** от Platform к Agent по таймеру — по-прежнему не реализован (Platform отвечает на `Enrollment`/дожидается `CommandResult`, но никогда не инициирует `Ping` сама).
- **Многоузловой `WS /events`** — `services/event_bus.py` — in-process broadcast, тот же принцип (и то же ограничение), что и `SessionRegistry` ниже: подписчик видит только события, которые обработал ЕГО процесс.
- **Многоузловой Platform (несколько реплик за балансировщиком)** — `SessionRegistry` в `grpc_server/servicer.py` — в памяти одного процесса; `dispatch_command()` работает только если вызывающий REST-запрос обслуживается тем же процессом, что держит gRPC-сессию нужной ноды. Для нескольких реплик Platform понадобится вынести реестр в общий брокер (Redis pub/sub и т.п.) — сейчас предполагается один процесс на всё (см. `main.py`, `docker-compose.yml`), что соответствует текущему объёму задачи, но не масштабируется горизонтально как есть.
- ~~**REALITY `dest`/`serverName` не операторски настраиваемый**~~ — закрыто в восьмом слайсе, см. "Отклонения от ТЗ" п.14: `REALITY_DONOR_POOL` (список), выбор по каскаду/линку через детерминированный хэш, а не общая константа. Сам пул доменов по-прежнему не runtime-настраиваемый через API (только правка кода/константы) — если понадобится менять пул без деплоя, это отдельная задача.
- **P4: TLS-сертификат — ручное размещение, не ACME** — `P4_TLS_CERT_PATH`/`P4_TLS_KEY_PATH` (`xray_templates.py`) — фиксированные пути, которые оператор наполняет сам (`certbot`, Cloudflare Origin Certificate). Автоматизация через ACME-клиент на стороне Agent (открытый вопрос ещё в ТЗ-4 §3.4) не реализована. Соответствующей `provisioning/`-роли для размещения сертификата тоже пока нет — отдельная задача вне `platform/`.
- **Через `GET /cascades/{id}` наружу отдаётся только Hop A REALITY публичный ключ**, не Hop B (`CascadeExitNode.hopb_reality_public_key` — только в БД) — Hop B никому за пределами Platform/Agent не нужен, но если появится диагностический сценарий "проверить, что Hop B ключ реально совпадает на обеих нодах" через Admin Client, эндпоинта для этого пока нет.
- **`public_address` не валидируется** (не проверяется, что это похоже на IP/hostname, не проверяется, что нода вообще того же типа `role: entry`) — просто хранится как есть, отвечает `admin`.
- **Лимит устройств — per-node, не глобальный по всем каскадам/нодам пользователя** — см. "Отклонения от ТЗ" п.12 за полным обоснованием; настоящая глобальная агрегация потребовала бы общего кэша между процессами/нодами, которого сейчас нет.
- **`NodeResourceSample` без retention/pruning** — при дефолтном `heartbeat_interval_seconds=15` это ~5760 строк/ноду/день, растёт неограниченно. Первый кандидат на будущую ARQ-задачу (см. пункт про фоновые задачи выше), не решено в этом слайсе.
- **Кик по лимиту устройств не учитывает "мягкое" предупреждение** — превышение лимита сразу означает принудительный реконнект всех устройств этого пользователя на этой ноде, нет промежуточного шага "предупредить, но не отключать" — не запрашивалось, но реальный UX-вопрос для будущего слайса.
