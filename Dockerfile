FROM python:3.11-slim

# Системные пакеты: tzdata — чтобы APScheduler корректно работал с Europe/Moscow.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Сначала зависимости — кэшируется отдельным слоем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код
COPY . .

# Логи и база реестра монтируются volume'ами извне
RUN mkdir -p /app/logs /app/data

EXPOSE 8000

# Health-чек проверяет /health у самого приложения
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
