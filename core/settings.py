from pathlib import Path
import os
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')


def _env_bool(name: str) -> bool:
    return _env_required(name).lower() in {'1', 'true', 'yes', 'on'}


def _env_csv(name: str) -> list:
    raw = _env_required(name)
    return [item.strip() for item in str(raw).split(',') if item.strip()]


def _env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return str(value).strip()


# --- SECURITY SETTINGS ---
SECRET_KEY = _env_required('DJANGO_SECRET_KEY')
DEBUG = _env_bool('DJANGO_DEBUG')
ALLOWED_HOSTS = _env_csv('DJANGO_ALLOWED_HOSTS')
SSH_PUBLIC_KEY_ENCRYPTION_KEY = _env_required('SSH_PUBLIC_KEY_ENCRYPTION_KEY')

# CSRF trusted origins must include ingress/browser origins with scheme,
# e.g. https://jupiter.prod.home
CSRF_TRUSTED_ORIGINS = _env_csv('DJANGO_CSRF_TRUSTED_ORIGINS')

# If not explicitly set, derive conservative defaults from ALLOWED_HOSTS.
if not CSRF_TRUSTED_ORIGINS:
    inferred_origins = []
    for host in ALLOWED_HOSTS:
        if host in {'*', '0.0.0.0'}:
            continue
        inferred_origins.append(f'http://{host}')
        inferred_origins.append(f'https://{host}')
    CSRF_TRUSTED_ORIGINS = sorted(set(inferred_origins))

# Reverse proxy / ingress awareness (k3s ingress / nginx / traefik)
USE_X_FORWARDED_HOST = _env_bool('DJANGO_USE_X_FORWARDED_HOST')
USE_X_FORWARDED_PORT = _env_bool('DJANGO_USE_X_FORWARDED_PORT')
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Cookie security can be toggled from env for HTTPS ingress deployments.
SESSION_COOKIE_SECURE = _env_bool('DJANGO_SESSION_COOKIE_SECURE')
CSRF_COOKIE_SECURE = _env_bool('DJANGO_CSRF_COOKIE_SECURE')

# core/settings.py

INSTALLED_APPS = [
    "unfold",  # <--- MUST BE AT THE VERY TOP
    "unfold.contrib.filters",  
    "unfold.contrib.forms",    
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "manager", # Your app
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',  # Serve static files with Daphne
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'

# --- CHANNELS & WEBSOCKETS ---
# Requires: pip install channels channels-redis
REDIS_HOST = _env_required('REDIS_HOST')
REDIS_PORT = int(_env_required('REDIS_PORT'))
REDIS_CACHE_TIMEOUT = int(_env_required('REDIS_CACHE_TIMEOUT'))
REDIS_CACHE_MAX_CONNECTIONS = int(_env_required('REDIS_CACHE_MAX_CONNECTIONS'))

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(REDIS_HOST, REDIS_PORT)],
            "capacity": 1500,
            "expiry": 10,
        },
    },
}

# --- DATABASE (Postgres on Docker) ---
# Requires: pip install psycopg2-binary
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': _env_required('DB_NAME'),
        'USER': _env_required('DB_USER'),
        'PASSWORD': _env_required('DB_PASSWORD'),
        'HOST': _env_required('DB_HOST'),
        'PORT': _env_required('DB_PORT'),
        'CONN_MAX_AGE': 600,  # Connection pooling
        'OPTIONS': {
            'connect_timeout': 10,
        }
    }
}

# --- CACHE (Redis on Docker) ---
# Requires: pip install django-redis
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://{REDIS_HOST}:{REDIS_PORT}/1",
        "TIMEOUT": REDIS_CACHE_TIMEOUT,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 2,
            "SOCKET_TIMEOUT": 2,
            "CONNECTION_POOL_KWARGS": {
                "max_connections": REDIS_CACHE_MAX_CONNECTIONS,
                "retry_on_timeout": True,
                "socket_keepalive": True,
            },
            # Keep UI usable during short Redis/network blips.
            "IGNORE_EXCEPTIONS": True,
        }
    }
}

# --- INTERNATIONALIZATION ---
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# --- STATIC FILES ---
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# --- WHITENOISE CONFIG (for Daphne static file serving) ---
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- UNFOLD CONFIGURATION ---
UNFOLD = {
    "SITE_TITLE": "Jupiter Manager",
    "SITE_HEADER": "Jupiter Admin",
    "SITE_SYMBOL": "public",
    "SHOW_HISTORY": True,
    "SEARCH_PAGE_LABEL": "Search Inventory",
    "COLORS": {
        "primary": {
            "50": "253 246 242",
            "100": "250 234 225",
            "200": "244 209 193",
            "300": "235 176 152",
            "400": "222 138 106",
            "500": "206 111 77",
            "600": "182 90 58",
            "700": "150 70 46",
            "800": "116 54 37",
            "900": "83 39 29"
        }
    },
    "SIDEBAR": {
        "show_search": True,
        "navigation": [
            {
                "title": "Administration",
                "items": [
                    {"title": "Admin Page", "icon": "dashboard", "link": "/admin/"},
                    {"title": "User Management", "icon": "group", "link": "/admin/auth/user/"},
                    {"title": "Host Management", "icon": "dns", "link": "/admin/manager/host/"},
                ],
            },
            {
                "title": "Infrastructure",
                "items": [
                    {"title": "Virtual Machines", "icon": "memory", "link": "/admin/manager/virtualmachine/"},
                    {"title": "Network", "icon": "hub", "link": "/admin/manager/host/?tab=network#main-tab-bar"},
                    {"title": "Storage", "icon": "storage", "link": "/admin/manager/host/?tab=storage#main-tab-bar"},
                ],
            },
            {
                "title": "Development",
                "items": [
                    {"title": "ESXi API Docs", "icon": "api", "link": "/api/v1/docs#ESXi"},
                    {"title": "Proxmox API Docs", "icon": "api", "link": "/api/v1/docs#Proxmox"},
                ],
            },
        ],
    },
}