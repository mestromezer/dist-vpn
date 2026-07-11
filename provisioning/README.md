# Provisioning

Полное ТЗ: [../docs/TZ-7-vds-provisioning.md](../docs/TZ-7-vds-provisioning.md).

Кратко: Ansible playbook'и (не bash-скрипты) — базовое хардненинг чистой VDS (SSH, ufw, fail2ban, Docker), роль-специфичное открытие портов (входная/выходная нода), установка Agent'а и enrollment к Platform. Секреты — Ansible Vault или SOPS+age, никогда в открытом виде в репозитории.

## Старт работы

1. Структура — типовая для Ansible (`inventory/`, `roles/`, `site.yml`) — конкретная раскладка на усмотрение разработчика в рамках требований ТЗ.
2. Секреты — сразу через Vault/SOPS с первого коммита, не добавлять "потом зашифруем".
3. Критерии приёмки — [TZ-7, §7](../docs/TZ-7-vds-provisioning.md#7-критерии-приёмки).

Критерии выбора самой VDS (провайдер/ASN/AUP) — отдельно, см. [../docs/TZ-1-rossiyskiy-vds.md](../docs/TZ-1-rossiyskiy-vds.md), это решение администратора вне рамок этого кода.
