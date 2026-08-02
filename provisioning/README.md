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
    platform_nodes.yml      — role: platform
  roles/
    base_hardening/         — пользователь, SSH, ufw, fail2ban, updates, chrony
    docker_install/         — Docker CE из официального apt-репозитория
    agent_install/          — Docker-контейнер Agent'а (сборка/pull образа,
                               enrollment через vault, проверка running)
    platform_install/       — стек Platform (docker compose: db+сервис),
                               Caddy (TLS reverse-proxy), сборка+деплой Admin
                               Client, забор CA-сертификата на машину
                               оператора — добавлен позже entry/exit, найдено
                               при реальном деплое (см. roles/platform_install/
                               defaults/main.yml за полным обоснованием: до
                               этого Platform разворачивался только вручную
                               по шагам deploy.md, невоспроизводимо)
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

`~/platform-ca.pem` в примере выше не появляется само — раньше это был
отдельный ручной шаг (см. «Bootstrap mTLS Agent↔Platform: операторский
workflow» ниже за историческим пошаговым описанием). С `roles/platform_install`
он появляется автоматически: сама роль забирает CA-сертификат на машину
оператора при первом деплое Platform (см. `platform_ca_cert_local_dest`,
по умолчанию `~/platform-ca.pem` — совпадает с тем, что ожидают команды выше).

### Platform — тот же поток, роль `platform`

Разворачивается ПЕРВОЙ (у Agent'а при enrollment нет ещё ни CA-сертификата, ни
enrollment-токена, взять их неоткуда, пока Platform не поднят):

```bash
ansible-playbook -i inventory/hosts.ini site.yml \
  -l vds-platform-01 -e role=platform \
  -e "provision_user_ssh_public_key=$(cat ~/.ssh/id_ed25519.pub)" \
  -e @platform-extravars.json \
  --ask-vault-pass
```

где `platform-extravars.json` (см. «⚠️ `-e "key=значение с пробелами"` теряет
хвост» ниже за тем, почему JSON-файл, а не инлайн `-e`):

```json
{
  "platform_source_git_repo": "git@github.com:<org>/dist-vpn.git",
  "platform_source_git_ref": "<тег-или-commit-sha>"
}
```

(`vault_platform_jwt_secret_key` / `vault_platform_postgres_password` — не
через `-e`, а из `group_vars/all/vault.yml`, см. «Секреты и Vault» ниже.
`platform_domain` — опционален, см. `roles/platform_install/defaults/
main.yml`: пусто = временный самоподписанный сертификат Caddy.)

После успешного прогона: enrollment-токены для entry/exit-нод выпускаются
через REST API Platform (`POST /api/v1/nodes`, см. `platform/README.md`), не
этим playbook'ом — provisioning entry/exit-нод и создание/раздача токенов
остаются раздельными операциями, как и раньше.

### ⚠️ `-e "key=значение с пробелами"` теряет хвост

Найдено при реальном деплое: `ansible-playbook -e "provision_user_ssh_public_key=$(cat
~/.ssh/id_ed25519.pub)"` (публичный SSH-ключ — строка вида `ssh-ed25519 AAAA... comment`,
с пробелами) незаметно обрезался ansible-core до первого пробела —
`ansible.posix.authorized_key` затем падал с невнятным `Module failed: list index out
of range` (кривой парсинг усечённого ключа), а не понятной ошибкой валидации. Инлайн
`-e "key=значение"` **безопасен только для значений без пробелов** (адреса, токены,
теги) — для всего остального (SSH-ключи и любые многословные значения) используйте
`-e @file.json`:

```bash
python3 -c 'import json; json.dump({"provision_user_ssh_public_key": open("'"$HOME"'/.ssh/id_ed25519.pub").read().strip()}, open("extravars.json", "w"))'
ansible-playbook ... -e @extravars.json ...
```

(файл с секретами/ключами — не коммитить; тот же принцип, что и для `vault.yml`.)

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

### ⚠️ Найдено при реальном bootstrap-прогоне на 3 новых VDS: ControlPersist не гарантирует, что весь `site.yml` доедет за один вызов

Абзац выше про ControlPersist («уже установленное соединение не разрывается»)
верен для задач, которые переиспользуют master-соединение — но подтверждено
не на всех платах ролей одинаково. Реальный прогон (роль `platform`, свежая
VDS): `base_hardening` + `docker_install` + первые задачи `platform_install`
(preflight, `apt install python3-docker`, `apt install git`) отработали
чисто за один вызов, ЧЕРЕЗ уже установленное root-соединение, несмотря на то
что `PermitRootLogin no` применился раньше в этом же прогоне — подтверждает
абзац выше. Но следующая задача (`ansible.builtin.git`, клонирование
исходников) вызвала `UNREACHABLE: Permission denied (publickey,password)` —
`ansible.builtin.git` открывает **отдельное** соединение (не через тот же
persistent control socket, что модули на базе `ansible.builtin.command`/
raw exec), и к этому моменту root уже реально не пускает. Причина именно в
конкретном модуле/типе задачи, а не в истечении ControlPersist по таймауту
(предыдущие задачи в том же прогоне отрабатывали за секунды до этой).

**Практический вывод**: не полагайтесь на «один прогон точно докатится до
конца» даже с учётом ControlPersist — по крайней мере `ansible.builtin.git`
(и, вероятно, другие задачи, открывающие соединение не через основной control
socket) может внезапно потребовать свежую SSH-сессию посреди уже
захарденного прогона. План на этот случай:

1. Первый прогон как `root` — ожидаемо может упасть где-то в середине
   (`agent_install`/`platform_install`) с `UNREACHABLE`. Это нормально, не
   повод паниковать — `base_hardening`/`docker_install` уже применились.
2. Повторный прогон с `-e ansible_user={{ provision_user }}` (по умолчанию
   `deploy`) докатывает оставшееся — все более ранние задачи идемпотентны и
   просто отрепортят `ok`.

### ⚠️ Fail2ban может забанить ВАС САМИХ во время этого двухфазного bootstrap'а

`base_hardening` включает `fail2ban` (sshd jail, `maxretry: 3` за
`findtime: 10m`, `bantime: 1h` — см. `defaults/main.yml`) **в том же самом
первом прогоне**, в котором ещё происходит сам bootstrap. Если после
`UNREACHABLE` из предыдущего пункта вы (или скрипт/агент) начинаете
диагностировать проблему несколькими ручными `ssh root@...`/`ssh deploy@...`
подряд (каждый получает `Permission denied`, потому что причина вовсе не в
угаданном пользователе, а в отключённом root/несинхронизированном ключе) —
это тоже засчитывается sshd-jail'ом как неудачные попытки, и через 3 подряд
IP оператора (или CI-раннера) банится на этой конкретной ноде на час.
Симптом бана — не `Permission denied`, а `kex_exchange_identification:
Connection closed by remote host` (соединение обрывается ДО стадии
аутентификации, ufw/iptables дропает пакеты). Подтверждено на практике этой
сессией (VDS-A). **Практический вывод**: если `UNREACHABLE`/`Permission
denied` — не тратьте попытки на ручную диагностику подряд, сразу проверьте
гипотезу №1 (сменился `ansible_user`) через ОДИН прогон; если и это не
помогло — остановитесь и подождите `fail2ban_bantime` (по умолчанию 1 час),
а не продолжайте пробовать разные варианты пользователя/ключа — каждая
попытка приближает к бану, а не отдаляет от него.

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
  системами DPI-инспекции трафика). Если Agent в итоге поднимет несколько пресетов на разных портах
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
  `agent_insecure_dev_mode: true` (default `false`, никогда неявно). Полный
  операторский workflow получения и распространения этого CA-сертификата —
  отдельный раздел ниже, «Bootstrap mTLS Agent↔Platform: операторский
  workflow».
  Клиентский сертификат/ключ ноды (`agent_mtls_client_cert_src`/
  `agent_mtls_client_key_src`) — опциональны и, начиная с этого слайса,
  **явно ограничены edge-case сценарием** (восстановление/миграция уже
  выданной идентичности ноды), а не часть обычного enrollment — см. тот же
  раздел ниже, подраздел «`agent_mtls_client_cert_src`/`key_src` — зачем они
  теперь нужны», за полным обоснованием этого вывода.
  **Известное ограничение**, задокументированное в
  `roles/agent_install/tasks/run.yml`: bind-mount не транслирует UID/GID
  между хостом и контейнером, а Agent внутри контейнера работает под
  непривилегированным пользователем `agent` (`agent/Dockerfile: USER
  agent`) — значит, права `0600 root:root` на хосте сделали бы файл
  нечитаемым внутри контейнера. Для CA-сертификата это не проблема (это
  публичный якорь доверия, не секрет — `0644` осознанно достаточен). Для
  клиентского **ключа** это компромисс — `0644` вместо строгих `0600`,
  задокументированный, а не спрятанный — но теперь известно, что он
  актуален только для edge-case пути (см. выше), а не для основного потока,
  так что именованный Docker volume с init-контейнером-`chown` (упомянутый
  как возможное решение в прошлом слайсе) имеет смысл только если/когда
  сам edge-case станет частым сценарием на практике — не приоритет сейчас.

## Docker-доступ Agent'а: docker-socket-proxy, а не прямой bind-mount сокета

**Найдено при написании пошагового деплоя на два VDS**, не было учтено при
первом написании `agent_install`: `agent/docker_manager.py` управляет
соседним контейнером Xray-core через Docker Engine API
(`docker.from_env()`) — Agent'у внутри его собственного контейнера нужен
доступ к Docker. Более ранний слайс `agent/` (см. `docker_manager.py`,
докстринг модуля) явно решил **не** контейнеризовать Agent именно затем,
чтобы не монтировать хостовый Docker-сокет в контейнер — но эта роль его
как раз контейнеризует, и это расхождение не было замечено раньше, потому
что без реального сквозного теста ничего не ломалось явно: `docker_manager.py`
просто не мог реально создать контейнер Xray без доступа к сокету, и
RunTest/сама работа прокси никогда не были бы работоспособны в проде.

Прямой `bind-mount /var/run/docker.sock:/var/run/docker.sock` решил бы
задачу, но дал бы скомпрометированному Agent-контейнеру root-эквивалентный
доступ ко **всему** хосту — не только к управлению одним Xray-контейнером.
Важное уточнение (важнее, чем «контейнер или нет»): тот же риск существовал
бы и без контейнеризации Agent'а, если бы сам процесс на хосте имел прямой
доступ к сокету/группе `docker` — дело не в том, контейнер это или обычный
процесс, а в том, сколько прав реально нужно процессу для его задачи.

Решение — **docker-socket-proxy** ([Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy),
активно поддерживаемый HAProxy-based прокси, стандартный паттерн для «дать
контейнеру ограниченный Docker API без полного сокета»,
[документация по фильтрации по категориям API](https://github.com/Tecnativa/docker-socket-proxy#grant-or-revoke-access-to-certain-api-endpoints)),
как sidecar (`roles/agent_install/tasks/run.yml`, `defaults/main.yml`
`agent_docker_proxy_*`):

- Реальный `/var/run/docker.sock` смонтирован (read-only) **только** в
  прокси-контейнер, никогда в Agent.
- Прокси разрешает только `CONTAINERS=1`, `IMAGES=1`, `POST=1` (создание/
  старт/инспект контейнеров, pull образов) — deny-by-default для всего
  остального (`EXEC`, `VOLUMES`, `NETWORKS`, `SWARM`, `SECRETS`, `SERVICES`,
  `NODES`, `PLUGINS`, `SYSTEM`, `AUTH`, `BUILD`, `COMMIT`, ...).
- Agent и прокси сидят в общей приватной (`internal: true`) Docker-сети,
  без публикации портов прокси на хост.
- `docker.from_env()` (`docker_manager.py`) нативно читает переменную
  окружения `DOCKER_HOST` — эта роль просто передаёт
  `DOCKER_HOST=tcp://<имя-прокси-контейнера>:2375` в Agent-контейнер,
  **изменений в коде Agent'а не потребовалось**.

**Честная оговорка, не спрятанная**: Tecnativa-прокси не умеет ACL по имени
конкретного контейнера «из коробки» — `CONTAINERS=1` разрешает операции над
**любым** контейнером на хосте, не только над управляемым Agent'ом Xray.
То есть скомпрометированный Agent всё ещё может создать/запустить/
остановить произвольный контейнер (не задев volumes/networks/secrets/exec
других контейнеров) — существенно уже прямого доступа к сокету, но не
идеальная изоляция «только этот контейнер». `agent_docker_proxy_image`
(`tecnativa/docker-socket-proxy:latest`) — та же самая, не устранённая в
этом слайсе проблема незакреплённого тега, что и у `agent_registry_image`
(нет собственного CI/CD, публикующего закреплённую версию) — оператор
должен закрепить версию сам перед production-раскаткой.

## Bootstrap mTLS Agent↔Platform: операторский workflow

Этот раздел описывает **полный** операторский путь от чистого деплоя
Platform до работающего Agent'а с настоящим mTLS — то, чего не хватало в
прошлом слайсе (там `agent_mtls_ca_cert_src` уже существовал как
переменная, но не было описано, откуда оператор физически берёт файл,
который в неё передать).

**Важная честная оговорка перед началом**: код, который реализует это
(генерация CA на стороне Platform; генерация keypair/CSR и персист
сертификата на стороне Agent), пишется **в этом же раунде параллельными
сессиями** — `platform/` и `agent/` — и **не смержен** в эту ветку
(`provisioning/base-hardening-role`). В этой ветке `platform/` по-прежнему
содержит только исходный ТЗ-стаб `README.md` (см. `platform/README.md` —
прочитан для этого слайса, но там нет ничего, кроме ссылок на ТЗ-5).
Дизайн ниже — это **согласованный контракт**, который обе параллельные
сессии реализуют в этом раунде (передан как обязывающая вводная для этого
слайса), не результат чтения реального кода. Там, где это дизайн, а не
подтверждённый факт, это явно помечено.

### Шаги

1. **Развернуть Platform.** Вне рамок `provisioning/` в этом слайсе — у
   Platform пока нет собственной provisioning-роли (сам `platform/`, как и
   написано выше, на момент этого слайса ещё ТЗ-стаб без кода). Когда
   появится — это, вероятно, `roles/platform_install`, тем же паттерном,
   что и `roles/agent_install` (Docker-контейнер, секреты через Vault).
2. **Найти и скопировать CA-сертификат, который Platform сгенерировала при
   первом старте.** По согласованному дизайну этого раунда: при первом
   запуске Platform генерирует собственный self-signed CA (см.
   `docs/TZ-5-platform.md` §6.1/6.2 — CA на базе
   [step-ca/smallstep](https://github.com/smallstep/certificates)) и
   персистит его в своём хранилище; публичный сертификат CA (PEM)
   записывается по предсказуемому пути, и Platform **один раз, при первой
   генерации**, логирует сообщение, предлагающее оператору скопировать
   этот файл для распространения на Agent-ноды — тот же паттерн, что уже
   существующий (по ТЗ-5 §6.1, таблица OWASP A05) флоу "случайный пароль
   администратора при первом деплое, залогированный один раз". **Точный
   путь файла и точный формат лог-сообщения в этой сессии не проверены**
   (не видны — код Platform реализуется параллельной сессией и не смержен
   сюда). Прежде чем полагаться на конкретный путь на реальном деплое —
   свериться с `platform/README.md`, когда тот слайс будет смержен, и при
   необходимости поправить этот шаг здесь.
3. **Скопировать файл на control-машину оператора** (ту же машину, что
   запускает `ansible-playbook`) и указать путь к нему через
   `agent_mtls_ca_cert_src` — так же, как уже описано в «Быстрый старт»
   выше (`-e agent_mtls_ca_cert_src=~/platform-ca.pem`). Это не секрет
   (публичный сертификат CA, не приватный ключ), но он специфичен для
   конкретного деплоя Platform — так что, как и
   `provision_user_ssh_public_key`, не хранится в репозитории как
   константа; для нескольких нод удобнее задать его один раз в
   host_vars/group_vars оператора (вне репозитория, тот же принцип, что и
   с инвентарём — см. «Секреты и Vault»), а не повторять в каждой `-e`.
4. **Запустить `agent_install`** (пример — в «Быстрый старт» выше). Роль
   копирует CA-сертификат на целевую ноду
   (`roles/agent_install/tasks/run.yml`) и монтирует его в контейнер
   Agent'а как якорь доверия (`AGENT_MTLS_CA_CERT_PATH`). Дальше Agent сам
   генерирует локальный keypair, формирует и отправляет CSR при
   enrollment, и сам персистит сертификат, который вернёт Platform — эта
   часть, по согласованному дизайну этого раунда, целиком внутри процесса
   Agent'а и **не требует новой provisioning-плумбинга** с этой стороны
   (см. подраздел ниже за тем, что это означает для
   `agent_mtls_client_cert_src`/`key_src`).

### `agent_mtls_client_cert_src`/`key_src` — зачем они теперь нужны

Прошлый слайс уже держал эти два поля опциональными, но по временной
причине: на тот момент Agent (`agent/grpc-client-skeleton`) ещё не умел
сохранять сертификат, выданный в `EnrollmentAck`, на диск (открытый вопрос
апстрима) — комментарии формулировали это как «пока не реализовано»,
подразумевая, что проблема появится позже, когда Agent научится это
делать.

Согласованный дизайн bootstrap этого раунда закрывает этот вопрос
определённо, а не откладывает его: при штатном enrollment Agent **сам**
генерирует локальный keypair, **сам** формирует и отправляет CSR
Platform, и **сам** персистит сертификат, который получит в ответ —
целиком внутри собственного процесса/контейнера, без файла, который
Ansible должен был бы туда заранее положить. Из этого следует прямой
вывод: **`agent_mtls_client_cert_src`/`agent_mtls_client_key_src` — не
часть обычного (штатного) флоу и никогда не должны заполняться при
развёртывании новой ноды с нуля.** Это не наблюдение "пока нечего туда
положить, потому что Agent не готов" — это постоянное свойство дизайна.

Поля оставлены в `defaults/main.yml`/`tasks/preflight.yml`/`tasks/run.yml`
(не удалены) ради edge case, где они по-прежнему полезны — оператор
вручную переносит УЖЕ существующую идентичность ноды, минуя новый
enrollment:

- восстановление ноды из бэкапа/образа диска, когда Platform уже выдала
  сертификат этому `node_id` — новый enrollment означал бы либо трату
  свежего одноразового токена, либо (хуже) дублирующую запись `Node` в
  реестре Platform для той же физической ноды;
- ручная миграция контейнера Agent'а на пересозданный VDS с сохранением
  прежней идентичности перед Platform (тот же `node_id`, тот же
  сертификат);
- ручное тестирование/отладка mTLS-цепочки с заранее подготовленным
  сертификатом, вне обычного потока enrollment.

Во всех этих случаях сертификат/ключ существуют вне Agent'а
(экспортированы заранее или из бэкапа), и оператор передаёт их роли явно
через эти два поля. `tasks/preflight.yml` по-прежнему проверяет, что оба
заданы вместе или ни один — половинчатая пара (сертификат без ключа или
наоборот) не образует рабочую TLS-идентичность независимо от того, штатный
это путь или edge-case.

**Открытый вопрос, оставленный на следующий слайс, а не решённый здесь**:
если Agent персистит СВОЙ собственный сгенерированный сертификат внутри
контейнера (а не через bind-mount, которым управляет эта роль), эти
данные переживут `docker_container: state: started` с тем же образом, но
могут не пережить полную рекреацию контейнера (новый образ,
`docker_container` с `state: absent` → `started`, замена ноды), если
Agent не пишет их в путь, отдельно смонтированный как именованный Docker
volume. Эта роль на данный момент **не** заводит такой volume — по прямой
инструкции для этого слайса ("none of that needs new provisioning-side
plumbing"), и потому что точный путь, который Agent использует для
персиста, не виден из этой сессии (код на ветке `agent/`, не смержен).
Стоит перепроверить это предположение и, если нужно, завести именованный
volume под путь персиста Agent'а, как только код Agent'а из этого раунда
будет смержен и станет видно, что именно и куда он пишет.

## Control-plane через REALITY, зачем

Найдено при реальном деплое, не спроектировано заранее: entry-нода в РФ
(VDS-RU) не могла удержать соединение с control-plane Platform (50051/50052)
несмотря на то, что и mTLS, и firewall-правила были корректны. Диагностика —
`tcpdump` на Platform-ноде во время реального `curl`/`openssl s_client` с
российской entry-ноды — показала: TCP-хендшейк (SYN/SYN-ACK/ACK) проходит
идеально каждый раз, но ни один байт TLS ClientHello ни разу не долетел до
Platform ни на одной попытке (443 и 50052 дважды). Это классическая сигнатура
активного DPI (ТСПУ): хендшейк пропускают, режут именно первый пакет с
данными, где виден SNI/сертификат — self-signed сертификат control-plane
канала к тому же сам по себе похож на паттерн, по которому такое фильтруют.

Обычный домен + Let's Encrypt здесь ненадёжен: по состоянию на середину 2026
ТСПУ частично перешёл на поведенческий fingerprinting, а не только проверку
SNI/сертификата, так что "просто настоящий сертификат" не гарантирует
проход. Вместо этого control-plane трафик entry-нод в РФ заворачивается в тот
же VLESS+REALITY, что уже доказанно работает для пользовательского
data-plane (`platform/src/dist_vpn_platform/services/xray_templates.py`,
`security/reality_keys.py`) — REALITY делает настоящий TLS-хендшейк с
настоящим сторонним сайтом (донор из `platform_control_gateway_donor`/
`agent_control_plane_tunnel_donor`, по умолчанию `img.yandex.ru`), так что
для DPI это неотличимо от обычного HTTPS к этому сайту.

**Голого REALITY оказалось недостаточно.** Даже с настоящим хендшейком к
реальному донору TCP-хендшейк проходил, но ClientHello всё равно не доходил
до Platform — тот же симптом, что и без REALITY вообще. Решение нашлось
только с фрагментацией: `freedom`-outbound с `settings.fragment` (`packets:
"tlshello"`, `length: "2-10"`, `interval: "10-20"`), подключённый к основному
VLESS+REALITY outbound'у через `streamSettings.sockopt.dialerProxy` —
официальный, задокументированный Xray-паттерн именно для этого случая
(<https://xtls.github.io/en/config/outbounds/freedom.html>,
<https://xtls.github.io/en/config/transports/sockopt.html>). Раздробленный на
мелкие TCP-фрагменты ClientHello доходит и разбирается control-gateway'ем без
ошибок — подтверждено `tcpdump`-захватом на живом VDS-A (фрагменты видны как
несколько мелких PSH-пакетов вместо одного) и логами Xray
(`dialing to tcp:127.0.0.1:50051` → `connection opened`, т.е. реально
доходит до gRPC-порта Platform).

### Архитектура

- **Platform-нода** (`roles/platform_install/tasks/control_gateway.yml`):
  статический (не управляемый через gRPC AddInbound — он и есть тот канал,
  который защищает) Xray-инстанс, REALITY-inbound на публичном 443. Routing
  ограничивает авторизованного клиента ТОЛЬКО адресами
  `127.0.0.1:50051`/`127.0.0.1:50052` (свои же gRPC-порты Platform) — не
  открытый прокси наружу.
- **Entry-нода** (`roles/agent_install/tasks/control_plane_tunnel.yml`):
  сайдкар-контейнер с двумя `dokodemo-door` inbound'ами на loopback
  (15051/15052) + один VLESS+REALITY outbound (с `fragment`-цепочкой через
  `dialerProxy`) до control-gateway'я.
  `network_mode: "container:{{ agent_container_name }}"` — сайдкар делит
  сетевой неймспейс с самим Agent'ом (не файловую систему), так что
  `127.0.0.1:15051` реально видим из Agent'а как его собственный loopback.
  `AGENT_PLATFORM_ADDRESS` автоматически переопределяется на
  `127.0.0.1:15051` в `tasks/run.yml`, когда туннель включён — Agent-код
  не менялся вообще, используется уже существующая конвенция
  `resolved_platform_enrollment_address` (port+1 для enrollment-порта).

Официальная документация полей: <https://xtls.github.io/en/config/transports/reality.html>
(REALITY) и <https://xtls.github.io/en/config/inbounds/dokodemo.html> (dokodemo-door).

### Ещё одна найденная гоча: non-root образ Xray-core на привилегированном порту

Официальный образ `ghcr.io/xtls/xray-core` — distroless, работает под UID 65532
(не root, без shell). Два независимых следствия, оба закрыты в
`control_gateway.yml`/`control_plane_tunnel.yml`:
1. Конфиг-файл на хосте должен быть `chown`'нут на этот UID (bind-mount не
   транслирует владельца между хостом и контейнером) — иначе
   `open /etc/xray/config.json: permission denied` в цикле рестартов.
2. Bind на порт 443 (control-gateway) падает `permission denied` под non-root
   UID даже с `network_mode: host`. Точечная `docker_container: capabilities:
   [NET_BIND_SERVICE]` не сработала сама по себе на живом тесте (не выяснено
   до конца, почему) — рабочий фикс — `ansible.posix.sysctl`:
   `net.ipv4.ip_unprivileged_port_start` (персистентно, через
   `/etc/sysctl.d/`), стандартный приём для non-root Xray/nginx на 443.

### Включение

Оба флага — явный opt-in (`false` по умолчанию), тот же принцип, что и
`agent_insecure_dev_mode`:

```
platform_control_gateway_enabled: true       # на Platform-ноде
agent_control_plane_tunnel_enabled: true     # на КОНКРЕТНОЙ entry-ноде в РФ
```

Ключи — один общий комплект на весь control-gateway (см. честную оговорку в
`roles/platform_install/defaults/main.yml`, `platform_control_gateway_client_uuid`),
генерируются один раз тем же кодом, что и REALITY-ключи пользовательских
каскадов Platform:

```bash
cd platform && python3 -c "
import sys; sys.path.insert(0, 'src')
from dist_vpn_platform.security.reality_keys import generate_reality_keypair, generate_short_id
priv, pub = generate_reality_keypair()
print(f'private={priv}\npublic={pub}\nshort_id={generate_short_id()}')"
python3 -c "import uuid; print(uuid.uuid4())"
```

Приватный ключ → `vault_control_gateway_reality_private_key` (только Platform),
публичный ключ/shortId/uuid → те же значения на entry-ноде как
`vault_control_gateway_reality_public_key`/`vault_control_gateway_reality_short_id`/
`vault_control_gateway_client_uuid` — см. `group_vars/all/vault.yml.example`.

### Побочный эффект: Caddy уходит на loopback

`platform_control_gateway_enabled: true` забирает у Caddy публичный 443 (REALITY
должен слушать именно его — нестандартный порт сам по себе сигнал для
поведенческого DPI). Caddy переключается на
`127.0.0.1:{{ platform_caddy_bind_port }}` (по умолчанию 8443) — доступ к Admin
Client/REST API снаружи только через SSH-port-forward оператора:

```bash
ssh -L 8443:127.0.0.1:8443 <алиас Platform-хоста>
# затем https://localhost:8443 в браузере, пока сессия открыта
```

Осознанное дополнительное ужесточение, не только следствие занятого порта:
без домена ACME (порт 80) и так не используется (`tls internal`), так что
публичная экспозиция Caddy раньше давала только доступ к самой админке — а
она и так чувствительный компонент (полный контроль над флотом).

### ⚠️ Открытая проблема: устойчивый TLS-in-TLS поток из РФ

**Статус на конец этой сессии: найден и исправлен реальный баг конфигурации
(рассинхрон `flow` между клиентом и gateway'ем — см. ниже), исправление
формализовано в шаблонах и подтверждено байт-в-байт идентичным рендерингом
через Jinja2. Но после фикса enrollment с реальной entry-ноды в РФ (VDS-RU)
ВСЁ РАВНО не проходит стабильно — глубинная причина не в коде, а в активном
вмешательстве ТСПУ в уже установленное REALITY-соединение, не только на этапе
ClientHello.** Задокументировано подробно, чтобы не потерялось — следующая
сессия должна начать отсюда, а не переоткрывать то же самое заново.

**Найденный и исправленный баг (реальный, не гипотеза):** ошибка
`"account ... is rejected since the client flow is empty"`, которую эта сессия
долго пыталась лечить через XTLS Vision, оказалась **рассинхроном значения
`flow` между `control-plane-tunnel-config.json.j2` (клиент) и
`control-gateway-config.json.j2` (gateway)** — оба файла в git ВСЕГДА были
`flow: ""` (согласованы), но живые конфиги на хостах разъехались в ходе ручной
отладки предыдущих сессий (на gateway'е вручную выставлялся то `""`, то
`"xtls-rprx-vision"`, без синхронной правки клиента). Решающий тест: на VDS-B
(нет цензуры) byte-identical конфиг с `dialerProxy`+`fragment`+
`flow: "xtls-rprx-vision"` на ОБЕИХ синхронных сторонах отработал чисто
(`firstLen=1184`, `CopyRawConn splice`) — то есть Vision+dialerProxy+
dokodemo-door НЕ конфликтуют технически, вопреки более ранней гипотезе этой же
сессии (см. ниже, "что оказалось неверным"). Когда на VDS-RU обе стороны
синхронизировали на `flow: ""` — VLESS-уровень тоже стабильно принимал
enrollment-трафик (`firstLen=276`, `accepted ... taking detour
to-platform-loopback`). **Vision не нужен — синхронный `flow: ""` на обеих
сторонах и есть верная продовая конфигурация**, что и закреплено в шаблонах
сейчас. Живые конфиги на VDS-A и VDS-RU передеплоены точным рендерингом
шаблонов (Jinja2, все переменные — из `defaults/main.yml` и vault), чтобы
устранить любой будущий дрейф между git и хостом.

**Что оказалось неверным в более ранних версиях этого раздела (для истории,
чтобы не повторить тот же тупик):** гипотеза "`dialerProxy` не форвардит
Vision-маркеры" и "`dokodemo-door` не даёт Vision сырой сокет" — обе
опровергнуты A/B-тестом на VDS-B (Vision+dialerProxy+dokodemo-door работает
чисто без цензуры). Двухступенчатая SOCKS-релейная архитектура (dokodemo-door →
SOCKS → VLESS+Vision), собранная как попытка обойти "проблему dokodemo-door",
тоже не помогла — и не могла, т.к. проблема была не архитектурной, а в
рассинхроне `flow` между хостами. Урок: при живой ручной правке конфигов на
хостах в ходе отладки — ВСЕГДА проверять оба конца туннеля синхронно, не
только тот, что редактируешь.

**Факты, подтверждённые тестами, о том, что остаётся нерешённым (не теория):**

1. Без фрагментации ClientHello REALITY-хендшейк не проходит вообще — `tcpdump`
   на Platform-ноде показывал полный TCP SYN/SYN-ACK/ACK, но ноль байт TLS
   ClientHello. Фрагментация (`freedom`-outbound + `sockopt.dialerProxy`,
   `packets: "tlshello"`) решает именно эту часть — подтверждено многократно.
2. **A/B-тест, решающий факт:** тот же самый туннель (identical код, конфиг,
   образ), включённый на VDS-B (НЕ в РФ, цензуры нет), проходит enrollment
   ПОЛНОСТЬЮ и чисто (`enrollment accepted`, нода становится `healthy`).
   Значит архитектура сама по себе рабочая — проблема специфична для
   российского сетевого пути, не общий баг кода.
3. **Байт-точное доказательство (три независимых попытки соединения, `tcpdump`
   на интерфейсе `ens3`, абсолютные seq-номера):** во всех трёх случаях ВСЕ
   TCP-сегменты ClientHello ACK'аются сервером за <20мс, КРОМЕ самого последнего
   — того, что дописывает объявленную в заголовке TLS-записи длину до конца
   (наблюдалось на смещениях 1670:1811, 1766:1844, 1473:1743, 1779:1812 —
   размер и позиция последнего сегмента каждый раз разные, но это ВСЕГДА
   последний по порядку кусок). Финальный сегмент ретранслируется клиентом с
   классическим TCP exponential backoff (6–8 попыток, интервалы 0.28с → 8с) и
   НИКОГДА не подтверждается. Не зависит от длины фрагментации Xray (`100-200`,
   `2-10`, ~`300-400`) — значит цензор считает байты от начала TLS-записи (длина
   записи — поле протокола, видимое уже в первом фрагменте, не зависит от
   TCP-сегментации) и глушит ИМЕННО тот сегмент, который завершил бы запись
   заявленной длины. Это не проблема тюнинга фрагментации на уровне Xray в
   принципе — Xray's `fragment` работает только на уровне TCP-сегментации и не
   может обмануть счётчик байт на уровне TLS-записи.
4. **После фикса рассинхрона `flow`:** отдельные попытки ВСЁ РАВНО иногда
   проходят весь VLESS/REALITY-уровень (`accepted ... taking detour
   to-platform-loopback` на порту 50052, дважды подтверждено в финальном тесте
   этой сессии) — то есть ClientHello-уровень не блокируется на 100%,
   вероятностно проходит. Но даже когда TCP/VLESS-диспетчеризация успешна,
   реальный mTLS-handshake Agent↔Platform ВСЁ РАВНО не завершается — Agent
   получает `Handshake read failed (Endpoint closing)` через ровно ~20 секунд
   (таймаут gRPC) в 100% попыток за последние 5+ циклов ретрая подряд. Значит
   ТСПУ вмешивается не только в ClientHello, но и в уже установленное
   REALITY-соединение — глушит или искажает данные ПОСЛЕ успешного хендшейка,
   ещё до завершения внутреннего (настоящего) mTLS-обмена Agent↔Platform.

**Эксперимент с `zapret`/`nfqws` (проверено в этой сессии, результат отрицательный,
но информативный):** раз проблема — в стейтфул-реассемблинге, а не в конфиге Xray,
опробован специализированный анти-DPI инструмент `zapret` (github.com/bol-van/zapret),
именно он таргетирует такой класс цензуры на уровне ядра через `NFQUEUE`.

- Собран `nfqws` из исходников на VDS-RU (сборка прошла чисто после установки
  `libnetfilter-queue-dev`, `libmnl-dev`, `zlib1g-dev`, `libcap-dev`).
  **Важный гочтя инфраструктуры:** правило `iptables -t mangle` нужно вешать на
  `POSTROUTING`, НЕ на `OUTPUT` — трафик туннель-контейнера идёт как forwarded/NAT'd
  через `docker0` (`network_mode: container:dist-vpn-agent`), а не как локально
  сгенерированный процесс хоста, поэтому `OUTPUT`-правило матчит 0 пакетов.
  Официальный пример `zapret` использует `POSTROUTING` — с первого раза это было
  упущено. Второй гочтя: фоновый `nfqws`, запущенный через `nohup ... & disown`
  по SSH, надёжно убивался при закрытии SSH-сессии (похоже, `systemd-logind`
  чистит cgroup пользователя независимо от `nohup`) — решение: `systemd-run
  --unit=... --collect nfqws ...` (transient systemd-юнит, переживает закрытие
  сессии).
- `--dpi-desync-any-protocol=1` вместе с фрагментацией Xray (`dialerProxy`+`fragment`)
  ломает TLS-парсер `nfqws` (двойная фрагментация — Xray режет поток ДО того, как
  `nfqws` его увидит, парсер не может собрать валидный ClientHello для анализа) —
  фрагментацию Xray пришлось ОТКЛЮЧИТЬ (убрать `dialerProxy`/`fragment-out` из
  `to-control-gateway`, отдать всю работу по обходу цензуры `nfqws`) для честного
  теста.
- `--dpi-desync=multidisorder --dpi-desync-split-pos=sniext+1` — РЕАЛЬНО работает
  (подтверждено `--debug=1` логом: корректно детектит полный TLS ClientHello,
  честно переупорядочивает сегменты на проводе — поздний диапазон отправляется
  ПЕРЕД более ранним). Результат: точка обрыва СМЕСТИЛАСЬ (теперь роняется не
  последний, а "средний" — по новому порядку отправки — сегмент), но обрыв
  ОСТАЛСЯ. Цензор пережил простое переупорядочивание — согласно самой
  документации `zapret`, это ожидаемо для "более строгого фильтра", который
  сам умеет корректно реассемблировать TCP-поток с учётом переупорядочивания
  (`README.md` `zapret`: "на некоторых диапазонах IP используется более строгий
  фильтр, распознающий попытки обмана через сегментацию").
- Эскалация до `--dpi-desync=fake,multidisorder --dpi-desync-autottl=1
  --dpi-desync-fooling=badseq` — **сделала ХУЖЕ**, не лучше: в этой конфигурации
  НИЧЕГО не подтверждается сервером (даже первый, самый ранний сегмент,
  который раньше всегда ACK'ался мгновенно) — цензор, похоже, распознаёт саму
  попытку инъекции фейковых/некорректных пакетов как аномалию и усиливает
  блокировку, а не обманывается ей.
- **Вывод:** ТСПУ на этом конкретном сетевом пути (VDS-RU) — не наивный
  first-packet-inspection DPI, а стейтфул реассемблер с устойчивостью к базовым
  TCP-трюкам (переупорядочивание, TTL-фейки, bad seq/ack). `zapret`/`nfqws` сам
  по себе — рабочий, корректно собранный и корректно интегрированный инструмент
  (это доказано), но требует намного более тонкого, итеративного подбора
  стратегии, чем удалось сделать за одну сессию — сама документация `zapret`
  прямо говорит, что готовых пресетов под конкретные российские ISP/диапазоны
  не существует, только эмпирика. **VDS-RU после теста возвращён к чистому,
  задокументированному baseline** (фрагментация Xray восстановлена, `nfqws` и
  iptables-правило удалены, `--queue-bypass` гарантировал отсутствие деградации
  трафика на всём протяжении эксперимента).

**Куда копать дальше (по убыванию вероятности успеха):**
- Дальнейший, более терпеливый подбор стратегии `nfqws` — инфраструктура для
  этого теперь есть и проверена (сборка, systemd-юнит, точечное POSTROUTING-
  правило с `--queue-bypass`). Кандидаты, ещё не опробованные: `seqovl`
  (перекрытие с уменьшенным seq вместо отдельных fake-пакетов — избегает
  проблемы "цензор детектит сам факт инъекции", которая провалила `fake`-режим),
  `ipfrag2` (фрагментация на уровне IP, не TCP — принципиально другой вектор),
  комбинации `multisplit`+`seqovl` без `fake`. Каждая итерация — отдельный
  цикл гипотеза→тест, не гарантирован успех (сам `zapret` описывает это как
  эмпирику даже для community).
- Раз проблема — в устойчивости УЖЕ установленного REALITY-соединения (не
  ClientHello), а не в конфиге flow/Vision — рассмотреть `packets: "1-5"`
  вместо `tlshello` в Xray fragment-outbound'е (фрагментировать первые N
  пакетов, не только ClientHello) — не пробовалось.
- Проверить `mux` (мультиплексирование) — Xray-документация: одна из находок
  при поиске — `mux` тоже не всегда совместим с REALITY/TCP-транспортом
  (github.com/XTLS/Xray-core/discussions/5481) — не проверялось.
- Рассмотреть alternative donor/SNI для REALITY (не `img.yandex.ru`) — если
  ТСПУ таргетирует конкретно этот донор по накопленной статистике, смена SNI
  может временно снять эскалацию (не проверялось, дешёвый тест).
- Связаться с сообществом/issue-трекером `zapret` напрямую с конкретным
  воспроизводимым кейсом (стейтфул-реассемблер с устойчивостью к
  multidisorder+fake/badseq) — за пределами типичных сценариев, документация
  которых уже покрыта.

**Текущее поведение в проде:** `agent_control_plane_tunnel_enabled: true` на
entry-ноде в РФ разворачивает всю инфраструктуру корректно (сайдкар, роутинг,
sysctl, и т.д.), конфиг теперь гарантированно синхронен с gateway'ем (баг
рассинхрона исправлен), но Agent будет бесконечно ретраить enrollment с
экспоненциальным backoff, не переходя в `healthy`, пока не решена глубинная
проблема устойчивости REALITY-потока под активным ТСПУ. Это НЕ ломает остальной
флот — VDS-A (Platform) и любые нероссийские entry/exit-ноды (VDS-B — живой
пример) работают полностью штатно, с туннелем или без.

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
- **Та же оговорка, но для нового раздела «Bootstrap mTLS Agent↔Platform»
  этого слайса**: описанный там дизайн (CA генерируется Platform при
  первом старте, публичный сертификат пишется по предсказуемому пути,
  логируется одноразовое сообщение) — это согласованный контракт для
  параллельных сессий этого раунда, а не результат чтения реального кода
  Platform. Проверено: соседний worktree `C:\access\dist-vpn-platform`
  существует и содержит куда более развитый Platform, чем стаб в этой
  ветке, но на branch `platform/api-skeleton` — это стабильная ветка
  **предыдущего** раунда (REST API skeleton, без mTLS/CA-bootstrap кода,
  рабочее дерево там чистое, без незакоммиченных изменений на момент
  проверки). Код CA-bootstrap этого раунда, если он вообще существует как
  диff где-либо на диске, не был найден и намеренно не разыскивался
  глубже — задача этого слайса прямо указывает описывать это как
  "согласованный дизайн, не увиденный код", и попытка вычитать
  potentially-mid-flight код рискует задокументировать нечто, что изменится
  до реального мержа. Путь к файлу CA-сертификата и точный текст
  лог-сообщения в разделе «Bootstrap mTLS» — **не проверены**, помечены как
  таковые прямо в тексте раздела.

Короче: **код написан и вручную вычитан внимательно, синтаксис YAML/Jinja2
подтверждён инструментально, но фактическое исполнение — включая
идемпотентность и реальный `docker build`/`docker run` — не подтверждено
запуском ни разу.** Это не должно применяться к реальной ноде без прогона
на настоящей control-машине (с Docker daemon) сначала.

### ⚠️ Обновление: `yamllint`/`ansible-lint` реально прогнаны впервые (GitHub Actions CI)

Написанное выше про «`ansible-lint` не запускается в этой sandbox» по-прежнему
верно **для Windows-песочницы разработки** — но `provisioning-lint` в
`.github/workflows/ci.yml` гоняет оба линтера на настоящем `ubuntu-latest`, и
это первый раз за всё время существования проекта, когда они реально
выполнились против этого кода (`.ansible-lint`/`.yamllint` были написаны
заранее, "на бумаге", без возможности проверить). Найдено и исправлено:

- **`yamllint .` линтил и `provisioning/collections/`** — каталог, куда
  `ansible-galaxy collection install -r requirements.yml` (по
  `ansible.cfg`'s `collections_path`) кладёт сторонние коллекции
  (`ansible.posix` и т.д.). Уже в `.gitignore`, но раньше никто не гонял
  `yamllint .` целиком, чтобы это заметить. `.yamllint` теперь переиспользует
  `.gitignore` (`ignore-from-file: [.gitignore]`) — исключения остаются в
  одном месте, не дублируются.
- 7 настоящих находок `yamllint` (`comments: too few spaces before comment`,
  дефолтное правило требует 2 пробела перед инлайн `#`) — поправлены в
  `roles/agent_install/defaults/main.yml`, `roles/agent_install/tasks/run.yml`,
  `roles/base_hardening/defaults/main.yml`, `roles/base_hardening/handlers/main.yml`.
- `.yamllint` дополнен пятью значениями, которые требует собственный
  yaml-профиль `ansible-lint` (`comments.min-spaces-from-content`,
  `comments-indentation`, `braces.max-spaces-inside`,
  `octal-values.forbid-implicit-octal`/`forbid-explicit-octal`) — без них
  `ansible-lint` предупреждал о "incompatible custom yamllint configuration"
  и отключал fix-mode.
- `schema[meta]`: во все три `meta/main.yml` (`agent_install`, `base_hardening`,
  `docker_install`) добавлено обязательное поле `galaxy_info.author`.
- `name[casing]`/`name[template]`: 2 имени задач с маленькой буквы
  (`base_hardening/tasks/main.yml`) и 3 с Jinja-подстановкой в середине
  строки (а не в конце, как требует `ansible-lint`) переформулированы —
  смысл не менялся, только порядок слов.
- **`var-naming[no-role-prefix]` (54 находки) — сознательно оставлено
  невыполненным**, через `skip_list` в `.ansible-lint`, а не переименованием
  всех переменных ролей. Причина зафиксирована прямо в файле: конвенция
  существует, чтобы переменные разных ролей не конфликтовали при композиции
  с чужими/сторонними ролями — у нас все три роли свои (`dependencies: []`
  везде), коллизия физически невозможна. Проверено по официальной
  документации ansible-lint: `var-naming` — правило уровня `basic` (входит
  и в `moderate`/`safety`/`shared`/`production`), так что понижение профиля
  не помогло бы избежать именно этого правила, зато выкинуло бы реальные
  security-проверки уровня `safety` (`risky-file-permissions`,
  `risky-shell-pipe` и т.д.) — `skip_list` конкретного правила остаётся
  единственным точечным решением, и это официально задокументированный,
  предусмотренный механизм (`docs.ansible.com/projects/lint/configuring`),
  не обход.

## Scope of this slice / TODO

Прошлый слайс закрывал «Базовую подготовку»
([ТЗ-7 §4.1](../docs/TZ-7-vds-provisioning.md#4-функциональные-требования-к-процедуре-provisioning)).
Позапрошлый (относительно этого) слайс закрывал **роль-специфичные
firewall-порты** и **установку Agent'а**
([ТЗ-7 §4.2](../docs/TZ-7-vds-provisioning.md#4-функциональные-требования-к-процедуре-provisioning)
и §4.3) — см. «Роль-специфичные firewall-порты» и «Agent: сборка образа»
выше. Этот слайс закрывает **операторский bootstrap-workflow для mTLS
Agent↔Platform** — откуда физически берётся CA-сертификат Platform и как
он попадает в `agent_mtls_ca_cert_src`, плюс пересмотр
`agent_mtls_client_cert_src`/`key_src` в свете подтверждённого дизайна
enrollment — см. «Bootstrap mTLS Agent↔Platform: операторский workflow»
выше за полным содержанием.

Явно НЕ входит в этот слайс (следующие слайсы provisioning):

- **[ЯВНЫЙ TODO следующего слайса] Проверка результата — Agent HEALTHY в
  реестре Platform** (ТЗ-7 §4.4, критерий приёмки ТЗ-7 §7). Это
  **намеренно не сделано в этом слайсе** — не забыто и не упрощено, а
  сознательно отложено, потому что настоящая проверка требует **живого
  стека Platform+Agent, реально соединённых по mTLS**, чего физически нет
  в этой sandbox-сессии, и (что более существенно) зависит от кода обеих
  параллельных сессий этого раунда (Platform: генерация CA, REST/WebSocket
  API статуса ноды; Agent: enrollment, CSR, персист сертификата) —
  реально смёрженного и прошедшего собственное ревью. Пытаться подделать
  эту проверку сейчас (например, замокать API-ответ) означало бы соврать
  о том, что реально проверено — прямое нарушение дисциплины, которой
  следует этот README. `roles/agent_install/tasks/verify.yml` на сегодня
  проверяет только, что Docker-контейнер Agent'а в состоянии `running` —
  это НЕ то же самое, что "Agent прошёл enrollment и Platform считает его
  `HEALTHY`".
  **План на следующий слайс** (когда сиблинг-сессии смержены): добавить
  `post_tasks` в `site.yml` (либо отдельный `verify_platform_status.yml`),
  который через `ansible.builtin.uri` опрашивает REST API Platform по
  `node_id`, полученному при enrollment (нужно сначала решить, как Agent
  сообщает свой `node_id` наружу, чтобы Ansible вообще мог его узнать —
  открытый вопрос контракта, не решённый ни в этом, ни в предыдущих
  слайсах provisioning), с ретраями/таймаутом и понятным сообщением об
  ошибке — это и есть финальный шаг приёмки всего провижининга (ТЗ-7 §7,
  последний пункт). Прежде чем писать эту задачу — свериться с реальным
  `platform/README.md` и `agent/README.md` после мержа за точными путями
  API/полями ответа, а не угадывать заранее.
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
- **[Обновлено этим слайсом] Volume под персист собственного сертификата
  Agent'а, если он окажется нужен.** Прошлый слайс формулировал этот
  пункт как «заблокировано тем, что Agent пока не умеет персистить
  сертификат» — эта формулировка устарела: согласованный дизайн этого
  раунда подтверждает, что Agent будет персистить сертификат сам, внутри
  собственного процесса (см. «Bootstrap mTLS Agent↔Platform» выше) — то
  есть `agent_mtls_client_cert_src`/`key_src` и связанный
  bind-mount/UID-компромисс для НИХ (`0644` вместо `0600`) остаются
  актуальны только для edge-case пути (восстановление/миграция ноды), не
  для основного потока — см. подробный вывод там же. Отдельный, новый
  открытый вопрос (см. последний абзац того же раздела): переживёт ли
  путь, куда Agent сам пишет свой сертификат, полную рекреацию контейнера
  (не просто перезапуск) — зависит от того, пишет ли Agent его в путь,
  смонтированный как именованный volume, или просто в filesystem
  контейнера. Не решено в этом слайсе, потому что путь персиста не виден
  из кода Agent'а этого раунда (не смержен сюда) — решить, как только он
  станет виден.
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
| Agent появляется в реестре Platform со статусом `HEALTHY` | Вне рамок этого слайса, явный TODO следующего — см. «Bootstrap mTLS Agent↔Platform» и TODO выше. Операторский workflow получения/распространения CA-сертификата задокументирован в этом слайсе; сама HEALTHY-проверка блокирована на смерж кода параллельных сессий этого раунда (Platform CA/API, Agent enrollment/CSR) — намеренно не подделана |
| Ни один playbook не использует «скачать и сразу исполнить» | Выполнено по конструкции — Ansible push по SSH; на хост скачивается: apt-пакеты, GPG-ключ Docker (доверенный ключ репозитория, не исполняемый код), и — новое в этом слайсе — весь git-репозиторий через `ansible.builtin.git`, закреплённый на конкретном теге/commit SHA (не ветке), после чего `docker build` исполняется ЛОКАЛЬНО на уже доверенном (проверенным SSH-ключом) хосте из проверяемого содержимого — не `curl \| bash` откуда-то извне |

## Прочее

Критерии выбора самой VDS (провайдер/ASN/AUP) — отдельно, см.
[../docs/TZ-1-vds-selection.md](../docs/TZ-1-vds-selection.md), это решение
администратора вне рамок этого кода.
