# Используем официальный образ Python
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Создаем необходимые директории
RUN mkdir -p instance/uploads

# Устанавливаем переменные окружения
ENV FLASK_APP=run.py
ENV FLASK_ENV=production

# Открываем порт
EXPOSE 5000

# Запускаем приложение
CMD ["python", "run.py"]