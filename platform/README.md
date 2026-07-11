# Platform

Полное ТЗ: [../docs/TZ-5-platform.md](../docs/TZ-5-platform.md).

Кратко: Python 3.12+, FastAPI + SQLAlchemy 2.0 (async) + Alembic + PostgreSQL, ARQ для фоновых задач (пока не подключён — см. "Scope of this slice / TODO"), gRPC-сервер к Agent'ам по контракту [`../contracts/proto/agent_control.proto`](../contracts/proto/agent_control.proto), REST/WebSocket API для Admin Client по контракту [`../contracts/rest/openapi.yaml`](../contracts/rest/openapi.yaml).

Текущая ветка (`platform/api-skeleton`) — первый слайс: рабочий REST + gRPC каркас с полноценной аутентификацией администратора и enrollment-циклом ноды из §9 критериев приёмки. Остальное — см. раздел TODO ниже.

---

## Что это делает (этот слайс)

- REST API (`/api/v1/...`) на FastAPI: вход/обновление токена администратора, CRUD пула нод (список, выпуск enrollment-токена, детали/патч/вывод из эксплуатации).
- gRPC-сервер (`AgentControl.Session`, bidi-streaming) — принимает enrollment новых Agent'ов по одноразовому токену, обрабатывает heartbeat (перевод `ENROLLING → HEALTHY`, журналирование переходов в `HealthEvent`).
- Полная модель данных из ТЗ-5 §3 (SQLAlchemy 2.0 async) + Alembic-миграция.
- Argon2id-хеширование паролей, JWT access+refresh с ротацией и deny-list, per-IP (`slowapi`) и per-account rate limiting/lockout на `/auth/login`.
- Структурированное JSON-логирование с редактированием секретов.
- `docker-compose.yml` для локальной разработки (Postgres + сам сервис).

Остальные эндпоинты контракта (`/cascades/*`, `/users/*`, `/usage`, `/tests/{id}`, `WS /events`) объявлены в роутере (видна форма контракта), но возвращают `501 Not Implemented` с явным `# TODO` — см. ниже.

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

**16/16 тестов проходят.** Тесты используют `aiosqlite` (файловая SQLite-БД, создаётся и удаляется на каждый тест — см. `tests/conftest.py`), а не PostgreSQL — потому что не в каждом окружении, где может запускаться этот набор тестов, гарантированно доступен Postgres, а модели используют только переносимые типы SQLAlchemy (`String`/`DateTime`/`JSON`/`Enum(..., native_enum=False)` — см. `models/base.py::sa_enum`) специально ради этого. **В production используется исключительно PostgreSQL** (ТЗ-5 §2) — сама Alembic-миграция проверена отдельно против реального Postgres (см. раздел "Миграции" выше), тесты pytest проверяют слой приложения/ORM, а не DDL.

Состав:
- `tests/test_auth.py` — успешный/неуспешный вход, per-IP rate limiting (429 после порога), per-account lockout, ротация refresh-токена + отказ при повторном использовании (theft detection), logout.
- `tests/test_nodes.py` — список нод (пустой пул, авторизация обязательна), выпуск enrollment-токена, полный цикл GET/PATCH/DELETE ноды, 404 на несуществующую ноду.
- `tests/test_grpc_servicer.py` — unit-тесты обработчика gRPC (`Enrollment`/`Heartbeat`) напрямую (без реального gRPC-канала — сама транспортная часть сгенерирована `grpcio-tools`): валидный/невалидный/повторно использованный токен, переход `ENROLLING → HEALTHY` по heartbeat с записью `HealthEvent`.

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
2. **`POST /auth/logout` в `contracts/rest/openapi.yaml` не имеет `requestBody`** — в спеке (v0.1, черновик) описание "Отзыв текущего refresh-токена" не уточняет, как этот токен передаётся. Реализовано: требуется bearer access-токен (наследуется от глобального `security: bearerAuth`, путь его не переопределяет) **и** тело `{"refresh_token": "..."}` — иначе у stateless JWT-access-токена нет способа однозначно определить, какой именно refresh-токен отозвать. Контракт не менялся (файл не тронут) — это зона, требующая уточнения на PR-ревью между Platform и Admin Client командами.
3. **`TokenPair` в ответе `/auth/login`/`/auth/refresh` содержит дополнительное поле `must_change_password`**, которого нет в `components/schemas/TokenPair` в `openapi.yaml`. Это аддитивное расширение (схема не помечена `additionalProperties: false`, так что это обратно совместимо) — добавлено, чтобы поддержать требование ТЗ-5 §6.3 "обязательная смена дефолтного пароля при первом входе" уже сейчас, до появления отдельного эндпоинта смены пароля (которого пока нет в контракте — см. TODO ниже).
4. **mTLS между Agent↔Platform (ТЗ-5 §6.2) не реализован** — gRPC-сервер поднят на `insecure_server_credentials`, `EnrollmentAck.mtls_client_cert_pem` всегда пустой. Явно указано в задании как допустимое для этого слайса ("validate a fake/seeded enrollment token for now"); интеграция со `step-ca` — отдельная работа, требующая своего PKI-поднятия, вне рамок api-skeleton.
5. **TOTP/MFA (ТЗ-5 §6.3)**: реализована только верификация (`security/totp.py`, поле `AdminAccount.totp_enabled`/`totp_secret`, проверка кода при входе, если включена). Провижининг нового секрета (QR-код и т.п.) не реализован — в `contracts/rest/openapi.yaml` нет эндпоинта для этого; дефолтный администратор сидируется с `totp_enabled=False`. Задача была явно сформулирована как "implement the baseline: Argon2id, JWT rotation, rate limiting" — MFA сверх этого сделана насколько дёшево вписывалась, но полный флоу — TODO.

Ни один файл вне `platform/` не менялся; ни `contracts/proto/agent_control.proto`, ни `contracts/rest/openapi.yaml` не редактировались.

---

## Scope of this slice / TODO (следующие слайсы)

Явно НЕ входит в `platform/api-skeleton` (объявлено в роутере/протоколе с `501`/явным TODO, не молчком пропущено):

- **Сборка каскадов и failover (ТЗ-5 §4.2)** — `CascadeInput`/`Cascade` CRUD, трансляция в `AddOutbound`/`AddInbound`/`OverrideBalancer` команды Agent'ам. `POST/GET/PATCH/DELETE /cascades*` возвращают `501`.
- **Command dispatch Platform → Agent** — `grpc_server/servicer.py::dispatch_command` — заглушка, `NotImplementedError`. Нужен реестр открытых bidi-стримов по `node_id` (например, `asyncio.Queue` на ноду), которого пока нет — бессмысленно строить раньше, чем появится логика сборки каскадов, которая должна инициировать эти команды.
- **Управление пользователями VPN (ТЗ-5 §4.4)** — `POST/GET/DELETE /users*` — `501`; зависит от существования каскадов.
- **Учёт потребления (ТЗ-5 §4.4/§6.4)** — `GET /usage` — `501`; ingestion `TelemetryReport` в gRPC-сервисере пока только логируется (`_handle_telemetry_stub`), не пишется в `UsageStat`.
- **Тест-запросы по связке (ТЗ-5 §4.3)** — `POST /cascades/{id}/test`, `GET /tests/{id}` — `501`; `TestResult` в gRPC-сервисере только логируется (`_handle_test_result_stub`), не пишется в `LinkTest`.
- **`WS /events`** — маршрут существует, но соединение сразу закрывается с сообщением "не реализовано" — нет источника событий для трансляции (появится вместе с cascade-assembly/command-dispatch).
- **Ротация секретов (ТЗ-5 §4.5) и фоновые задачи через ARQ** — `RotationRecord` есть в модели данных, но ничего не пишет/не читает эту таблицу; ARQ/Redis не подключены вовсе (нет фоновых задач, которые бы их использовали).
- **mTLS/step-ca для Agent↔Platform (ТЗ-5 §6.2)** и **TOTP-провижининг (ТЗ-5 §6.3)** — см. "Отклонения от ТЗ" выше.
- **Keep-alive `Ping`** от Platform к Agent по таймеру, независимо от входящих сообщений — сейчас Platform отвечает только на `Enrollment` (через `EnrollmentAck`), на `Heartbeat` явного ответа не шлёт (это соответствует протоколу — `Heartbeat` не требует ack), но никогда не инициирует `Ping` сама.
- **Автоматическая деградация статуса ноды** (`HEALTHY → DOWN` по таймауту отсутствия heartbeat, `DEGRADED` по порогам ресурсов) — сейчас статус меняется только "по факту" получения heartbeat/decommission; нет фоновой проверки "нода замолчала N секунд назад".
