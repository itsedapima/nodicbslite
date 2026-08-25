#!/bin/bash
set -e

# ═══════════════════════════════════════════════════════════════════════════════
# NODi Lite — Container Entrypoint
# ═══════════════════════════════════════════════════════════════════════════════
# Routes to the correct process based on CONTAINER_ROLE:
#   web     → Gunicorn (Django app server)
#   worker  → Django-Q2 background tasks
# ═══════════════════════════════════════════════════════════════════════════════

ROLE="${CONTAINER_ROLE:-web}"
echo "=== NODi Lite Entrypoint ==="
echo "Chama: ${CHAMA_NAME:-unknown}"
echo "Role:  ${ROLE}"

# ─── Wait for PostgreSQL ──────────────────────────────────────────────────────
wait_for_db() {
    echo "Waiting for database..."
    local retries=0
    until python -c "
import psycopg2, os
psycopg2.connect(
    host=os.environ.get('DB_HOST', 'peshap_pgbouncer'),
    port=os.environ.get('DB_PORT', '6432'),
    user=os.environ.get('DB_USER', ''),
    password=os.environ.get('DB_PASSWORD', ''),
    dbname=os.environ.get('DB_NAME', ''),
)
" 2>/dev/null; do
        retries=$((retries + 1))
        if [ $retries -ge 30 ]; then
            echo "ERROR: Database not ready after 60s. Aborting."
            exit 1
        fi
        echo "  DB not ready, waiting 2s... (attempt ${retries}/30)"
        sleep 2
    done
    echo "Database is ready."
}

# ─── Wait for Redis ──────────────────────────────────────────────────────────
wait_for_redis() {
    echo "Waiting for Redis..."
    local retries=0
    until python -c "
import redis, os
r = redis.Redis(
    host=os.environ.get('REDIS_HOST', 'redis'),
    port=int(os.environ.get('REDIS_PORT', '6379')),
    password=os.environ.get('REDIS_PASSWORD', ''),
)
r.ping()
" 2>/dev/null; do
        retries=$((retries + 1))
        if [ $retries -ge 15 ]; then
            echo "ERROR: Redis not ready after 30s. Aborting."
            exit 1
        fi
        echo "  Redis not ready, waiting 2s... (attempt ${retries}/15)"
        sleep 2
    done
    echo "Redis is ready."
}

# ─── Role: web ────────────────────────────────────────────────────────────────
start_web() {
    wait_for_db
    wait_for_redis

    echo "Running migrations..."
    python manage.py migrate --noinput

    echo "Collecting static files..."
    python manage.py collectstatic --noinput 2>/dev/null || true

    # Create superuser if env vars are set
    if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
        echo "Ensuring superuser exists..."
        python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='${DJANGO_SUPERUSER_USERNAME}').exists():
    User.objects.create_superuser(
        username='${DJANGO_SUPERUSER_USERNAME}',
        email='${DJANGO_SUPERUSER_EMAIL:-admin@nodicbslite.co.ke}',
        password='${DJANGO_SUPERUSER_PASSWORD}',
        role='admin',
    )
    print('Superuser created.')
else:
    print('Superuser already exists.')
"
    fi

    echo "Starting Gunicorn..."
    exec gunicorn nodicbslite.wsgi:application \
        --bind 0.0.0.0:8000 \
        --workers "${GUNICORN_WORKERS:-2}" \
        --threads 2 \
        --timeout 120 \
        --access-logfile - \
        --error-logfile -
}

# ─── Role: worker ─────────────────────────────────────────────────────────────
start_worker() {
    wait_for_db
    wait_for_redis

    echo "Starting Django-Q2 cluster..."
    exec python manage.py qcluster
}

# ─── Dispatch ─────────────────────────────────────────────────────────────────
case "$ROLE" in
    web)
        start_web
        ;;
    worker)
        start_worker
        ;;
    *)
        echo "ERROR: Unknown CONTAINER_ROLE '${ROLE}'. Expected: web, worker"
        exit 1
        ;;
esac
