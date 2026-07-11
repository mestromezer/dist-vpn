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
  requirements.yml         — внешние коллекции (ansible.posix, community.general)
  site.yml                 — точка входа: применяет обе роли по порядку
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
  Docker) и `community.general.ufw` (правила firewall).
- Docker-образ для мок-тестирования (например, через Molecule с
  `docker`-драйвером — стандартный инструмент для проверки idempotency роли,
  упомянутый в [ТЗ-7 §2.3](../docs/TZ-7-vds-provisioning.md#23-версионирование-и-ревью))
  не поднимался — Docker daemon недоступен в этой sandbox.

Короче: **код написан и вручную вычитан внимательно, синтаксис YAML/Jinja2
подтверждён инструментально, но фактическое исполнение — включая
идемпотентность — не подтверждено запуском ни разу.** Это не должно
применяться к реальной ноде без прогона на настоящей control-машине сначала.

## Scope of this slice / TODO

Этот слайс закрывает **только** «Базовая подготовка» из
[ТЗ-7 §4.1](../docs/TZ-7-vds-provisioning.md#4-функциональные-требования-к-процедуре-provisioning).
Явно НЕ входит в этот слайс (следующие слайсы provisioning):

- **Роль-специфичные порты firewall** (ТЗ-7 §4.2): порты пресетов Hop A на
  входных нодах, порт(ы) backhaul Hop B на обеих сторонах, порт mTLS-канала
  Agent↔Platform. Механизм уже вписан и работает (`site.yml` уже
  параметризован `role=entry|exit`, `roles/base_hardening/tasks/firewall.yml`
  уже читает `role_firewall_rules[role]`), но сам список портов —
  **намеренно пустой словарь** в `roles/base_hardening/defaults/main.yml`,
  потому что конкретные порты зависят от ещё не финализированного дизайна
  Agent'а/Xray-пресетов (ТЗ-4). Открывать что-то «на всякий случай» сейчас
  значило бы нарушить прямой запрет ТЗ-7 §3.2. Когда порты зафиксируются в
  ТЗ-4 — просто заполнить `role_firewall_rules` в `group_vars/entry_nodes.yml`/
  `exit_nodes.yml` (форма уже задокументирована комментарием в
  `roles/base_hardening/defaults/main.yml`).
- **Установка и enrollment Agent'а** (ТЗ-7 §4.3): разворачивание
  Docker-контейнера Agent'а, передача адреса Platform и одноразового
  enrollment-токена. Заблокировано тем, что Docker-образ Agent'а — отдельный,
  параллельный трек разработки и ещё не существует. Задел уже сделан:
  - `group_vars/all/vault.yml.example` уже документирует форму
    `agent_platform_address` и `agent_enrollment_token` (явно
    прокомментировано как «не используется ни одной задачей этого слайса»).
  - `site.yml` заканчивается явным `debug`-плейсхолдером, указывающим, что
    здесь появится следующий шаг, а не молча завершается кодом 0.
  - Когда образ Agent'а будет готов — добавляется роль вида `roles/agent_install/`
    (Docker-контейнер + передача токена через vault-переменную, никогда как
    plaintext `-e`/аргумент командной строки) и вызывается из `site.yml`
    после `docker_install`.
- **Проверка результата — Agent HEALTHY в реестре Platform** (ТЗ-7 §4.4,
  критерий приёмки ТЗ-7 §7): невозможно до появления роли `agent_install` и
  самого Platform API для проверки статуса ноды — добавится вместе с
  предыдущим пунктом.
- **Ansible Molecule** (ТЗ-7 §2.3) — тестовые сценарии для `base_hardening`/
  `docker_install` не добавлены в этом слайсе (объём ролей пока небольшой,
  ручного ревью + `--check`/`--syntax-check` на реальной control-машине
  достаточно, как и допускает ТЗ-7 §2.3 формулировкой «если объём ролей
  вырастет настолько, что ручной проверки станет недостаточно»). Кандидат на
  следующий слайс, если роли начнут расти.
- **SOPS+age** как альтернатива Ansible Vault (ТЗ-7 §2.1) — не реализовано в
  этом слайсе; Vault выбран как «простой случай, один администратор»
  (формулировка самого ТЗ-7 §2.1). Переход на SOPS+age, если понадобится
  более гранулярная git-история секретов, — самостоятельная будущая задача,
  не блокирует текущий слайс.

## Соответствие критериям приёмки ТЗ-7 §7

| Критерий | Статус в этом слайсе |
|---|---|
| Секреты нигде в открытом виде в репозитории | Сделано — Vault-плейсхолдер, `.gitignore` дополнен, честно не проверено сканом истории git (нет истории для сканирования, ветка новая) |
| `PermitRootLogin no`, `PasswordAuthentication no` после применения | Реализовано в `roles/base_hardening/tasks/ssh.yml` + шаблон; **не проверено фактическим SSH-подключением** — нет реальной VDS в этой песочнице |
| `ufw` активен, только нужные порты | Реализовано для базового слайса (только SSH); роль-специфичные порты — TODO выше |
| `fail2ban` активен и банит по неудачным SSH-попыткам | Реализовано (`jail.local`, backend systemd); **не проверено имитацией** — нет реальной VDS |
| Повторный прогон не меняет состояние (идемпотентность) | Модули выбраны с расчётом на идемпотентность; **не подтверждено запуском** — см. «Что реально проверено» выше |
| Agent появляется в реестре Platform со статусом `HEALTHY` | Вне рамок этого слайса — см. TODO выше |
| Ни один playbook не использует «скачать и сразу исполнить» | Выполнено по конструкции — Ansible push по SSH, единственное, что скачивается автоматически, — apt-пакеты и открыто публикуемый GPG-ключ Docker (не исполняемый код, только доверенный ключ репозитория) |

## Прочее

Критерии выбора самой VDS (провайдер/ASN/AUP) — отдельно, см.
[../docs/TZ-1-rossiyskiy-vds.md](../docs/TZ-1-rossiyskiy-vds.md), это решение
администратора вне рамок этого кода.
