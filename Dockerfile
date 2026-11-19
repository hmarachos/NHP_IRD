FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential tesseract-ocr tesseract-ocr-rus libreoffice-writer fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-runtime.txt requirements-django.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p instance/uploads
RUN python manage.py collectstatic --noinput
ENTRYPOINT ["sh", "docker/django-entrypoint.sh"]
EXPOSE 8000
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "180", "--bind", "0.0.0.0:8000", "django_site.wsgi:application"]
