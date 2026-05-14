# Инструкция по запуску с Docker

## Предварительные требования

1. Установите [Docker](https://docs.docker.com/get-docker/)
2. Установите [Docker Compose](https://docs.docker.com/compose/install/)

## Быстрый старт

1. Скопируйте файл с переменными окружения (если еще не создан):
   ```bash
   cp .env.example .env
   ```

2. Отредактируйте файл `.env` и укажите свои значения:
   ```bash
   # Откройте файл в текстовом редакторе
   nano .env
   ```

3. Запустите приложение:
   ```bash
   docker-compose up -d
   ```

4. Приложение будет доступно по адресу: http://localhost:5000

**Примечание:** Dockerfile автоматически скопирует `.env` файл в контейнер. Если `.env` не существует, будет использован `.env.example`.

## Команды Docker Compose

- **Запуск в фоновом режиме**: `docker-compose up -d`
- **Остановка**: `docker-compose down`
- **Просмотр логов**: `docker-compose logs -f`
- **Пересборка образа**: `docker-compose build`
- **Перезапуск**: `docker-compose restart`
- **Проверка статуса**: `docker-compose ps`

## Переменные окружения

Основные переменные окружения, которые можно настроить в файле `.env`:

| Переменная | Описание | Значение по умолчанию |
|------------|----------|----------------------|
| `SECRET_KEY` | Ключ безопасности Flask | `dev-secret-change-me` |
| `OPENAI_API_KEY` | Ключ API OpenAI | (обязательно для AI функций) |
| `OPENAI_MODEL` | Модель OpenAI | `gpt-5.2` |
| `USE_AI` | Использовать AI функции | `true` |
| `TESSERACT_LANG` | Язык для OCR | `rus+eng` |
| `OCR_DPI` | DPI для OCR | `300` |
| `MAX_CONTENT_LENGTH` | Максимальный размер файла | `26214400` (25MB) |

## Структура томов

- `./instance` - монтируется как `/app/instance` в контейнере (содержит базу данных)
- `./uploads` - монтируется как `/app/instance/uploads` в контейнере (загруженные файлы)

## Разработка с Docker

Для разработки можно использовать Docker с монтированием исходного кода:

```bash
# Запуск с пересборкой
docker-compose up --build

# Запуск с просмотром логов
docker-compose up

# Остановка и удаление контейнеров
docker-compose down
```

## Устранение неполадок

1. **Проблемы с правами доступа к файлам**:
   ```bash
   sudo chown -R $USER:$USER instance/ uploads/
   ```

2. **Контейнер не запускается**:
   ```bash
   docker-compose logs web
   ```

3. **Пересборка образа**:
   ```bash
   docker-compose build --no-cache
   ```

4. **Очистка Docker**:
   ```bash
   docker-compose down -v  # удаляет тома
   docker system prune -a  # очистка неиспользуемых ресурсов
   ```

## Без Docker

Если вы предпочитаете запускать приложение без Docker:

1. Установите Python 3.11+
2. Установите системные зависимости:
   ```bash
   sudo apt-get install tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng poppler-utils
   ```
3. Установите Python зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Настройте переменные окружения в `.env`
5. Запустите приложение:
   ```bash
   python run.py
   ```