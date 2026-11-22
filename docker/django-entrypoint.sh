#!/bin/sh
set -eu

if [ "${DJANGO_SKIP_MIGRATIONS:-false}" != "true" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
