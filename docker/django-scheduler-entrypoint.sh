#!/bin/sh
set -eu

# The web container owns Django migrations. Start the queue worker only after
# that container has completed its migrations and responds to the DB health check.
until python -c 'from urllib.request import Request, urlopen; urlopen(Request("http://django-web:8000/health/", headers={"Host": "127.0.0.1", "X-Forwarded-Proto": "https"}), timeout=3).read()' >/dev/null 2>&1; do
    sleep 2
done

exec python manage.py run_scheduler
