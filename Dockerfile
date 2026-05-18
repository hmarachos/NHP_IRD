# Используем официальный образ Python
FROM python:3.12-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    poppler-utils \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем .env.example как .env (если файла нет, он будет создан)
COPY .env.example .env

# Копируем исходный код
COPY . .

# Создаем необходимые директории
RUN mkdir -p instance/uploads

# Устанавливаем переменные окружения
ENV FLASK_APP=run.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# Открываем порт
EXPOSE 5000

# Запускаем приложение
CMD ["python", "run.py"]