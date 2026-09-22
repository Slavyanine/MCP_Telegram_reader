# Поддержка Telegram ACL MCP

Этот регламент рассчитан на установку из `README.md`: Linux/VPS, systemd-служба
`telegram-acl-mcp`, код в `/opt/telegram-acl-mcp`, конфигурация и секреты в
`/etc/telegram-acl-mcp`, состояние в `/var/lib/telegram-acl-mcp`. Команды
выполняются от `root`. Не передавайте каталог `/etc/telegram-acl-mcp`, резервные
копии или SQLite-базу пользователю Hermes: они содержат Telegram-сессию и текст
переписки.

## Перед любыми работами

1. Запишите текущую версию и состояние службы:

   ```bash
   git -C /opt/telegram-acl-mcp status --short
   git -C /opt/telegram-acl-mcp rev-parse HEAD
   /opt/telegram-acl-mcp/.venv/bin/python -c 'from importlib.metadata import version; print(version("telegram-acl-mcp"))'
   systemctl status telegram-acl-mcp --no-pager
   journalctl -u telegram-acl-mcp -n 100 --no-pager
   ```

   Если `git status --short` показывает изменения, сначала выясните, кто и зачем
   их сделал. Не затирайте их обновлением.

2. Остановите службу и создайте закрытую резервную копию. Остановка обязательна
   для согласованной копии SQLite в WAL-режиме:

   ```bash
   maintenance_stamp=$(date -u +%Y%m%dT%H%M%SZ)
   maintenance_backup=/var/backups/telegram-acl-mcp/$maintenance_stamp
   install -d -o root -g root -m 0700 "$maintenance_backup"
   systemctl stop telegram-acl-mcp
   cp -a /etc/telegram-acl-mcp "$maintenance_backup/etc"
   cp -a /var/lib/telegram-acl-mcp "$maintenance_backup/state"
   git -C /opt/telegram-acl-mcp rev-parse HEAD > "$maintenance_backup/source-commit.txt"
   ```

3. Убедитесь, что процесс действительно остановлен:

   ```bash
   systemctl is-active telegram-acl-mcp
   ```

   Ожидаемый результат — `inactive`. После завершения работ резервную копию
   храните по принятому сроку хранения и защищайте как рабочие секреты.

## Обновление на новую программную версию

Обновляйтесь на конкретный проверенный тег или commit, а не на произвольное
состояние ветки. Сначала прочитайте release notes: особенно изменения формата
конфигурации, минимальной версии Python и схемы базы.

### Установка обновления

После резервного копирования и остановки службы:

```bash
git -C /opt/telegram-acl-mcp fetch --tags --prune origin
release_ref='vX.Y.Z'
git -C /opt/telegram-acl-mcp switch --detach "$release_ref"
chown -R root:root /opt/telegram-acl-mcp
chmod -R go-w /opt/telegram-acl-mcp

/opt/telegram-acl-mcp/.venv/bin/python -m pip install \
  -r /opt/telegram-acl-mcp/requirements.lock
/opt/telegram-acl-mcp/.venv/bin/python -m pip install \
  --no-deps --force-reinstall /opt/telegram-acl-mcp
/opt/telegram-acl-mcp/.venv/bin/python -m pip check

cd /opt/telegram-acl-mcp
/opt/telegram-acl-mcp/.venv/bin/python -m pytest -q
/opt/telegram-acl-mcp/.venv/bin/telegram-acl-mcp --help
/opt/telegram-acl-mcp/.venv/bin/telegram-acl-admin --help
```

`vX.Y.Z` замените на опубликованный тег или полный commit SHA.
Если новая версия требует другой Python, не переиспользуйте старое виртуальное
окружение: создайте новое рядом, установите зафиксированные зависимости,
прогоните тесты и только затем измените `ExecStart` в unit-файле.

Если в релизе менялись `deploy/telegram-acl-mcp.service` или параметры запуска,
сравните unit и установите новый вариант:

```bash
diff -u /etc/systemd/system/telegram-acl-mcp.service \
  /opt/telegram-acl-mcp/deploy/telegram-acl-mcp.service
install -o root -g root -m 0644 \
  /opt/telegram-acl-mcp/deploy/telegram-acl-mcp.service \
  /etc/systemd/system/telegram-acl-mcp.service
systemctl daemon-reload
```

Не заменяйте рабочий `config.json` файлом `config.example.json`: пример содержит
вымышленные значения и не является миграцией конфигурации. Требуемые новые поля
перенесите в рабочий конфиг вручную.

Запустите и проверьте службу:

```bash
systemctl start telegram-acl-mcp
systemctl status telegram-acl-mcp --no-pager
journalctl -u telegram-acl-mcp -n 100 --no-pager
```

Затем переподключите Hermes к MCP: набор инструментов и их JSON Schema могли
измениться. Проверьте `telegram_list_allowed_chats`, чтение одного известного
сообщения и, если индекс включён, `telegram_get_index_status`. Дождитесь свежего
`last_sync` и проверьте поиск по известной фразе.

### Откат

Если smoke test не прошёл, остановите службу. Верните commit из
`source-commit.txt`, снова установите зависимости и пакет из старого checkout.
Если новая версия успела изменить данные несовместимым образом, восстановите
также **весь** каталог состояния из одной резервной копии, а не только файл
`index.sqlite3`:

```bash
systemctl stop telegram-acl-mcp
previous_commit='FULL_COMMIT_FROM_BACKUP'
git -C /opt/telegram-acl-mcp switch --detach "$previous_commit"
/opt/telegram-acl-mcp/.venv/bin/python -m pip install \
  -r /opt/telegram-acl-mcp/requirements.lock
/opt/telegram-acl-mcp/.venv/bin/python -m pip install \
  --no-deps --force-reinstall /opt/telegram-acl-mcp
```

Верните старые unit/config/state из резервной копии при необходимости,
восстановите владельцев и права, выполните `systemctl daemon-reload`, запустите
службу и повторите проверки. Не запускайте старую версию поверх базы с более
новой схемой: сервер специально отклоняет неизвестную новую версию схемы.

## Очистка SQLite-базы

Локальная база — производный индекс Telegram: сообщения, поисковый FTS-индекс,
транскрипции, очередь и прогресс синхронизации. Источником остаётся Telegram,
поэтому безопасный штатный способ очистки — создать пустую базу и позволить
службе построить её заново.

### Полный сброс с возможностью восстановления

1. Посмотрите фактический путь в `indexing.database`:

   ```bash
   /opt/telegram-acl-mcp/.venv/bin/python -c \
     'import json; print(json.load(open("/etc/telegram-acl-mcp/config.json"))["indexing"].get("database", "/var/lib/telegram-acl-mcp/index.sqlite3"))'
   ```

   Дальнейший пример предполагает стандартный путь
   `/var/lib/telegram-acl-mcp/index.sqlite3`.

2. Остановите службу и переместите все части базы в закрытый карантин:

   ```bash
   systemctl stop telegram-acl-mcp
   database_stamp=$(date -u +%Y%m%dT%H%M%SZ)
   database_quarantine=/var/backups/telegram-acl-mcp/db-reset-$database_stamp
   install -d -o root -g root -m 0700 "$database_quarantine"
   for database_file in \
     /var/lib/telegram-acl-mcp/index.sqlite3 \
     /var/lib/telegram-acl-mcp/index.sqlite3-wal \
     /var/lib/telegram-acl-mcp/index.sqlite3-shm \
     /var/lib/telegram-acl-mcp/index.sqlite3.lock
   do
     if [ -e "$database_file" ]; then
       mv "$database_file" "$database_quarantine/"
     fi
   done
   ```

   Если настроен другой путь, замените **каждый** путь явно; не применяйте
   рекурсивное удаление или широкий wildcard к `/var/lib`.

3. Решите, что должно произойти после очистки:

   - чтобы оставить индекс пустым и не собирать его снова, установите
     `"indexing": {"enabled": false}` и только затем запускайте службу;
   - чтобы перестроить индекс, оставьте индексацию включённой. Новая база будет
     создана автоматически, а история заново загрузится начиная с
     `history_since` либо сохранённого в конфиге периода;
   - если `history_since` не задан, новая база рассчитает начало как «текущее
     время минус `history_days`». Зафиксируйте `history_since` явно, если период
     должен остаться прежним.

4. Запустите службу и проверьте журнал и покрытие:

   ```bash
   systemctl start telegram-acl-mcp
   systemctl status telegram-acl-mcp --no-pager
   journalctl -u telegram-acl-mcp -n 100 --no-pager
   ```

   При перестроении дождитесь `history_done`, свежего `last_sync` и нужного
   количества транскрипций через `telegram_get_index_status`.

Не очищайте отдельные таблицы ручными `DELETE`: таблицы `messages`,
`search_index`, `transcripts`, `transcription_jobs`, `sync_state` и `meta`
связаны между собой. Частичная очистка может оставить неверное покрытие,
поисковый индекс или очередь. Для удаления данных только одного чата удалите
его из ACL и перезапустите включённый индекс: приложение уберёт этот peer из
активных таблиц. Это логическое удаление; для требования гарантированного
стирания применяйте принятую на сервере процедуру уничтожения файла, его
резервных копий и снимков диска. На SSD одного `VACUUM` или перезаписи файла
недостаточно для доказуемого физического стирания.

## Авторизация под другим пользователем Telegram

Сессию нельзя заменить через MCP. Это делает администратор отдельной CLI.
Действующая утилита намеренно не перезаписывает существующий файл, поэтому новую
сессию сначала создают под временным именем.

Важно:

- новый аккаунт должен уже состоять в нужных чатах и видеть нужную историю;
- `access_hash` пользователя/канала зависит от авторизованного аккаунта, поэтому
  старый ACL нельзя считать пригодным;
- SQLite привязан к Telegram account ID. Для другого аккаунта нужна новая база
  или полный сброс старой;
- отдельный служебный Telegram-аккаунт безопаснее личного: read-only относится
  к MCP-интерфейсу, а сама MTProto-сессия обладает правами аккаунта.

### Переключение

1. Выполните общую резервную копию и оставьте службу остановленной.

2. Авторизуйте новый аккаунт в новом файле. Можно использовать прежние
   `api_id`/API hash приложения, если это соответствует вашей политике:

   ```bash
   /opt/telegram-acl-mcp/.venv/bin/telegram-acl-admin \
     --api-id 123456 \
     --api-hash-file /etc/telegram-acl-mcp/api-hash.secret \
     --session-file /etc/telegram-acl-mcp/session.next.secret
   ```

   Замените `123456` на `api_id` из рабочего `config.json`. Введите телефон
   нового аккаунта, одноразовый код и 2FA-пароль. CLI выведет
   метаданные доступных диалогов, но не саму сессию.

3. По выводу CLI подготовьте `/etc/telegram-acl-mcp/config.next.json` с новым
   ACL. Для `channel`/`user` возьмите новый `access_hash`; для обычной группы
   используется `kind: "group"` без обязательного hash. Сохраните прежние
   `token_file` и, если не хотите менять MCP-клиентов, прежний Bearer token.

   Для включённой индексации укажите новый путь, например:

   ```json
   "database": "/var/lib/telegram-acl-mcp/index-next-account.sqlite3"
   ```

4. Проверьте JSON и установите безопасные права:

   ```bash
   /opt/telegram-acl-mcp/.venv/bin/python -m json.tool \
     /etc/telegram-acl-mcp/config.next.json >/dev/null
   chown root:telegram-mcp \
     /etc/telegram-acl-mcp/session.next.secret \
     /etc/telegram-acl-mcp/config.next.json
   chmod 0640 \
     /etc/telegram-acl-mcp/session.next.secret \
     /etc/telegram-acl-mcp/config.next.json
   ```

5. Атомарно переключите конфигурацию и сессию в том же каталоге. Резервная
   копия из начала процедуры уже содержит старые файлы:

   ```bash
   mv /etc/telegram-acl-mcp/session.next.secret \
     /etc/telegram-acl-mcp/session.secret
   mv /etc/telegram-acl-mcp/config.next.json \
     /etc/telegram-acl-mcp/config.json
   ```

6. Запустите службу:

   ```bash
   systemctl start telegram-acl-mcp
   systemctl status telegram-acl-mcp --no-pager
   journalctl -u telegram-acl-mcp -n 100 --no-pager
   ```

   Ошибка `Telegram session is not authorized` означает, что новый session-файл
   не завершил вход. Ошибка `Index belongs to another Telegram account` означает,
   что в конфиге остался путь к базе предыдущего аккаунта.

7. Переподключите Hermes, чтобы он получил новый enum `chat_key`. Проверьте
   список разрешённых чатов, чтение известного сообщения и состояние индекса.
   Убедитесь, что ни один чат старого аккаунта не доступен.

### Откат смены аккаунта

Остановите службу, верните согласованную пару старых `config.json` и
`session.secret` из одной резервной копии, при необходимости верните прежний
каталог состояния, восстановите владельца `root:telegram-mcp` и права `0640` для
секретов, затем запустите службу. Не смешивайте сессию одного аккаунта, ACL с
`access_hash` другого и SQLite третьего.

## Краткий чек-лист после обслуживания

- `systemctl status` показывает `active (running)`, а в свежем журнале нет
  циклических рестартов и ошибок авторизации/схемы.
- MCP по-прежнему слушает только `127.0.0.1:8765`; внешний порт не открыт.
- `telegram_list_allowed_chats` содержит ровно ожидаемый ACL.
- Читается известное сообщение из каждого критичного чата.
- При включённом индексе обновляется `last_sync`, понятны все
  `incomplete_reasons`, поиск находит контрольную фразу.
- Hermes не читает session/API hash/SQLite, а файлы в `/etc/telegram-acl-mcp`
  принадлежат `root:telegram-mcp` и не доступны остальным пользователям.
- Зафиксированы установленный commit, время работ, результат проверки и путь к
  резервной копии.
