# Provisioning

Полное ТЗ: [../docs/TZ-7-vds-provisioning.md](../docs/TZ-7-vds-provisioning.md).

Этот слайс (`provisioning/base-hardening-role`) реализует **базовую подготовку**
чистой Debian/Ubuntu VDS перед установкой [Agent](../docs/TZ-4-agent.md):
создание непривилегированного sudo-пользователя, SSH-хардненинг по Mozilla/CIS,
`ufw` (default-deny + только SSH), `fail2ban` поверх `ufw`,
`unattended-upgrades`, синхронизация времени (`chrony`), установка Docker CE из
официального репозитория Docker. Всё — через Ansible-плейбуки, не bash-скрипты
(см. [ТЗ-7 §2](../docs/TZ-7-vds-provisioning.md#2-требование-1-пользователя-production-ready-хранение-и-выполнение-скриптов)),
и никогда не через `curl | bash` (см. §2.2 там же).

## Структура

```
provisioning/
  ansible.cfg              — конфигурация (inventory по умолчанию, become, ssh)
  requirements.yml         — внешние коллекции (ansible.posix, community.general,
                              community.docker)
  site.yml                 — точка входа: применяет все роли по порядку
  inventory/
    example.ini            — документированный пример (НЕ реальные хосты)
  group_vars/
    all/
      main.yml              — общие несекретные переменные (provision_user, ...)
      vault.yml.example     — ФОРМА секретов (см. «Секреты и Vault» ниже)
    entry_nodes.yml         — role: entry
    exit_nodes.yml          — role: exit
  roles/
    base_hardening/         — пользователь, SSH, ufw, fail2ban, updates, chrony
    docker_install/         — Docker CE из официального apt-репозитория
    agent_install/          — Docker-контейнер Agent'а (сборка/pull образа,
                               enrollment через vault, проверка running)
  .gitignore                — паттерны, специфичные для provisioning (см. ниже)
  .ansible-lint, .yamllint  — конфигурация линтеров (см. «Что реально проверено»)
```

## Быстрый старт

1. Нужен **Linux/macOS/WSL control-машина оператора** — Ansible-controller не
   поддерживает нативный Windows (см. «Что реально проверено» ниже). Ansible
   ставится там же, где будет выполняться `ansible-playbook`, никогда на
   целевой VDS.
2. Установить Ansible (`>= 2.15`) и коллекции:
   ```bash
   cd provisioning
   python3 -m pip install --user ansible-core ansible-lint
   ansible-galaxy collection install -r requirements.yml
   ```
3. Создать свой инвентарь из документированного примера (реальные IP/hostname
   никогда не коммитятся — `inventory/*.ini` в `.gitignore`, кроме
   `example.ini`):
   ```bash
   cp inventory/example.ini inventory/hosts.ini
   # вписать реальный IP/hostname ноды в группу entry_nodes/exit_nodes
   ```
4. Один раз вручную подтвердить SSH host key новой VDS (Ansible намеренно НЕ
   отключает host key checking — см. `ansible.cfg`):
   ```bash
   ssh-keyscan -H <ip-новой-ноды> >> ~/.ssh/known_hosts
   ```
5. Настроить секреты — см. «Секреты и Vault» ниже — прежде чем что-либо
   применять к реальной ноде.
6. Запустить (первый прогон — от `root`, у хостера пока нет другого
   пользователя; см. «Идемпотентность и bootstrap» про то, почему это
   безопасно даже при отключении root-логина в этом же прогоне):
   ```bash
   ansible-playbook -i inventory/hosts.ini site.yml \
     -l vds-entry-01 -e role=entry \
     -e "provision_user_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
     --ask-vault-pass
   ```
   Для последующих прогонов на уже захарденной ноде — `ansible_user` в
   инвентаре указывает уже на `provision_user` (по умолчанию `deploy`, см.
   `group_vars/all/main.yml`), не на `root` (root-логин к этому моменту уже
   отключён).

Роль ноды (`entry`/`exit`) можно не передавать через `-e`, если хост уже лежит
в группе `entry_nodes`/`exit_nodes` инвентаря — `group_vars/entry_nodes.yml` и
`group_vars/exit_nodes.yml` задают `role` автоматически; `-e role=...`
переопределяет это явно (extra-vars в Ansible всегда приоритетнее group_vars).

С этого слайса playbook доходит и до `roles/agent_install` — потребуются ещё
как минимум (см. `roles/agent_install/defaults/main.yml` за полным списком и
«Agent: сборка образа» ниже за обоснованием):

```bash
ansible-playbook -i inventory/hosts.ini site.yml \
  -l vds-entry-01 -e role=entry \
  -e "provision_user_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
  -e agent_source_git_repo=git@github.com:<org>/dist-vpn.git \
  -e agent_source_git_ref=<тег-или-commit-sha-где-уже-есть-agent/Dockerfile> \
  -e agent_mtls_ca_cert_src=~/platform-ca.pem \
  --ask-vault-pass
```

(`agent_platform_address` / `agent_enrollment_token` — не через `-e`, а из
`group_vars/all/vault.yml`, см. «Секреты и Vault» ниже.)

## Секреты и Vault

Ничего секретного никогда не коммитится в открытом виде (ТЗ-7 §5). Форма
того, что должно быть зашифровано, — [`group_vars/all/vault.yml.example`](group_vars/all/vault.yml.example).
Реальный workflow:

```bash
cd provisioning
cp group_vars/all/vault.yml.example group_vars/all/vault.yml
# заполнить настоящими значениями (см. комментарии в файле — как
# сгенерировать ansible_become_password и его хэш)
ansible-vault encrypt group_vars/all/vault.yml
```

(либо сразу `ansible-vault create group_vars/all/vault.yml` и вставить форму
из `.example` внутрь редактора, который откроет команда). `group_vars/all/vault.yml`
(реальный, зашифрованный файл) — в `provisioning/.gitignore`, в репозиторий не
попадает никогда.

Пароль от Vault — **не в репозитории**:

- Интерактивно: `--ask-vault-pass` (используется в примере выше).
- Файлом: `--vault-password-file ~/.vault_pass.txt`, где сам файл — с правами
  `600`, вне репозитория. Паттерны `vault_pass.txt` и `*.vault-password` уже
  покрыты **корневым** `.gitignore` репозитория — проверено, не продублировано
  здесь намеренно (см. ТЗ-7 §2).
- В CI/CD: пароль передаётся как секрет пайплайна, пишется во временный файл
  на время выполнения и удаляется после (см. ТЗ-7 §2.1).

Публичный SSH-ключ оператора (`provision_user_ssh_public_key`) — **не
секрет** по своей природе (это открытый ключ), но всё равно не хранится в
репозитории как константа — передаётся явно через `-e` или `host_vars`,
потому что он специфичен для оператора/окружения, а не для кода.

## Идемпотентность и bootstrap

- Все задачи используют декларативные модули Ansible (`ansible.builtin.user`,
  `ansible.builtin.template` с `validate:`, `community.general.ufw`,
  `ansible.builtin.apt`, `ansible.builtin.systemd`, `ansible.builtin.apt_repository`
  и т.д.), **не** `shell`/`command`, — так что повторный прогон на уже
  захарденной ноде должен не менять состояние (все модули сравнивают текущее
  состояние с желаемым и репортят `changed=false`, если менять нечего).
- SSH-хардненинг — Jinja2-шаблон `roles/base_hardening/templates/sshd_hardening.conf.j2`,
  разворачивается как drop-in `/etc/ssh/sshd_config.d/99-hardening.conf`,
  каждое применение проверяется `sshd -t` (`validate:` в задаче) до того, как
  файл реально заменяется — синтаксически неверный конфиг никогда не попадёт
  в `/etc/ssh/`.
- Порядок задач в `roles/base_hardening/tasks/firewall.yml` намеренно такой:
  сначала политика `deny incoming` + все allow-правила (включая SSH),
  `ufw enable` — **в самом конце**. На первом прогоне (`ufw` ещё выключен) нет
  момента, когда правила уже «deny all», а SSH ещё не разрешён.
- Известный (стандартный для этого класса задач) нюанс bootstrap: первый
  прогон подключается как `root`, и в рамках **этого же прогона** роль
  создаёт `provision_user` И отключает `PermitRootLogin`/`PasswordAuthentication`.
  Это безопасно благодаря тому, как работает SSH ControlPersist
  (`ansible.cfg`: `ssh_args`) — уже установленное, аутентифицированное
  соединение не разрывается, когда `sshd` перезапускается или когда
  `PermitRootLogin` меняется; это влияет только на *новые* попытки
  подключения. Тем не менее: **не закрывайте текущую SSH-сессию/терминал**,
  пока не открыли отдельным терминалом новое подключение и не убедились, что
  `provision_user` действительно логинится и может `sudo` — это стандартная
  рекомендация для любого initial-server-setup такого рода, не специфика
  этого репозитория.
- Тот же нюанс касается смены `ssh_port` с 22 на нестандартный порт в одном
  прогоне с самим bootstrap'ом — при следующем запуске обновите
  `ansible_port` в инвентаре.

## Роль-специфичные firewall-порты (ТЗ-7 §3.2/§4.2)

`role_firewall_rules` (см. `roles/base_hardening/defaults/main.yml` за
формой и `roles/base_hardening/tasks/firewall.yml` за механизмом) теперь
заполнен по факту в `group_vars/entry_nodes.yml` / `group_vars/exit_nodes.yml`
— комментарии в этих двух файлах разбирают обоснование подробно, здесь —
только выжимка:

- **entry (Hop A)**: `443/tcp` разрешён. Число нигде в ТЗ (`TZ-3`, `TZ-4`)
  не зафиксировано дословно как "порт = 443" — оно выведено технически из
  `TZ-4-agent.md §5.1` ("обычные VLESS-порты, замаскированные под HTTPS") и
  из самой конструкции REALITY (проксирует настоящий TLS-хендшейк реального
  HTTPS-сайта, а те живут на 443 — иначе маскировка не работала бы перед
  ТСПУ). Если Agent в итоге поднимет несколько пресетов на разных портах
  одновременно — список нужно будет расширить, когда появится код Agent'а,
  который реально поднимает пресеты (на момент этого слайса — не
  существует, см. «Agent: сборка образа» ниже).
- **exit (Hop B backhaul)**: намеренно пуст. `TZ-4-agent.md §6` явно
  откладывает выбор транспорта (VLESS+REALITY chaining — вероятно tcp/443,
  как и Hop A, но на другой ноде — либо AmneziaWG — UDP, симметричная
  топология) до этапа реализации; эти два варианта дают разный протокол и
  разную топологию (нужен ли inbound на обеих сторонах или только на
  принимающей), так что заполнить одно число без угадывания невозможно.
- **mTLS control-plane порт (Agent↔Platform)**: НЕ нуждается в inbound
  allow-правиле ни на одной из ролей — Agent всегда сам инициирует
  исходящее соединение к Platform (`TZ-4-agent.md §5.1`, подтверждено
  `agent/src/dist_vpn_agent/config.py`: `platform_address` — куда Agent
  подключается, не то, что он слушает), а `ufw` уже по умолчанию разрешает
  весь `outgoing`. Развёрнутое обоснование — `group_vars/exit_nodes.yml`.

## Agent: сборка образа — откуда и почему

`roles/agent_install` разворачивает Agent как Docker-контейнер.
Ключевое решение и источники, на которые оно опирается:

- **Что реально существует на момент этого слайса**: у Agent'а есть
  реализованный control-plane gRPC-клиент, `Dockerfile` и `README.md` — но
  **на ветке `agent/grpc-client-skeleton`**, отдельной от
  `provisioning/base-hardening-role` (параллельный трек разработки, как и
  предупреждал прошлый слайс). На ЭТОЙ ветке каталог `agent/` на момент
  написания этой роли содержит только исходный ТЗ-стаб `README.md` — без
  `Dockerfile`, без кода. Это не оговорка "на будущее" —
  `roles/agent_install/tasks/image.yml` содержит явную проверку (`ansible.builtin.stat` +
  `assert` на `agent/Dockerfile` после клонирования), которая честно
  падает с понятным сообщением, если `agent_source_git_ref` указывает на
  версию репозитория без `agent/Dockerfile`, вместо непонятной ошибки
  `docker build`.
- **build_from_source vs registry**: эта роль по умолчанию
  (`agent_image_source: build_from_source`) клонирует репозиторий,
  закреплённый на конкретном теге/commit SHA (`agent_source_git_repo` /
  `agent_source_git_ref` — оба обязательны, роль явно падает в preflight,
  если не заданы; SHA/тег, не имя ветки — тот же запрет ТЗ-7 §2.2 на
  «последнюю версию ветки»), на целевой хост и собирает образ команда в
  команду с тем, что уже задокументировано в самом `agent/Dockerfile`
  («build context is the repository root... `docker build -f
  agent/Dockerfile -t dist-vpn-agent:dev .`», см. комментарий вверху
  файла) — контекст сборки обязан быть корнем репозитория, не `agent/`,
  потому что образу нужен доступ к `contracts/proto/agent_control.proto`,
  а Docker не умеет `COPY` за пределы своего контекста.
  Альтернатива — `agent_image_source: registry`, готовый образ из
  container-реестра — **сознательно не режим по умолчанию**: в проекте на
  данный момент нет ни CI/CD, публикующего образ Agent'а куда-либо, ни
  самого реестра/схемы тегов (в отличие, например, от
  `ghcr.io/xtls/xray-core`, который упоминается в `TZ-3-arhitektura-flota.md` как уже
  существующий референс). Задать `agent_registry_image` по умолчанию
  значило бы придумать несуществующий образ — то же нарушение принципа "не
  выдумывать факты", что и с номерами портов выше. `registry`-режим
  оставлен как явный opt-in для оператора, который сам подготовил и
  опубликовал образ. Когда у проекта появится реальный CI/CD +
  реестр — это единственное, что нужно будет поменять в этой роли,
  переключив умолчание.
- **Vault-переменные `agent_platform_address` / `agent_enrollment_token`**
  (`group_vars/all/vault.yml.example`) теперь реально читаются
  `roles/agent_install` (`tasks/preflight.yml` проверяет, что заданы и не
  остались `CHANGE_ME`; `tasks/run.yml` передаёт их в контейнер как
  `AGENT_PLATFORM_ADDRESS`/`AGENT_ENROLLMENT_TOKEN` через Docker Engine
  API — `community.docker.docker_container` использует `docker-py`, не
  шеллит `docker run -e ...`, так что токен не попадает в аргументы
  процесса на хосте/shell-историю). Честная оговорка: это НЕ скрывает
  значения от `docker inspect dist-vpn-agent` на самом хосте — Docker
  хранит резолвнутые env-переменные контейнера в открытом виде в своих
  метаданных независимо от способа передачи (`-e`, `--env-file` или API),
  это ограничение самого Docker. Доступ к `docker inspect` уже ограничен
  тем же доверенным периметром, что и доступ к хосту (root, `provision_user`
  из группы `docker`) — не новая поверхность атаки, но и не полная защита
  секрета в состоянии покоя. Более сильная защита (например, реальный
  secret-store с runtime-инъекцией) — вне рамок этого слайса.
- **mTLS**: `agent/src/dist_vpn_agent/config.py` требует
  `AGENT_MTLS_CA_CERT_PATH` на каждом запуске, если явно не включён
  `insecure_dev_mode` — `roles/agent_install` отражает это требование:
  `agent_mtls_ca_cert_src` (путь на control-машине к CA-сертификату
  Platform) обязателен, если оператор осознанно не установил
  `agent_insecure_dev_mode: true` (default `false`, никогда неявно).
  Клиентский сертификат/ключ ноды — опциональны (тот же bootstrap-нюанс,
  что и в `agent/README.md`: на первом запуске их ещё не существует, Agent
  проходит enrollment по токену). **Известное ограничение**, задокументированное
  в `roles/agent_install/tasks/run.yml`: bind-mount не транслирует
  UID/GID между хостом и контейнером, а Agent внутри контейнера работает
  под непривилегированным пользователем `agent` (`agent/Dockerfile: USER
  agent`) — значит, права `0600 root:root` на хосте сделали бы файл
  нечитаемым внутри контейнера. Для CA-сертификата это не проблема (это
  публичный якорь доверия, не секрет — `0644` осознанно достаточен). Для
  клиентского **ключа** это компромисс — `0644` вместо строгих `0600`,
  задокументированный, а не спрятанный. Практическое смягчение: в срезе
  `agent/grpc-client-skeleton` Agent ещё не персистит выданный сертификат
  автоматически (см. `agent/README.md`, «Замеченные вопросы к контракту»)
  — то есть на практике это поле сейчас почти всегда пустое, и проблема
  реально всплывёт только тогда, когда Agent научится сохранять сертификат
  между перезапусками (тогда и стоит решить её по-настоящему — например,
  именованный Docker volume с init-контейнером-`chown`, а не bind-mount).

## Что реально проверено в этой песочнице

Честно, без прикрас — то, что **не** было и не могло быть проверено
исполнением здесь:

- **`ansible-playbook` и `ansible-lint` не запускаются в этой sandbox-среде.**
  Это не «не хватило времени установить» — это фундаментальное ограничение:
  Ansible **официально не поддерживает Windows как control-машину** (только
  как managed-target через WinRM, никогда как controller). Практически это
  подтвердилось так: `ansible-core` (`pip install ansible-core`, версия
  2.17.14) на нативном Windows Python падает уже на `import ansible.cli` с
  `AttributeError: module 'os' has no attribute 'get_blocking'`
  (`ansible/cli/__init__.py` вызывает `os.get_blocking()` безусловно при
  импорте — POSIX-only вызов). После точечного monkeypatch этого вызова
  всплывает `ERROR: Ansible requires the locale encoding to be UTF-8` (легко
  чинится), а после и этого — `ModuleNotFoundError: No module named 'fcntl'`,
  и у `ansible-lint` отдельно `No module named 'grp'` — оба модуля целиком
  POSIX-only и глубоко зашиты в стек исполнения Ansible, а не что-то, что
  разумно шиммить дальше.
  - WSL в этой машине есть, но единственный установленный дистрибутив —
    внутренний `docker-desktop` (не предназначен для general-purpose
    использования) и он был остановлен; Docker Desktop daemon тоже не был
    запущен (`docker info` — `failed to connect to the docker API`). Ни
    поднимать системные сервисы, ни ставить полноценный Linux-дистрибутив в
    рамках этой задачи не делалось.
  - **Практический вывод**: перед применением к реальной production-ноде
    обязательно прогнать на настоящей Linux/macOS/WSL control-машине как
    минимум:
    ```bash
    ansible-playbook site.yml --syntax-check
    ansible-lint
    ansible-playbook -i inventory/hosts.ini site.yml -l <host> -e role=entry --check --diff
    ```
    и повторный прогон без `--check` дважды подряд, сравнив вывод второго
    прогона на предмет `changed=0` по всем задачам — это и есть проверка
    идемпотентности из [ТЗ-7 §7](../docs/TZ-7-vds-provisioning.md#7-критерии-приёмки).
- Что **было** проверено локально: каждый `.yml`/`.yaml` файл в этом каталоге
  прогнан через `yaml.safe_load` (синтаксис, дубликаты ключей), каждый `.j2`
  шаблон — через `jinja2.Environment().parse()` (синтаксис Jinja2), плюс
  структурная эвристика «каждая задача — словарь с ключом `name`». Всё прошло
  без ошибок. Это ловит опечатки/отступы/незакрытые кавычки, но **не**
  проверяет корректность имён модулей (FQCN), имён их параметров, реальное
  поведение на целевой ОС — то, что реально проверяет `ansible-lint`/
  `ansible-playbook --syntax-check`/фактический прогон.
- Модули и их параметры (`community.general.ufw`, `ansible.posix.authorized_key`,
  `ansible.builtin.apt_repository`, и т.д.) выверены вручную по памяти/опыту,
  без доступа к `ansible-doc` в этой среде (та же POSIX-проблема) — не
  претендую на 100% точность параметров без реального прогона; наиболее
  типичная точка риска — `ansible.builtin.apt_repository` (репозиторий
  Docker) и `community.general.ufw` (правила firewall). Этот слайс добавляет
  новую точку риска того же рода: `community.docker.docker_image` /
  `docker_container` / `docker_container_info` (`roles/agent_install`) —
  параметры (`build.dockerfile`, `restart_policy`, формат `volumes`/`env` и
  т.д.) выверены по памяти/документации библиотеки, без реального прогона
  и без доступа к `ansible-doc`/самому Docker daemon (недоступен в этой
  sandbox — см. ниже) для проверки.
- Docker-образ для мок-тестирования (например, через Molecule с
  `docker`-драйвером — стандартный инструмент для проверки idempotency роли,
  упомянутый в [ТЗ-7 §2.3](../docs/TZ-7-vds-provisioning.md#23-версионирование-и-ревью))
  не поднимался — Docker daemon недоступен в этой sandbox. Это же ограничение
  означает, что `roles/agent_install` (сборка образа, запуск контейнера,
  проверка `running`) не было исполнено ни разу целиком — только
  прочитано и синтаксически проверено, как и остальные роли.
- **Cross-branch зависимость, специфичная для этого слайса**: `agent/Dockerfile`
  и актуальный `agent/README.md` (переменные окружения, mTLS-конфигурация),
  на которые опирается `roles/agent_install`, реально существуют только на
  ветке `agent/grpc-client-skeleton` (параллельный трек разработки) — не на
  `provisioning/base-hardening-role`. Здесь, в этой ветке, `agent/` по-прежнему
  содержит только исходный ТЗ-стаб `README.md` без кода/Dockerfile (это НЕ
  изменялось этим слайсом — задача прямо запрещает трогать файлы вне
  `provisioning/`). Прочитаны и процитированы реальные файлы с ветки
  `agent/grpc-client-skeleton` (через соседний worktree
  `C:\access\dist-vpn-agent`), но эта роль ссылается на них только
  ФАКТИЧЕСКИ — через `agent_source_git_ref`, который оператор должен
  указать на коммит/тег, где они уже существуют (см. «Agent: сборка
  образа» выше и явную проверку в `roles/agent_install/tasks/image.yml`).
  До тех пор, пока ветки не смержены (или `agent_source_git_ref` не указан
  явно на `agent/grpc-client-skeleton`), запуск `agent_install` на реальной
  ноде упадёт на этой самой проверке — с понятным сообщением, не тихо.

Короче: **код написан и вручную вычитан внимательно, синтаксис YAML/Jinja2
подтверждён инструментально, но фактическое исполнение — включая
идемпотентность и реальный `docker build`/`docker run` — не подтверждено
запуском ни разу.** Это не должно применяться к реальной ноде без прогона
на настоящей control-машине (с Docker daemon) сначала.

## Scope of this slice / TODO

Прошлый слайс закрывал «Базовую подготовку»
([ТЗ-7 §4.1](../docs/TZ-7-vds-provisioning.md#4-функциональные-требования-к-процедуре-provisioning)).
Этот слайс закрывает **роль-специфичные firewall-порты** и **установку
Agent'а** ([ТЗ-7 §4.2](../docs/TZ-7-vds-provisioning.md#4-функциональные-требования-к-процедуре-provisioning)
и §4.3) — см. «Роль-специфичные firewall-порты» и «Agent: сборка образа»
выше за деталями того, что именно сделано и на каких источниках основано.

Явно НЕ входит в этот слайс (следующие слайсы provisioning):

- **Проверка результата — Agent HEALTHY в реестре Platform** (ТЗ-7 §4.4,
  критерий приёмки ТЗ-7 §7). `roles/agent_install/tasks/verify.yml`
  проверяет только, что Docker-контейнер Agent'а в состоянии `running` —
  это НЕ то же самое, что "Agent прошёл enrollment и Platform считает его
  `HEALTHY`". Настоящая проверка требует REST/WebSocket API Platform (см.
  [TZ-5-platform.md](../docs/TZ-5-platform.md)), которого пока не
  существует (параллельный трек разработки, ещё в стадии
  `platform/api-skeleton`). Когда API появится — это, вероятно, `post_tasks` в
  `site.yml` (или отдельный `verify.yml`/handler) с `ansible.builtin.uri`,
  опрашивающий статус ноды по `node_id`, полученному при enrollment'е, с
  ретраями и понятным сообщением о таймауте — это и есть
  финальный шаг приёмки всего провижининга (ТЗ-7 §7, последний пункт).
- **Ansible Molecule / прогон на реальной VDS** (ТЗ-7 §2.3) — по-прежнему
  не добавлено. Актуально уже для трёх ролей (`base_hardening`,
  `docker_install`, `agent_install`), и `agent_install` — первая роль,
  которая реально запускает Docker-контейнер (не просто ставит пакеты),
  так что порог "объём ролей вырос настолько, что ручной проверки
  недостаточно" (формулировка самого ТЗ-7 §2.3) стал заметно ближе, чем в
  прошлом слайсе. Кандидат на первоочередной пункт следующего слайса.
  Дополнительно: этот слайс ни разу не проверялся ни на реальной VDS, ни
  на локальном Docker-контейнере (Docker daemon недоступен в этой
  sandbox, см. «Что реально проверено» выше) — Molecule с `docker`-
  драйвером снял бы заодно и часть этого ограничения, раз он тоже
  работает через контейнеры.
- **SOPS+age** как альтернатива Ansible Vault (ТЗ-7 §2.1) — по-прежнему не
  реализовано. Актуальность растёт вместе с числом секретных значений
  (в этом слайсе добавился `agent_enrollment_token` — второй настоящий
  секрет в `vault.yml`, помимо пароля пользователя) — SOPS+age даёт
  читаемый построчный git-diff секретов (что именно изменилось), а Vault
  шифрует файл целиком. Остаётся самостоятельной будущей задачей, не
  блокирует текущий слайс (тот же вывод, что и в прошлый раз, — Vault
  по-прежнему устраивает «простой случай, один администратор» из
  формулировки самого ТЗ-7 §2.1).
- **Персист mTLS-сертификата, выданного в `EnrollmentAck`, и связанный
  UID/GID-компромисс для клиентского ключа** (см. «Agent: сборка образа»
  выше, раздел про mTLS) — заблокировано тем, что сам Agent
  (`agent/grpc-client-skeleton`) пока не сохраняет полученный сертификат
  на диск (задокументированный TODO апстрима, `agent/README.md`,
  «Замеченные вопросы к контракту»). Как только это появится в Agent'е —
  здесь нужно решить bind-mount/UID-проблему по-настоящему (например,
  именованный Docker volume с init-контейнером `chown` вместо текущего
  `0644`-компромисса).
- **Реальный `docker build`/`docker run` этой роли** — ни разу не
  выполнялся (Docker daemon недоступен в этой sandbox); в частности,
  проверка `agent/Dockerfile` через `ansible.builtin.git` +
  shallow-clone (`depth: 1`) конкретного commit SHA (не тега) может
  потребовать `uploadpack.allowReachableSHA1InWant` на git-сервере — не
  проверено практически, см. комментарий в
  `roles/agent_install/tasks/image.yml`.

## Соответствие критериям приёмки ТЗ-7 §7

| Критерий | Статус в этом слайсе |
|---|---|
| Секреты нигде в открытом виде в репозитории | Сделано — Vault-плейсхолдер (теперь реально используемый `agent_enrollment_token` в т.ч.), `.gitignore` дополнен; честно не проверено сканом истории git (нет реального удалённого git-хостинга для сканирования в этой песочнице) |
| `PermitRootLogin no`, `PasswordAuthentication no` после применения | Реализовано в `roles/base_hardening/tasks/ssh.yml` + шаблон; **не проверено фактическим SSH-подключением** — нет реальной VDS в этой песочнице |
| `ufw` активен, только нужные порты | Реализовано: SSH (все роли) + `443/tcp` Hop A (entry) — см. «Роль-специфичные firewall-порты» за обоснованием и честной оговоркой про то, что backhaul Hop B по-прежнему не заполнен (не наша забывчивость — решение отложено самим ТЗ-4 §6) |
| `fail2ban` активен и банит по неудачным SSH-попыткам | Реализовано (`jail.local`, backend systemd); **не проверено имитацией** — нет реальной VDS |
| Повторный прогон не меняет состояние (идемпотентность) | Модули выбраны с расчётом на идемпотентность (включая новые `community.docker.*` — `docker_image`/`docker_container` идемпотентны по конструкции библиотеки); **не подтверждено запуском** — см. «Что реально проверено» выше |
| После provisioning + установка Agent'а — Agent разворачивается как Docker-контейнер | Реализовано (`roles/agent_install`): образ собирается локально из закреплённой версии исходников, контейнер запускается с адресом Platform/enrollment-токеном из Vault, `verify.yml` подтверждает состояние `running`. **Не проверено запуском** — см. «Что реально проверено» |
| Agent появляется в реестре Platform со статусом `HEALTHY` | Вне рамок этого слайса — см. TODO выше (требует API Platform, которого ещё нет) |
| Ни один playbook не использует «скачать и сразу исполнить» | Выполнено по конструкции — Ansible push по SSH; на хост скачивается: apt-пакеты, GPG-ключ Docker (доверенный ключ репозитория, не исполняемый код), и — новое в этом слайсе — весь git-репозиторий через `ansible.builtin.git`, закреплённый на конкретном теге/commit SHA (не ветке), после чего `docker build` исполняется ЛОКАЛЬНО на уже доверенном (проверенным SSH-ключом) хосте из проверяемого содержимого — не `curl \| bash` откуда-то извне |

## Прочее

Критерии выбора самой VDS (провайдер/ASN/AUP) — отдельно, см.
[../docs/TZ-1-rossiyskiy-vds.md](../docs/TZ-1-rossiyskiy-vds.md), это решение
администратора вне рамок этого кода.
