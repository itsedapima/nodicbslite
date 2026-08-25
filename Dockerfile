FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps for PostgreSQL, Pillow, weasyprint
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev libjpeg62-turbo-dev zlib1g-dev \
    libffi-dev libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
