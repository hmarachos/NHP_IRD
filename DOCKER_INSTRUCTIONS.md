# Запуск Django и PostgreSQL в Docker

## Подготовка

1. Установите Docker Desktop.
2. Скопируйте `.env.example` в `.env` и задайте уникальные `SECRET_KEY` и `DJANGO_POSTGRES_PASSWORD`.
3. Укажите SMTP и OpenAI параметры, если нужны email-уведомления и AI-извлечение.

## Запуск

```bash
docker compose up --build -d django-web
docker compose --profile post-migration up --build -d django-scheduler
```

Портал доступен на `http://127.0.0.1:5050`, Django Admin — по адресу
`http://127.0.0.1:5050/admin/`, PostgreSQL доступен локально на `127.0.0.1:5438`.
Внешний доступ настраивается через HTTPS reverse proxy; `ALLOWED_HOSTS` и
`CSRF_TRUSTED_ORIGINS` должны включать домен прокси. Портал требует авторизации.

При первом запуске entrypoint применяет Django-миграции. Архивная SQLite не
импортируется автоматически: PostgreSQL уже содержит записи, созданные после
перехода, и является единственным рабочим источником данных. Не запускайте
`import_legacy_sqlite --replace` поверх действующей базы. Команду аудита можно
использовать только для чтения:

```bash
docker compose exec django-web python manage.py audit_legacy_sqlite \
  --source /app/instance/documents.db
```

Создание superuser:

```bash
docker compose exec django-web python manage.py createsuperuser
```

## Управление

```bash
docker compose ps
docker compose logs -f django-web
docker compose logs -f django-scheduler
docker compose restart django-web django-scheduler
docker compose stop django-web django-scheduler
```

Не запускайте `docker compose down -v` без отдельной резервной копии: команда
удаляет PostgreSQL volume. Загруженные документы хранятся в `uploads/`, акты —
в `instance/acts/`; их резервируйте отдельно вместе с PostgreSQL.

## Проверки

```bash
docker compose exec django-web python manage.py check
docker compose exec django-web python manage.py makemigrations --check --dry-run
docker compose exec django-web python manage.py test migration_portal
```
