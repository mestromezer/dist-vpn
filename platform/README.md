# Platform

Полное ТЗ: [../docs/TZ-5-platform.md](../docs/TZ-5-platform.md).

Кратко: Python 3.12+, FastAPI + SQLAlchemy 2.0 (async) + Alembic + PostgreSQL, ARQ для фоновых задач (пока не подключён — см. "Scope of this slice / TODO"), gRPC-сервер к Agent'ам по контракту [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto), REST/WebSocket API для Admin Client по контракту [`../contracts/rest/openapi.yaml`](../contracts/rest/openapi.yaml).

Текущая ветка (`platform/api-skeleton`) — второй слайс: к REST+gRPC каркасу первого слайса (аутентификация администратора, enrollment-цикл ноды) добавлены реальный command dispatch Platform→Agent, сборка каскадов, управление пользователями VPN и приём телеметрии. Остальное — см. раздел TODO ниже.

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

Остальные эндпоинты контракта (`PATCH /cascades/{id}`, `GET /cascades/{id}/events`, `POST /cascades/{id}/test`, `GET /tests/{id}`, `GET /usage`, `WS /events`) по-прежнему объявлены в роутере, но возвращают `501 Not Implemented` с явным `# TODO` — см. ниже.

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
3. запуск HTTP (`:8000`) + gRPC (`:50051`) в одном процессе (`python -m dist_vpn_platform.main`).

Порты хоста переопределяемы через `PLATFORM_HOST_HTTP_PORT` / `PLATFORM_HOST_GRPC_PORT` / `PLATFORM_HOST_DB_PORT`, если 8000/50051/5432 заняты чем-то ещё на машине.

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

**38/38 тестов проходят.** Тесты используют `aiosqlite` (файловая SQLite-БД, создаётся и удаляется на каждый тест — см. `tests/conftest.py`), а не PostgreSQL — потому что не в каждом окружении, где может запускаться этот набор тестов, гарантированно доступен Postgres, а модели используют только переносимые типы SQLAlchemy (`String`/`DateTime`/`JSON`/`Enum(..., native_enum=False)` — см. `models/base.py::sa_enum`) специально ради этого. **В production используется исключительно PostgreSQL** (ТЗ-5 §2) — сама Alembic-миграция проверена отдельно против реального Postgres (см. раздел "Миграции" выше), тесты pytest проверяют слой приложения/ORM, а не DDL.

Состав:
- `tests/test_auth.py` — успешный/неуспешный вход, per-IP rate limiting (429 после порога), per-account lockout, ротация refresh-токена + отказ при повторном использовании (theft detection), logout (с телом и без), `POST /auth/change-password` (успех + отзыв старых refresh-токенов, неверный текущий пароль → 401, требует аутентификации).
- `tests/test_nodes.py` — список нод (пустой пул, авторизация обязательна), выпуск enrollment-токена, полный цикл GET/PATCH/DELETE ноды, 404 на несуществующую ноду.
- `tests/test_grpc_servicer.py` — unit-тесты обработчика gRPC (`Enrollment`/`Heartbeat`/`TelemetryReport`) напрямую (без реального gRPC-канала — сама транспортная часть сгенерирована `grpcio-tools`): валидный/невалидный/повторно использованный токен, переход `ENROLLING → HEALTHY` по heartbeat с записью `HealthEvent`, накопление `UsageStat` по часовым бакетам с резолвом `user_id`.
- `tests/test_grpc_dispatch.py` — live command dispatch: открывает фейковый `Session`-стрим (`tests/grpc_test_helpers.py`), проверяет, что `Command`, отправленный через `dispatch_command()`, реально приходит в этот стрим, а присланный в ответ `CommandResult` резолвит ожидающий `asyncio.Future`; отдельно — `NodeNotConnectedError` для неизвестной ноды и таймаут для неотвечающего Agent'а.
- `tests/test_cascades.py` — `POST /cascades` реально диспатчит `AddInbound`/`AddOutbound`/`AddRoutingRule` подключённым фейковым Agent'ам (`tests/grpc_test_helpers.py::FakeAgent`) и возвращает `status: up`; офлайн exit-нода → `status: degraded`; несуществующая entry-нода → 404; `DELETE` диспатчит `Remove*` и удаляет запись; `total` в списке.
- `tests/test_users.py` — `POST /users` генерирует `id`/`uuid` на сервере и диспатчит `AddUser` на entry-ноду каскада; `DELETE` диспатчит `RemoveUser` и помечает пользователя отозванным (`cascade_ids` пустеет); 404 на несуществующий каскад/пользователя.

---

## Переменные окружения

Полный список с описанием — `.env.example` в этой директории и `src/dist_vpn_platform/config.py`. Ключевые:

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `PLATFORM_DATABASE_URL` | Строка подключения к Postgres (asyncpg) | `postgresql+asyncpg://platform:platform@localhost:5432/platform` |
| `PLATFORM_HTTP_PORT` / `PLATFORM_GRPC_PORT` | Порты REST- и gRPC-серверов | `8000` / `50051` |
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
7. **Xray inbound/outbound/routing-rule JSON — не буквальная копия ранее проверенной конфигурации** (`services/xray_templates.py`). ТЗ-3 §0 говорит, что конкретные P1-P4 шаблоны и точная форма `balancer`/`observatory`/`fallbackTag` были "проверены ранее в проекте", но физически утрачены при сбое диска до появления этого репозитория — сохранилось только текстовое описание полей (ТЗ-3 §0: `realitySettings`, `network:"raw"/"xhttp"/"grpc"`, `password`=бывший `publicKey`; ТЗ-5 §4.2: "балансировщик с префиксным `selector`, `observatory`, `fallbackTag`"). Ни в `agent/`, ни в `provisioning/`, ни в `contracts/` нет буквального JSON — проверено. `services/xray_templates.py` — реконструкция формы полей по стандартным конвенциям Xray-core, согласованная с этим текстовым описанием, а не копия. Насчёт открытого вопроса — подхватывает ли префиксный `selector` балансировщика новый outbound автоматически без пересборки самого правила: это задокументированное поведение самого Xray (`selector: [string]` в `RoutingObject.balancers[]` — префиксное сопоставление с тегами **существующих на данный момент** outbound'ов, переоценивается на каждый выбор балансировщика, а не фиксируется один раз при создании правила), а не только предположение этого слайса — тот же факт уже был использован в более ранней части этого проекта (ротация Hop B через `rotate-hopb.sh`, где новый outbound с префиксным тегом подхватывался балансировщиком без правки `routing.balancers`). Эмпирически именно в ЭТОМ Python-кодовом слое (а не в прежнем bash-варианте) это по-прежнему не проверено — разница между "задокументированный факт про Xray" и "проверено в этой конкретной реализации" сохраняется как честная оговорка.
8. **Dispatch команд Platform→Agent — best-effort, не блокирует HTTP-запрос** (`api/command_dispatch.py`): если нода не подключена или не отвечает, `POST /cascades`/`POST /users`/`DELETE /cascades/{id}`/`DELETE /users/{id}` всё равно завершаются успешно (состояние в БД — источник истины), а `Cascade.status` отражает `degraded`/`down`. Решение принято по аналогии с уже существующим паттерном `decommission_node()` (`api/routes/nodes.py`), явного требования в задании на этот счёт не было — если желаемое поведение "выведенная из сети нода = запрос падает", это отдельное решение для следующего PR.
9. **mTLS между Agent↔Platform (ТЗ-5 §6.2) всё ещё не реализован** — gRPC-сервер на `insecure_server_credentials`, `EnrollmentAck.mtls_client_cert_pem` пустой. Как следствие, "переподключение по постоянному сертификату" (ТЗ-4 §5.2) реализовано как "первый `Heartbeat` на новом стриме с уже известным `node_id`, если `Enrollment` не отправлялся" (`grpc_server/servicer.py::Session`, ветка `kind == "heartbeat"`) — рабочий суррогат до появления настоящей mTLS-идентификации.
10. **TOTP/MFA (ТЗ-5 §6.3)**: без изменений с первого слайса — только верификация, провижининг нового секрета всё ещё не реализован.

Ни один файл вне `platform/` не менялся; ни `contracts/proto/agent_control.proto`, ни `contracts/rest/openapi.yaml` не редактировались.

---

## Scope of this slice / TODO (следующие слайсы)

### Сделано в этом слайсе (было TODO в первом слайсе, теперь реализовано)

- ~~Сборка каскадов и failover~~ — `POST/GET/DELETE /cascades*` реализованы (см. выше). `PATCH /cascades/{id}` — всё ещё `501` (см. ниже).
- ~~Command dispatch Platform → Agent~~ — `SessionRegistry` + `dispatch_command()` реализованы (`grpc_server/servicer.py`).
- ~~Управление пользователями VPN~~ — `POST/GET/DELETE /users*` реализованы.
- ~~Ingestion телеметрии в `UsageStat`~~ — реализовано (`_handle_telemetry`).
- ~~Смена пароля администратора~~ — `POST /auth/change-password` реализован.

### Явно НЕ входит в этот слайс (объявлено в роутере/протоколе с `501`/явным TODO)

- **`PATCH /cascades/{id}`** — `501`. Нужно диффить пул exit-нод и диспатчить только инкрементальные `AddOutbound`/`RemoveOutbound`, а не пересоздавать каскад целиком.
- **Тест-запросы по связке (ТЗ-5 §4.3)** — `POST /cascades/{id}/test`, `GET /tests/{id}` — `501`; `TestResult` в gRPC-сервисере коррелируется через тот же `SessionRegistry.resolve()`, что и `CommandResult` (механизм уже есть), но ни один REST-путь пока не вызывает `RunTest`, и `LinkTest`-строки не пишутся.
- **`GET /usage`** — `501`. `UsageStat` теперь реально наполняется (см. выше), но сам агрегирующий запрос (фильтры `user_id`/`node_id`/`from`/`to`) не написан.
- **`GET /cascades/{id}/events`** — `501`. Дёшево реализовать прямо сейчас (строки `HealthEvent` уже пишутся при создании/удалении каскада), но не запрашивалось в этом слайсе.
- **`WS /events`** — маршрут существует, но соединение сразу закрывается с сообщением "не реализовано". Нужен in-process (или Redis pub/sub) broadcast HealthEvent/Cascade/LinkTest-обновлений в реальном времени — ничего из этого не подключено.
- **ARQ / фоновые задачи** — Redis/ARQ не подключены вовсе. Нужны как минимум: (а) плановая ротация секретов по расписанию (ТЗ-5 §4.5, `RotationRecord` есть в модели, ничего её не пишет/читает), (б) любая периодическая фоновая работа вообще — сейчас Platform целиком реактивна (отвечает только на входящие gRPC/HTTP запросы).
- **Автоматическая деградация статуса ноды/каскада** (`HEALTHY → DOWN` по таймауту отсутствия heartbeat, `DEGRADED` по порогам ресурсов) — по-прежнему нет фоновой проверки "нода замолчала N секунд назад"; статус меняется только по факту входящего heartbeat. Это первая кандидатная задача для ARQ выше.
- **mTLS/step-ca для Agent↔Platform** и **TOTP-провижининг** — без изменений, см. "Отклонения от ТЗ" пп.9-10.
- **Keep-alive `Ping`** от Platform к Agent по таймеру — по-прежнему не реализован (Platform отвечает на `Enrollment`/дожидается `CommandResult`, но никогда не инициирует `Ping` сама).
- **Многоузловой Platform (несколько реплик за балансировщиком)** — `SessionRegistry` в `grpc_server/servicer.py` — в памяти одного процесса; `dispatch_command()` работает только если вызывающий REST-запрос обслуживается тем же процессом, что держит gRPC-сессию нужной ноды. Для нескольких реплик Platform понадобится вынести реестр в общий брокер (Redis pub/sub и т.п.) — сейчас предполагается один процесс на всё (см. `main.py`, `docker-compose.yml`), что соответствует текущему объёму задачи, но не масштабируется горизонтально как есть.
