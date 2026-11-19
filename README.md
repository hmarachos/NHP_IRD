# Архив ИРД на Django

## Django и PostgreSQL

Проект работает только на Django 5.2 и PostgreSQL 16. Ветка `main` обслуживает
приложение на `127.0.0.1:5050`; внешний HTTPS-доступ идёт через ngrok.
Для портала и Django Admin требуется вход. PostgreSQL доступен локально на
`127.0.0.1:5438`. Flask runtime и ветка удалены; общие OCR/документные сервисы
оставлены в `app/` как Django-зависимости. SQLite-резерв и файловый архив
сохранены. Подробности перехода описаны в [`DJANGO_MIGRATION_PLAN.md`](DJANGO_MIGRATION_PLAN.md).

Запуск Django web и PostgreSQL:

```bash
docker compose up --build -d django-web
```

Планировщик OCR и очереди запускается отдельным профилем:

```bash
docker compose --profile post-migration up --build -d django-scheduler
```

При запуске Django применяет миграции и использует постоянный PostgreSQL
volume; старый SQLite-снимок автоматически не импортируется. Финальный импорт
на этой установке выполнен 2026-11-19: из архивной SQLite перенесены 197 строк
в 11 таблиц. После cutover в PostgreSQL появились новые Django-only записи,
поэтому SQLite теперь является архивным снимком, а не источником обновления.
Не запускайте `import_legacy_sqlite --replace` поверх действующей базы.
Резервные копии находятся в `instance/backups/django-cutover-*`; текущие файлы
проекта — в `uploads` и `instance/acts`.

Email-уведомления Django включены; SMTP настроен. Django Admin доступен по
`/admin/`; superuser создаётся отдельно для каждой установки и не входит в
импорт бизнес-данных.

## Возможности

- Проекты с отдельными документами, комплектностью, согласованиями и перепиской.
- Пакетная загрузка PDF и фотографий, включая HEIC; OCR, извлечение реквизитов
  AI-моделью и локальный fallback.
- Редактирование реквизитов, сроков и заметок, архивирование/восстановление
  документов, PDF и Word preview и постраничный просмотр PDF.
- Редактируемые шаблоны комплектности, несколько документов в поддерживаемых
  типах строк, сроки, замена редакций и архивные связи.
- Генерация описей, запросов данных и актов приёма-передачи.
- ZIP-экспорт проекта с JSON-данными, оригиналами и SHA-256 манифестом.

## Проверка кода

```bash
.venv-django/bin/python manage.py check
.venv-django/bin/python manage.py makemigrations --check --dry-run
.venv-django/bin/python manage.py test migration_portal
```

При cutover все 11 таблиц прошли SHA-256 аудит. После cutover Django продолжил
записывать данные в PostgreSQL, поэтому текущее состояние намеренно отличается
от исходной SQLite. Резервные копии PostgreSQL, SQLite и файлов сохранены.
