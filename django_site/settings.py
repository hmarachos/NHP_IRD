"""Django settings for the migrated NHP IRD application."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
os.environ.setdefault(
    "DJANGO_DATABASE_PATH",
    os.getenv("DJANGO_DATABASE_PATH", str(BASE_DIR / "instance" / "django-documents.db")),
)
SECRET_KEY = os.getenv("SECRET_KEY", "replace-this-before-deploying")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    host for host in [*os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(","), "testserver"] if host
]
ROOT_URLCONF = "django_site.urls"
WSGI_APPLICATION = "django_site.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "migration_portal.apps.MigrationPortalConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django_site.csrf_alias.CsrfAliasMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "migration_portal.middleware.ProjectAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "migration_portal" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.contrib.auth.context_processors.auth",
            "django.template.context_processors.request",
            "django.contrib.messages.context_processors.messages",
        ]},
    },
    {
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "app" / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "environment": "django_site.jinja.environment",
            "context_processors": ["django_site.jinja.context_processor"],
        },
    },
]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.getenv("DJANGO_POSTGRES_DB", "nhp_ird_django"),
    "USER": os.getenv("DJANGO_POSTGRES_USER", "nhp_ird"),
    "PASSWORD": os.getenv("DJANGO_POSTGRES_PASSWORD", "local-dev-only-change-me"),
    "HOST": os.getenv("DJANGO_POSTGRES_HOST", "127.0.0.1"),
    "PORT": os.getenv("DJANGO_POSTGRES_PORT", "5438"),
    "CONN_MAX_AGE": 60,
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Minsk"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "app" / "static"]
MEDIA_ROOT = os.getenv("UPLOAD_DIR", str(BASE_DIR / "instance" / "uploads"))
ACTS_DIR = os.getenv("ACTS_DIR", str(BASE_DIR / "instance" / "acts"))
MEDIA_URL = "/media/"
# Keep Django's disk-spooling threshold; it does not restrict file size.
DATA_UPLOAD_MAX_MEMORY_SIZE = None
DATA_UPLOAD_MAX_NUMBER_FILES = None
MIGRATION_SOURCE_DB = os.getenv("LEGACY_SQLITE_SOURCE_PATH", str(BASE_DIR / "instance" / "documents.db"))
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "rus+eng")
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
USE_AI = os.getenv("USE_AI", "true").lower() in {"1", "true", "yes", "on"}
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
EXPIRY_WARNING_DAYS = int(os.getenv("EXPIRY_WARNING_DAYS", "30"))
MAIL_TIMEZONE = os.getenv("MAIL_TIMEZONE", "Europe/Minsk")
MAIL_SEND_HOUR = int(os.getenv("MAIL_SEND_HOUR", "8"))
MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_APP_PASSWORD = os.getenv("MAIL_APP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "") or MAIL_USERNAME
MAIL_RECIPIENT = os.getenv("MAIL_RECIPIENT", "")
REGISTRATION_NOTIFICATION_EMAIL = os.getenv("REGISTRATION_NOTIFICATION_EMAIL", "a.kazantsev@beloil.by")
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = MAIL_SERVER
EMAIL_PORT = MAIL_PORT
EMAIL_HOST_USER = MAIL_USERNAME
EMAIL_HOST_PASSWORD = "".join(MAIL_APP_PASSWORD.split())
EMAIL_USE_TLS = MAIL_PORT != 465
EMAIL_USE_SSL = MAIL_PORT == 465
EMAIL_TIMEOUT = 30
DEFAULT_FROM_EMAIL = MAIL_FROM
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
CSRF_TRUSTED_ORIGINS = [origin for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 3600 if not DEBUG else 0
X_FRAME_OPTIONS = "SAMEORIGIN"
