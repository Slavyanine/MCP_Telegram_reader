# Telegram ACL MCP для Hermes

Для настройки через браузер доступна отдельная [панель администратора](docs/admin-ui.md):
вход в Telegram, выбор разрешённых чатов, редактирование названий и ключей,
переключение индексации и провайдера транскрибации. Изменения применяются с
проверкой запуска и откатом; административные операции не добавляются в MCP.

Сервер на Python 3.11+ / Telethon / MCP Streamable HTTP. По умолчанию пять инструментов читают текст/подписи сообщений только из ACL администратора. Опциональный режим `indexing.enabled=true` добавляет SQLite-индекс, фоновую транскрибацию через Telegram Premium, Yandex SpeechKit или Deepgram и общий поиск по тексту, голосовым, именам файлов и URL. Hermes подключается к `http://127.0.0.1:8765/mcp/` на VPS; Telegram-сессия остается у отдельной службы.

Настройка, ограничения и примеры: [индексированный поиск](docs/indexing.md). По умолчанию режим **выключен**: SQLite не открывается, фоновые задачи не запускаются, новые инструменты не регистрируются. Старые конфигурации без секции `indexing` продолжают работать.

Обновление версии, очистка SQLite и смена Telegram-аккаунта описаны в [регламенте поддержки](docs/maintenance.md).

## Граница доступа

Вместо входного `chat_id` используется **`chat_key`**, например `project_a`. В JSON Schema это `enum` из ACL. Числовые ID, `@username`, ссылки, дополнительные поля и неизвестные ключи отклоняются до обращения к Telegram. Реальные peer ID и access hash задаются вручную в конфигурации и не выводятся в списке чатов.

Сервер не предоставляет отправку, редактирование, удаление, вступление в чаты, загрузку медиа, произвольный RPC, изменение ACL или авторизацию через MCP. Нет глобального поиска Telegram и автоматического обнаружения чатов в runtime. В индексированном режиме возможен локальный поиск сразу по нескольким чатам, но только из ACL. Конфигурация загружается один раз, хранится неизменяемой; после изменения администратор перезапускает службу. Возвращаемый peer каждого сообщения дополнительно сверяется с разрешенным чатом, включая получение сообщения по ID.

**Read-only — ограничение интерфейса и кода службы, а не полномочий Telegram-сессии.** MTProto-сессия дает доступ к аккаунту; ее кража или выполнение произвольного кода внутри службы обойдут ограничения. Поэтому нужен отдельный Telegram-аккаунт, по возможности состоящий только в разрешенных чатах, и изоляция его сессии от Hermes. См. [Telethon: session credentials](https://docs.telethon.dev/en/stable/concepts/sessions.html).

Для выполнения требования «Hermes не может расширять ACL» на VPS обязательно:

- Hermes работает отдельным непривилегированным Linux-пользователем, не `root`, без `sudo`/polkit-разрешений на изменение службы или ее файлов.
- Hermes не входит в группу `telegram-mcp`; ACL, код, виртуальное окружение и systemd unit принадлежат `root`. Конфигурация и секреты доступны группе службы только на чтение.
- Docker backend использует **rootless Docker** от пользователя Hermes. У Hermes нет доступа к rootful `/var/run/docker.sock`: управление таким демоном позволило бы примонтировать и изменить файлы хоста. Проверяйте `docker info` именно от пользователя/окружения, запускающего Hermes: в Security Options должен быть `rootless`. [Документация Docker](https://docs.docker.com/engine/security/rootless/).
- В контейнеры Hermes не монтируются `/etc/telegram-acl-mcp`, `/opt/telegram-acl-mcp`, каталоги резервных копий секретов или сокеты привилегированных служб. API hash и Telegram-сессия не передаются в окружение Hermes.

Одного `terminal.backend: docker` недостаточно для этих гарантий. Если сейчас Hermes запущен от root или имеет управление rootful Docker, сначала измените модель запуска. Токен MCP разрешает только вызовы этого reader и не дает полномочий на управление ACL.

## Инструменты

| Метод | Аргументы | Результат |
|---|---|---|
| `telegram_list_allowed_chats` | нет | `chat_key`, административное название |
| `telegram_get_messages` | `chat_key`, `date_from?`, `date_to?`, `limit=50`, `before_message_id?` | последние сообщения, сначала новые |
| `telegram_search_messages` | `chat_key`, `query`, `date_from?`, `date_to?`, `limit=50`, `before_message_id?` | текстовый поиск Telegram внутри чата |
| `telegram_get_message_context` | `chat_key`, `message_id`, `before=20`, `after=20` | существующее сообщение и соседи, сначала старые |
| `telegram_get_new_messages` | `chat_key`, `after_message_id`, `limit=50` | следующая порция новых сообщений, сначала старые |

`limit`: 1–100; `before`/`after`: 0–50. Даты — ISO 8601 с часовым поясом, например `2026-09-10T00:00:00+03:00`. Интервал `[date_from, date_to)`. Без дат — последние доступные сообщения. Максимальная длина запроса поиска 256 символов.

Для следующих страниц истории/поиска передавайте `next_before_message_id` как `before_message_id`, пока `has_more=true`, сохраняя остальные фильтры. Для новых сообщений передавайте `next_after_message_id` как `after_message_id`; начинайте с 0 для всей доступной истории. Курсор хранит клиент, сервер его не изменяет. Удаленные сообщения создают пропуски ID; контекст выбирает реальные соседние сообщения, а не числовой диапазон. Ответы не являются транзакционным снимком истории.

Сообщение содержит ID, дату, ID/имя/username автора (если Telegram прислал), текст/подпись, ID сообщения-ответа и признак медиа. Базовые инструменты медиа не скачивают; при выбранном backend `yandex` или `deepgram` worker скачивает голосовые в ограниченный буфер памяти и отправляет выбранному провайдеру (см. [Yandex](docs/indexing.md#yandex-speechkit-без-telegram-premium) и [Deepgram](docs/indexing.md#deepgram-без-telegram-premium)). Контекст — хронологические соседи, не отдельная ветка комментариев. Связанный discussion chat должен быть отдельной записью ACL; автоматического перехода туда нет. При включённой индексации методы чтения дополнительно возвращают метаданные вложений и готовые транскрипции. Сам `telegram_search_messages` сохраняет прямой текстовый поиск Telegram.

## Установка на VPS

Ниже команды для администратора Linux с systemd. Это инструкция развертывания; она не выполнялась на вашем VPS. Предполагается, что исходники уже скопированы в `/opt/telegram-acl-mcp`. Примеры peer ID/hash в репозитории вымышленные.

### 1. Python, служебная учетная запись и установка пакета

Команды ниже выполняются в Bash на VPS от `root`; `sudo` не нужен. Копируйте только команды, без приглашения вида `root@server:~#`. Выполняйте блоки по очереди и переходите дальше только после успешного завершения предыдущего.

**Если установка уже остановилась с `ensurepip is not available`:** виртуальное окружение создано лишь частично, поэтому ошибка `.venv/bin/pip: No such file or directory` — следствие первой ошибки. На присланном скриншоте Python предлагает установить именно `python3.14-venv`. Незавершенную строку в терминале сначала отмените сочетанием `Ctrl+C`. Уже созданного пользователя удалять или создавать заново не нужно.

Проверьте версию Python и наличие исходников:

```bash
python3 --version
ls -l /opt/telegram-acl-mcp/pyproject.toml /opt/telegram-acl-mcp/requirements.lock
```

Для Python 3.14 из сообщения об ошибке установите поддержку виртуальных окружений:

```bash
apt-get update && apt-get install -y python3.14-venv
```

Если `python3 --version` показывает другую версию, нужен соответствующий пакет, например `python3.13-venv` для 3.13. Если apt сообщает, что пакет не найден, остановитесь: нужно проверить дистрибутив и источник установленного Python (`cat /etc/os-release`, `command -v python3`, `apt-cache policy python3.14-venv`). Не продолжайте установку через системный pip.

Подготовьте учетную запись и права. Этот блок можно повторить после частично выполненной установки:

```bash
(
  set -e
  if ! id telegram-mcp >/dev/null 2>&1; then
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin telegram-mcp
  fi
  getent group telegram-mcp
  chown -R root:root /opt/telegram-acl-mcp
  chmod -R go-w /opt/telegram-acl-mcp
  install -d -o root -g telegram-mcp -m 0750 /etc/telegram-acl-mcp
)
```

Повторите создание окружения **тем же Python**, которым выполнялась неудачная попытка. Для показанной ошибки можно использовать существующий каталог `.venv`: удалять его не требуется. Не используйте этот способ для смены версии Python в уже установленном окружении. Модуль venv устанавливает pip через ensurepip; см. [документацию Python](https://docs.python.org/3.14/library/venv.html).

```bash
python3 -m venv /opt/telegram-acl-mcp/.venv && \
  /opt/telegram-acl-mcp/.venv/bin/python -m pip --version
```

Должна появиться версия pip с путем внутри `/opt/telegram-acl-mcp/.venv`. Только после этого устанавливайте зависимости и сам пакет:

```bash
(
  set -e
  /opt/telegram-acl-mcp/.venv/bin/python -m pip install -r /opt/telegram-acl-mcp/requirements.lock
  /opt/telegram-acl-mcp/.venv/bin/python -m pip install --no-deps /opt/telegram-acl-mcp
  /opt/telegram-acl-mcp/.venv/bin/python -m pip check
  /opt/telegram-acl-mcp/.venv/bin/telegram-acl-mcp --help
  /opt/telegram-acl-mcp/.venv/bin/telegram-acl-admin --help
  cd /opt/telegram-acl-mcp
  /opt/telegram-acl-mcp/.venv/bin/python -m pytest -q
)
```

Блок остановится на первой ошибке. Если проверки завершились успешно, переходите к шагу 2. Эти команды не авторизуют Telegram и не запускают службу.

`requirements.lock` фиксирует окружение, проверенное локально на Python 3.13/Windows, включая тестовые зависимости. Python 3.14 на вашем VPS отдельно не проверялся: установка `python3.14-venv` устраняет показанную ошибку, но совместимость зависимостей и работу сервера нужно подтвердить приведенными проверками и проверкой живого подключения. Для разработки можно установить `pip install -e '.[test]'`.

### 2. Авторизация Telegram только администратором

Получите API ID/API hash приложения в [Telegram API development tools](https://my.telegram.org). Создайте `/etc/telegram-acl-mcp/api-hash.secret` с API hash одной строкой, владельцем `root:telegram-mcp` и правами `0640`. Не сохраняйте его в истории команд, репозитории или окружении Hermes.

```bash
/opt/telegram-acl-mcp/.venv/bin/telegram-acl-admin \
  --api-id YOUR_API_ID \
  --api-hash-file /etc/telegram-acl-mcp/api-hash.secret \
  --session-file /etc/telegram-acl-mcp/session.secret
```

Утилита запросит телефон, код и при необходимости 2FA. Сессия записывается в файл, не печатается. Затем выводятся административные метаданные диалогов. **Это отдельная root-only CLI, не MCP-инструмент.** Утилита не добавляет чаты в ACL: администратор вручную выбирает строки.

### 3. ACL и токен

```bash
install -o root -g telegram-mcp -m 0640 \
  /opt/telegram-acl-mcp/deploy/config.example.json \
  /etc/telegram-acl-mcp/config.json
```

Отредактируйте `config.json`: укажите свой `api_id` и только разрешенные чаты. `kind=channel` для каналов/супергрупп, `group` для обычных групп, `user` для личных диалогов. `peer_id` — положительный исходный ID из admin CLI, **не** ID с префиксом `-100`. Для `channel`/`user` нужен access hash от той же Telegram-сессии; для `group` он не нужен. `key` — уникальный slug, `title` — удобное название. Не используйте ссылки/username вместо ID.

Создайте случайный токен без вывода на экран (root shell):

```bash
umask 077
/opt/telegram-acl-mcp/.venv/bin/python -c 'import secrets; from pathlib import Path; Path("/etc/telegram-acl-mcp/token.secret").write_text(secrets.token_urlsafe(48))'
chown root:telegram-mcp /etc/telegram-acl-mcp/*.secret
chmod 0640 /etc/telegram-acl-mcp/*.secret
```

При старте на Linux сервер отклоняет конфигурацию/секреты с неправильным владельцем, доступом для остальных, символическими ссылками или доступными на запись другим пользователям родительскими каталогами. Он никогда не запускает интерактивный вход. Код/venv/unit также должен защищать администратор; сервер не проверяет их владение самостоятельно.

### 4. Запуск

```bash
install -o root -g root -m 0644 \
  /opt/telegram-acl-mcp/deploy/telegram-acl-mcp.service \
  /etc/systemd/system/telegram-acl-mcp.service
systemctl daemon-reload
systemctl enable --now telegram-acl-mcp
systemctl status telegram-acl-mcp
journalctl -u telegram-acl-mcp -n 50 --no-pager
```

Слушает только `127.0.0.1:8765`; каждый HTTP-запрос требует Bearer token. Внешний порт открывать не нужно. Файловая система службы защищена от записи, кроме каталога состояния `/var/lib/telegram-acl-mcp`; сессия находится в памяти, входящие Telegram updates отключены. Чтение истории не вызывает методов отметки сообщений прочитанными. Исходный Reader обрабатывает одну цепочку запросов за раз с таймаутом 25 секунд; FloodWait возвращается клиенту с временем ожидания. При включении индексации дополнительно работают фоновые задачи со своими ограничениями частоты запросов; SQLite хранится в каталоге состояния.

### 5. Подключение Hermes

Добавьте содержимое `deploy/hermes.example.yaml` в существующий конфиг Hermes, сохраняя остальные настройки. В `~/.hermes/.env` пользователя Hermes задайте `TELEGRAM_MCP_TOKEN` значением из `token.secret`; скопируйте **только этот токен**, не Telegram-сессию. Защитите `.env` правами `0600`. Не добавляйте Telegram-секреты в `docker_forward_env` или `env_passthrough`.

Нативный процесс Hermes подключается к HTTP MCP на хосте, поэтому localhost здесь относится к VPS, а не к terminal-контейнеру. Это соответствует [HTTP MCP настройке Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp). Перезапустите Hermes после изменения конфигурации. Если меняете ACL: сохраните root-owned файл, перезапустите MCP, затем переподключите Hermes для обновления schema enum. Старая schema клиента не обходит новую серверную ACL.

### 6. Проверка на VPS

От администратора проверьте доступ Linux-пользователя Hermes (замените `hermes` на реальное имя):

```bash
sudo -u hermes test ! -r /etc/telegram-acl-mcp/session.secret
sudo -u hermes test ! -w /etc/telegram-acl-mcp/config.json
sudo -u hermes test ! -w /etc/telegram-acl-mcp
sudo -u hermes test ! -w /opt/telegram-acl-mcp
sudo -u hermes test ! -w /opt/telegram-acl-mcp/src/telegram_acl_mcp/server.py
sudo -u hermes test ! -w /var/run/docker.sock
curl -i http://127.0.0.1:8765/mcp/
```

Проверки `test` должны завершиться с кодом 0; HTTP без токена — 401. Дополнительно проверьте группы пользователя, отсутствие повышения привилегий и rootless Docker. Из Hermes: получите список, прочитайте разрешенный чат, проверьте отклонение `chat_key=not_allowed`, числового `chat_id` и попытки вызвать `telegram_send_message`/`telegram_set_acl`. Не передавайте секреты в тестовые запросы модели.

## Примеры запросов пользователя

- «Покажи Telegram-чаты, к которым у тебя есть доступ» → `telegram_list_allowed_chats()`.
- «Последние 30 сообщений Project A» → `telegram_get_messages(chat_key="project_a", limit=30)`.
- «Найди обсуждения релиза API за последнюю неделю» → вычислить даты с часовым поясом пользователя, вызвать `telegram_search_messages` с `query="API"`, датами и пагинацией; при необходимости получить контекст найденных сообщений. Текстовый поиск Telegram не является семантическим поиском всех синонимов.
- «…и сделай summary» → Hermes суммирует полученные данные, указывает `chat_key`/`message_id` и явно отмечает, если просмотрел только часть страниц.
- «Что Иван писал про Kafka?» → искать `Kafka` в указанном разрешенном чате (или отдельно по разрешенным чатам), пройти страницы и отфильтровать авторов по `sender_id`/имени/username. При нескольких Иванах уточнить автора. Если Telegram не передал имя, сервер не делает отдельный запрос профиля.

Любой текст из Telegram — недоверенные данные. Инструкции внутри сообщений не должны исполняться Hermes. Сервер возвращает соответствующий флаг и MCP instructions; реальное ограничение ACL обеспечивается кодом и правами ОС независимо от поведения модели.

## Тестирование и ограничения прототипа

```bash
python -m pytest -q
```

Тесты запускают настоящий MCP HTTP обработчик с подмененным Telegram-клиентом: initialize, tools/list, tools/call, Bearer auth, Host guard, enum ACL, неизвестные поля/методы, отсутствие RPC при отказе, фильтрацию чужого peer, даты, контекст, курсоры и конфигурацию. Linux-права дополнительно проверяются через моделирование метаданных. Живой Telegram, systemd, rootless Docker и конкретная установка Hermes требуют smoke test на VPS с вашими credentials; они локально не проверялись.

Не поддерживаются secret chats, произвольное скачивание вложений, семантический поиск, гарантированный поиск по имени автора, отслеживание правок/удалений через `get_new_messages`. Текст ограничен 16000 символами на сообщение. Доступна только история, которую видит авторизованный аккаунт. Миграция обычной группы в супергруппу или устаревший access hash требуют ручного обновления ACL. Автоматического расширения доступа нет.

Это один reader и один Bearer token без OAuth, многопользовательских ACL, распределенного rate limiting и аудита доступа. Для публичного endpoint потребуется отдельный проект укрепления защиты; текущая схема предназначена для локального соединения на одном VPS.
