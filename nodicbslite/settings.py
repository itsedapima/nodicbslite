"""
NODi Lite — Settings
====================
Lightweight chama management backend, adapted from NODi CBS.
Each chama instance runs its own Django project with its own database.
"""

import os
from decimal import Decimal
from pathlib import Path
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Load .env ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
except ImportError:
    pass


# ── Env helpers ────────────────────────────────────────────────────────────
def env_bool(key, default=False):
    return os.environ.get(key, str(default)).strip().lower() in ('true', '1', 'yes', 'on')


def env_list(key, default=''):
    return [v.strip() for v in os.environ.get(key, default).split(',') if v.strip()]


def env_required(key):
    val = os.environ.get(key, '').strip()
    if not val:
        raise ImproperlyConfigured(f"Required environment variable {key} is missing or empty.")
    return val


# ══════════════════════════════════════════════════════════════════════════
#  CORE
# ══════════════════════════════════════════════════════════════════════════
DEBUG = env_bool('DJANGO_DEBUG', True)

if DEBUG:
    SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'insecure-dev-key-not-for-production')
else:
    SECRET_KEY = env_required('DJANGO_SECRET_KEY')

CHAMA_NAME = os.environ.get('CHAMA_NAME', 'demo')

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'web,127.0.0.1,localhost')
CSRF_TRUSTED_ORIGINS = env_list(
    'CSRF_TRUSTED_ORIGINS',
    'https://nodicbslite.peshapcloud.com',
)

ROOT_URLCONF = 'nodicbslite.urls'
WSGI_APPLICATION = 'nodicbslite.wsgi.application'
AUTH_USER_MODEL = 'accounts.CustomUser'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = 'login'

# ── Admin hardening ────────────────────────────────────────────────────────
ADMIN_URL_PATH = os.environ.get('ADMIN_URL_PATH', 'chama-admin')
ADMIN_OTP_REQUIRED = env_bool('ADMIN_OTP_REQUIRED', True)


# ══════════════════════════════════════════════════════════════════════════
#  STATIC & MEDIA
# ══════════════════════════════════════════════════════════════════════════
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'mediafiles'


# ══════════════════════════════════════════════════════════════════════════
#  APPS — Lightweight chama stack
# ══════════════════════════════════════════════════════════════════════════
INSTALLED_APPS = [
    'django_q',
    'accounts',
    'audit',
    'administration',
    'accounting',
    'approvals',
    'customers',
    'dashboard',
    'transactions',
    'reports',
    'statements',
    'loans',
    'androidapi',
    'androidadminapi',
    'sms',
    'data_imports',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'crispy_forms',
    'crispy_bootstrap5',
    'widget_tweaks',
    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt',
    'corsheaders',
]


# ══════════════════════════════════════════════════════════════════════════
#  MIDDLEWARE
# ══════════════════════════════════════════════════════════════════════════
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'accounts.middleware.OfficialLockoutMiddleware',
    'accounts.middleware.GlobalRateLimitMiddleware',
    'nodicbslite.audit_middleware.AuditMiddleware',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'administration.context_processors.chama_branding',
                'administration.context_processors.pending_approvals_count',
            ],
        },
    },
]


# ══════════════════════════════════════════════════════════════════════════
#  DATABASE — One DB per chama, accessed via PgBouncer
# ══════════════════════════════════════════════════════════════════════════
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'nodicbslite_db'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', 'Bigman@2026'),
        'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
        'PORT': os.environ.get('DB_PORT', '5432'),
        'DISABLE_SERVER_SIDE_CURSORS': True,
        'CONN_MAX_AGE': 0,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'connect_timeout': 10,
        },
    }
}

if not DEBUG and not DATABASES['default']['PASSWORD']:
    raise ImproperlyConfigured("DB_PASSWORD is missing or empty in production.")


# ══════════════════════════════════════════════════════════════════════════
#  CACHE
# ══════════════════════════════════════════════════════════════════════════
REDIS_HOST = os.environ.get('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.environ.get('REDIS_PORT', '6379'))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', '')

_redis_auth = f':{REDIS_PASSWORD}@' if REDIS_PASSWORD else ''

if os.name == 'nt':
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'nodicbslite-cache',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': f'redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/1',
            'OPTIONS': {'protocol': 2},
        }
    }


# ══════════════════════════════════════════════════════════════════════════
#  DJANGO-Q2 — Single lightweight worker for chamas
# ══════════════════════════════════════════════════════════════════════════
Q_CLUSTER = {
    'name': f'{CHAMA_NAME}-cluster',
    'workers': 2,
    'recycle': 300,
    'timeout': 120,
    'retry': 180,
    'max_attempts': 2,
    'queue': 'default',
    'schedule': True,
    'orm': 'default',
}

if os.name != 'nt':
    Q_CLUSTER.pop('orm', None)
    Q_CLUSTER['redis'] = {
        'host': REDIS_HOST,
        'port': REDIS_PORT,
        'db': 0,
        **(({'password': REDIS_PASSWORD, 'protocol': 2}) if REDIS_PASSWORD else {}),
    }


# ══════════════════════════════════════════════════════════════════════════
#  DRF / JWT / CORS
# ══════════════════════════════════════════════════════════════════════════
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    'DEFAULT_FILTER_BACKENDS': [
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '20/minute',
        'user': '120/minute',
    },
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(hours=12),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
}

CORS_ALLOWED_ORIGINS = env_list(
    'CORS_ALLOWED_ORIGINS',
    'http://127.0.0.1:8000,http://localhost:8000',
)


# ══════════════════════════════════════════════════════════════════════════
#  M-PESA DARAJA
# ══════════════════════════════════════════════════════════════════════════
MPESA_ENVIRONMENT     = os.environ.get('MPESA_ENVIRONMENT', 'sandbox')
MPESA_CONSUMER_KEY    = os.environ.get('MPESA_CONSUMER_KEY', '')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET', '')
MPESA_SHORTCODE       = os.environ.get('MPESA_SHORTCODE', '')
MPESA_PASSKEY         = os.environ.get('MPESA_PASSKEY', '')
MPESA_STK_CALLBACK_URL = os.environ.get('MPESA_STK_CALLBACK_URL', '')

MPESA_B2C_SHORTCODE           = os.environ.get('MPESA_B2C_SHORTCODE', '')
MPESA_B2C_INITIATOR_NAME      = os.environ.get('MPESA_B2C_INITIATOR_NAME', '')
MPESA_B2C_SECURITY_CREDENTIAL = os.environ.get('MPESA_B2C_SECURITY_CREDENTIAL', '')
MPESA_B2C_RESULT_URL          = os.environ.get('MPESA_B2C_RESULT_URL', '')
MPESA_B2C_TIMEOUT_URL         = os.environ.get('MPESA_B2C_TIMEOUT_URL', '')

C2B_WHITELIST_ENFORCE = not DEBUG
TRUST_XFF_FOR_MPESA = True
C2B_WHITELISTED_IPS = [
    '196.201.214.200', '196.201.214.206', '196.201.213.114', '196.201.214.207',
    '196.201.214.208', '196.201.213.44', '196.201.212.127', '196.201.212.138',
    '196.201.212.129', '196.201.212.136', '196.201.212.74', '196.201.212.69',
    '127.0.0.1', 'localhost',
]


# ── SMS Gateway ────────────────────────────────────────────────────────────
SMS_API_URL     = os.environ.get('SMS_API_URL', 'https://isms.celcomafrica.com/api/services/sendsms/')
SMS_API_KEY     = os.environ.get('SMS_API_KEY', '')
SMS_PARTNER_ID  = os.environ.get('SMS_PARTNER_ID', '')
SMS_SHORTCODE   = os.environ.get('SMS_SHORTCODE', '')
SMS_TIMEOUT     = int(os.environ.get('SMS_TIMEOUT', '15'))
SMS_SENDER_NAME = os.environ.get('SMS_SENDER_NAME', 'NODiLite')


# ══════════════════════════════════════════════════════════════════════════
#  EMAIL
# ══════════════════════════════════════════════════════════════════════════
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND',
    'django.core.mail.backends.smtp.EmailBackend',
)

EMAIL_HOST          = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT          = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_USE_TLS       = env_bool('EMAIL_USE_TLS', True)
EMAIL_HOST_USER     = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL  = os.environ.get('DEFAULT_FROM_EMAIL', '')
ADMIN_EMAIL         = os.environ.get('ADMIN_EMAIL', '')


# ══════════════════════════════════════════════════════════════════════════
#  APPROVAL WORKFLOW
# ══════════════════════════════════════════════════════════════════════════
DEFAULT_APPROVAL_COUNT = int(os.environ.get('DEFAULT_APPROVAL_COUNT', '2'))
ADMIN_SELF_APPROVE = env_bool('ADMIN_SELF_APPROVE', False)


# ══════════════════════════════════════════════════════════════════════════
#  SESSIONS & SECURITY
# ══════════════════════════════════════════════════════════════════════════
SESSION_COOKIE_AGE = 1800  # 30 min for chama officials
SESSION_SAVE_EVERY_REQUEST = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

X_FRAME_OPTIONS             = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER   = True
SECURE_REFERRER_POLICY      = 'same-origin'

SECURITY_MAX_LOGIN_ATTEMPTS              = 5
SECURITY_LOGIN_LOCKOUT_MINUTES           = 30
SECURITY_OTP_EXPIRY_MINUTES              = 10
SECURITY_OTP_DAILY_ABUSE_THRESHOLD       = 10
SECURITY_OTP_COOLDOWN_SECONDS            = 60
SECURITY_PASSWORD_RESET_COOLDOWN_SECONDS = 60

if not DEBUG:
    SECURE_SSL_REDIRECT            = False
    SECURE_PROXY_SSL_HEADER        = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_HSTS_SECONDS            = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD            = True
    SESSION_COOKIE_SECURE          = True
    CSRF_COOKIE_SECURE             = True
    SESSION_COOKIE_HTTPONLY         = True


# ══════════════════════════════════════════════════════════════════════════
#  QUOTATION / PRICING (for onboarding flow)
# ══════════════════════════════════════════════════════════════════════════
PRICING_BASE_MONTHLY = int(os.environ.get('PRICING_BASE_MONTHLY', '1000'))    # KES
PRICING_PER_MEMBER   = int(os.environ.get('PRICING_PER_MEMBER', '20'))        # KES per member
PRICING_PER_PRODUCT  = int(os.environ.get('PRICING_PER_PRODUCT', '100'))      # KES per product
PRICING_MOBILE_ADDON = int(os.environ.get('PRICING_MOBILE_ADDON', '500'))     # KES if mobile loans
PRICING_MIN_MONTHLY  = int(os.environ.get('PRICING_MIN_MONTHLY', '2000'))     # KES minimum


# ══════════════════════════════════════════════════════════════════════════
#  I18N / FORMS
# ══════════════════════════════════════════════════════════════════════════
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_L10N = True
USE_TZ = True

CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap5'
CRISPY_TEMPLATE_PACK = 'bootstrap5'


# ══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'nodicbslite.logging_fmt.JSONFormatter',
        },
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console_json': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console_json'],
        'level': 'INFO',
    },
    'loggers': {
        'django.security': {'handlers': ['console_json'], 'level': 'WARNING', 'propagate': False},
        'django.request': {'handlers': ['console_json'], 'level': 'WARNING', 'propagate': False},
        'audit': {'handlers': ['console_json'], 'level': 'INFO', 'propagate': False},
        'security': {'handlers': ['console_json'], 'level': 'INFO', 'propagate': False},
    },
}
