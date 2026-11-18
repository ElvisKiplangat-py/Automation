import os
import json
import base64
import calendar
import time
import hashlib
import hmac
import struct
import re
import ipaddress
import html
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from collections import defaultdict
from threading import Lock
from functools import wraps
from base64 import b32encode, b32decode, urlsafe_b64encode
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


from flask import (
   Flask, render_template, make_response,
   request, redirect,
   url_for, flash, jsonify, g, session, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import inspect as sqlalchemy_inspect, text as sqlalchemy_text

try:
   from flask_limiter import Limiter
   from flask_limiter.util import get_remote_address
except ImportError:
   Limiter = None
   get_remote_address = lambda: request.remote_addr or "unknown"
from flask_login import (
   LoginManager, UserMixin,
   login_user, logout_user,
   login_required, current_user
)
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from country_data import COUNTRIES, COUNTRY_NAME_BY_CODE

from dotenv import load_dotenv

try:
   NAIROBI_TIMEZONE = ZoneInfo("Africa/Nairobi")
except ZoneInfoNotFoundError:
   # Kenya uses EAT (UTC+3) year round; retain the correct offset in slim images
   # that do not include the system IANA timezone database.
   NAIROBI_TIMEZONE = timezone(timedelta(hours=3), name="EAT")


def utc_now_naive():
   """Return UTC time without tzinfo for the app's existing naive DB columns."""
   return datetime.now(timezone.utc).replace(tzinfo=None)


try:
   from authlib.integrations.flask_client import OAuth
except ImportError:
   OAuth = None

try:
   from xai_sdk import Client
   from xai_sdk.chat import user, system
except ImportError:
   Client = None
   user = None
   system = None

# ----------------- LOAD ENV -----------------
load_dotenv()


def env_first(*keys, default=None):
   for key in keys:
       raw_value = os.getenv(key)
       if raw_value is None:
           continue
       value = raw_value.strip()
       if value:
           return value
   return default


def env_bool(*keys, default=False):
   value = env_first(*keys)
   if value is None:
       return default
   return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(*keys, default=0):
   value = env_first(*keys)
   if value is None:
       return default
   try:
       return int(value.strip())
   except ValueError:
       return default


app_env = env_first("APP_ENV", "FLASK_ENV", default="development").strip().lower()
is_production = app_env == "production"
debug_mode = env_bool("FLASK_DEBUG", default=False)
if is_production:
   debug_mode = False

app = Flask(__name__)
app.config["WITHDRAWAL_TEST_LOOP_ENABLED"] = (
   not is_production and env_bool("WITHDRAWAL_TEST_LOOP_ENABLED", default=False)
)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
if not app.secret_key:
   raise RuntimeError("SECRET_KEY is required")
if is_production and app.secret_key == "dev-secret-key":
   raise RuntimeError("Set a unique SECRET_KEY before running in production")
if is_production and len(app.secret_key) < 32:
   raise RuntimeError("Use a production SECRET_KEY with at least 32 characters")

# ----------------- DATABASE CONFIG -----------------
def normalize_database_uri(uri):
   if not uri:
       return uri
   # Railway or other hosts may provide postgres:// or an explicit psycopg 3
   # URL. This app installs psycopg2-binary, so select that driver explicitly
   # instead of relying on SQLAlchemy's default PostgreSQL driver.
   if uri.startswith("postgres://"):
       uri = "postgresql://" + uri[len("postgres://"):]
   if uri.startswith("postgresql+psycopg://"):
       return "postgresql+psycopg2://" + uri[len("postgresql+psycopg://"):]
   if uri.startswith("postgresql://"):
       return "postgresql+psycopg2://" + uri[len("postgresql://"):]
   # Railway provides mysql://, but SQLAlchemy needs mysql+pymysql://
   if uri.startswith("mysql://"):
       return "mysql+pymysql://" + uri[len("mysql://"):]
   return uri


# Prioritize full database URLs provided by hosting platforms like Render/Railway

database_uri = normalize_database_uri(
   env_first("DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "MYSQL_PUBLIC_URL", "MYSQL_URL")
)

if not database_uri:
   # Prefer Railway-style keys first because MYSQL_HOST can be "db" from local compose.
   db_user = env_first("MYSQLUSER", "MYSQL_USER")
   db_password = env_first("MYSQLPASSWORD", "MYSQL_PASSWORD")
   db_host = env_first("MYSQLHOST", "MYSQL_HOST")
   db_port = env_first("MYSQLPORT", "MYSQL_PORT", default="3306")
   db_name = env_first("MYSQLDATABASE", "MYSQL_DATABASE")

   if is_production and db_host == "db":
       raise RuntimeError(
           "Invalid database host 'db' for production. "
           "Set SQLALCHEMY_DATABASE_URI, MYSQL_PUBLIC_URL, MYSQL_URL, "
           "or MYSQLHOST to your Railway database host."
       )

   missing = [
       name for name, value in (
           ("MYSQLUSER/MYSQL_USER", db_user),
           ("MYSQLPASSWORD/MYSQL_PASSWORD", db_password),
           ("MYSQLHOST/MYSQL_HOST", db_host),
           ("MYSQLDATABASE/MYSQL_DATABASE", db_name),
       ) if not value
   ]
   if missing:
       if is_production:
           raise RuntimeError(f"Missing database environment variables: {', '.join(missing)}")

       os.makedirs(app.instance_path, exist_ok=True)
       database_uri = f"sqlite:///{os.path.join(app.instance_path, 'app-dev.sqlite')}"
   else:
       database_uri = (
           f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
       )

if is_production and database_uri.startswith("sqlite:"):
   raise RuntimeError("Production requires a managed PostgreSQL or MySQL database; SQLite is for local use only.")

base_url = env_first("BASE_URL")
if not base_url:
   railway_domain = env_first("RAILWAY_PUBLIC_DOMAIN")
   if railway_domain:
       base_url = railway_domain.strip()
       if not base_url.startswith(("http://", "https://")):
           base_url = f"https://{base_url}"
   else:
       base_url = "http://localhost:5000"
base_url = base_url.rstrip("/")
if is_production and not base_url.startswith("https://"):
   raise RuntimeError("Production requires BASE_URL to use the public HTTPS website address.")

app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
   "pool_pre_ping": True,
   "pool_recycle": 280,
   "pool_size": max(1, env_int("DB_POOL_SIZE", default=5)),
   "max_overflow": max(0, env_int("DB_MAX_OVERFLOW", default=10)),
   "pool_timeout": max(1, env_int("DB_POOL_TIMEOUT", default=30)),
}
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = env_bool("SESSION_COOKIE_SECURE", default=is_production)
app.config["REMEMBER_COOKIE_SECURE"] = env_bool("REMEMBER_COOKIE_SECURE", default=is_production)
app.config["GOOGLE_CLIENT_ID"] = env_first("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = env_first("GOOGLE_CLIENT_SECRET")
app.config["MPESA_ENV"] = env_first("MPESA_ENV", default="sandbox").lower()
app.config["MPESA_ENABLED"] = env_bool("MPESA_ENABLED", default=not is_production)
app.config["MPESA_CONSUMER_KEY"] = env_first("MPESA_CONSUMER_KEY", "DARAJA_CONSUMER_KEY")
app.config["MPESA_CONSUMER_SECRET"] = env_first("MPESA_CONSUMER_SECRET", "DARAJA_CONSUMER_SECRET")
app.config["MPESA_SHORTCODE"] = env_first("MPESA_SHORTCODE", "DARAJA_SHORTCODE", default="174379")
app.config["MPESA_PASSKEY"] = env_first("MPESA_PASSKEY", "DARAJA_PASSKEY")
app.config["MPESA_TRANSACTION_TYPE"] = env_first(
   "MPESA_TRANSACTION_TYPE",
   default="CustomerPayBillOnline",
)
app.config["MPESA_ACCOUNT_REFERENCE"] = env_first(
   "MPESA_ACCOUNT_REFERENCE",
   default="SkyViewRecharge",
)
app.config["MPESA_TRANSACTION_DESC"] = env_first(
   "MPESA_TRANSACTION_DESC",
   default="Wallet recharge",
)
app.config["MPESA_CALLBACK_URL"] = env_first("MPESA_CALLBACK_URL", "DARAJA_CALLBACK_URL")
app.config["MPESA_CALLBACK_TOKEN"] = env_first("MPESA_CALLBACK_TOKEN")
live_mpesa_credentials_configured = bool(
   app.config["MPESA_ENV"] == "production"
   and app.config["MPESA_CONSUMER_KEY"]
   and app.config["MPESA_CONSUMER_SECRET"]
   and app.config["MPESA_SHORTCODE"]
   and app.config["MPESA_PASSKEY"]
)
if live_mpesa_credentials_configured and not is_production:
   raise RuntimeError("Set APP_ENV=production before configuring live M-PESA credentials.")
if (
   is_production
   and app.config["MPESA_ENABLED"]
   and app.config["MPESA_CALLBACK_URL"]
   and urlsplit(app.config["MPESA_CALLBACK_URL"]).scheme != "https"
):
   raise RuntimeError("Production MPESA_CALLBACK_URL must use HTTPS.")
app.config["MPESA_REQUEST_TIMEOUT"] = env_int("MPESA_REQUEST_TIMEOUT", default=20)
app.config["MPESA_MAX_RETRIES"] = max(1, env_int("MPESA_MAX_RETRIES", default=3))
app.config["PAYPAL_ENV"] = env_first("PAYPAL_ENV", default="live").lower()
app.config["PAYPAL_CLIENT_ID"] = env_first("PAYPAL_CLIENT_ID")
app.config["PAYPAL_CLIENT_SECRET"] = env_first("PAYPAL_CLIENT_SECRET")
app.config["PAYPAL_WEBHOOK_ID"] = env_first("PAYPAL_WEBHOOK_ID")
app.config["PAYPAL_KES_PER_USD"] = env_first("PAYPAL_KES_PER_USD")
app.config["DISPLAY_KES_PER_USD"] = env_first(
   "DISPLAY_KES_PER_USD",
   "CURRENCY_KES_PER_USD",
   "PAYPAL_KES_PER_USD",
)
app.config["PAYPAL_REQUEST_TIMEOUT"] = max(5, env_int("PAYPAL_REQUEST_TIMEOUT", default=20))
if app.config["PAYPAL_ENV"] not in {"sandbox", "live"}:
   raise RuntimeError("PAYPAL_ENV must be either sandbox or live.")
if app.config["PAYPAL_ENV"] == "live" and not is_production:
   raise RuntimeError("Set APP_ENV=production before configuring live PayPal payments.")
app.config["PAYSTACK_ENV"] = env_first("PAYSTACK_ENV", default="test").lower()
app.config["PAYSTACK_SECRET_KEY"] = env_first("PAYSTACK_SECRET_KEY")
app.config["PAYSTACK_USD_ENABLED"] = env_bool("PAYSTACK_USD_ENABLED", default=False)
app.config["PAYSTACK_REQUEST_TIMEOUT"] = max(5, env_int("PAYSTACK_REQUEST_TIMEOUT", default=20))
if app.config["PAYSTACK_ENV"] not in {"test", "live"}:
   raise RuntimeError("PAYSTACK_ENV must be either test or live.")
if app.config["PAYSTACK_ENV"] == "live" and not is_production:
   raise RuntimeError("Set APP_ENV=production before configuring live Paystack payments.")
if app.config["PAYSTACK_SECRET_KEY"]:
   expected_paystack_prefix = "sk_live_" if app.config["PAYSTACK_ENV"] == "live" else "sk_test_"
   if not app.config["PAYSTACK_SECRET_KEY"].startswith(expected_paystack_prefix):
       raise RuntimeError(f"PAYSTACK_SECRET_KEY must be a {app.config['PAYSTACK_ENV']} secret key.")
app.config["MPESA_RETRY_DELAY_SECONDS"] = max(0, env_int("MPESA_RETRY_DELAY_SECONDS", default=1))
app.config["COMPANY_LEGAL_NAME"] = env_first("COMPANY_LEGAL_NAME", default="Sky View Agency")
app.config["COMPANY_REGISTRATION_NUMBER"] = env_first("COMPANY_REGISTRATION_NUMBER", default="Certified by the County Government")
app.config["COMPANY_ADDRESS"] = env_first("COMPANY_ADDRESS", default="P.O BOX 10-20200")
app.config["SUPPORT_EMAIL"] = env_first(
   "SUPPORT_EMAIL",
   default="" if is_production else "mymail9115@proton.me",
)
if is_production and not app.config["SUPPORT_EMAIL"]:
   raise RuntimeError("Set SUPPORT_EMAIL to the public support mailbox before starting production.")
app.config["SUPPORT_PHONE"] = env_first("SUPPORT_PHONE", default="Contact Referee for support")
app.config["SUPPORT_HOURS"] = env_first("SUPPORT_HOURS", default="Monday to Friday, 9:00 AM - 5:00 PM")
app.config["POLICY_LAST_UPDATED"] = env_first("POLICY_LAST_UPDATED", default="July 8, 2026")
app.config["ADMIN_EMAILS"] = env_first("ADMIN_EMAILS", default="")
app.config["TURNSTILE_SITE_KEY"] = env_first("TURNSTILE_SITE_KEY", default="")
app.config["TURNSTILE_SECRET_KEY"] = env_first("TURNSTILE_SECRET_KEY", default="")
turnstile_keys_configured = bool(
   app.config["TURNSTILE_SITE_KEY"] and app.config["TURNSTILE_SECRET_KEY"]
)
if bool(app.config["TURNSTILE_SITE_KEY"]) != bool(app.config["TURNSTILE_SECRET_KEY"]):
   raise RuntimeError("Configure both TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY, or remove both.")
app.config["TURNSTILE_ENABLED"] = env_bool(
   "TURNSTILE_ENABLED",
   default=turnstile_keys_configured,
)
if app.config["TURNSTILE_ENABLED"] and not turnstile_keys_configured:
   raise RuntimeError("TURNSTILE_ENABLED requires both Turnstile keys.")
app.config["TURNSTILE_REQUEST_TIMEOUT"] = max(
   3,
   env_int("TURNSTILE_REQUEST_TIMEOUT", default=8),
)
app.config["TRUST_CLOUDFLARE_IP_HEADER"] = env_bool("TRUST_CLOUDFLARE_IP_HEADER", default=False)
app.config["CLOUDFLARE_ORIGIN_TOKEN"] = env_first("CLOUDFLARE_ORIGIN_TOKEN", default="")
app.config["COUNTRY_LOCATION_ENFORCEMENT"] = env_bool(
   "COUNTRY_LOCATION_ENFORCEMENT",
   default=is_production,
)
app.config["RATELIMIT_STORAGE_URI"] = env_first(
   "RATELIMIT_STORAGE_URI",
   "REDIS_URL",
   default="memory://",
)
app.config["ADMIN_SESSION_MAX_AGE"] = 30 * 60


def get_client_ip():
   if app.config["TRUST_CLOUDFLARE_IP_HEADER"] and trusted_cloudflare_request():
       cloudflare_ip = request.headers.get("CF-Connecting-IP", "").strip()
       if cloudflare_ip:
           try:
               return str(ipaddress.ip_address(cloudflare_ip))
           except ValueError:
               pass
   return get_remote_address()


def trusted_cloudflare_request():
   expected = app.config.get("CLOUDFLARE_ORIGIN_TOKEN", "")
   supplied = request.headers.get("X-SVA-Origin-Token", "")
   return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def detected_country_code():
   """Return Cloudflare's country only when its proxy headers are configured as trusted."""
   if (
       not app.config.get("TRUST_CLOUDFLARE_IP_HEADER")
       or not trusted_cloudflare_request()
   ):
       return None
   country_code = request.headers.get("CF-IPCountry", "").strip().upper()
   return country_code if country_code in COUNTRY_NAME_BY_CODE else None


def validate_country_location(selected_country_code):
   if not app.config.get("COUNTRY_LOCATION_ENFORCEMENT"):
       return None, None
   detected = detected_country_code()
   if not detected:
       return None, (
           "We could not verify your location. Turn off any VPN or proxy and try again. "
           "If the problem continues, contact support."
       )
   if selected_country_code != detected:
       return detected, (
           f"Your network location appears to be {COUNTRY_NAME_BY_CODE[detected]}. "
           "Select that country to create your account. A VPN or proxy can affect this check."
       )
   return detected, None


if Limiter is None:
   if is_production:
       raise RuntimeError("Install requirements.txt; Flask-Limiter is required in production")

   class LocalDevelopmentLimiter:
       """Process-local development fallback; production uses Flask-Limiter with Redis."""
       def __init__(self):
           self._events = defaultdict(list)
           self._lock = Lock()

       def limit(self, limit_value, key_func=None, methods=None, deduct_when=None, error_message=None, **kwargs):
           match = re.fullmatch(
               r"\s*(\d+)\s+per\s+(?:(\d+)\s+)?(second|minute|hour|day)s?\s*",
               limit_value,
           )
           if not match:
               raise ValueError(f"Unsupported local development rate limit: {limit_value}")
           count = int(match.group(1))
           window_seconds = int(match.group(2) or 1) * {
               "second": 1,
               "minute": 60,
               "hour": 3600,
               "day": 86400,
           }[match.group(3)]

           def decorate(view):
               @wraps(view)
               def wrapped(*args, **inner_kwargs):
                   if methods and request.method not in methods:
                       return view(*args, **inner_kwargs)
                   domain = (key_func or get_client_ip)()
                   bucket = (view.__module__, view.__name__, limit_value, str(domain))
                   now = time.monotonic()
                   with self._lock:
                       events = [stamp for stamp in self._events[bucket] if now - stamp < window_seconds]
                       if len(events) >= count:
                           self._events[bucket] = events
                           abort(429, description=error_message or "Too many requests")
                       events.append(now)
                       self._events[bucket] = events
                   response = view(*args, **inner_kwargs)
                   if deduct_when and not deduct_when(response):
                       with self._lock:
                           events = self._events[bucket]
                           try:
                               events.remove(now)
                           except ValueError:
                               pass
                   return response
               return wrapped
           return decorate

   limiter = LocalDevelopmentLimiter()
   app.logger.warning("Flask-Limiter is unavailable; using process-local development limits")
else:
   limiter = Limiter(
       key_func=get_client_ip,
       app=app,
       default_limits=[],
       meta_limits=["20 per hour"],
       storage_uri=app.config["RATELIMIT_STORAGE_URI"],
       headers_enabled=True,
   )
   if is_production and app.config["RATELIMIT_STORAGE_URI"].startswith("memory://"):
       raise RuntimeError(
           "Production requires shared rate-limit storage. Configure RATELIMIT_STORAGE_URI with Redis; "
           "in-memory limits do not span workers or instances."
       )

if (
   is_production
   and app.config["COUNTRY_LOCATION_ENFORCEMENT"]
   and not app.config["TRUST_CLOUDFLARE_IP_HEADER"]
):
   raise RuntimeError(
       "Production country verification requires Cloudflare proxy headers. "
       "Enable Cloudflare IP Geolocation, configure the origin-token transform rule, "
       "then set TRUST_CLOUDFLARE_IP_HEADER=true."
   )
if (
   is_production
   and app.config["COUNTRY_LOCATION_ENFORCEMENT"]
   and len(app.config["CLOUDFLARE_ORIGIN_TOKEN"]) < 32
):
   raise RuntimeError(
       "Production country verification requires CLOUDFLARE_ORIGIN_TOKEN with at least 32 characters. "
       "Set the same value in Railway and a Cloudflare request-header transform rule."
   )
if (
   app.config["MPESA_ENABLED"]
   and (is_production or live_mpesa_credentials_configured)
   and not app.config["MPESA_CALLBACK_TOKEN"]
):
   raise RuntimeError("Production requires MPESA_CALLBACK_TOKEN to authenticate payment callbacks.")
if (
   app.config["MPESA_ENABLED"]
   and (is_production or live_mpesa_credentials_configured)
   and len(app.config["MPESA_CALLBACK_TOKEN"]) < 32
):
   raise RuntimeError("MPESA_CALLBACK_TOKEN must contain at least 32 characters in production.")


db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
oauth = OAuth(app) if OAuth else None

if oauth:
   if app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
       oauth.register(
           name="google",
           client_id=app.config["GOOGLE_CLIENT_ID"],
           client_secret=app.config["GOOGLE_CLIENT_SECRET"],
           authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
           access_token_url="https://oauth2.googleapis.com/token",
           userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
           jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
           client_kwargs={"scope": "openid email profile"},
       )

# Custom Jinja2 filter to format timestamp
@app.template_filter('strftime')
def _jinja2_filter_datetime(timestamp, fmt='%Y-%m-%d %H:%M:%S'):
   if timestamp is None:
       return 'N/A'
   # The filter expects a datetime object from SQLAlchemy, but the original
   # instruction specified a unix timestamp. To be safe, we handle both.
   if not isinstance(timestamp, datetime):
       try:
           timestamp = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
       except (ValueError, TypeError):
           return timestamp  # Return original value if conversion fails
   if timestamp.tzinfo is None:
       timestamp = timestamp.replace(tzinfo=timezone.utc)
   return timestamp.astimezone(NAIROBI_TIMEZONE).strftime(fmt)


def current_currency_code(user=None):
   user = user or current_user
   if getattr(user, "is_authenticated", False):
       endpoint = request.endpoint or ""
       if endpoint.startswith("admin_") or endpoint.startswith("control_center"):
           return "KES"
       return "KES" if getattr(user, "country_code", None) == "KE" else "USD"
   return "KES"


def currency_kes_per_usd():
   raw_rate = app.config.get("DISPLAY_KES_PER_USD")
   try:
       rate = Decimal(str(raw_rate))
   except (InvalidOperation, TypeError, ValueError):
       return None
   return rate if rate.is_finite() and rate > 0 else None


def user_currency_amount(value, user=None, rounding=ROUND_HALF_UP):
   """Convert a KES ledger amount into the signed-in member's display currency."""
   try:
       amount = Decimal(str(value or 0))
   except (InvalidOperation, TypeError, ValueError):
       amount = Decimal("0")
   if current_currency_code(user) == "KES":
       return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
   rate = currency_kes_per_usd()
   if rate is None:
       return None
   return (amount / rate).quantize(Decimal("0.01"), rounding=rounding)


@app.template_filter("money")
def format_user_money(value):
   currency = current_currency_code()
   converted = user_currency_amount(value)
   if converted is None:
       return "USD "
       amount /= rate

   amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
   return f"{currency} {amount:,.2f}"


def format_loan_limit(value):
   """Display a loan cap without rounding its USD input ceiling above the cap."""
   currency = current_currency_code()
   converted = user_currency_amount(value, rounding=ROUND_DOWN)
   if converted is None:
       return "USD "
   if currency == "KES":
       return f"KES {converted:,.0f}"
   return f"USD {converted:,.2f}"


app.add_template_filter(format_loan_limit, "loan_limit")
app.add_template_filter(format_loan_deposit_threshold, "loan_deposit_threshold")


def parse_user_amount_to_kes(raw_value, *, allow_decimals=False):
   """Parse an amount entered in the member's display currency into ledger KES."""
   currency = current_currency_code()
   pattern = r"\d{1,9}(?:\.\d{1,2})?" if allow_decimals and currency == "USD" else r"\d{1,9}"
   raw_value = str(raw_value or "").strip()
   if not re.fullmatch(pattern, raw_value):
       raise ValueError("Enter a valid amount.")
   amount = Decimal(raw_value)
   if currency == "KES":
       if amount != amount.to_integral_value():
           raise ValueError("Enter a whole KES amount.")
       return int(amount)
   rate = currency_kes_per_usd()
   if rate is None:
       raise ValueError("Currency conversion is temporarily unavailable. Please contact support.")
   converted = int((amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
   if converted > 999_999_999:
       raise ValueError("That amount is above the maximum supported wallet transaction.")
   return converted


# Add security headers to prevent caching
@app.after_request
def add_security_headers(response):
   static_filename = (request.view_args or {}).get("filename", "")
   is_shared_static_image = (
       request.endpoint == "static"
       and bool(request.args.get("v"))
       and (
           static_filename == "logo.jpeg"
           or static_filename.startswith(("img/pages/", "img/dashboard/"))
       )
   )
   if is_shared_static_image:
       # These versioned images are safe to reuse across full-page navigation.
       response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
       response.headers.pop("Pragma", None)
       response.headers.pop("Expires", None)
   else:
       response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
       response.headers["Pragma"] = "no-cache"
       response.headers["Expires"] = "0"
   response.headers["X-Content-Type-Options"] = "nosniff"
   response.headers["X-Frame-Options"] = "DENY"
   response.headers["Referrer-Policy"] = "no-referrer"
   response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
   response.headers["Content-Security-Policy"] = "base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
   if is_production:
       response.headers["Strict-Transport-Security"] = "max-age=31536000"
   return response


@app.errorhandler(429)
def handle_rate_limit_exceeded(error):
   log_security_event("rate_limit_exceeded", reason=getattr(error, "description", "limit_exceeded"))
   message = "Too many requests. Wait a bit and try again."
   if request.is_json or request.accept_mimetypes.best == "application/json":
       return jsonify({"error": message}), 429
   return message, 429


login_manager = LoginManager(app)
login_manager.login_view = "form2"


@app.before_request
def require_cloudflare_origin_and_member_country():
   if (
       is_production
       and app.config["COUNTRY_LOCATION_ENFORCEMENT"]
       and not trusted_cloudflare_request()
   ):
       abort(403)
   if (
       current_user.is_authenticated
       and not current_user.country_code
       and request.endpoint not in {"country_selection", "logout", "static"}
   ):
       return redirect(url_for("country_selection"))

serializer = URLSafeTimedSerializer(app.secret_key)
passport_serializer = URLSafeTimedSerializer(
   app.secret_key,
   salt="skyview-passport-invite-v1",
)


def is_admin_user(user=None):
   user = user or current_user
   if not getattr(user, "is_authenticated", False):
       return False
   if str(session.get("control_center_user_id", "")) != str(user.get_id()):
       return False
   authenticated_at = session.get("control_center_authenticated_at")
   if not authenticated_at or time.time() - float(authenticated_at) > app.config["ADMIN_SESSION_MAX_AGE"]:
       return False
   return UserRole.query.filter_by(user_id=user.id, role_name="admin").first() is not None


def user_has_role(user, role):
   if not user:
       return False
   return UserRole.query.filter_by(user_id=user.id, role_name=role).first() is not None


def set_agent_role(user_id, enabled):
   role = UserRole.query.filter_by(user_id=user_id, role_name="agent").first()
   if enabled and role is None:
       db.session.add(UserRole(user_id=user_id, role_name="agent"))
   elif not enabled and role is not None:
       db.session.delete(role)


def configured_admin_emails():
   return {
       email.strip().lower()
       for email in app.config.get("ADMIN_EMAILS", "").split(",")
       if email.strip()
   }


def grant_configured_admin_role(user):
   if user.email.strip().lower() in configured_admin_emails() and not user_has_role(user, "admin"):
       db.session.add(UserRole(user_id=user.id, role_name="admin"))


def log_security_event(event, email="", reason=""):
   safe_email = re.sub(r"[\r\n\t]", "", (email or "").strip().lower())[:254]
   safe_reason = re.sub(r"[\r\n\t]", "", (reason or "").strip())[:160]
   safe_path = request.url_rule.rule if request.url_rule else request.path[:120]
   app.logger.warning(
       "security_event=%s ip=%s email=%s path=%s reason=%s",
       event,
       get_client_ip(),
       safe_email or "-",
       safe_path,
       safe_reason or "-",
   )


def admin_mfa_cipher():
   encryption_key = urlsafe_b64encode(hashlib.sha256(app.secret_key.encode("utf-8")).digest())
   return Fernet(encryption_key)


def generate_totp_secret():
   return b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def encrypt_admin_mfa_secret(secret):
   return admin_mfa_cipher().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_admin_mfa_secret(record):
   try:
       return admin_mfa_cipher().decrypt(record.secret_encrypted.encode("ascii")).decode("ascii")
   except (InvalidToken, UnicodeDecodeError, ValueError):
       log_security_event("mfa_secret_decrypt_failed", reason=f"user_id={record.user_id}")
       return None


def totp_value(secret, counter):
   padded_secret = secret + ("=" * ((8 - len(secret) % 8) % 8))
   key = b32decode(padded_secret, casefold=True)
   digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
   offset = digest[-1] & 0x0F
   binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
   return f"{binary % 1_000_000:06d}"


def matching_totp_counter(secret, submitted_code):
   code = (submitted_code or "").strip()
   if not re.fullmatch(r"\d{6}", code):
       return None
   current_counter = int(time.time() // 30)
   for counter in (current_counter - 1, current_counter, current_counter + 1):
       if hmac.compare_digest(totp_value(secret, counter), code):
           return counter
   return None


def consume_totp_counter(user_id, counter):
   changed = AdminMFA.query.filter_by(user_id=user_id).filter(
       AdminMFA.last_counter.is_(None) | (AdminMFA.last_counter < counter)
   ).update({AdminMFA.last_counter: counter}, synchronize_session=False)
   db.session.commit()
   return changed == 1


def login_account_rate_key():
   account = request.form.get("email", "").strip().lower()[:254] or "unknown"
   matched_user = User.query.filter(
       (User.email == account)
       | (User.full_name == request.form.get("email", "").strip()[:100])
   ).first()
   if matched_user:
       account = f"user:{matched_user.id}"
   else:
       account = "unknown:" + hashlib.sha256(account.encode("utf-8")).hexdigest()
   return f"login-account:{account}"


def is_login_attempt(response):
   return request.form.get("action", "").strip().lower() == "login"


def is_registration_attempt(response):
   return request.form.get("action", "").strip().lower() == "register"


def is_forgot_password_attempt(response):
   return request.form.get("action", "").strip().lower() == "forgot"


def is_resend_email_verification_attempt(response):
   return request.form.get("action", "").strip().lower() == "resend_verification"


def hashed_form_value_rate_key(prefix, field):
   value = request.form.get(field, "").strip().lower()[:254] or "unknown"
   digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
   return f"{prefix}:{digest}"


def password_reset_rate_key():
   token = (request.view_args or {}).get("token", "")
   digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
   return f"password-reset:{digest}"


def email_verification_rate_key():
   token = (request.view_args or {}).get("token", "")
   digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
   return f"email-verification:{digest}"


def authenticated_user_rate_key():
   user_id = current_user.get_id() if current_user.is_authenticated else "anonymous"
   return f"user:{user_id}"


def password_policy_error(password):
   if (
       len(password) < 8
       or len(password) > 128
       or not re.search(r"[a-z]", password)
       or not re.search(r"[A-Z]", password)
       or not re.search(r"\d", password)
       or not re.search(r"[^A-Za-z0-9]", password)
   ):
       return "Use 8 to 128 characters with an uppercase letter, a lowercase letter, a number, and a symbol."
   return None


def control_center_account_rate_key():
   account = request.form.get("email", "").strip().lower()[:254] or "unknown"
   digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
   return f"control-center-account:{digest}"


def admin_required(view):
   @wraps(view)
   def wrapped(*args, **kwargs):
       if not current_user.is_authenticated:
           log_security_event("admin_access_denied", reason="not_authenticated")
           return redirect(url_for("control_center"))
       if not user_has_role(current_user, "admin"):
           log_security_event("admin_access_denied", current_user.email, "missing_admin_role")
           abort(403)
       session_user_id = str(session.get("control_center_user_id", ""))
       authenticated_at = session.get("control_center_authenticated_at")
       if session_user_id != str(current_user.id) or not authenticated_at:
           log_security_event("admin_access_denied", current_user.email, "admin_mfa_required")
           return redirect(url_for("control_center"))
       if time.time() - float(authenticated_at) > app.config["ADMIN_SESSION_MAX_AGE"]:
           expired_email = current_user.email
           logout_user()
           session.clear()
           log_security_event("admin_session_expired", expired_email)
           flash("Your admin session expired. Sign in again to continue.", "warning")
           return redirect(url_for("control_center"))
       return view(*args, **kwargs)
   return wrapped


def get_csrf_token():
   token = session.get("csrf_token")
   if not token:
       token = secrets.token_urlsafe(32)
       session["csrf_token"] = token
   return token


def valid_csrf_request():
   expected = session.get("csrf_token", "")
   data = request.get_json(silent=True)
   if not isinstance(data, dict):
       data = {}
   submitted = (
       request.form.get("csrf_token", "")
       or data.get("csrf_token", "")
       or request.headers.get("X-CSRF-Token", "")
   )
   return bool(expected and submitted and secrets.compare_digest(expected, submitted))


def verify_turnstile_response(token):
   """Validate a Turnstile token with Cloudflare before accepting an auth form."""
   if not app.config.get("TURNSTILE_ENABLED"):
       return True
   if not token or len(token) > 2048:
       return False

   payload = urlencode({
       "secret": app.config["TURNSTILE_SECRET_KEY"],
       "response": token,
       "idempotency_key": str(uuid.uuid4()),
   }).encode("utf-8")
   verification_request = urllib_request.Request(
       "https://challenges.cloudflare.com/turnstile/v0/siteverify",
       data=payload,
       headers={"Content-Type": "application/x-www-form-urlencoded"},
       method="POST",
   )

   try:
       with urllib_request.urlopen(
           verification_request,
           timeout=app.config["TURNSTILE_REQUEST_TIMEOUT"],
       ) as response:
           result = json.loads(response.read().decode("utf-8"))
   except Exception as exc:
       # Fail closed, but never log the submitted token or secret key.
       app.logger.warning("Turnstile verification request failed: %s", type(exc).__name__)
       return False

   if not isinstance(result, dict) or not result.get("success"):
       error_codes = result.get("error-codes", []) if isinstance(result, dict) else []
       app.logger.info("Turnstile verification rejected auth form codes=%s", error_codes)
       return False

   expected_host = (urlsplit(request.host_url).hostname or "").lower()
   verified_host = str(result.get("hostname", "")).lower()
   return bool(
       expected_host
       and verified_host == expected_host
       and result.get("action") == "account_access"
   )


def presence_rate_key():
   if current_user.is_authenticated:
       return f"presence-session:{presence_session_key()}"
   return get_client_ip()


def presence_session_key():
   session_token = session.get("presence_session_id")
   if not session_token:
       session_token = secrets.token_urlsafe(32)
       session["presence_session_id"] = session_token
   return hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def remove_current_presence_session():
   session_token = session.pop("presence_session_id", None)
   if not session_token:
       return
   session_key = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
   try:
       ActiveSession.query.filter_by(session_key=session_key).delete(synchronize_session=False)
       db.session.commit()
   except Exception:
       db.session.rollback()
       app.logger.exception("Could not remove presence record during logout")


@app.context_processor
def inject_company_profile():
   admin_access = is_admin_user()
   referral_support_contact = None
   sidebar_investment_value = None
   if current_user.is_authenticated:
       incoming_referral = Referral.query.filter_by(referred_user_id=current_user.id).first()
       if incoming_referral:
           referral_support_contact = agent_whatsapp_support_contact(
               incoming_referral.referrer_user_id
           )
       locked_investments = Investment.query.filter_by(
           user_id=current_user.id,
           status="locked",
       ).all()
       if locked_investments:
           sidebar_now = utc_now_naive()
           sidebar_investment_value = sum(
               (accrued_investment_value(item, sidebar_now) for item in locked_investments),
               Decimal("0.00"),
           )
   else:
       referral_key = find_referral_key(request.args.get("referral_code", ""))
       if referral_key:
           referral_support_contact = agent_whatsapp_support_contact(referral_key.user_id)
   return {
       "is_admin_user": admin_access,
       "email_verification_required": app.config["EMAIL_VERIFICATION_REQUIRED"],
       "pending_withdrawal_count": (
           WithdrawalRequest.query.filter_by(status="pending").count()
           if admin_access else 0
       ),
       "pending_deposit_count": (
           Payment.query.filter_by(
               payment_type="wallet_deposit",
               status="pending",
           ).filter(
               Payment.created_at
               > utc_now_naive() - timedelta(minutes=WALLET_DEPOSIT_PENDING_TIMEOUT_MINUTES)
           ).count()
           if admin_access else 0
       ),
       "pending_task_submission_count": (
           TaskSubmission.query.filter_by(status="pending").count()
           if admin_access else 0
       ),
       "has_agent_role": user_has_role(current_user, "agent") if current_user.is_authenticated else False,
       "sidebar_investment_value": sidebar_investment_value,
       "countries": COUNTRIES,
       "current_country_code": (
           current_user.country_code if current_user.is_authenticated else "KE"
       ),
       "currency_code": current_currency_code(),
       "kes_per_usd_display": str(currency_kes_per_usd() or ""),
       "usd_display_ready": currency_kes_per_usd() is not None,
       "referral_support_whatsapp_url": (
           referral_support_contact["url"] if referral_support_contact else None
       ),
       "referral_support_name": (
           referral_support_contact["name"] if referral_support_contact else None
       ),
       "csrf_token": get_csrf_token(),
       "canonical_base_url": base_url,
       "company_profile": {
           "legal_name": app.config["COMPANY_LEGAL_NAME"],
           "registration_number": app.config["COMPANY_REGISTRATION_NUMBER"],
           "address": app.config["COMPANY_ADDRESS"],
           "support_email": app.config["SUPPORT_EMAIL"],
           "support_phone": app.config["SUPPORT_PHONE"],
           "support_hours": app.config["SUPPORT_HOURS"],
           "policy_last_updated": app.config["POLICY_LAST_UPDATED"],
       }
   }

# ----------------- RESEND EMAIL CONFIG -----------------
app.config["RESEND_API_KEY"] = env_first("RESEND_API_KEY")
app.config["RESEND_API_BASE"] = env_first("RESEND_API_BASE", default="https://api.resend.com")
app.config["RESEND_FROM_EMAIL"] = env_first("RESEND_FROM_EMAIL")
app.config["RESEND_REQUEST_TIMEOUT"] = max(5, env_int("RESEND_REQUEST_TIMEOUT", default=20))
local_development = app_env in {"development", "dev", "local"}
app.config["EMAIL_VERIFICATION_REQUIRED"] = is_production or env_bool(
   "EMAIL_VERIFICATION_REQUIRED",
   default=not local_development,
)
app.config["ADMIN_NOTIFICATION_EMAIL"] = env_first(
   "ADMIN_NOTIFICATION_EMAIL",
   "WITHDRAWAL_NOTIFICATION_EMAIL",
   default=app.config["SUPPORT_EMAIL"],
)
PASSWORD_RESET_GENERIC_MESSAGE = (
   "If an account matches that email, we {payout_amount_text}"
   text_body = (
       "A new withdrawal request needs review.\n\n"
       f"Request: #{withdrawal.id}\n"
       f"User: {user_name} (account {withdrawal.user_id})\n"
       f"Wallet amount reserved: KES {withdrawal.amount:,}\n"
       f"Payout amount: {payout_amount_text}\n"
       f"Payout method: {payout_method.title()}\n"
       f"{payout_label}: {payout_destination}\n"
       f"Status: Pending\n"
       f"Requested: {requested_at}\n\n"
       f"Review it in the control center: {review_url}"
   )
   escaped_name = html.escape(user_name)
   escaped_destination = html.escape(payout_destination)
   escaped_url = html.escape(review_url, quote=True)
   html_body = f"""
       <html><body style="font-family:Arial,sans-serif;color:#20243a">
         <h2>New withdrawal request</h2>
         <p>Request <strong>#{withdrawal.id}</strong> is waiting for review.</p>
         <table cellpadding="7" cellspacing="0" style="border-collapse:collapse">
           <tr><td><strong>User</strong></td><td>{escaped_name} (account {withdrawal.user_id})</td></tr>
           <tr><td><strong>Wallet amount reserved</strong></td><td>KES {withdrawal.amount:,}</td></tr>
           <tr><td><strong>Payout amount</strong></td><td>{html.escape(payout_amount_text)}</td></tr>
           <tr><td><strong>Payout method</strong></td><td>{html.escape(payout_method.title())}</td></tr>
           <tr><td><strong>{html.escape(payout_label)}</strong></td><td>{escaped_destination}</td></tr>
           <tr><td><strong>Status</strong></td><td>Pending</td></tr>
           <tr><td><strong>Requested</strong></td><td>{requested_at}</td></tr>
         </table>
         <p><a href="{escaped_url}">Open withdrawal requests in the control center</a></p>
       </body></html>
   """
   send_email(recipient, subject, text_body, html_body)


def send_deposit_notification(payment):
   recipient = app.config["ADMIN_NOTIFICATION_EMAIL"]
   if not recipient:
       app.logger.warning(
           "deposit_email_skipped payment_id=%s reason=no_recipient",
           payment.id,
       )
       return
   if not is_email_service_configured():
       app.logger.warning(
           "deposit_email_skipped payment_id=%s reason=email_service_not_configured",
           payment.id,
       )
       return

   deposited_at = datetime.now(NAIROBI_TIMEZONE).strftime("%d %b %Y, %H:%M EAT")
   user_name = payment.user.full_name
   is_provider_checkout = payment.payment_method in {"paypal", "paystack"}
   provider_label = {
       "paypal": "PayPal",
       "paystack": "Paystack card",
       "mpesa": "M-PESA",
   }.get(payment.payment_method, payment.payment_method.title())
   phone = f"{provider_label} checkout" if is_provider_checkout else (
       f"+{payment.phone}" if payment.phone.startswith("254") else payment.phone
   )
   receipt = payment.provider_reference if is_provider_checkout else payment.mpesa_receipt_number
   receipt = receipt or "Not provided"
   payment_method = provider_label
   subject = f"Wallet recharge received t start Google sign-in. Please try again or contact support.", "danger")
       return redirect(url_for("form2", next=section))


@app.route("/auth/<provider>/callback")
def oauth_callback(provider):
   client = get_oauth_client(provider)
   if not client:
       session.pop("oauth_signup_phone", None)
       session.pop("oauth_signup_country_code", None)
       flash("Google sign-in is temporarily unavailable. Please try again or use email and password.", "warning")
       return redirect(url_for("form2"))

   try:
       if provider != "google":
           session.pop("oauth_signup_phone", None)
           session.pop("oauth_signup_country_code", None)
           flash("Unsupported sign up provider.", "danger")
           return redirect(url_for("form2"))
       profile = get_verified_google_profile(client)

       user = login_or_create_social_user(
           profile,
           provider,
           referral_code=session.get("oauth_referral_code", ""),
           passport_inviter_id=session.get("oauth_passport_inviter_id"),
           signup_phone=session.get("oauth_signup_phone"),
           signup_country_code=session.get("oauth_signup_country_code"),
           allow_create=session.get("oauth_section") == "register",
       )
       session.pop("oauth_section", None)
       session.pop("oauth_referral_code", None)
       session.pop("oauth_passport_inviter_id", None)
       session.pop("oauth_signup_phone", None)
       session.pop("oauth_signup_country_code", None)
       session.pop("passport_invite_token", None)
       session.pop("invite_referral_code", None)
       return redirect(
           url_for("country_selection")
           if not user.country_code
           else url_for("dashboard")
       )
   except Exception as exc:
       app.logger.exception("OAuth callback failed for %s", provider)
       section = session.pop("oauth_section", "register")
       session.pop("oauth_referral_code", None)
       session.pop("oauth_passport_inviter_id", None)
       session.pop("oauth_signup_phone", None)
       session.pop("oauth_signup_country_code", None)
       session.pop("invite_referral_code", None)
       flash("We couldnll send a fresh link. Check your inbox and Spam folder; if it "
               "doesnt arrive, request a new "
                   "confirmation link from the login screen.",
                   "success",
               )
           else:
               flash("Account created. You can now sign in.", "success")
           return redirect(url_for("form2", next="login", country_code=country_code))

       # ----------------- LOGIN -----------------
       elif action == "login":
           identifier = request.form.get("email", "").strip()
           password = request.form.get("password", "").strip()

           if not identifier or not password:
               log_security_event("login_failed", identifier, "missing_credentials")
               flash("Username/email and password are required.", "danger")
               return redirect(url_for("form2"))

           identifier_lower = identifier.lower()
           user = User.query.filter(
               (User.email == identifier_lower) | (User.full_name == identifier)
           ).first()

           if user and bcrypt.check_password_hash(user.password_hash, password):
               verification = EmailVerification.query.filter_by(user_id=user.id).first()
               if (
                   app.config["EMAIL_VERIFICATION_REQUIRED"]
                   and verification
                   and verification.verified_at is None
               ):
                   flash(
                       "Confirm your email before signing in. Check your inbox, or "
                       "request a new confirmation link below.",
                       "warning",
                   )
                   return redirect(url_for("form2", next="login"))
               login_user(user)
               session.pop("control_center_user_id", None)
               session.pop("control_center_authenticated_at", None)
               session.pop("admin_mfa_setup_user_id", None)
               session.pop("admin_mfa_setup_started_at", None)
               session.pop("passport_invite_token", None)
               session.pop("invite_referral_code", None)
               return redirect(url_for("dashboard"))

           log_security_event("login_failed", identifier, "invalid_credentials")
           flash("Invalid credentials", "danger")
           return redirect(url_for("form2", next="login"))

       # ----------------- FORGOT PASSWORD -----------------
       elif action == "forgot":
           email = request.form.get("email", "").strip().lower()

           if not email:
               flash("Enter the email address associated with your account.", "danger")
               return redirect(url_for("form2", next="forgot"))

           user = User.query.filter_by(email=email).first()

           if not user:
               app.logger.warning(
                   "Password reset email not sent: no local account matched the submitted address"
               )
               flash(PASSWORD_RESET_GENERIC_MESSAGE, "success")
               return redirect(url_for("form2", next="forgot"))

           token = serializer.dumps(email, salt="reset-password")
           reset_link = f"{base_url}/reset/{token}"

           if not is_email_service_configured():
               app.logger.error(
                   "Password reset email was not sent: Resend is not configured",
               )
               flash(PASSWORD_RESET_GENERIC_MESSAGE, "success")
               return redirect(url_for("form2", next="forgot"))

           user.reset_token = token
           db.session.commit()

           try:
               send_reset_email(email, reset_link)
           except Exception:
               app.logger.exception(
                   "Password reset email delivery failed via Resend",
               )
               try:
                   user.reset_token = None
                   db.session.commit()
               except Exception:
                   app.logger.exception("Failed to clear reset token after mail error")

               flash(PASSWORD_RESET_GENERIC_MESSAGE, "success")
               return redirect(url_for("form2", next="forgot"))

           flash(PASSWORD_RESET_GENERIC_MESSAGE, "success")
           return redirect(url_for("form2", next="forgot"))

       return redirect(url_for("form2"))

   section = request.args.get("next", "register")
   if section not in {"register", "login", "forgot"}:
       section = "register"
   query_referral_code = request.args.get("referral_code", "").strip()
   referral_code = normalize_referral_code(
       query_referral_code or session.get("invite_referral_code", "")
   )
   if not query_referral_code and referral_code and not find_referral_key(referral_code):
       referral_code = ""
       session.pop("invite_referral_code", None)
   passport_token = request.args.get("passport_invite", "").strip()
   if passport_token:
       if passport_inviter_id_from_token(passport_token):
           session["passport_invite_token"] = passport_token
           if not query_referral_code:
               referral_code = ""
               session.pop("invite_referral_code", None)
       else:
           session.pop("passport_invite_token", None)
   selected_country_code = request.args.get("country_code", "").strip().upper()
   if selected_country_code not in COUNTRY_NAME_BY_CODE:
       selected_country_code = ""
   visitor_country = detected_country_code()
   detected_country = (
       visitor_country
       if app.config.get("COUNTRY_LOCATION_ENFORCEMENT") else None
   )
   if detected_country:
       selected_country_code = detected_country
   referral_key = find_referral_key(referral_code) if referral_code else None
   referral_support_contact = (
       agent_whatsapp_support_contact(referral_key.user_id) if referral_key else None
   )
   return render_template(
       "form2.html",
       section=section,
       selected_country_code=selected_country_code,
       detected_country_code=detected_country,
       visitor_country_code=visitor_country,
       detected_country_name=COUNTRY_NAME_BY_CODE.get(detected_country, "") if detected_country else "",
       country_location_enforcement=app.config.get("COUNTRY_LOCATION_ENFORCEMENT"),
       turnstile_enabled=app.config.get("TURNSTILE_ENABLED"),
       turnstile_site_key=app.config.get("TURNSTILE_SITE_KEY"),
       referral_code=referral_code,
       has_invitation=bool(referral_code or session.get("passport_invite_token")),
       passport_invite_token=session.get("passport_invite_token", ""),
       referral_support_whatsapp_url=(
           referral_support_contact["url"] if referral_support_contact else None
       ),
       referral_support_name=(
           referral_support_contact["name"] if referral_support_contact else None
       ),
   )


@app.route("/trust")
def trust_center():
   return render_template("public_info.html", page="trust")


@app.route("/terms")
def terms():
   return render_template("public_info.html", page="terms")


@app.route("/privacy")
def privacy():
   return render_template("public_info.html", page="privacy")


@app.route("/contact")
def contact():
   return render_template("public_info.html", page="contact")


@app.route("/referrals/validate")
def validate_referral_code():
   code = normalize_referral_code(request.args.get("code", ""))
   if not code:
       return jsonify({"valid": False, "message": "Enter a referral code to validate."}), 400

   referral_key = find_referral_key(code)
   if not referral_key:
       return jsonify({"valid": False, "message": "Referral code was not found."}), 404

   return jsonify({
       "valid": True,
       "message": "Referral code is valid.",
       "code": referral_key.key[:10].upper(),
   })


# ----------------- DASHBOARD -----------------
CLAIMABLE_CASHBACK = 5500
DEPOSIT_REQUIREMENT_RATE = 0.40
MINIMUM_WALLET_DEPOSIT = 500
TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET = 2200
TASK_OFFER_AVAILABLE_DAYS = 5
WHATSAPP_VIEW_REWARD = 100
LOAN_DEPOSIT_BANDS = (
   {"deposit_minimum": 7000, "maximum_loan": 15500, "repayment_months": 2},
   {"deposit_minimum": 10000, "maximum_loan": 20000, "repayment_months": 3},
   {"deposit_minimum": 15000, "maximum_loan": 30000, "repayment_months": 5},
)
MINIMUM_LOAN_DEPOSIT = LOAN_DEPOSIT_BANDS[0]["deposit_minimum"]
AGENT_PROGRAM_FEE = 5000
WITHDRAWAL_PACKAGES = (5500, 7500, 10000, 15000, 20000, 25000, 30000, 35000)
WITHDRAWAL_REVIEW_HOURS = 12


def qualifying_loan_balance_for_wallet(wallet):
   """Count only successful deposits still covered by available wallet funds."""
   return min(int(wallet["deposit_balance"]), int(wallet["available_balance"]))


def maximum_loan_for_deposit_balance(deposit_balance):
   """Return the highest loan cap unlocked by the member's qualifying deposits."""
   for band in reversed(LOAN_DEPOSIT_BANDS):
       if deposit_balance >= band["deposit_minimum"]:
           return band["maximum_loan"]
   return 0


def loan_repayment_term_months(deposit_balance):
   """Return the repayment term for the qualifying wallet-recharge band."""
   for band in reversed(LOAN_DEPOSIT_BANDS):
       if deposit_balance >= band["deposit_minimum"]:
           return band["repayment_months"]
   return None


def add_calendar_months(value, months):
   """Add calendar months while clamping month-end dates (for example Jan 31)."""
   month_index = value.month - 1 + int(months)
   year = value.year + month_index // 12
   month = month_index % 12 + 1
   day = min(value.day, calendar.monthrange(year, month)[1])
   return value.replace(year=year, month=month, day=day)


def wallet_summary_for_user(user_id):
   paid_total = (
       db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
       .filter(
           Payment.user_id == user_id,
           Payment.payment_type == "wallet_deposit",
           Payment.status == "paid",
       )
       .scalar()
   )
   awarded_loan_total = (
       db.session.query(db.func.coalesce(db.func.sum(LoanRequest.amount), 0))
       .filter(
           LoanRequest.user_id == user_id,
           LoanRequest.status == "approved",
           LoanRequest.credit_awarded_at.isnot(None),
       )
       .scalar()
   )
   withdrawn_total = (
       db.session.query(db.func.coalesce(db.func.sum(WithdrawalRequest.amount), 0))
       .filter(
           WithdrawalRequest.user_id == user_id,
           WithdrawalRequest.status == "paid",
       )
       .scalar()
   )
   reserved_withdrawal_total = (
       db.session.query(db.func.coalesce(db.func.sum(WithdrawalRequest.amount), 0))
       .filter(
           WithdrawalRequest.user_id == user_id,
           WithdrawalRequest.status.in_(("pending", "processing")),
       )
       .scalar()
   )
   referral_credit_total = referral_reward_balance(user_id)
   agent_commission_total = agent_deposit_commission_balance(user_id)
   task_credit_query = (
       db.session.query(db.func.coalesce(db.func.sum(TaskSubmission.reward_amount), 0))
       .select_from(TaskSubmission)
       .join(TaskOffer, TaskOffer.id == TaskSubmission.task_id)
       .filter(
           TaskSubmission.user_id == user_id,
           TaskSubmission.reward_credited_at.isnot(None),
       )
   )
   task_entitlement_exists = db.exists().where(
       db.and_(
           PackagePurchase.user_id == TaskSubmission.user_id,
           db.or_(
               PackagePurchase.package_key == TaskOffer.package_key,
               PackagePurchase.package_key == ALL_INCLUSIVE_PACKAGE_KEY,
           ),
       )
   )
   task_credit_total = task_credit_query.filter(task_entitlement_exists).scalar()
   locked_task_credit_total = task_credit_query.filter(~task_entitlement_exists).scalar()
   all_task_credit_total = int(task_credit_total or 0) + int(locked_task_credit_total or 0)
   package_spend_total = (
       db.session.query(db.func.coalesce(db.func.sum(PackagePurchase.amount), 0))
       .filter_by(user_id=user_id)
       .scalar()
   )
   investment_principal_locked = (
       db.session.query(db.func.coalesce(db.func.sum(Investment.principal_amount), 0))
       .filter(
           Investment.user_id == user_id,
           Investment.status == "locked",
       )
       .scalar()
   )
   investment_maturity_credits = (
       db.session.query(db.func.coalesce(db.func.sum(Investment.maturity_amount), 0))
       .filter(
           Investment.user_id == user_id,
           Investment.status == "paid",
       )
       .scalar()
   )
   account_balance = max(
       int(paid_total)
       + int(awarded_loan_total)
       + int(referral_credit_total)
       + int(agent_commission_total)
       + int(task_credit_total or 0)
       + int(locked_task_credit_total or 0)
       + int(investment_maturity_credits or 0)
       - int(withdrawn_total)
       - int(package_spend_total)
       - int(investment_principal_locked or 0),
       0,
   )
   available_balance = max(
       account_balance
       - int(locked_task_credit_total or 0)
       - int(reserved_withdrawal_total),
       0,
   )
   deposit_required = int(CLAIMABLE_CASHBACK * DEPOSIT_REQUIREMENT_RATE)
   amount_needed = max(deposit_required - int(paid_total), 0)
   progress = 100 if deposit_required == 0 else min(100, int((int(paid_total) / deposit_required) * 100))

   return {
       "cashback": CLAIMABLE_CASHBACK,
       "deposit_required": deposit_required,
       "deposit_balance": int(paid_total),
       "loan_credits": int(awarded_loan_total),
       "referral_credits": int(referral_credit_total),
       "agent_deposit_commission_credits": int(agent_commission_total),
       "task_credits": int(task_credit_total or 0),
       "task_credits_earned": all_task_credit_total,
       "task_credits_locked": int(locked_task_credit_total or 0),
       "investment_principal_locked": int(investment_principal_locked or 0),
       "investment_maturity_credits": int(investment_maturity_credits or 0),
       "package_spend": int(package_spend_total),
       "account_balance": account_balance,
       "available_balance": available_balance,
       # Task rewards never pay the package activation fee. The full fee must
       # come from the member's available wallet balance.
       "pass_purchase_balance": int(available_balance),
       "withdrawn_total": int(withdrawn_total),
       "withdrawal_reserved": int(reserved_withdrawal_total),
       "amount_needed": amount_needed,
       "progress": progress,
       "claim_ready": amount_needed == 0,
   }


@app.route("/dashboard")
@login_required
def dashboard():
   expire_stale_pending_wallet_deposits()
   recent_payments = (
       Payment.query.filter_by(user_id=current_user.id, payment_type="wallet_deposit")
       .order_by(Payment.created_at.desc())
       .limit(5)
       .all()
   )
   recent_withdrawals = (
       WithdrawalRequest.query.filter_by(user_id=current_user.id)
       .filter(WithdrawalRequest.status.in_(("pending", "processing", "paid")))
       .order_by(WithdrawalRequest.created_at.desc())
       .limit(8)
       .all()
   )
   withdrawal_status_labels = {
       "pending": "Pending",
       "processing": "Processing",
       "paid": "Withdrawn successfully",
   }
   dashboard_activity = []
   for withdrawal in recent_withdrawals:
       if withdrawal.payout_method == "paypal":
           payout_email = withdrawal.payout_details or ""
           email_name, separator, email_domain = payout_email.partition("@")
           masked_destination = (
               f"{email_name[:1]}***@{email_domain}"
               if separator and email_name and email_domain
               else "PayPal email hidden"
           )
       else:
           phone_digits = "".join(character for character in (withdrawal.payout_details or withdrawal.phone) if character.isdigit())
           masked_destination = (
               f"{phone_digits[:3]} **** {phone_digits[-3:]}"
               if len(phone_digits) >= 7 else "Phone hidden"
           )
       dashboard_activity.append({
           "kind": "Withdrawal",
           "destination": masked_destination,
           "amount": withdrawal.amount,
           "payout_amount": withdrawal.payout_amount,
           "payout_currency": withdrawal.payout_currency or "KES",
           "status": withdrawal.status,
           "status_label": withdrawal_status_labels.get(withdrawal.status, withdrawal.status.title()),
           "created_at": withdrawal.created_at,
           "time_label": None,
           "is_sample": False,
       })
   payment_status_labels = {
       "paid": "Deposit confirmed",
       "pending": "Deposit pending",
       "failed": "Deposit failed",
   }
   for payment in recent_payments:
       phone_digits = "".join(character for character in (payment.phone or "") if character.isdigit())
       if payment.payment_method == "mpesa":
           destination = (
               f"{phone_digits[:3]} **** {phone_digits[-3:]}"
               if len(phone_digits) >= 7 else "M-PESA"
           )
       else:
           destination = {
               "paypal": "PayPal",
               "paystack": "Card payment",
           }.get(payment.payment_method, "Payment")
       dashboard_activity.append({
           "kind": "Deposit",
           "destination": destination,
           "amount": payment.amount,
           "payout_amount": None,
           "payout_currency": "KES",
           "status": payment.status if payment.status in payment_status_labels else "other",
           "status_label": payment_status_labels.get(
               payment.status,
               f"Deposit {str(payment.status or 'status updated').replace('_', ' ')}",
           ),
           "created_at": payment.created_at,
           "time_label": None,
           "is_sample": False,
       })
   dashboard_activity.sort(key=lambda activity: activity["created_at"] or datetime.min, reverse=True)

   sample_dashboard_activity = [
       {"kind": "Withdrawal", "phone": "071 **** 248", "email": "j***@gmail.com", "amount": WITHDRAWAL_PACKAGES[0], "status": "pending", "status_label": "Pending", "time_label": "Just now"},
       {"kind": "Withdrawal", "phone": "072 **** 631", "email": "a***@outlook.com", "amount": WITHDRAWAL_PACKAGES[1], "status": "paid", "status_label": "Withdrawn successfully", "time_label": "2 min ago"},
       {"kind": "Withdrawal", "phone": "074 **** 905", "email": "m***@gmail.com", "amount": WITHDRAWAL_PACKAGES[2], "status": "paid", "status_label": "Withdrawn successfully", "time_label": "4 min ago"},
       {"kind": "Withdrawal", "phone": "075 **** 117", "email": "s***@yahoo.com", "amount": WITHDRAWAL_PACKAGES[3], "status": "processing", "status_label": "Processing", "time_label": "6 min ago"},
       {"kind": "Withdrawal", "phone": "079 **** 482", "email": "k***@gmail.com", "amount": WITHDRAWAL_PACKAGES[4], "status": "pending", "status_label": "Pending", "time_label": "8 min ago"},
       {"kind": "Deposit", "phone": "071 **** 316", "email": "j***@gmail.com", "amount": 500, "status": "paid", "status_label": "Deposit confirmed", "time_label": "1 min ago"},
       {"kind": "Deposit", "phone": "072 **** 804", "email": "a***@outlook.com", "amount": 1000, "status": "paid", "status_label": "Deposit confirmed", "time_label": "3 min ago"},
       {"kind": "Deposit", "phone": "074 **** 029", "email": "m***@gmail.com", "amount": 2200, "status": "paid", "status_label": "Deposit confirmed", "time_label": "5 min ago"},
       {"kind": "Deposit", "phone": "075 **** 671", "email": "s***@yahoo.com", "amount": 5000, "status": "pending", "status_label": "Deposit pending", "time_label": "7 min ago"},
       {"kind": "Deposit", "phone": "079 **** 435", "email": "k***@gmail.com", "amount": 2000, "status": "paid", "status_label": "Deposit confirmed", "time_label": "9 min ago"},
   ]
   sample_rng = secrets.SystemRandom()
   sample_rng.shuffle(sample_dashboard_activity)
   for sample_activity in sample_dashboard_activity:
       sample_entry = {
           **sample_activity,
           "is_sample": True,
           "created_at": None,
           "payout_amount": None,
           "payout_currency": "KES",
       }
       dashboard_activity.insert(sample_rng.randrange(len(dashboard_activity) + 1), sample_entry)

   package_catalog = {
       **PACKAGE_OPTIONS,
       **WHATSAPP_EARN_PACKAGES,
       ALL_INCLUSIVE_PACKAGE_KEY: ALL_INCLUSIVE_PACKAGE,
   }
   package_purchases = sorted(
       package_purchases_for_user(current_user.id),
       key=lambda purchase: purchase.created_at or datetime.min,
       reverse=True,
   )
   has_all_inclusive_purchase = any(
       purchase.package_key == ALL_INCLUSIVE_PACKAGE_KEY
       for purchase in package_purchases
   )
   purchased_packages = []
   for purchase in package_purchases:
       package = package_catalog.get(purchase.package_key, {})
       if purchase.package_key == ALL_INCLUSIVE_PACKAGE_KEY:
           package_type = "Full access"
       elif purchase.package_key in WHATSAPP_EARN_PACKAGES:
           package_type = "WhatsApp"
       else:
           package_type = "Task pass"
       purchased_packages.append({
           "name": package.get("name") or purchase.package_name,
           "type": package_type,
       })

   agent_application = AgentApplication.query.filter_by(user_id=current_user.id).first()
   agent_access = user_has_role(current_user, "agent")
   if agent_access:
       agent_status = "Agent access active"
       agent_status_detail = "Your agent features are ready to use."
   elif not agent_application:
       agent_status = "Not applied yet"
       agent_status_detail = "Review the program requirements and application steps."
   elif (
       agent_application.status == "awaiting_payment"
       and has_all_inclusive_purchase
   ):
       agent_status = "Application under review"
       agent_status_detail = "All-Inclusive covers the program fee; administrator approval is still required."
   else:
       agent_application_status = {
           "awaiting_payment": ("Activation needed", "Continue your application to complete its activation step."),
           "pending": ("Under review", "Your application is waiting for an administrators supported range."}), 400

   locked_user = User.query.filter_by(id=current_user.id).with_for_update().first()
   if not locked_user or not locked_user.is_active:
       return jsonify({"error": "This account is closed."}), 403
   existing_payment = Payment.query.filter_by(
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paystack",
       status="pending",
   ).order_by(Payment.created_at.desc()).first()
   if existing_payment:
       stored_url = (existing_payment.raw_request or {}).get("authorization_url")
       stored_url_is_valid = (
           isinstance(stored_url, str)
           and urlsplit(stored_url).scheme == "https"
           and urlsplit(stored_url).hostname == "checkout.paystack.com"
       )
       same_amount = (
           existing_payment.provider_currency == currency
           and Decimal(str(existing_payment.provider_amount)) == provider_amount
       )
       if stored_url_is_valid and same_amount:
           return jsonify({
               "payment_id": existing_payment.id,
               "authorization_url": stored_url,
               "message": "Resuming your existing Paystack checkout.",
           })
       if stored_url_is_valid and not same_amount:
           return jsonify({
               "error": (
                   "You already have a pending card checkout for "
                   f"{existing_payment.provider_currency} "
                   f"{Decimal(str(existing_payment.provider_amount)):,.2f}. "
                   "Finish or cancel that checkout before starting a different amount."
               ),
               "pending": True,
           }), 409
       return jsonify({
           "error": "A Paystack checkout is already being prepared. Wait a moment, then try again.",
           "pending": True,
       }), 409

   payment = Payment(
       user_id=current_user.id,
       phone="Paystack card",
       amount=amount_kes,
       payment_type="wallet_deposit",
       payment_method="paystack",
       status="pending",
       provider_amount=provider_amount,
       provider_currency=currency,
       exchange_rate=rate,
   )
   db.session.add(payment)
   db.session.flush()
   payment.checkout_request_id = f"SVA-{payment.id}-{secrets.token_hex(12)}"
   db.session.commit()

   try:
       callback_origin = base_url if is_production else request.url_root.rstrip("/")
       response = paystack_json_request(
           "POST",
           "/transaction/initialize",
           {
               "email": current_user.email,
               "amount": str(amount_minor),
               "currency": currency,
               "reference": payment.checkout_request_id,
               "callback_url": f"{callback_origin}{url_for('paystack_return', payment_id=payment.id)}",
               "channels": ["card"],
               "metadata": json.dumps({
                   "payment_id": payment.id,
                   "user_id": current_user.id,
                   "wallet_amount_kes": amount_kes,
               }),
           },
       )
       authorization_url = response.get("authorization_url")
       returned_reference = response.get("reference")
       parsed_url = urlsplit(authorization_url) if isinstance(authorization_url, str) else None
       if (
           not parsed_url
           or parsed_url.scheme != "https"
           or parsed_url.hostname != "checkout.paystack.com"
           or returned_reference != payment.checkout_request_id
       ):
           raise RuntimeError("Paystack returned an invalid checkout link.")
       payment.raw_request = response
       payment.response_description = "Paystack card checkout initialized."
       db.session.commit()
       return jsonify({
           "payment_id": payment.id,
           "authorization_url": authorization_url,
           "message": "Secure card checkout is ready. Paystack will collect your card details.",
       })
   except Exception as exc:
       db.session.rollback()
       payment = db.session.get(Payment, payment.id)
       if payment and payment.status != "paid":
           payment.status = "failed"
           payment.response_description = str(exc)[:255]
           db.session.commit()
       diagnostic = str(exc)[:240] if isinstance(exc, RuntimeError) else type(exc).__name__
       app.logger.warning(
           "Paystack checkout initialization failed payment_id=%s reason=%s",
           payment.id if payment else "unknown",
           diagnostic,
       )
       message = str(exc) if isinstance(exc, RuntimeError) else "Could not start card checkout. Please try again."
       return jsonify({"error": message}), 502


@app.route("/payments/paystack/return")
@login_required
def paystack_return():
   expire_stale_pending_wallet_deposits()
   payment = Payment.query.filter_by(
       id=request.args.get("payment_id", type=int),
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paystack",
   ).first_or_404()
   reference = request.args.get("reference", "")
   if (
       not payment.checkout_request_id
       or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", reference)
       or not secrets.compare_digest(payment.checkout_request_id, reference)
   ):
       abort(400)
   if payment.status == "paid":
       return redirect(url_for("payment_success", payment_id=payment.id))
   return render_template(
       "paystack_return.html",
       payment=payment,
       reference=reference,
       csrf_token=get_csrf_token(),
       verify_url=url_for("verify_paystack_payment"),
       payment_timeout_at=(
           (payment.created_at + timedelta(minutes=WALLET_DEPOSIT_PENDING_TIMEOUT_MINUTES)).isoformat() + "Z"
           if payment.created_at else utc_now_naive().isoformat() + "Z"
       ),
   )


@app.route("/payments/paystack/verify", methods=["POST"])
@login_required
@limiter.limit("20 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("60 per hour", key_func=get_client_ip, methods=["POST"])
def verify_paystack_payment():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh the page and try again."}), 400
   expire_stale_pending_wallet_deposits()
   data = request.get_json(silent=True)
   if not isinstance(data, dict):
       return jsonify({"error": "Invalid payment check."}), 400
   payment = Payment.query.filter_by(
       id=data.get("payment_id"),
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paystack",
   ).with_for_update().first()
   if not payment:
       return jsonify({"error": "Payment not found."}), 404
   reference = data.get("reference", "")
   if (
       not isinstance(reference, str)
       or not payment.checkout_request_id
       or not secrets.compare_digest(payment.checkout_request_id, reference)
   ):
       return jsonify({"error": "Paystack reference does not match this wallet recharge."}), 400
   if payment.status == "paid":
       return jsonify({"status": "paid", "redirect_url": url_for("payment_success", payment_id=payment.id)})
   if not app.config.get("PAYSTACK_SECRET_KEY"):
       if payment.status == "failed":
           return jsonify({
               "status": "failed",
               "redirect_url": url_for("recharge", attempt=payment.id),
               "message": payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
           })
       return jsonify({"error": "Card payment confirmation is temporarily unavailable. Please contact support."}), 503

   try:
       transaction = paystack_json_request("GET", f"/transaction/verify/{reference}")
   except RuntimeError as exc:
       if payment.status == "failed":
           return jsonify({
               "status": "failed",
               "redirect_url": url_for("recharge", attempt=payment.id),
               "message": payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
           })
       return jsonify({"status": "pending", "message": str(exc)}), 502

   transaction_id = paystack_transaction_matches_payment(transaction, payment)
   if transaction_id:
       finalize_paystack_transaction(payment, transaction)
       return jsonify({"status": "paid", "redirect_url": url_for("payment_success", payment_id=payment.id)})

   if transaction.get("status") in {"abandoned", "failed", "reversed"} and payment.status != "paid":
       payment.status = "failed"
       payment.result_description = "Paystack card checkout was not completed. No wallet credit was applied."
       payment.raw_callback = {"data": transaction}
       db.session.commit()
       return jsonify({
           "status": "failed",
           "redirect_url": url_for("recharge", attempt=payment.id),
           "message": payment.result_description,
       })

   if transaction.get("status") == "success":
       log_security_event("paystack_transaction_mismatch", str(payment.id), "amount_currency_or_metadata_mismatch")
       return jsonify({
           "status": "review",
           "message": "Paystack reported a payment, but its details did not match this wallet recharge. Your wallet has not been credited; contact support.",
       }), 409
   if payment.status == "failed":
       return jsonify({
           "status": "failed",
           "redirect_url": url_for("recharge", attempt=payment.id),
           "message": payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
       })
   return jsonify({"status": "pending", "message": "Paystack is still confirming the payment."}), 202


@app.route("/payments/paystack/webhook", methods=["POST"])
@limiter.limit("120 per minute", key_func=get_client_ip, methods=["POST"])
def paystack_webhook():
   secret_key = app.config.get("PAYSTACK_SECRET_KEY")
   if not secret_key:
       return jsonify({"error": "Paystack webhook is not configured."}), 503
   raw_body = request.get_data(cache=True)
   supplied_signature = request.headers.get("x-paystack-signature", "")
   expected_signature = hmac.new(secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
   if not supplied_signature or not hmac.compare_digest(expected_signature, supplied_signature):
       log_security_event("paystack_webhook_rejected", reason="signature_verification_failed")
       return jsonify({"error": "Paystack webhook signature verification failed."}), 400
   event = request.get_json(silent=True)
   if not isinstance(event, dict):
       return jsonify({"error": "Invalid Paystack event."}), 400
   if event.get("event") != "charge.success":
       return jsonify({"received": True})
   event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
   reference = event_data.get("reference")
   if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", reference):
       app.logger.warning("Paystack webhook did not include a valid transaction reference")
       return jsonify({"received": True})
   try:
       payment = Payment.query.filter_by(
           checkout_request_id=reference,
           payment_type="wallet_deposit",
           payment_method="paystack",
       ).with_for_update().first()
       if not payment or payment.status == "paid":
           return jsonify({"received": True})
       transaction = paystack_json_request("GET", f"/transaction/verify/{reference}")
       transaction_id = paystack_transaction_matches_payment(transaction, payment)
       if not transaction_id:
           log_security_event("paystack_transaction_mismatch", str(payment.id), "webhook_verification_mismatch")
           db.session.rollback()
           return jsonify({"error": "Verified transaction did not match the wallet recharge."}), 409
       finalize_paystack_transaction(payment, transaction)
       return jsonify({"received": True})
   except RuntimeError:
       db.session.rollback()
       app.logger.exception("Paystack webhook verification failed")
       return jsonify({"error": "Paystack webhook verification failed."}), 500


@app.route("/wallet/deposits", methods=["POST"])
@login_required
@limiter.limit("5 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def record_wallet_deposit():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400
   if current_user.country_code != "KE":
       return jsonify({"error": "M-PESA wallet recharges are available only for accounts registered in Kenya. Choose PayPal instead."}), 400
   return start_stk_payment()


@app.route("/payments/paypal/order", methods=["POST"])
@login_required
@limiter.limit("5 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def create_paypal_order():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400
   expire_stale_pending_wallet_deposits()
   if current_user.country_code == "KE":
       return jsonify({"error": "PayPal wallet recharges are not available for Kenyan accounts. Please use M-PESA."}), 403
   if not is_paypal_configured():
       return jsonify({
           "error": (
               "PayPal recharges are temporarily unavailable. Choose another payment method or contact support."
               if current_user.country_code != "KE"
               else "PayPal recharges are temporarily unavailable. Use M-PESA for now or contact support."
           )
       }), 503

   form_submission = not request.is_json
   data = request.get_json(silent=True)
   if not isinstance(data, dict) and request.form:
       data = request.form
   if not isinstance(data, dict):
       return jsonify({"error": "Enter a valid wallet recharge amount."}), 400
   raw_amount = data.get("amount")
   if isinstance(raw_amount, bool):
       return jsonify({"error": "Enter a valid wallet recharge amount."}), 400
   try:
       amount = parse_user_amount_to_kes(raw_amount, allow_decimals=True)
   except ValueError as exc:
       return jsonify({"error": str(exc)}), 400
   if amount < MINIMUM_WALLET_DEPOSIT:
       return jsonify({"error": f"Minimum wallet recharge is {format_user_money(MINIMUM_WALLET_DEPOSIT)}."}), 400

   rate = paypal_kes_per_usd()
   try:
       provider_amount = paypal_amount_for_kes(amount, rate)
   except ValueError:
       return jsonify({
           "error": (
               "PayPals KES to USD quote is unavailable. Use M-PESA for now."
           )
       }), 503
   if provider_amount <= 0:
       return jsonify({"error": "This amount is too small for PayPal checkout."}), 400

   # Serialize checkout creation per account so two rapid requests cannot create
   # two simultaneous pending orders for the same wallet.
   locked_user = User.query.filter_by(id=current_user.id).with_for_update().first()
   if not locked_user or not locked_user.is_active:
       return jsonify({"error": "This account is closed."}), 403
   existing_payment = Payment.query.filter_by(
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paypal",
       status="pending",
   ).order_by(Payment.created_at.desc()).first()
   if existing_payment:
       stored_response = existing_payment.raw_request if isinstance(existing_payment.raw_request, dict) else {}
       stored_links = stored_response.get("links") or []
       stored_approval_url = next((
           link.get("href") for link in stored_links
           if isinstance(link, dict)
           and link.get("rel") in {"approve", "payer-action"}
           and isinstance(link.get("href"), str)
           and link["href"].startswith("https://")
       ), None)
       existing_order = None
       order_not_found = False
       if existing_payment.checkout_request_id:
           try:
               existing_order = paypal_json_request(
                   "GET",
                   f"/v2/checkout/orders/{existing_payment.checkout_request_id}",
               )
           except RuntimeError as exc:
               provider_error = exc.__cause__
               if isinstance(provider_error, urllib_error.HTTPError) and provider_error.code == 404:
                   order_not_found = True
               else:
                   return jsonify({
                       "error": "We could not confirm the status of your existing PayPal checkout. Please wait and try again, or contact support before starting another payment.",
                       "pending": True,
                   }), 502

       if order_not_found:
           existing_payment.status = "failed"
           existing_payment.response_description = "PayPal could not find the old checkout order. No wallet credit was applied."
           db.session.commit()
       elif existing_order:
           if existing_order.get("id") != existing_payment.checkout_request_id:
               app.logger.error("PayPal existing order id mismatch payment_id=%s", existing_payment.id)
               return jsonify({
                   "error": "PayPal returned a different order than the one saved for this wallet recharge. Contact support before paying again.",
                   "pending": True,
               }), 409
           order_status = existing_order.get("status")
           if order_status == "COMPLETED":
               capture_id, _ = finalize_paypal_capture(existing_payment, existing_order)
               if not capture_id:
                   app.logger.error("PayPal existing order validation failed payment_id=%s", existing_payment.id)
                   return jsonify({
                       "error": "PayPal reports this order as completed, but its details do not match the wallet recharge. Contact support before paying again.",
                       "pending": True,
                   }), 409
               if form_submission:
                   return redirect(url_for("payment_success", payment_id=existing_payment.id), code=303)
               return jsonify({
                   "success": True,
                   "payment_id": existing_payment.id,
                   "redirect_url": url_for("payment_success", payment_id=existing_payment.id),
                   "message": "Your existing PayPal payment has been confirmed.",
               })

           if order_status == "APPROVED":
               try:
                   capture_result = paypal_json_request(
                       "POST",
                       f"/v2/checkout/orders/{existing_payment.checkout_request_id}/capture",
                       {},
                       request_id=f"sva-paypal-capture-{existing_payment.id}",
                   )
               except RuntimeError:
                   return jsonify({
                       "error": "PayPal has approved the existing payment and is still processing it. Do not pay again; check again shortly or contact support.",
                       "pending": True,
                   }), 502
               capture_id, _ = finalize_paypal_capture(existing_payment, capture_result)
               if capture_id:
                   if form_submission:
                       return redirect(url_for("payment_success", payment_id=existing_payment.id), code=303)
                   return jsonify({
                       "success": True,
                       "payment_id": existing_payment.id,
                       "redirect_url": url_for("payment_success", payment_id=existing_payment.id),
                       "message": "Your existing PayPal payment has been confirmed.",
                   })
               if capture_result.get("status") in {"PENDING", "APPROVED", "CREATED"}:
                   return jsonify({
                       "error": "PayPal is still processing the approved payment. Do not pay again; check again shortly.",
                       "pending": True,
                   }), 202
               if capture_result.get("status") in {"DECLINED", "VOIDED"}:
                   existing_payment.status = "failed"
                   existing_payment.response_description = "PayPal did not capture the approved order. No wallet credit was applied."
                   db.session.commit()
                   order_status = "VOIDED"
               else:
                   return jsonify({
                       "error": "PayPal has not confirmed the approved payment. Contact support before starting another payment.",
                       "pending": True,
                   }), 409

           # PayPal's standard order window is three hours. Reuse an approval
           # link only after checking the provider's current order status.
           order_age = paypal_order_age(existing_order, existing_payment)
           if order_status == "VOIDED" or (
               order_status in {"CREATED", "PAYER_ACTION_REQUIRED"}
               and order_age is not None
               and order_age >= timedelta(hours=3)
           ):
               existing_payment.status = "failed"
               existing_payment.response_description = "PayPal checkout expired before payment completed. No wallet credit was applied."
               db.session.commit()
               app.logger.info(
                   "PayPal stale checkout closed payment_id=%s status=%s age_seconds=%s",
                   existing_payment.id,
                   order_status,
                   int(order_age.total_seconds()) if order_age is not None else "unknown",
               )
           elif order_status in {"CREATED", "PAYER_ACTION_REQUIRED"}:
               existing_approval_url = next((
                   link.get("href") for link in (existing_order.get("links") or [])
                   if isinstance(link, dict)
                   and link.get("rel") in {"approve", "payer-action"}
                   and isinstance(link.get("href"), str)
                   and link["href"].startswith("https://")
               ), None) or stored_approval_url
               if existing_approval_url:
                   if form_submission:
                       return redirect(existing_approval_url, code=303)
                   return jsonify({
                       "payment_id": existing_payment.id,
                       "approval_url": existing_approval_url,
                       "amount_kes": existing_payment.amount,
                       "amount_usd": f"{Decimal(existing_payment.provider_amount):.2f}",
                       "currency": existing_payment.provider_currency,
                       "message": "Resuming your active PayPal checkout.",
                   })
               return jsonify({
                   "error": "Your PayPal order is still active but has no checkout link. Contact support before paying again.",
                   "pending": True,
               }), 409
           else:
               return jsonify({
                   "error": "PayPal returned an unexpected status for your existing checkout. Contact support before paying again.",
                   "pending": True,
               }), 409
       elif existing_payment.checkout_request_id:
           return jsonify({
               "error": "We could not read the status of your existing PayPal order. Please contact support before starting another payment.",
               "pending": True,
           }), 502
       else:
           preparation_age = paypal_order_age({}, existing_payment)
           if preparation_age is not None and preparation_age >= timedelta(hours=3):
               existing_payment.status = "failed"
               existing_payment.response_description = "PayPal checkout preparation expired before an order was saved. No wallet credit was applied."
               db.session.commit()
           else:
               return jsonify({
                   "error": "A PayPal checkout is still being prepared for your account. Wait a moment, then try again.",
                   "pending": True,
               }), 409

   payment = Payment(
       user_id=current_user.id,
       phone="PayPal",
       amount=amount,
       payment_type="wallet_deposit",
       payment_method="paypal",
       status="pending",
       provider_amount=provider_amount,
       provider_currency="USD",
       exchange_rate=rate,
   )
   db.session.add(payment)
   db.session.commit()

   try:
       callback_origin = base_url if is_production else request.url_root.rstrip("/")
       order_payload = {
           "intent": "CAPTURE",
           "purchase_units": [{
               "reference_id": f"SVA-{payment.id}",
               "custom_id": str(payment.id),
               "invoice_id": f"SVA-{payment.id}",
               "description": f"Sky View Agency wallet recharge KES {amount:,}",
               "amount": {
                   "currency_code": "USD",
                   "value": f"{provider_amount:.2f}",
               },
           }],
           "payment_source": {
               "paypal": {
                   "experience_context": {
                       "brand_name": "Sky View Agency",
                       "shipping_preference": "NO_SHIPPING",
                       "user_action": "PAY_NOW",
                       "return_url": f"{callback_origin}{url_for('paypal_return', payment_id=payment.id)}",
                       "cancel_url": f"{callback_origin}{url_for('paypal_cancel', payment_id=payment.id)}",
                   },
               },
           },
       }
       response = paypal_json_request(
           "POST",
           "/v2/checkout/orders",
           order_payload,
           request_id=f"sva-paypal-order-{payment.id}",
       )
       order_id = response.get("id")
       if not isinstance(order_id, str) or not re.fullmatch(r"[A-Z0-9-]{5,80}", order_id):
           raise RuntimeError("PayPal returned an invalid order reference.")
       approval_url = next((
           link.get("href") for link in response.get("links", [])
           if isinstance(link, dict)
           and link.get("rel") in {"approve", "payer-action"}
           and isinstance(link.get("href"), str)
           and link["href"].startswith("https://")
       ), None)
       if not approval_url:
           raise RuntimeError("PayPal did not provide a checkout link. Please try again.")
       payment.checkout_request_id = order_id
       payment.raw_request = response
       payment.response_description = "PayPal checkout order created."
       db.session.commit()
       if form_submission:
           return redirect(approval_url, code=303)
       return jsonify({
           "payment_id": payment.id,
           "approval_url": approval_url,
           "amount_kes": payment.amount,
           "amount_usd": f"{provider_amount:.2f}",
           "currency": "USD",
           "message": "PayPal checkout is ready. Review the USD amount before approving."
       })
   except Exception as exc:
       db.session.rollback()
       payment = db.session.get(Payment, payment.id)
       if payment and payment.status != "paid":
           payment.status = "failed"
           payment.response_description = str(exc)[:255]
           db.session.commit()
       diagnostic = str(exc)[:240] if isinstance(exc, RuntimeError) else type(exc).__name__
       app.logger.warning(
           "PayPal order creation failed payment_id=%s reason=%s",
           payment.id if payment else "unknown",
           diagnostic,
       )
       message = str(exc) if isinstance(exc, RuntimeError) else "Could not start PayPal checkout. Please try again."
       return jsonify({"error": message}), 502


@app.route("/payments/paypal/return")
@login_required
def paypal_return():
   expire_stale_pending_wallet_deposits()
   payment_id = request.args.get("payment_id", type=int)
   payment = Payment.query.filter_by(
       id=payment_id,
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paypal",
   ).first_or_404()
   order_id = request.args.get("token", "")
   if not payment.checkout_request_id or not secrets.compare_digest(payment.checkout_request_id, order_id):
       abort(400)
   if payment.status == "paid":
       return redirect(url_for("payment_success", payment_id=payment.id))
   return render_template(
       "paypal_return.html",
       payment=payment,
       csrf_token=get_csrf_token(),
       capture_url=url_for("capture_paypal_order"),
       payment_timeout_at=(
           (payment.created_at + timedelta(minutes=WALLET_DEPOSIT_PENDING_TIMEOUT_MINUTES)).isoformat() + "Z"
           if payment.created_at else utc_now_naive().isoformat() + "Z"
       ),
   )


@app.route("/payments/paypal/cancel")
@login_required
def paypal_cancel():
   payment = Payment.query.filter_by(
       id=request.args.get("payment_id", type=int),
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paypal",
   ).first_or_404()
   supplied_order_id = request.args.get("token", "")
   if supplied_order_id and payment.checkout_request_id and not secrets.compare_digest(
       payment.checkout_request_id, supplied_order_id
   ):
       abort(400)
   if payment.status == "pending":
       payment.status = "failed"
       payment.response_description = "PayPal checkout was cancelled. No wallet credit was applied."
       db.session.commit()
   return redirect(url_for("recharge", attempt=payment.id))


@app.route("/payments/paypal/capture", methods=["POST"])
@login_required
@limiter.limit("10 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("30 per hour", key_func=get_client_ip, methods=["POST"])
def capture_paypal_order():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400
   expire_stale_pending_wallet_deposits()
   data = request.get_json(silent=True)
   if not isinstance(data, dict):
       return jsonify({"error": "Invalid PayPal confirmation request."}), 400
   payment = Payment.query.filter_by(
       id=data.get("payment_id"),
       user_id=current_user.id,
       payment_type="wallet_deposit",
       payment_method="paypal",
   ).with_for_update().first()
   if not payment:
       return jsonify({"error": "Payment not found."}), 404
   order_id = data.get("order_id", "")
   if (
       not isinstance(order_id, str)
       or not payment.checkout_request_id
       or not secrets.compare_digest(payment.checkout_request_id, order_id)
   ):
       return jsonify({"error": "PayPal order does not match this wallet recharge."}), 400
   if payment.status == "paid":
       return jsonify({"success": True, "payment_id": payment.id, "message": "This wallet recharge is already confirmed."})
   if not paypal_credentials_configured():
       if payment.status == "failed":
           return jsonify({
               "status": "failed",
               "message": payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
           }), 400
       return jsonify({"error": "PayPal verification is temporarily unavailable. Contact support before retrying."}), 503

   try:
       capture_result = paypal_json_request(
           "POST",
           f"/v2/checkout/orders/{order_id}/capture",
           {},
           request_id=f"sva-paypal-capture-{payment.id}",
       )
   except RuntimeError as exc:
       db.session.rollback()
       refreshed_payment = db.session.get(Payment, data.get("payment_id"))
       if refreshed_payment and refreshed_payment.status == "failed":
           return jsonify({
               "status": "failed",
               "message": refreshed_payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
           }), 400
       return jsonify({"error": str(exc), "pending": True}), 502

   if capture_result.get("status") != "COMPLETED":
       if capture_result.get("status") in {"PENDING", "APPROVED", "CREATED"}:
           if payment.status == "failed":
               return jsonify({
                   "status": "failed",
                   "message": payment.result_description or WALLET_DEPOSIT_TIMEOUT_MESSAGE,
               }), 400
           payment.status = "pending"
           payment.response_description = "PayPal is still processing this payment."
           db.session.commit()
           return jsonify({"pending": True, "message": payment.response_description}), 202
       payment.status = "failed"
       payment.response_description = "PayPal did not complete this payment. No wallet credit was applied."
       payment.raw_callback = capture_result
       db.session.commit()
       return jsonify({"error": payment.response_description}), 400

   capture_id = paypal_capture_matches_payment(capture_result, payment)
   if not capture_id:
       app.logger.error("PayPal capture validation failed payment_id=%s", payment.id)
       db.session.rollback()
       return jsonify({
           "error": "PayPal reported a completed payment, but its details did not match this wallet recharge. Contact support with your payment reference.",
           "pending": True,
       }), 409

   capture_id, _ = finalize_paypal_capture(payment, capture_result)
   return jsonify({"success": True, "payment_id": payment.id, "message": "PayPal payment confirmed."})


@app.route("/payments/paypal/webhook", methods=["POST"])
@limiter.limit("120 per minute", key_func=get_client_ip, methods=["POST"])
def paypal_webhook():
   webhook_id = app.config.get("PAYPAL_WEBHOOK_ID")
   if not webhook_id or not paypal_credentials_configured():
       return jsonify({"error": "PayPal webhook is not configured."}), 503
   event = request.get_json(silent=True)
   if not isinstance(event, dict):
       return jsonify({"error": "Invalid webhook event."}), 400
   signature_fields = {
       "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO"),
       "cert_url": request.headers.get("PAYPAL-CERT-URL"),
       "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID"),
       "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG"),
       "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME"),
       "webhook_id": webhook_id,
       "webhook_event": event,
   }
   if not all(signature_fields[key] for key in (
       "auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time"
   )):
       log_security_event("paypal_webhook_rejected", reason="missing_signature_headers")
       return jsonify({"error": "Missing PayPal signature headers."}), 400

   try:
       verification = paypal_json_request(
           "POST",
           "/v1/notifications/verify-webhook-signature",
           signature_fields,
       )
       if verification.get("verification_status") != "SUCCESS":
           log_security_event("paypal_webhook_rejected", reason="signature_verification_failed")
           return jsonify({"error": "PayPal webhook signature verification failed."}), 400

       event_type = event.get("event_type")
       resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
       order_id = None
       should_capture = event_type == "CHECKOUT.ORDER.APPROVED"
       if should_capture:
           order_id = resource.get("id")
       elif event_type == "PAYMENT.CAPTURE.COMPLETED":
           supplementary = resource.get("supplementary_data") or {}
           related_ids = supplementary.get("related_ids") or {}
           order_id = related_ids.get("order_id")
       else:
           return jsonify({"received": True})

       if not isinstance(order_id, str) or not re.fullmatch(r"[A-Z0-9-]{5,80}", order_id):
           app.logger.warning("PayPal webhook event had no valid order reference")
           return jsonify({"received": True})
       payment = Payment.query.filter_by(
           checkout_request_id=order_id,
           payment_type="wallet_deposit",
           payment_method="paypal",
       ).with_for_update().first()
       if not payment or payment.status == "paid":
           return jsonify({"received": True})

       if should_capture:
           capture_result = paypal_json_request(
               "POST",
               f"/v2/checkout/orders/{order_id}/capture",
               {},
               request_id=f"sva-paypal-capture-{payment.id}",
           )
       else:
           capture_result = paypal_json_request("GET", f"/v2/checkout/orders/{order_id}")
       if capture_result.get("status") == "COMPLETED":
           capture_id, _ = finalize_paypal_capture(payment, capture_result)
           if not capture_id:
               app.logger.error("PayPal webhook capture validation failed payment_id=%s", payment.id)
               db.session.rollback()
               return jsonify({"error": "Completed capture did not match the wallet recharge."}), 409
       return jsonify({"received": True})
   except RuntimeError:
       db.session.rollback()
       app.logger.exception("PayPal webhook processing failed")
       return jsonify({"error": "PayPal webhook processing failed."}), 500


def start_stk_payment(payment_type="wallet_deposit", fixed_amount=None):
   if payment_type == "wallet_deposit":
       expire_stale_pending_wallet_deposits()
   # Coordinate payment creation with account closure, which takes this same
   # user-row lock before checking for outstanding transactions.
   locked_user = User.query.filter_by(id=current_user.id).with_for_update().first()
   if not locked_user or not locked_user.is_active:
       return jsonify({"error": "This account is closed."}), 403

   if payment_type == "agent_program_fee" and current_user.country_code != "KE":
       return jsonify({
           "error": "Agent Program payments currently require a Kenyan M-PESA account."
       }), 400
   if not app.config["MPESA_ENABLED"]:
       return jsonify({"error": "M-PESA payments are temporarily unavailable."}), 503
   if not is_mpesa_configured():
       if payment_type == "agent_program_fee":
           return jsonify({
               "error": "Agent program payments are temporarily unavailable. Please contact support before making a payment."
           }), 503
       return jsonify({
           "error": "M-PESA recharges are temporarily unavailable. Please choose another payment method or contact support."
       }), 503

   data = request.get_json(silent=True)
   if not isinstance(data, dict):
       data = request.form

   try:
       phone = normalize_mpesa_phone(data.get("phone"))
       amount = int(fixed_amount if fixed_amount is not None else data.get("amount", 0))
   except (TypeError, ValueError) as exc:
       return jsonify({"error": str(exc)}), 400

   if fixed_amount is None and amount < MINIMUM_WALLET_DEPOSIT:
       return jsonify({"error": f"Amount must be at least KES {MINIMUM_WALLET_DEPOSIT:,}."}), 400

   if payment_type == "agent_program_fee":
       application = AgentApplication.query.filter_by(user_id=current_user.id).with_for_update().first()
       if (
           not application
           or application.status != "awaiting_payment"
           or application.fee_paid_at
           or application.fee_waived_at
       ):
           return jsonify({"error": "Submit your agent application details before paying the program fee."}), 400

       existing_payment = Payment.query.filter_by(
           user_id=current_user.id,
           payment_type="agent_program_fee",
           status="pending",
       ).order_by(Payment.created_at.desc()).first()
       if existing_payment:
           return jsonify({
               "message": "An agent program payment is already awaiting M-PESA confirmation.",
               "payment": payment_to_dict(existing_payment),
           })

   payment = Payment(
       user_id=current_user.id,
       phone=phone,
       amount=amount,
       payment_type=payment_type,
       payment_method="mpesa",
       status="pending",
   )
   db.session.add(payment)
   db.session.commit()

   try:
       response = initiate_stk_push(payment)
   except Exception as exc:
       app.logger.exception("Failed to initiate M-PESA STK Push")
       payment.status = "failed"
       payment.response_description = str(exc)
       db.session.commit()
       return jsonify({"error": str(exc), "payment": payment_to_dict(payment)}), 502

   payment.raw_request = response
   payment.merchant_request_id = response.get("MerchantRequestID")
   payment.checkout_request_id = response.get("CheckoutRequestID")
   payment.response_code = response.get("ResponseCode")
   payment.response_description = response.get("ResponseDescription") or response.get("CustomerMessage")
   if response.get("ResponseCode") != "0":
       payment.status = "failed"
   db.session.commit()

   if payment.status == "failed":
       return jsonify({"error": payment.response_description, "payment": payment_to_dict(payment)}), 400

   return jsonify({
       "message": response.get("CustomerMessage") or "Check your phone and enter your M-PESA PIN.",
       "payment": payment_to_dict(payment),
   })


@app.route("/payments/stk-push", methods=["POST"])
@login_required
@limiter.limit("5 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def create_stk_payment():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400
   return start_stk_payment()


def agent_application_rate_key():
   user_id = current_user.get_id() if current_user.is_authenticated else "anonymous"
   return f"agent-application:{user_id}:{get_client_ip()}"


@app.route("/agent-program/fee", methods=["POST"])
@login_required
@limiter.limit("3 per hour", key_func=agent_application_rate_key, methods=["POST"])
@limiter.limit("10 per hour", key_func=get_client_ip, methods=["POST"])
def create_agent_program_fee_payment():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400
   application = AgentApplication.query.filter_by(user_id=current_user.id).first()
   if all_inclusive_purchase_for_user(current_user.id):
       return jsonify({"error": "All-Inclusive covers the Agent Program fee. Submit your Agent application for review instead."}), 409
   if not application or application.status != "awaiting_payment":
       return jsonify({"error": "Submit an Agent Program application before paying its fee."}), 409
   if application.fee_paid_at or application.fee_waived_at:
       return jsonify({"error": "The Agent Program fee is already paid or covered."}), 409
   return start_stk_payment(payment_type="agent_program_fee", fixed_amount=AGENT_PROGRAM_FEE)


@app.route("/payments/<int:payment_id>/status")
@login_required
@limiter.limit("60 per minute", key_func=authenticated_user_rate_key)
def payment_status(payment_id):
   expire_stale_pending_wallet_deposits()
   payment = Payment.query.get_or_404(payment_id)
   if payment.user_id != current_user.id:
       return jsonify({"error": "Payment not found."}), 404
   return jsonify({"payment": payment_to_dict(payment)})


@app.route("/payments/mpesa/callback", methods=["POST"])
def mpesa_callback():
   if not app.config["MPESA_ENABLED"]:
       return jsonify({"ResultCode": 1, "ResultDesc": "M-PESA callbacks are disabled"}), 503
   callback_token = app.config["MPESA_CALLBACK_TOKEN"]
   supplied_token = request.args.get("callback_token", "")
   if callback_token and not secrets.compare_digest(callback_token, supplied_token):
       log_security_event("mpesa_callback_rejected", reason="invalid_callback_token")
       return jsonify({"ResultCode": 1, "ResultDesc": "Unauthorized"}), 403

   data = request.get_json(silent=True)
   body = data.get("Body") if isinstance(data, dict) else None
   stk_callback = body.get("stkCallback") if isinstance(body, dict) else None
   if not isinstance(stk_callback, dict):
       app.logger.warning("M-PESA callback rejected because its payload shape was invalid")
       return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback body"}), 400

   checkout_request_id = stk_callback.get("CheckoutRequestID")
   result_code = stk_callback.get("ResultCode")
   if not isinstance(checkout_request_id, str) or not checkout_request_id or type(result_code) is not int:
       app.logger.warning("M-PESA callback rejected because its transaction fields were invalid")
       return jsonify({"ResultCode": 1, "ResultDesc": "Invalid transaction fields"}), 400

   try:
       payment_lookup = Payment.query.filter_by(checkout_request_id=checkout_request_id).first()
       if not payment_lookup:
           app.logger.warning("M-PESA callback received for an unknown checkout request")
           return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
       fee_application = None
       if payment_lookup.payment_type == "agent_program_fee":
           fee_application = AgentApplication.query.filter_by(
               user_id=payment_lookup.user_id
           ).with_for_update().first()
       payment = Payment.query.filter_by(
           checkout_request_id=checkout_request_id
       ).with_for_update().first()
       if not payment:
           app.logger.warning("M-PESA callback payment disappeared while being processed")
           return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
       if payment.payment_method != "mpesa":
           app.logger.warning("M-PESA callback ignored for non-M-PESA payment id=%s", payment.id)
           return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

       metadata = stk_callback.get("CallbackMetadata")
       items = metadata.get("Item", []) if isinstance(metadata, dict) else []
       items = items if isinstance(items, list) else []
       metadata_values = {
           item.get("Name"): item.get("Value")
           for item in items
           if isinstance(item, dict) and isinstance(item.get("Name"), str)
       }

       if result_code == 0:
           try:
               callback_amount = int(metadata_values["Amount"])
               callback_phone = normalize_mpesa_phone(str(metadata_values["PhoneNumber"]))
           except (KeyError, TypeError, ValueError):
               callback_amount = None
               callback_phone = None

           if callback_amount != payment.amount or callback_phone != payment.phone:
               if payment.status == "paid":
                   app.logger.warning("M-PESA callback mismatch ignored for paid payment id=%s", payment.id)
                   return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
               payment.status = "failed"
               payment.result_code = result_code
               payment.result_description = "Payment callback details did not match the request."
               payment.raw_callback = data
               clear_closed_payment_personal_data(payment)
               db.session.commit()
               app.logger.warning("M-PESA callback did not match payment id=%s", payment.id)
               return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

       if payment.status == "paid" and result_code != 0:
           app.logger.warning(
               "Ignoring non-success callback for already-paid payment %s",
               payment.id,
           )
           return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

       was_paid = payment.status == "paid"
       payment.result_code = result_code
       result_description = stk_callback.get("ResultDesc")
       payment.result_description = str(result_description)[:255] if result_description is not None else ""
       payment.raw_callback = data
       payment.status = "paid" if result_code == 0 else "failed"

       for item in items:
           if not isinstance(item, dict):
               continue
           if item.get("Name") == "MpesaReceiptNumber":
               receipt = item.get("Value")
               payment.mpesa_receipt_number = str(receipt)[:64] if receipt is not None else None

       closed_account = payment_owner_has_closed_account(payment)
       if closed_account:
           clear_closed_payment_personal_data(payment)
       elif payment.status == "paid" and payment.payment_type == "wallet_deposit":
           award_first_deposit_referral_rewards(payment)
           award_agent_deposit_commission(payment)
       elif payment.status == "paid" and payment.payment_type == "agent_program_fee":
           application = fee_application
           if application and application.fee_waived_at:
               app.logger.warning(
                   "agent_fee_payment_completed_after_waiver payment_id=%s application_id=%s",
                   payment.id,
                   application.id,
               )
           elif application and not application.fee_paid_at:
               application.fee_payment_id = payment.id
               application.fee_paid_at = utc_now_naive()
               if application.status == "awaiting_payment":
                   application.status = "pending"
               application.admin_note = None

       should_notify_deposit = (
           not was_paid
           and payment.status == "paid"
           and payment.payment_type == "wallet_deposit"
           and not closed_account
       )
       db.session.commit()
       if should_notify_deposit:
           try:
               send_deposit_notification(payment)
           except Exception:
               app.logger.exception(
                   "deposit_email_failed payment_id=%s",
                   payment.id,
               )
   except Exception:
       db.session.rollback()
       app.logger.exception("Failed to process M-PESA callback")

   return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/passport", methods=["GET", "POST"])
@login_required
def passport():
   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)
       if request.form.get("share_consent") != "yes":
           flash("Confirm the public-card disclosure before creating a share link.", "warning")
           return redirect(url_for("passport"))
       token = make_passport_invite_token(current_user.id)
       return redirect(url_for("passport", share=token))

   stats = passport_stats_for_user(current_user)
   agent_application = AgentApplication.query.filter_by(user_id=current_user.id).first()
   agent_program_active = user_has_role(current_user, "agent")
   if agent_program_active:
       agent_program_detail = "Your Agent Program access is active."
   elif not agent_application:
       agent_program_detail = "You have not applied to the Agent Program yet."
   else:
       agent_program_detail = {
           "awaiting_payment": "Complete the activation step to submit your application for review.",
           "pending": "Your application is waiting for an administrator "
       f"{(end_local - timedelta(days=1)).strftime('%d %b %Y')} EAT"
   )

   return render_template(
       "agent_earnings.html",
       has_qualifying_client_activity=bool(qualified_referrals),
       bonus_count=bonus_count,
       bonus_amount=bonus_amount,
       recharge_transaction_count=int(recharge_transaction_count),
       spend_share_amount=spend_share_amount,
       total_earnings=total_earnings,
       selected_week_start=selected_week_start.isoformat(),
       previous_week_start=(selected_week_start - timedelta(days=7)).isoformat(),
       next_week_start=(selected_week_start + timedelta(days=7)).isoformat(),
       has_next_week=selected_week_start < current_week_start,
       week_label=week_label,
   )


@app.route("/referral-code/<int:key_id>/delete", methods=["POST"])
@login_required
def delete_referral_code(key_id):
   if not valid_csrf_request():
       abort(400)
   if not user_has_role(current_user, "agent"):
       flash("Referral Rewards are available to approved agents.", "warning")
       return redirect(url_for("agent_program"))
   key_to_delete = APIKey.query.get_or_404(key_id)
   if key_to_delete.user_id != current_user.id:
       flash("You do not have permission to remove this referral code.", "danger")
       return redirect(url_for("referral_code"))
   
   if Referral.query.filter_by(referral_code_id=key_to_delete.id).first():
       flash("This code has already been used and is kept active for referral tracking.", "warning")
       return redirect(url_for("referral_code"))

   db.session.delete(key_to_delete)
   db.session.commit()
   flash("Referral code removed.", "success")
   return redirect(url_for("referral_code"))


@app.route("/api-keys", methods=["GET", "POST"])
def legacy_api_keys_redirect():
   return redirect(
       url_for("referral_code"),
       code=301 if request.method == "GET" else 308,
   )


@app.route("/api-keys/<int:key_id>/delete", methods=["POST"])
def legacy_api_key_delete_redirect(key_id):
   return redirect(url_for("delete_referral_code", key_id=key_id), code=308)


try:
   from openai import OpenAI
except ImportError:
   OpenAI = None

# ----------------- CHATBOT ROUTES -----------------

SUPPORT_CHAT_MENU = (
   {"key": "deposits", "label": "Wallet recharges"},
   {"key": "withdrawals", "label": "Withdrawals"},
   {"key": "packages", "label": "Packages & tasks"},
   {"key": "wallet", "label": "Wallet & cashback"},
   {"key": "referrals", "label": "Referrals & agents"},
   {"key": "loans", "label": "Loans"},
   {"key": "account", "label": "Login & account"},
   {"key": "contact", "label": "Contact support"},
)
SUPPORT_CHAT_MENU_BY_KEY = {item["key"]: item for item in SUPPORT_CHAT_MENU}

SUPPORT_GUIDED_RESPONSES = {
   "deposits": (
       "To recharge your wallet:\n"
       "1. Open Grow Wallet and choose a payment method shown for your country.\n"
       "2. Follow the selected providers Forgot password link to request a reset email. Check your inbox and spam folder. Use Account Settings to manage the profile options available to you.\n"
       "Never share your password, reset link, authenticator code, or one-time code in chat."
   ),
   "contact": (
       "Use the Contact details link above for the official support email address and support hours. For a payment issue, contact support directly with the provider, amount, approximate time, and payment reference.\n"
       "Never send a password, M-PESA PIN, card CVV, or one-time code."
   ),
   "menu": (
       "I can help with Sky View Agency website topics. Choose a topic from the menu above: wallet recharges, withdrawals, packages and tasks, wallet and cashback, referrals and agents, loans, login and account, or contact support."
   ),
   "sensitive": (
       "For your security, I did not send that message to Gemini because it appears to contain private contact, payment, or account information. Please remove those details and ask again. For payment-specific help, contact support directly."
   ),
}

SUPPORT_TOPIC_ACTIONS = {
   "deposits": ("recharge", "Open Recharge Wallet"),
   "withdrawals": ("withdrawals", "Open Withdrawals"),
   "packages": ("packages", "Open Packages & Tasks"),
   "wallet": ("dashboard", "Open Dashboard"),
   "referrals": ("agent_program", "Open Agent Program"),
   "loans": ("loans", "Open Loans"),
   "account": ("account_settings", "Open Account Settings"),
   "contact": ("contact", "Open Contact details"),
}

SUPPORT_FOLLOW_UP_TOPICS = {
   "deposits": ("wallet", "withdrawals", "contact"),
   "withdrawals": ("deposits", "wallet", "contact"),
   "packages": ("wallet", "referrals", "contact"),
   "wallet": ("deposits", "loans", "contact"),
   "referrals": ("packages", "wallet", "contact"),
   "loans": ("wallet", "deposits", "contact"),
   "account": ("contact", "deposits"),
   "contact": ("deposits", "account"),
}


def support_follow_up_replies(topic):
   topic_keys = SUPPORT_FOLLOW_UP_TOPICS.get(topic, ("deposits", "account", "contact"))
   return [
       {"topic": key, "label": SUPPORT_CHAT_MENU_BY_KEY[key]["label"]}
       for key in topic_keys
       if key in SUPPORT_CHAT_MENU_BY_KEY
   ]


def support_guided_responses_for_country(country_code):
   responses = dict(SUPPORT_GUIDED_RESPONSES)
   if country_code != "KE" and currency_kes_per_usd() is None:
       responses["loans"] = (
           "Open Loans to see your wallet recharge band and maximum request in USD. The USD conversion is "
           "currently unavailable, so loan amounts cannot be displayed until the exchange rate is restored. "
           "Repayment is due within 2 months for the KES 7,000 band, 3 months for the KES 10,000 band, or 5 months for the KES 15,000+ band, starting when the loan credit is issued. Late repayment may lead to a review and restrictions on company services or future financial options; another loan request is blocked while a credited loan is overdue. Only successful wallet recharges, limited by your available wallet balance, count toward a band."
       )
   else:
       responses["loans"] = (
           "Open Loans to see your current request cap. Wallet recharge balances of "
           f"{format_loan_deposit_threshold(7000)} to below {format_loan_deposit_threshold(10000)} "
           f"support requests up to {format_loan_limit(15500)}; from "
           f"{format_loan_deposit_threshold(10000)} to below {format_loan_deposit_threshold(15000)} "
           f"supports up to {format_loan_limit(20000)}; and from "
           f"{format_loan_deposit_threshold(15000)} or more supports up to {format_loan_limit(30000)}. "
           "Repayment is due within 2 months for the KES 7,000 band, 3 months for the KES 10,000 band, or 5 months for the KES 15,000+ band, starting when the loan credit is issued. Late repayment may lead to a review and restrictions on company services or future financial options; another loan request is blocked while a credited loan is overdue. "
           "Only successful wallet recharges, limited by your available wallet balance, count toward a band. "
           "Other wallet credits do not count. Your status and review note appear on the Loans page."
       )
   if country_code != "KE":
       responses["withdrawals"] = (
           "Open Withdrawals and choose one of the listed amounts. Enter the email linked to your PayPal account. "
           f"Purchase a matching task pass or All-Inclusive before submitting regular tasks. Before withdrawing task earnings, make successful wallet recharges totaling {format_user_money(TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET)} after package purchase; installments are allowed and only confirmed recharges count. WhatsApp status campaigns pay KES {WHATSAPP_VIEW_REWARD:,} per manually verified view, subject to funded budget, and require approved Agent access before withdrawal. All-Inclusive covers the Agent Program fee; otherwise members outside Kenya may request a manual fee waiver. "
           "An administrator reviews each request and pending funds are reserved."
       )
       responses["contact"] = responses["contact"].replace("M-PESA PIN", "payment PIN")
   return responses

SUPPORT_GEMINI_SYSTEM_INSTRUCTION = """
You are the Sky View Agency website support assistant. Answer only questions about using this website and its services. You cannot access a members account or a specific payment, loan, task, referral, or withdrawal status.

Website facts: members use Grow Wallet to make optional wallet recharges through a payment method shown for their country; wallets are credited after provider confirmation. The current dashboard target is a progress indicator and does not guarantee a cashback payout; direct members to Contact Support to confirm any claim process. Loan request limits use successful wallet recharges, capped by the available wallet balance: KES 7,000 supports up to KES 15,500; KES 10,000 supports up to KES 20,000; and KES 15,000 or more supports up to KES 30,000. Other wallet credits do not count toward the qualifying balance. When answering loan questions, direct members to the Loans page for the maximum displayed in their account currency; do not invent or calculate a USD equivalent. Loan requests are reviewed in the Control Center and their status appears on the Loans page. All-Inclusive costs KES 20,000 and unlocks all three regular task passes and all WhatsApp tiers, and covers the Agent Program fee. It does not change loan eligibility bands, limits, or review. Withdrawal instructions depend on the members first successful wallet recharge. Password reset starts from the login page; profile options are in Account Settings.

Loan repayment terms: the KES 7,000 qualifying recharge band has a 2-month term, the KES 10,000 band has a 3-month term, and the KES 15,000-or-more band has a 5-month term. The deadline starts when the approved loan credit is issued. Late repayment may lead to an account review and restrictions on company services or future financial options. A new loan application is blocked while a credited loan is overdue. Existing requests made before these terms were introduced have no retroactive deadline.

For investment withdrawals, explain that maturity alone does not release the funds: the member must first purchase a regular task pass or All-Inclusive, then complete the Agent Program fee/application requirements and receive admin approval. All-Inclusive covers the Agent Program fee but does not replace the application or approval.

Keep answers brief and practical, with numbered steps when useful. If the question is unrelated or asks for private account/payment status, explain the limitation and direct the user to the menu or Contact details. Never request or repeat passwords, reset links, authenticator codes, one-time codes, M-PESA PINs, card numbers, or CVVs. Do not claim to have verified an application or promise a specific processing time.
""".strip()

# Initialize a client for model listing, using the OpenAI library
grok_model_client = None
xai_api_key_for_models = env_first("XAI_API_KEY")
if xai_api_key_for_models and OpenAI:
   grok_model_client = OpenAI(
       api_key=xai_api_key_for_models,
       base_url="https://api.x.ai/v1",
       timeout=180,
   )


def infer_support_topic(message):
   normalized = message.lower()
   topic_terms = (
       ("withdrawals", ("withdraw", "payout", "cash out")),
       ("deposits", ("deposit", "recharge", "top up", "topup", "paystack", "paypal", "m-pesa", "mpesa")),
       ("packages", ("package", "task", "pass", "submission")),
       ("wallet", ("wallet", "cashback", "cash back", "claim", "balance")),
       ("referrals", ("referral", "refer", "invite", "agent")),
       ("loans", ("loan", "borrow")),
       ("account", ("login", "log in", "register", "account", "password", "reset")),
       ("contact", ("support", "contact", "email", "help")),
   )
   for topic, terms in topic_terms:
       if any(term in normalized for term in terms):
           return topic
   return None


def support_message_contains_private_data(message):
   if re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", message, re.IGNORECASE):
       return True

   phone_candidates = re.findall(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)", message)
   if any(9 <= sum(character.isdigit() for character in candidate) <= 15 for candidate in phone_candidates):
       return True

   if re.search(
       r"\b(?:pin|cvv|cvc|otp|one[- ]time code|password)\s*(?:is|=|:)\s*\S+",
       message,
       re.IGNORECASE,
   ):
       return True
   if re.search(r"\b(?:pin|cvv|cvc|otp)\s+\d{3,8}\b", message, re.IGNORECASE):
       return True
   if re.search(
       r"\b(?:m-?pesa|payment|transaction|receipt)\s+(?:code|reference|ref|id)\s*(?:is|=|:|#)?\s*[A-Z0-9-]{8,}\b",
       message,
       re.IGNORECASE,
   ):
       return True
   return False


def get_offline_chat_response(message, topic=None):
   selected_topic = topic if topic in SUPPORT_CHAT_MENU_BY_KEY else infer_support_topic(message)
   responses = support_guided_responses_for_country(
       current_user.country_code if current_user.is_authenticated else None
   )
   return responses.get(selected_topic or "menu", responses["menu"])


def gemini_support_reply(message, topic=None):
   api_key = env_first("GEMINI_API_KEY")
   if not api_key:
       return None

   try:
       from google import genai
       from google.genai import types
   except ImportError:
       app.logger.warning("Gemini support unavailable: google-genai SDK is not installed in this environment")
       return None

   model = env_first("GEMINI_MODEL", default="gemini-3.8-flash")
   selected_topic = SUPPORT_CHAT_MENU_BY_KEY.get(topic)
   prompt = message
   if selected_topic:
       prompt = f"Selected support topic: {selected_topic['label']}\nMember question: {message}"
   support_email = app.config.get("SUPPORT_EMAIL", "")
   support_hours = app.config.get("SUPPORT_HOURS", "")
   system_instruction = SUPPORT_GEMINI_SYSTEM_INSTRUCTION
   if current_user.country_code != "KE":
       system_instruction = system_instruction.replace("M-PESA PINs", "payment PINs")
   if support_email or support_hours:
       system_instruction += (
           f"\nOfficial support contact: {support_email or 'Use the Contact details link on the page.'}"
           f"\nSupport hours: {support_hours or 'See the Contact details page.'}"
       )
   if current_user.country_code == "KE":
       system_instruction += "\nThis member is in Kenya. For withdrawals, explain that the form requests a Kenyan M-PESA phone number."
   else:
       system_instruction += "\nThis member is outside Kenya. Do not mention Kenya, M-PESA, or +254. For withdrawals, explain that the form requests the email linked to the members voucher discount' if voucher_discount else ''}. Task earnings do not reduce the pass fee. "
                   f"Continue to recharge; the amount field starts at the full listed pass price of {format_user_money(package['price'])} and can be edited. "
                   "After the recharge is confirmed, return here to activate the pass.",
                   "warning",
               )
               return redirect(url_for("recharge", resume_task_package=package_key))

           purchase = PackagePurchase(
               user_id=current_user.id,
               package_key=package_key,
               package_name=package["name"],
               amount=purchase_amount,
           )
           db.session.add(purchase)
           if voucher_discount:
               db.session.flush()
               db.session.add(DailyVoucherRedemption(
                   user_id=current_user.id,
                   redeemed_on=voucher_state["redeemed_on"],
                   package_purchase_id=purchase.id,
                   discount_amount=voucher_discount,
                   qualifying_deposit_amount=voucher_state["deposit_total"],
               ))
           db.session.commit()
           session.pop("investment_purchase_resume", None)
           session.pop("all_inclusive_purchase_resume", None)
           session.pop("whatsapp_purchase_resume_package", None)
           session.pop("task_purchase_resume_package", None)
           app.logger.info(
               "package_purchased purchase_id=%s user_id=%s package=%s amount=%s voucher_discount=%s",
               purchase.id,
               current_user.id,
               package_key,
               purchase_amount,
               voucher_discount,
           )
           if voucher_discount:
               flash(
                   f"{package['name']} is active. Today's voucher saved you "
                   f"{format_user_money(voucher_discount)}; {format_user_money(purchase_amount)} was deducted. "
                   f"For regular task earnings, make successful wallet recharges totaling {format_user_money(TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET)} after activation before cashing out those rewards. You may recharge in installments. WhatsApp campaign rewards also require approved Agent access before withdrawal.",
                   "success",
               )
           else:
               flash(
                   f"{package['name']} is active. {format_user_money(purchase_amount)} was deducted. "
                   f"For regular task earnings, make successful wallet recharges totaling {format_user_money(TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET)} after activation before cashing out those rewards. You may recharge in installments. WhatsApp campaign rewards also require approved Agent access before withdrawal.",
                   "success",
               )
       resume_redirect = investment_claim_resume_redirect(locked_user.id)
       if resume_redirect:
           return resume_redirect
       return redirect(url_for("packages", _anchor="task-center"))

   requested_resume_package = request.args.get("resume_package", "").strip()
   if requested_resume_package in PACKAGE_OPTIONS:
       session.pop("investment_purchase_resume", None)
       session.pop("all_inclusive_purchase_resume", None)
       session.pop("whatsapp_purchase_resume_package", None)
       session["task_purchase_resume_package"] = requested_resume_package

   purchases = PackagePurchase.query.filter_by(user_id=current_user.id).order_by(
       PackagePurchase.created_at.desc()
   ).all()
   actual_purchase_keys = {purchase.package_key for purchase in purchases}
   all_inclusive_purchase = all_inclusive_purchase_for_user(current_user.id, purchases)
   has_all_inclusive = all_inclusive_purchase is not None
   purchased_package_keys = (
       set(PACKAGE_OPTIONS)
       if has_all_inclusive
       else actual_purchase_keys.intersection(PACKAGE_OPTIONS)
   )
   task_submissions = {
       submission.task_id: submission
       for submission in TaskSubmission.query.filter_by(user_id=current_user.id).all()
   }
   selected_package_key = current_user.selected_task_package_key
   resume_task_package_key = session.get("task_purchase_resume_package")
   if (
       resume_task_package_key not in PACKAGE_OPTIONS
       or has_all_inclusive
       or resume_task_package_key in actual_purchase_keys
       or resume_task_package_key != selected_package_key
   ):
       session.pop("task_purchase_resume_package", None)
       resume_task_package_key = None
   has_agent_role = user_has_role(current_user, "agent")
   task_offer_now = utc_now_naive()
   task_offers = [
       offer
       for offer in TaskOffer.query.filter(
           db.or_(
               TaskOffer.status == "active",
               TaskOffer.id.in_(list(task_submissions.keys())) if task_submissions else db.false(),
           )
       ).order_by(TaskOffer.created_at.desc()).all()
       if offer.task_type != "whatsapp_views"
       and (
           task_submissions.get(offer.id)
           and (offer.audience != "agents_only" or has_agent_role)
       or (
           offer.status == "active"
           and offer.package_key in purchased_package_keys
           and (has_all_inclusive or not selected_package_key or offer.package_key == selected_package_key)
           and task_offer_is_available(offer, task_offer_now)
           and offer.target_country in {"ALL", current_user.country_code}
           and (offer.audience != "agents_only" or has_agent_role)
           and task_offer_capacity(offer)["remaining"] > 0
       ))
   ]
   task_capacities = {offer.id: task_offer_capacity(offer) for offer in task_offers}
   task_offer_deadlines = {
       offer.id: task_offer_deadline(offer)
       for offer in task_offers
   }
   task_offer_availability = {
       offer.id: task_offer_is_available(offer, task_offer_now)
       for offer in task_offers
   }
   daily_regular_activity = regular_task_activity_for_eat_day(
       current_user.id, now=task_offer_now
   )
   has_submissions = bool(task_submissions)
   regular_submissions = any(
       submission.task and submission.task.task_type != "whatsapp_views"
       for submission in task_submissions.values()
   )
   task_package_selection_locked = (has_submissions and regular_submissions) or bool(
       actual_purchase_keys.intersection(PACKAGE_OPTIONS)
   )
   if has_all_inclusive:
       task_package_selection_locked = False
   if task_package_selection_locked:
       selectable_task_package_keys = actual_purchase_keys.intersection(PACKAGE_OPTIONS) | {
           offer.package_key
           for offer in TaskOffer.query.join(TaskSubmission, TaskSubmission.task_id == TaskOffer.id)
           .filter(TaskSubmission.user_id == current_user.id, TaskOffer.task_type != "whatsapp_views").all()
       }
   else:
       selectable_task_package_keys = set(PACKAGE_OPTIONS)
   withdrawal_gate = task_withdrawal_gate_for_user(current_user.id)
   investment_package_gate = None
   requested_investment_withdrawal_id = request.args.get("investment_withdrawal", type=int)
   if requested_investment_withdrawal_id:
       gate_investment = Investment.query.filter_by(
           id=requested_investment_withdrawal_id,
           user_id=current_user.id,
           status="locked",
       ).first()
       if gate_investment and gate_investment.matures_at <= task_offer_now:
           if investment_task_package_access_for_user(current_user.id):
               session["investment_withdrawal_resume_id"] = gate_investment.id
               if investment_agent_program_access_for_user(current_user.id):
                   return redirect(url_for("investments", claim_investment=gate_investment.id))
               return redirect(url_for(
                   "agent_program",
                   investment_withdrawal=gate_investment.id,
               ))
           session["investment_withdrawal_resume_id"] = gate_investment.id
           investment_package_gate = gate_investment
   return render_template(
       "packages.html",
       packages=PACKAGE_OPTIONS,
       purchases=purchases,
       purchased_package_keys=purchased_package_keys,
       actual_purchase_keys=actual_purchase_keys,
       all_inclusive_purchase=all_inclusive_purchase,
       has_all_inclusive=has_all_inclusive,
       selected_task_package_key=selected_package_key,
       resume_task_package_key=resume_task_package_key,
       selectable_task_package_keys=selectable_task_package_keys,
       task_package_selection_locked=task_package_selection_locked,
       agent_program_fee=AGENT_PROGRAM_FEE,
       wallet=wallet_summary_for_user(current_user.id),
       daily_voucher=daily_voucher_state_for_user(current_user.id),
       daily_voucher_discounts={
           key: daily_voucher_discount(package["price"])
           for key, package in PACKAGE_OPTIONS.items()
       },
       minimum_wallet_deposit=MINIMUM_WALLET_DEPOSIT,
       task_offers=task_offers,
       task_capacities=task_capacities,
       task_submissions=task_submissions,
       task_offer_deadlines=task_offer_deadlines,
       task_offer_availability=task_offer_availability,
       task_offer_reward_amounts={
           offer.id: task_offer_reward_amount(offer) for offer in task_offers
       },
       daily_regular_activity=daily_regular_activity,
       task_offer_available_days=TASK_OFFER_AVAILABLE_DAYS,
       task_types=TASK_TYPES,
       whatsapp_view_reward=WHATSAPP_VIEW_REWARD,
       has_agent_role=has_agent_role,
       withdrawal_gate=withdrawal_gate,
       investment_package_gate=investment_package_gate,
       activation_requested=(
           request.args.get("activate_for_withdrawal") == "1"
           and withdrawal_gate.get("required")
           and withdrawal_gate.get("state") == "activate"
       ),
       task_withdrawal_wallet_activation_target=TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET,
   )


@app.route("/tasks")
@login_required
def tasks():
   return redirect(url_for("packages", _anchor="task-center"))


def task_offer_deadline(offer):
   created_at = offer.created_at or utc_now_naive()
   return created_at + timedelta(days=TASK_OFFER_AVAILABLE_DAYS)


def task_offer_is_available(offer, now=None):
   now = now or utc_now_naive()
   return offer.status == "active" and task_offer_deadline(offer) > now


def task_offer_reward_amount(offer):
   """Return an offer's payout rate; database migrations update existing offers."""
   if offer.task_type == "whatsapp_views":
       return WHATSAPP_VIEW_REWARD
   return int(offer.reward_amount or 0)


def eat_day_utc_bounds(now=None):
   """Return UTC-naive database bounds for the Nairobi calendar day."""
   now = now or datetime.now(timezone.utc)
   if now.tzinfo is None:
       now = now.replace(tzinfo=timezone.utc)
   local_date = now.astimezone(NAIROBI_TIMEZONE).date()
   start_local = datetime.combine(local_date, datetime.min.time()).replace(
       tzinfo=NAIROBI_TIMEZONE
   )
   end_local = datetime.combine(
       local_date + timedelta(days=1), datetime.min.time()
   ).replace(tzinfo=NAIROBI_TIMEZONE)
   return (
       start_local.astimezone(timezone.utc).replace(tzinfo=None),
       end_local.astimezone(timezone.utc).replace(tzinfo=None),
   )


def regular_task_submission_for_eat_day(
   user_id, now=None, exclude_submission_id=None, statuses=("pending", "approved")
):
   """Find a regular task submission that uses this member's daily reward slot."""
   start_utc, end_utc = eat_day_utc_bounds(now)
   query = (
       TaskSubmission.query.join(TaskOffer, TaskOffer.id == TaskSubmission.task_id)
       .filter(
           TaskSubmission.user_id == user_id,
           TaskSubmission.status.in_(statuses),
           TaskSubmission.submitted_at >= start_utc,
           TaskSubmission.submitted_at < end_utc,
           TaskOffer.task_type != "whatsapp_views",
       )
   )
   if exclude_submission_id is not None:
       query = query.filter(TaskSubmission.id != exclude_submission_id)
   return query.order_by(TaskSubmission.submitted_at.asc()).first()


def regular_task_reward_credited_for_eat_day(
   user_id, now=None, exclude_submission_id=None
):
   """Find a regular task reward already credited during this Nairobi day."""
   start_utc, end_utc = eat_day_utc_bounds(now)
   query = (
       TaskSubmission.query.join(TaskOffer, TaskOffer.id == TaskSubmission.task_id)
       .filter(
           TaskSubmission.user_id == user_id,
           TaskSubmission.status == "approved",
           TaskSubmission.reward_credited_at >= start_utc,
           TaskSubmission.reward_credited_at < end_utc,
           TaskOffer.task_type != "whatsapp_views",
       )
   )
   if exclude_submission_id is not None:
       query = query.filter(TaskSubmission.id != exclude_submission_id)
   return query.order_by(TaskSubmission.reward_credited_at.asc()).first()


def regular_task_activity_for_eat_day(user_id, now=None):
   """Return today's task submission or a regular reward already credited today."""
   return regular_task_submission_for_eat_day(user_id, now=now) or (
       regular_task_reward_credited_for_eat_day(user_id, now=now)
   )


def task_offer_capacity(offer):
   submitted_slots, reserved_amount = (
       db.session.query(
           db.func.count(TaskSubmission.id),
           db.func.coalesce(db.func.sum(TaskSubmission.reward_amount), 0),
       )
       .filter(
           TaskSubmission.task_id == offer.id,
           TaskSubmission.status.in_(("pending", "approved")),
       )
       .one()
   )
   submitted_slots = int(submitted_slots)
   reserved_amount = int(reserved_amount)
   remaining_budget = max(int(offer.budget_amount) - reserved_amount, 0)
   is_whatsapp_task = offer.task_type == "whatsapp_views"
   reward_unit = task_offer_reward_amount(offer)
   total_slots = offer.budget_amount // reward_unit if reward_unit else 0
   return {
       "total": total_slots,
       "used": reserved_amount // reward_unit if is_whatsapp_task and reward_unit else submitted_slots,
       "remaining": remaining_budget // reward_unit if reward_unit else 0,
       "spent": reserved_amount,
       "budget": offer.budget_amount,
       "remaining_budget": remaining_budget,
   }


@app.route("/tasks/<int:task_id>/submit", methods=["POST"])
@login_required
@limiter.limit("15 per hour", key_func=authenticated_user_rate_key, methods=["POST"])
def submit_task(task_id):
   if not valid_csrf_request():
       abort(400)
   locked_user = User.query.filter_by(id=current_user.id).with_for_update().first()
   if not locked_user or not locked_user.is_active:
       abort(403)
   offer = TaskOffer.query.filter_by(id=task_id, status="active").with_for_update().first()
   proof = request.form.get("proof", "").strip()
   proof_url = request.form.get("proof_url", "").strip()
   is_whatsapp_task = bool(offer and offer.task_type == "whatsapp_views")
   try:
       claimed_view_count = int(request.form.get("claimed_view_count", "0")) if is_whatsapp_task else None
   except (TypeError, ValueError):
       claimed_view_count = 0
   proof_url_parts = urlsplit(proof_url) if proof_url else None
   proof_url_is_valid = bool(
       proof_url_parts
       and proof_url_parts.scheme == "https"
       and proof_url_parts.netloc
       and not proof_url_parts.username
       and not proof_url_parts.password
   )
   capacity = task_offer_capacity(offer) if offer else None
   package_keys = purchased_package_keys_for_user(locked_user.id) if locked_user else set()
   has_all_inclusive = ALL_INCLUSIVE_PACKAGE_KEY in package_keys
   has_package_access = bool(
       offer and user_has_package_access(locked_user.id, offer.package_key, package_keys)
   )
   whatsapp_quota = (
       whatsapp_package_submission_quota(locked_user.id, offer.package_key)
       if offer and is_whatsapp_task and offer.package_key in WHATSAPP_EARN_PACKAGES
       else None
   )
   daily_regular_activity = (
       regular_task_activity_for_eat_day(locked_user.id)
       if offer and not is_whatsapp_task
       else None
   )
   if not offer:
       flash("This task is no longer available.", "warning")
   elif not task_offer_is_available(offer):
       flash("This task's 5-day listing period has ended. Choose a currently available task.", "warning")
   elif is_whatsapp_task and offer.package_key not in WHATSAPP_EARN_PACKAGES:
       flash("This WhatsApp campaign is not assigned to an active WhatsApp tier.", "warning")
   elif offer.audience == "agents_only" and not user_has_role(locked_user, "agent"):
       flash("This task is reserved for approved Agents.", "warning")
   elif current_user.country_code != "KE" and currency_kes_per_usd() is None:
       flash("Currency conversion is temporarily unavailable. Contact support before submitting work.", "warning")
   elif offer.target_country not in {"ALL", current_user.country_code}:
       flash("This task is not available in your selected country.", "warning")
   elif not has_package_access:
       if is_whatsapp_task:
           flash("Purchase the matching WhatsApp package or All-Inclusive to unlock this campaign.", "warning")
       else:
           flash("Purchase the matching task pass or All-Inclusive to unlock this task.", "warning")
   elif not is_whatsapp_task and not has_all_inclusive and not locked_user.selected_task_package_key:
       flash("Choose your purchased task bracket before submitting work.", "warning")
   elif not is_whatsapp_task and not has_all_inclusive and locked_user.selected_task_package_key != offer.package_key:
       flash("This task is outside your selected package bracket. All-Inclusive unlocks every bracket.", "warning")
   elif whatsapp_quota is not None and not whatsapp_quota["allowed"]:
       quota_limits = f"{whatsapp_quota['weekly_limit']} per week"
       if whatsapp_quota["daily_limit"]:
           quota_limits += f", {whatsapp_quota['daily_limit']} per day"
       flash(
           f"You have reached this package's posting limit ({quota_limits}).",
           "warning",
       )
   elif is_whatsapp_task and (
       claimed_view_count is None
       or claimed_view_count < 1
       or claimed_view_count > 100000
   ):
       flash("Enter a view count from 1 to 100,000.", "danger")
   elif is_whatsapp_task and not proof_url_is_valid:
       flash("Add an HTTPS link to a screenshot of the WhatsApp status view count.", "danger")
   elif is_whatsapp_task and claimed_view_count * WHATSAPP_VIEW_REWARD > capacity["remaining_budget"]:
       flash(
           f"This task has room for up to {capacity['remaining']} more funded views. Reduce your claim or choose another task.",
           "warning",
       )
   elif len(proof) < 10:
       flash("Add at least 10 characters of completion details or proof.", "danger")
   elif len(proof) > 3000:
       flash("Proof must be 3,000 characters or fewer.", "danger")
   elif TaskSubmission.query.filter_by(task_id=offer.id, user_id=current_user.id).first():
       flash("You have already submitted this task. Check its review status below.", "info")
   elif not is_whatsapp_task and daily_regular_activity:
       flash(
           "You have already submitted a regular task or received a regular task reward today. Only one regular task reward can be credited per Nairobi day; WhatsApp campaigns have separate limits.",
           "info",
       )
   elif not capacity["remaining"]:
       flash(
           "This task's funded view budget has been fully allocated."
           if is_whatsapp_task
           else "This task's reward budget has been fully allocated.",
           "warning",
       )
   else:
       submission = TaskSubmission(
           task_id=offer.id,
           user_id=current_user.id,
           proof=proof,
           proof_url=proof_url if is_whatsapp_task else None,
           claimed_view_count=claimed_view_count,
           reward_amount=(
               claimed_view_count * WHATSAPP_VIEW_REWARD
               if is_whatsapp_task
               else task_offer_reward_amount(offer)
           ),
       )
       db.session.add(submission)
       db.session.commit()
       app.logger.info(
           "task_submitted submission_id=%s task_id=%s user_id=%s",
           submission.id,
           offer.id,
           current_user.id,
       )
       if is_whatsapp_task:
           flash(
               f"Your claim for {claimed_view_count:,} WhatsApp status views is waiting for manual review. Approved views earn KES {WHATSAPP_VIEW_REWARD:,} each, subject to the task's funded budget. WhatsApp campaign earnings require approved Agent access before withdrawal. The matching WhatsApp package or All-Inclusive purchase and the wallet recharge step also apply.",
               "success",
           )
       else:
           flash(
               f"Task submitted for review. If approved, {format_user_money(task_offer_reward_amount(offer))} will appear in your wallet balance. Each member can receive at most one regular task reward per Nairobi day. The matching task pass or All-Inclusive must be active before you can submit tasks; after earning, confirmed wallet recharges totaling {format_user_money(TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET)} are required before withdrawal. Installments are allowed. Non-WhatsApp task earnings do not require Agent access.",
               "success",
           )
   return redirect(url_for("whatsapp_earn" if is_whatsapp_task else "packages", _anchor="task-center"))


@app.route("/agent-program", methods=["GET", "POST"])
@login_required
@limiter.limit("5 per hour", key_func=agent_application_rate_key, methods=["POST"])
@limiter.limit("30 per hour", key_func=get_client_ip, methods=["POST"])
def agent_program():
   agent_application = AgentApplication.query.filter_by(user_id=current_user.id).first()
   all_inclusive_purchase = all_inclusive_purchase_for_user(current_user.id)
   agent_fee_included = all_inclusive_purchase is not None
   if (
       request.method == "GET"
       and agent_fee_included
       and agent_application
       and agent_application.status == "awaiting_payment"
       and not agent_application.fee_paid_at
       and not agent_application.fee_waived_at
   ):
       agent_application.fee_waived_at = all_inclusive_purchase.created_at or utc_now_naive()
       agent_application.fee_waiver_note = "Included in All-Inclusive package purchase."
       agent_application.status = "pending"
       db.session.commit()
   has_agent_role = user_has_role(current_user, "agent")
   investment_withdrawal_gate = None
   if request.method == "GET":
       requested_investment_id = request.args.get("investment_withdrawal", type=int)
       if requested_investment_id:
           requested_investment = Investment.query.filter_by(
               id=requested_investment_id,
               user_id=current_user.id,
               status="locked",
           ).first()
           if requested_investment and requested_investment.matures_at <= utc_now_naive():
               session["investment_withdrawal_resume_id"] = requested_investment.id

       resume_investment_id = session.get("investment_withdrawal_resume_id")
       try:
           resume_investment_id = int(resume_investment_id)
       except (TypeError, ValueError):
           resume_investment_id = None
           session.pop("investment_withdrawal_resume_id", None)
       resume_investment = (
           Investment.query.filter_by(
               id=resume_investment_id,
               user_id=current_user.id,
               status="locked",
           ).first()
           if resume_investment_id
           else None
       )
       if not resume_investment or resume_investment.matures_at > utc_now_naive():
           session.pop("investment_withdrawal_resume_id", None)
       elif not investment_task_package_access_for_user(current_user.id):
           return redirect(url_for(
               "packages",
               investment_withdrawal=resume_investment.id,
               _anchor="task-center",
           ))
       elif has_agent_role:
           session.pop("investment_withdrawal_resume_id", None)
           flash(
               "Your task package and Agent Program are active. The matured investment is ready for release.",
               "success",
           )
           return redirect(url_for("investments", claim_investment=resume_investment.id))
       else:
           investment_withdrawal_gate = resume_investment

   confirmed_payment_id = request.args.get("fee_payment", type=int)
   if (
       request.method == "GET"
       and confirmed_payment_id
       and agent_application
       and agent_application.fee_payment_id == confirmed_payment_id
   ):
       confirmed_payment = Payment.query.filter_by(
           id=confirmed_payment_id,
           user_id=current_user.id,
           payment_type="agent_program_fee",
           status="paid",
       ).first()
       if confirmed_payment:
           flash(f"{format_user_money(AGENT_PROGRAM_FEE)} payment confirmed. Your agent application is now in the review queue.", "success")
           return redirect(url_for("agent_program"))

   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)

       locked_user = User.query.filter_by(id=current_user.id).with_for_update().first()
       if not locked_user or not locked_user.is_active:
           abort(403)
       agent_application = AgentApplication.query.filter_by(user_id=locked_user.id).first()

       action = request.form.get("action", "apply").strip().lower()
       if action == "request_fee_waiver":
           agent_application = AgentApplication.query.filter_by(
               user_id=locked_user.id
           ).with_for_update().first()
           if locked_user.country_code == "KE":
               flash("Manual fee-waiver requests are available to members outside Kenya. Kenyan members can use the listed M-PESA payment option.", "warning")
           elif not agent_application or agent_application.status != "awaiting_payment":
               flash("Submit your Agent Program application before requesting a fee-waiver review.", "warning")
           elif agent_fee_included or agent_application.fee_paid_at or agent_application.fee_waived_at:
               flash("This Agent Program application already has a paid or waived fee.", "info")
           elif agent_application.fee_waiver_requested_at:
               flash("Your manual fee-waiver request is already waiting for review.", "info")
           else:
               agent_application.fee_waiver_requested_at = utc_now_naive()
               db.session.commit()
               app.logger.info(
                   "agent_fee_waiver_requested user_id=%s application_id=%s country=%s",
                   locked_user.id,
                   agent_application.id,
                   locked_user.country_code,
               )
               flash("Your manual Agent Program fee-waiver request has been sent for review. This request does not grant Agent access; your application must also be approved.", "success")
           return redirect(url_for("agent_program", whatsapp_withdrawal=request.args.get("whatsapp_withdrawal")))

       if action == "update_whatsapp":
           whatsapp_input = request.form.get("whatsapp_phone", "").strip()
           consent_given = request.form.get("whatsapp_contact_consent") == "yes"
           if not agent_application or agent_application.status not in {"approved", "revoked"}:
               flash("WhatsApp support details can only be changed from an existing agent application.", "warning")
           elif not whatsapp_input or not consent_given:
               flash("Enter your WhatsApp number and confirm that referred members may contact you.", "danger")
           elif not re.fullmatch(r"[+0-9()\s-]+", whatsapp_input):
               flash("Enter a valid WhatsApp number with your country code.", "danger")
           else:
               try:
                   whatsapp_phone = normalize_agent_whatsapp_phone(
                       whatsapp_input,
                       locked_user.country_code,
                   )
               except ValueError as exc:
                   flash(str(exc), "danger")
               else:
                   agent_application.whatsapp_phone = whatsapp_phone
                   agent_application.whatsapp_contact_consent_at = utc_now_naive()
                   db.session.commit()
                   app.logger.info(
                       "agent_whatsapp_contact_updated user_id=%s application_id=%s",
                       current_user.id,
                       agent_application.id,
                   )
                   flash("Your WhatsApp support number is saved for your invite link.", "success")
           return redirect(url_for("agent_program"))

       if action == "remove_whatsapp":
           if agent_application and agent_application.status in {"approved", "revoked"}:
               agent_application.whatsapp_phone = None
               agent_application.whatsapp_contact_consent_at = None
               db.session.commit()
               app.logger.info(
                   "agent_whatsapp_contact_removed user_id=%s application_id=%s",
                   current_user.id,
                   agent_application.id,
               )
               flash("Your WhatsApp number has been removed from your invite link.", "success")
           else:
               flash("There is no active agent support number to remove.", "warning")
           return redirect(url_for("agent_program"))

       phone_input = request.form.get("phone", "").strip()
       whatsapp_input = request.form.get("whatsapp_phone", "").strip()
       location = request.form.get("location", "").strip()
       network = request.form.get("network", "").strip()
       consent_given = request.form.get("whatsapp_contact_consent") == "yes"
       phone = None
       whatsapp_phone = None

       if not phone_input or not whatsapp_input or not location or not network or not consent_given:
           flash("Enter your details and confirm that people joining through your invite may contact you on WhatsApp.", "danger")
       elif not re.fullmatch(r"[+0-9()\s-]+", phone_input):
           flash("Enter a valid phone number with your country code.", "danger")
       else:
           try:
               phone = normalize_agent_contact_phone(
                   phone_input,
                   locked_user.country_code,
               )
           except ValueError as exc:
               flash(str(exc), "danger")
           if not re.fullmatch(r"[+0-9()\s-]+", whatsapp_input):
               phone = None
               flash("Enter a valid WhatsApp number with your country code.", "danger")
           else:
               try:
                   whatsapp_phone = normalize_agent_whatsapp_phone(
                       whatsapp_input,
                       locked_user.country_code,
                   )
               except ValueError as exc:
                   phone = None
                   flash(str(exc), "danger")

       valid_location = (
           2 <= len(location) <= 100
           and all(character.isalnum() or character in " .,' "
       f"{(end_local - timedelta(days=1)).strftime('%d %b %Y')} EAT"
   )

   return render_template(
       "admin_agent_earnings.html",
       agent_earnings=agent_earnings,
       initial_bonus_total=initial_bonus_total,
       deposit_share_total=deposit_share_total,
       total_earned=total_earned,
       reward_count=reward_count,
       deposit_count=deposit_count,
       selected_week_start=selected_week_start.isoformat(),
       previous_week_start=(selected_week_start - timedelta(days=7)).isoformat(),
       next_week_start=(selected_week_start + timedelta(days=7)).isoformat(),
       has_next_week=selected_week_start < current_week_start,
       week_label=week_label,
   )


@app.route("/control-center/loans")
@limiter.limit("120 per hour", key_func=get_client_ip)
@admin_required
def admin_loans():
   selected_status = request.args.get("status", "all").lower()
   valid_statuses = {"all", "pending", "approved", "overdue", "declined", "revoked"}
   if selected_status not in valid_statuses:
       selected_status = "all"

   query = LoanRequest.query
   if selected_status == "overdue":
       query = query.filter(
           LoanRequest.status == "approved",
           LoanRequest.credit_awarded_at.isnot(None),
           LoanRequest.repayment_due_at.isnot(None),
           LoanRequest.repayment_due_at <= utc_now_naive(),
           LoanRequest.repaid_at.is_(None),
       )
   elif selected_status != "all":
       query = query.filter_by(status=selected_status)

   requests_for_review = query.order_by(
       (LoanRequest.status == "pending").desc(),
       LoanRequest.created_at.desc(),
   ).all()
   counts = {
       "all": LoanRequest.query.count(),
       "pending": LoanRequest.query.filter_by(status="pending").count(),
       "approved": LoanRequest.query.filter_by(status="approved").count(),
       "overdue": LoanRequest.query.filter(
           LoanRequest.status == "approved",
           LoanRequest.credit_awarded_at.isnot(None),
           LoanRequest.repayment_due_at.isnot(None),
           LoanRequest.repayment_due_at <= utc_now_naive(),
           LoanRequest.repaid_at.is_(None),
       ).count(),
       "declined": LoanRequest.query.filter_by(status="declined").count(),
       "revoked": LoanRequest.query.filter_by(status="revoked").count(),
   }
   return render_template(
       "admin_loans.html",
       loan_requests=requests_for_review,
       selected_status=selected_status,
       counts=counts,
       loan_now=utc_now_naive(),
   )


@app.route("/control-center/withdrawals")
@limiter.limit("120 per hour", key_func=get_client_ip)
@admin_required
def admin_withdrawals():
   selected_status = request.args.get("status", "all").lower()
   valid_statuses = {"all", "pending", "processing", "paid", "declined"}
   if selected_status not in valid_statuses:
       selected_status = "all"

   query = WithdrawalRequest.query
   if selected_status != "all":
       query = query.filter_by(status=selected_status)
   requests_for_review = query.order_by(
       (WithdrawalRequest.status == "pending").desc(),
       (WithdrawalRequest.status == "processing").desc(),
       WithdrawalRequest.created_at.asc(),
   ).all()
   now = utc_now_naive()
   counts = {
       "all": WithdrawalRequest.query.count(),
       "pending": WithdrawalRequest.query.filter_by(status="pending").count(),
       "processing": WithdrawalRequest.query.filter_by(status="processing").count(),
       "paid": WithdrawalRequest.query.filter_by(status="paid").count(),
       "declined": WithdrawalRequest.query.filter_by(status="declined").count(),
       "overdue": WithdrawalRequest.query.filter(
           WithdrawalRequest.status.in_(("pending", "processing")),
           WithdrawalRequest.created_at < now - timedelta(hours=WITHDRAWAL_REVIEW_HOURS),
       ).count(),
   }
   deadlines = {
       item.id: item.created_at + timedelta(hours=WITHDRAWAL_REVIEW_HOURS)
       for item in requests_for_review
   }
   return render_template(
       "admin_withdrawals.html",
       withdrawal_requests=requests_for_review,
       selected_status=selected_status,
       counts=counts,
       withdrawal_deadlines=deadlines,
       withdrawal_review_hours=WITHDRAWAL_REVIEW_HOURS,
       now=now,
   )


@app.route("/control-center/deposits")
def legacy_admin_deposits_redirect():
   return redirect(url_for("admin_deposits", **request.args.to_dict()), code=301)


@app.route("/control-center/recharges")
@limiter.limit("120 per hour", key_func=get_client_ip)
@admin_required
def admin_deposits():
   expire_stale_pending_wallet_deposits()
   selected_status = request.args.get("status", "all").strip().lower()
   if selected_status not in {"all", "paid", "pending", "failed"}:
       selected_status = "all"

   selected_method = request.args.get("method", "all").strip().lower()
   if selected_method not in {"all", "mpesa", "paypal", "paystack"}:
       selected_method = "all"

   deposit_query = Payment.query.filter_by(payment_type="wallet_deposit")
   status_counts = {
       row.status: int(row.count)
       for row in (
           db.session.query(Payment.status, db.func.count(Payment.id).label("count"))
           .filter(Payment.payment_type == "wallet_deposit")
           .group_by(Payment.status)
           .all()
       )
   }
   counts = {
       "all": sum(status_counts.values()),
       "paid": status_counts.get("paid", 0),
       "pending": status_counts.get("pending", 0),
       "failed": status_counts.get("failed", 0),
   }
   paid_total = int(
       db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
       .filter_by(payment_type="wallet_deposit", status="paid")
       .scalar()
       or 0
   )

   if selected_status != "all":
       deposit_query = deposit_query.filter_by(status=selected_status)
   if selected_method != "all":
       deposit_query = deposit_query.filter_by(payment_method=selected_method)

   total_rows = deposit_query.count()
   per_page = 50
   page = max(1, request.args.get("page", 1, type=int) or 1)
   page_count = max(1, (total_rows + per_page - 1) // per_page)
   page = min(page, page_count)
   deposits = (
       db.session.query(Payment, User.email)
       .join(User, User.id == Payment.user_id)
       .filter(Payment.id.in_(
           deposit_query.with_entities(Payment.id)
           .order_by(Payment.created_at.desc(), Payment.id.desc())
           .offset((page - 1) * per_page)
           .limit(per_page)
       ))
       .order_by(Payment.created_at.desc(), Payment.id.desc())
       .all()
   )
   deposit_phones = {
       payment.id: (
           payment.phone
           if payment.payment_method == "mpesa" and payment.phone
           else "Not collected"
       )
       for payment, _email in deposits
   }

   return render_template(
       "admin_deposits.html",
       deposits=deposits,
       deposit_phones=deposit_phones,
       selected_status=selected_status,
       selected_method=selected_method,
       counts=counts,
       paid_total=paid_total,
       total_rows=total_rows,
       page=page,
       page_count=page_count,
       per_page=per_page,
       pending_timeout_ms=WALLET_DEPOSIT_PENDING_TIMEOUT_MINUTES * 60 * 1000,
   )


TASK_TYPES = {
   "sponsored_visit": "Sponsored visit",
   "survey": "Survey",
   "other": "Other task",
   "whatsapp_views": "WhatsApp status views",
}
TASK_AUDIENCES = {
   "all_members": "Everyone",
   "agents_only": "Only Agents",
}


def package_purchases_for_user(user_id):
   return PackagePurchase.query.filter_by(user_id=user_id).all()


def purchased_package_keys_for_user(user_id, purchases=None):
   purchases = purchases if purchases is not None else package_purchases_for_user(user_id)
   return {purchase.package_key for purchase in purchases}


def all_inclusive_purchase_for_user(user_id, purchases=None):
   purchases = purchases if purchases is not None else package_purchases_for_user(user_id)
   return next(
       (purchase for purchase in purchases if purchase.package_key == ALL_INCLUSIVE_PACKAGE_KEY),
       None,
   )


def user_has_package_access(user_id, package_key, purchased_keys=None):
   purchased_keys = purchased_keys if purchased_keys is not None else purchased_package_keys_for_user(user_id)
   return package_key in purchased_keys or ALL_INCLUSIVE_PACKAGE_KEY in purchased_keys


def whatsapp_package_submission_quota(user_id, package_key, now=None):
   package = WHATSAPP_EARN_PACKAGES.get(package_key)
   if not package:
       return {"allowed": False, "weekly_used": 0, "weekly_limit": 0, "daily_used": 0}

   now = now or datetime.now(timezone.utc)
   if now.tzinfo is None:
       now = now.replace(tzinfo=timezone.utc)
   local_now = now.astimezone(NAIROBI_TIMEZONE)
   local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
   local_week_start = local_day_start - timedelta(days=local_day_start.weekday())
   day_start_utc = local_day_start.astimezone(timezone.utc).replace(tzinfo=None)
   week_start_utc = local_week_start.astimezone(timezone.utc).replace(tzinfo=None)

   submission_query = (
       db.session.query(TaskSubmission)
       .join(TaskOffer, TaskOffer.id == TaskSubmission.task_id)
       .filter(
           TaskSubmission.user_id == user_id,
           TaskSubmission.status.in_(("pending", "approved")),
           TaskOffer.task_type == "whatsapp_views",
           TaskOffer.package_key == package_key,
       )
   )
   weekly_used = int(
       submission_query.filter(TaskSubmission.submitted_at >= week_start_utc).count()
   )
   daily_used = int(
       submission_query.filter(TaskSubmission.submitted_at >= day_start_utc).count()
   )
   daily_limit = package.get("posts_per_day")
   allowed = weekly_used < package["posts_per_week"] and (
       daily_limit is None or daily_used < daily_limit
   )
   return {
       "allowed": allowed,
       "weekly_used": weekly_used,
       "weekly_limit": package["posts_per_week"],
       "daily_used": daily_used,
       "daily_limit": daily_limit,
   }


@app.route("/control-center/tasks", methods=["GET", "POST"])
@limiter.limit("30 per hour", key_func=get_client_ip, methods=["POST"])
@admin_required
def admin_tasks():
   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)
       title = request.form.get("title", "").strip()
       task_type = request.form.get("task_type", "").strip()
       package_key = request.form.get("package_key", "").strip()
       description = request.form.get("description", "").strip()
       instructions = request.form.get("instructions", "").strip()
       destination_url = request.form.get("destination_url", "").strip()
       promo_image_url = request.form.get("promo_image_url", "").strip()
       target_country = request.form.get("target_country", "ALL").strip().upper()
       audience = request.form.get("audience", "all_members").strip()
       is_whatsapp_task = task_type == "whatsapp_views"
       task_package_catalog = WHATSAPP_EARN_PACKAGES if is_whatsapp_task else PACKAGE_OPTIONS
       package = task_package_catalog.get(package_key)
       reward_amount = WHATSAPP_VIEW_REWARD if is_whatsapp_task else (package["reward"] if package else 0)
       try:
           budget_amount = int(request.form.get("budget_amount", "0"))
       except (TypeError, ValueError):
           budget_amount = 0

       url_parts = urlsplit(destination_url) if destination_url else None
       url_is_valid = not destination_url or bool(
           url_parts
           and url_parts.scheme in {"http", "https"}
           and url_parts.netloc
           and not url_parts.username
           and not url_parts.password
       )
       image_parts = urlsplit(promo_image_url) if promo_image_url else None
       image_url_is_valid = not promo_image_url or bool(
           image_parts
           and image_parts.scheme == "https"
           and image_parts.netloc
           and not image_parts.username
           and not image_parts.password
       )
       if not 4 <= len(title) <= 120:
           flash("Task title must be 4 to 120 characters.", "danger")
       elif task_type not in TASK_TYPES:
           flash("Choose a valid task type.", "danger")
       elif audience not in TASK_AUDIENCES:
           flash("Choose whether this task is for everyone or approved agents only.", "danger")
       elif not package:
           flash(
               "Choose a WhatsApp package tier for this campaign."
               if is_whatsapp_task
               else "Choose a valid regular task pass bracket.",
               "danger",
           )
       elif target_country != "ALL" and target_country not in COUNTRY_NAME_BY_CODE:
           flash("Choose a valid task country.", "danger")
       elif not 20 <= len(description) <= 3000:
           flash("Task description must be 20 to 3,000 characters.", "danger")
       elif not 10 <= len(instructions) <= 3000:
           flash("Completion instructions must be 10 to 3,000 characters.", "danger")
       elif not 1 <= reward_amount <= 100000:
           flash("Set a reward between KES 1 and KES 100,000.", "danger")
       elif not reward_amount <= budget_amount <= 10000000:
           flash(
               "The task budget must cover at least one KES 100 view for WhatsApp tasks and cannot exceed KES 10,000,000."
               if is_whatsapp_task
               else "The task budget must cover at least one reward and cannot exceed KES 10,000,000.",
               "danger",
           )
       elif len(destination_url) > 500 or not url_is_valid:
           flash("Use a valid HTTP or HTTPS task link, up to 500 characters.", "danger")
       elif len(promo_image_url) > 500 or not image_url_is_valid:
           flash("Use a valid HTTPS image URL, up to 500 characters.", "danger")
       elif is_whatsapp_task and not destination_url:
           flash("WhatsApp status tasks need a public company profile or website link.", "danger")
       elif is_whatsapp_task and not promo_image_url:
           flash("WhatsApp status tasks need a public HTTPS image URL for members to post.", "danger")
       else:
           offer = TaskOffer(
               title=title,
               task_type=task_type,
               description=description,
               instructions=instructions,
               destination_url=destination_url or None,
               promo_image_url=promo_image_url or None,
               package_key=package_key,
               target_country=target_country,
               audience=audience,
               reward_amount=reward_amount,
               budget_amount=budget_amount,
               status="active",
               created_by=current_user.id,
           )
           db.session.add(offer)
           db.session.commit()
           app.logger.info(
               "task_offer_created task_id=%s admin_user_id=%s package=%s target_country=%s audience=%s reward=%s type=%s",
               offer.id,
               current_user.id,
               package_key,
               target_country,
               audience,
               reward_amount,
               task_type,
           )
           flash(
               (
                   f"WhatsApp views task published for {package['name']} ({TASK_AUDIENCES[audience]}; {'all countries' if target_country == 'ALL' else COUNTRY_NAME_BY_CODE[target_country]}). Each verified view pays KES {WHATSAPP_VIEW_REWARD:,}, within the funded budget."
                   if is_whatsapp_task
                   else f"Task published for {package['name']} ({TASK_AUDIENCES[audience]}; {'all countries' if target_country == 'ALL' else COUNTRY_NAME_BY_CODE[target_country]}). Each approved task pays KES {reward_amount:,}; a member can receive at most one regular task reward per Nairobi day."
               ),
               "success",
           )
           return redirect(url_for("admin_tasks"))

   selected_status = request.args.get("status", "pending").lower()
   valid_statuses = {"all", "pending", "approved", "declined"}
   if selected_status not in valid_statuses:
       selected_status = "pending"
   submission_query = TaskSubmission.query
   if selected_status != "all":
       submission_query = submission_query.filter_by(status=selected_status)
   submissions = submission_query.order_by(
       (TaskSubmission.status == "pending").desc(),
       TaskSubmission.submitted_at.asc(),
   ).limit(200).all()
   tasks = TaskOffer.query.order_by(TaskOffer.created_at.desc()).all()
   counts = {
       status: TaskSubmission.query.filter_by(status=status).count()
       for status in ("pending", "approved", "declined")
   }
   counts["all"] = TaskSubmission.query.count()
   task_capacities = {task.id: task_offer_capacity(task) for task in tasks}
   task_offer_now = utc_now_naive()
   task_package_catalog = {**PACKAGE_OPTIONS, **WHATSAPP_EARN_PACKAGES}
   return render_template(
       "admin_tasks.html",
       tasks=tasks,
       submissions=submissions,
       selected_status=selected_status,
       counts=counts,
       task_types=TASK_TYPES,
       task_audiences=TASK_AUDIENCES,
       whatsapp_view_reward=WHATSAPP_VIEW_REWARD,
       packages=PACKAGE_OPTIONS,
       whatsapp_packages=WHATSAPP_EARN_PACKAGES,
       task_package_catalog=task_package_catalog,
       countries=COUNTRIES,
       task_capacities=task_capacities,
       task_offer_reward_amounts={
           task.id: task_offer_reward_amount(task) for task in tasks
       },
       minimum_wallet_deposit=MINIMUM_WALLET_DEPOSIT,
       task_withdrawal_wallet_activation_target=TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET,
       task_offer_deadlines={task.id: task_offer_deadline(task) for task in tasks},
       task_offer_availability={
           task.id: task_offer_is_available(task, task_offer_now)
           for task in tasks
       },
   )


@app.route("/control-center/tasks/<int:task_id>/status", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def update_task_offer_status(task_id):
   if not valid_csrf_request():
       abort(400)
   offer = db.session.get(TaskOffer, task_id)
   if not offer:
       abort(404)
   decision = request.form.get("status", "")
   if decision not in {"active", "paused"}:
       flash("Choose a valid task status.", "danger")
   else:
       was_inactive = offer.status != "active"
       offer.status = decision
       if decision == "active" and was_inactive:
           offer.created_at = utc_now_naive()
       offer.updated_at = utc_now_naive()
       db.session.commit()
       flash(f"{offer.title} is now {decision}.", "success")
   return redirect(url_for("admin_tasks"))


@app.route("/control-center/tasks/submissions/<int:submission_id>/review", methods=["POST"])
@limiter.limit("60 per hour", key_func=get_client_ip)
@admin_required
def review_task_submission(submission_id):
   if not valid_csrf_request():
       abort(400)
   submission_lookup = db.session.get(TaskSubmission, submission_id)
   if not submission_lookup:
       abort(404)
   locked_user = User.query.filter_by(id=submission_lookup.user_id).with_for_update().first()
   if not locked_user:
       abort(404)
   submission = db.session.get(TaskSubmission, submission_id, with_for_update=True)
   if not submission:
       abort(404)
   offer = TaskOffer.query.filter_by(id=submission.task_id).with_for_update().first()
   if not offer:
       abort(404)
   decision = request.form.get("decision", "").strip()
   review_note = request.form.get("review_note", "").strip()
   verified_view_count = None
   if offer.task_type == "whatsapp_views" and decision == "approved":
       try:
           verified_view_count = int(request.form.get("verified_view_count", "0"))
       except (TypeError, ValueError):
           verified_view_count = 0
   if submission.status != "pending":
       flash("This task submission has already been reviewed.", "warning")
   elif decision not in {"approved", "declined"}:
       flash("Choose approve or decline for this submission.", "danger")
   elif decision == "declined" and not 10 <= len(review_note) <= 500:
       flash("Add a decline reason between 10 and 500 characters.", "danger")
   elif len(review_note) > 500:
       flash("Review notes must be 500 characters or fewer.", "danger")
   elif (
       offer.task_type == "whatsapp_views"
       and decision == "approved"
       and (
           not submission.claimed_view_count
           or submission.claimed_view_count < 1
           or submission.reward_amount != submission.claimed_view_count * WHATSAPP_VIEW_REWARD
           or verified_view_count < 1
           or verified_view_count > submission.claimed_view_count
       )
   ):
       flash(
           f"Enter a verified view count from 1 to the member's claimed count ({submission.claimed_view_count or 0}).",
           "danger",
       )
   elif (
       offer.task_type != "whatsapp_views"
       and decision == "approved"
       and regular_task_submission_for_eat_day(
           submission.user_id,
           now=submission.submitted_at,
           exclude_submission_id=submission.id,
           statuses=("approved",),
       )
   ):
       flash(
           "Another regular task reward has already been approved for this member on the submission's Nairobi day. Decline this duplicate-day submission or review it against the daily limit.",
           "warning",
       )
   elif (
       offer.task_type != "whatsapp_views"
       and decision == "approved"
       and regular_task_reward_credited_for_eat_day(
           submission.user_id,
           now=utc_now_naive(),
           exclude_submission_id=submission.id,
       )
   ):
       flash(
           "This member already received a regular task reward today. Only one regular task reward can be credited per Nairobi day; review this submission on a later day or decline it.",
           "warning",
       )
   elif not locked_user.is_active:
       flash("This member account is closed. The task cannot be reviewed or credited.", "warning")
   else:
       if offer.task_type == "whatsapp_views" and decision == "approved":
           submission.verified_view_count = verified_view_count
           submission.reward_amount = verified_view_count * WHATSAPP_VIEW_REWARD
       elif offer.task_type != "whatsapp_views" and decision == "approved":
           submission.reward_amount = task_offer_reward_amount(offer)
       submission.status = decision
       submission.review_note = review_note or None
       submission.reviewed_by = current_user.id
       submission.reviewed_at = utc_now_naive()
       if decision == "approved":
           submission.reward_credited_at = submission.reviewed_at
       db.session.commit()
       app.logger.info(
           "task_submission_reviewed submission_id=%s task_id=%s user_id=%s admin_user_id=%s decision=%s amount=%s",
           submission.id,
           submission.task_id,
           submission.user_id,
           current_user.id,
           decision,
           submission.reward_amount if decision == "approved" else 0,
       )
       if decision == "approved" and offer.task_type == "whatsapp_views":
           flash(
               f"{verified_view_count:,} WhatsApp status views approved at KES {WHATSAPP_VIEW_REWARD:,} each; KES {submission.reward_amount:,} credited to {locked_user.full_name}'s wallet.",
               "success",
           )
       elif decision == "approved":
           flash(
               f"Submission approved and KES {submission.reward_amount:,} credited to {locked_user.full_name}'s wallet.",
               "success",
           )
       else:
           flash("Submission declined and the reason was sent to the member.", "info")
   return redirect(url_for("admin_tasks"))


@app.route("/control-center/withdrawals/<int:withdrawal_id>/review", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def review_withdrawal_request(withdrawal_id):
   if not valid_csrf_request():
       abort(400)

   withdrawal = WithdrawalRequest.query.filter_by(id=withdrawal_id).with_for_update().first_or_404()
   decision = request.form.get("decision", "").strip().lower()
   admin_note = request.form.get("admin_note", "").strip()
   payout_reference = request.form.get("payout_reference", "").strip()
   selected_status = request.args.get("status", "pending")

   if decision == "processing" and withdrawal.status != "pending":
       flash("Only pending withdrawals can be moved to processing.", "warning")
   elif decision == "paid" and withdrawal.status != "processing":
       flash("Move this withdrawal to processing before marking it paid.", "warning")
   elif (
       decision == "paid"
       and wallet_summary_for_user(withdrawal.user_id)["account_balance"] < withdrawal.amount
   ):
       flash(
           "The member's current account balance no longer covers this payout. "
           "Resolve the balance or decline the request.",
           "warning",
       )
   elif decision == "paid" and not payout_reference:
       flash("Enter the payout receipt or reference before marking this withdrawal paid.", "danger")
   elif decision == "declined" and withdrawal.status not in {"pending", "processing"}:
       flash("Only pending or processing withdrawals can be declined.", "warning")
   elif decision == "declined" and not admin_note:
       flash("Enter a reason before declining this withdrawal.", "danger")
   elif len(admin_note) > 500 or len(payout_reference) > 100:
       flash("The note or payout reference is too long.", "danger")
   elif decision not in {"processing", "paid", "declined"}:
       flash("Choose a valid withdrawal action.", "danger")
   else:
       now = utc_now_naive()
       previous_status = withdrawal.status
       withdrawal.reviewed_by = current_user.id
       withdrawal.reviewed_at = now
       withdrawal.admin_note = admin_note or None
       if decision == "processing":
           withdrawal.status = "processing"
       elif decision == "paid":
           withdrawal.status = "paid"
           withdrawal.payout_reference = payout_reference
           withdrawal.processed_at = now
       else:
           withdrawal.status = "declined"
       db.session.commit()
       app.logger.info(
           "withdrawal_reviewed request_id=%s reviewer_id=%s previous_status=%s decision=%s amount=%s",
           withdrawal.id,
           current_user.id,
           previous_status,
           decision,
           withdrawal.amount,
       )
       flash(
           f"Withdrawal #{withdrawal.id} marked {decision}.",
           "success",
       )

   return redirect(url_for("admin_withdrawals", status=selected_status))


@app.route("/control-center/agents")
@limiter.limit("120 per hour", key_func=get_client_ip)
@admin_required
def admin_agent_applications():
   selected_status = request.args.get("status", "all").lower()
   valid_statuses = {"all", "awaiting_payment", "pending", "approved", "declined", "revoked", "appeals"}
   if selected_status not in valid_statuses:
       selected_status = "all"

   if selected_status == "appeals":
       applications = []
       appeals_for_review = AgentAppeal.query.filter_by(status="pending").order_by(
           AgentAppeal.submitted_at.asc()
       ).all()
   else:
       query = AgentApplication.query
       if selected_status != "all":
           query = query.filter_by(status=selected_status)
       applications = query.order_by(
           (AgentApplication.status == "pending").desc(),
           AgentApplication.created_at.desc(),
       ).all()
       appeals_for_review = []

   counts = {
       "all": AgentApplication.query.count(),
       "awaiting_payment": AgentApplication.query.filter_by(status="awaiting_payment").count(),
       "pending": AgentApplication.query.filter_by(status="pending").count(),
       "approved": AgentApplication.query.filter_by(status="approved").count(),
       "declined": AgentApplication.query.filter_by(status="declined").count(),
       "revoked": AgentApplication.query.filter_by(status="revoked").count(),
       "appeals": AgentAppeal.query.filter_by(status="pending").count(),
   }
   return render_template(
       "admin_agent_applications.html",
       applications=applications,
       appeals_for_review=appeals_for_review,
       selected_status=selected_status,
       counts=counts,
       agent_program_fee=AGENT_PROGRAM_FEE,
   )


@app.route("/control-center/agents/<int:application_id>/review", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def review_agent_application(application_id):
   if not valid_csrf_request():
       abort(400)

   application_lookup = AgentApplication.query.get_or_404(application_id)
   application_user = User.query.filter_by(id=application_lookup.user_id).with_for_update().first()
   application = AgentApplication.query.filter_by(id=application_id).with_for_update().first_or_404()
   decision = request.form.get("decision", "").strip().lower()
   admin_note = request.form.get("admin_note", "").strip()
   selected_status = request.args.get("status", "all")

   if not application_user or not application_user.is_active:
       flash("This member account is closed, so its agent access cannot be changed.", "warning")
   elif decision not in {"approved", "declined", "revoked"}:
       flash("Choose approve, decline, or revoke to record a decision.", "danger")
   elif decision == "approved" and not (application.fee_paid_at or application.fee_waived_at):
       flash(f"The {format_user_money(AGENT_PROGRAM_FEE)} agent program fee must be paid or explicitly waived before approval.", "danger")
   elif decision in {"approved", "declined"} and application.status != "pending":
       flash("Only pending agent applications can be approved or declined.", "warning")
   elif decision == "revoked" and application.status not in {"pending", "approved"}:
       flash("Only pending or approved agent applications can be revoked.", "warning")
   elif decision == "revoked" and not admin_note:
       flash("Enter a reason before revoking this agent application.", "danger")
   elif len(admin_note) > 500:
       flash("The review note must be 500 characters or fewer.", "danger")
   else:
       previous_status = application.status
       application.status = decision
       application.admin_note = admin_note or None
       application.reviewed_by = current_user.id
       application.reviewed_at = utc_now_naive()
       set_agent_role(application.user_id, decision == "approved")
       db.session.commit()
       app.logger.info(
           "agent_application_reviewed application_id=%s reviewer_id=%s previous_status=%s decision=%s",
           application.id,
           current_user.id,
           previous_status,
           decision,
       )
       if decision == "revoked" and previous_status == "approved":
           flash("Agent approval revoked.", "success")
       elif decision == "revoked":
           flash("Agent application revoked.", "success")
       else:
           flash(f"Agent application {decision}.", "success")

   return redirect(url_for("admin_agent_applications", status=selected_status))


@app.route("/control-center/agents/<int:application_id>/waive-fee", methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip)
@admin_required
def waive_agent_application_fee(application_id):
   if not valid_csrf_request():
       abort(400)

   application_lookup = AgentApplication.query.get_or_404(application_id)
   application_user = User.query.filter_by(id=application_lookup.user_id).with_for_update().first()
   application = AgentApplication.query.filter_by(id=application_id).with_for_update().first_or_404()
   waiver_note = request.form.get("fee_waiver_note", "").strip()
   selected_status = request.args.get("status", "all")
   if selected_status not in {"all", "awaiting_payment", "pending", "approved", "declined", "revoked", "appeals"}:
       selected_status = "all"

   if not application_user or not application_user.is_active:
       flash("This member account is closed, so its agent fee cannot be changed.", "warning")
   elif application.status != "awaiting_payment":
       flash("Only applications awaiting payment can receive a fee waiver.", "warning")
   elif application.fee_paid_at or application.fee_waived_at:
       flash("This application already has a paid or waived program fee.", "warning")
   elif len(waiver_note) < 3 or len(waiver_note) > 500:
       flash("Add a fee-waiver reason between 3 and 500 characters.", "danger")
   elif Payment.query.filter_by(
       user_id=application.user_id,
       payment_type="agent_program_fee",
       status="pending",
   ).first():
       flash("This applicant has an M-PESA fee payment awaiting confirmation. Resolve it before waiving the fee.", "warning")
   else:
       application.fee_waived_at = utc_now_naive()
       application.fee_waived_by = current_user.id
       application.fee_waiver_note = waiver_note
       application.status = "pending"
       application.admin_note = None
       db.session.commit()
       app.logger.info(
           "agent_application_fee_waived application_id=%s admin_id=%s",
           application.id,
           current_user.id,
       )
       flash("Agent program fee waived. Review the application and approve it to grant Agent access.", "success")

   return redirect(url_for("admin_agent_applications", status=selected_status))


@app.route("/control-center/agents/appeals/<int:appeal_id>/review", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def review_agent_appeal(appeal_id):
   if not valid_csrf_request():
       abort(400)

   appeal_lookup = AgentAppeal.query.get_or_404(appeal_id)
   appeal_user_id = appeal_lookup.application.user_id
   appeal_user = User.query.filter_by(id=appeal_user_id).with_for_update().first()
   appeal = AgentAppeal.query.filter_by(id=appeal_id).with_for_update().first_or_404()
   decision = request.form.get("decision", "").strip().lower()
   review_note = request.form.get("review_note", "").strip()

   if not appeal_user or not appeal_user.is_active:
       flash("This member account is closed, so its appeal cannot change account access.", "warning")
   elif decision not in {"approved", "declined"}:
       flash("Choose approve or decline to record an appeal decision.", "danger")
   elif appeal.status != "pending":
       flash("Only pending appeals can be reviewed.", "warning")
   elif appeal.application.status != "revoked":
       flash("This appeal can no longer be reviewed because the agent status has changed.", "warning")
   elif len(review_note) > 500:
       flash("The appeal review note must be 500 characters or fewer.", "danger")
   else:
       appeal.status = decision
       appeal.review_note = review_note or None
       appeal.reviewed_by = current_user.id
       appeal.reviewed_at = utc_now_naive()
       if decision == "approved":
           appeal.application.status = "approved"
           appeal.application.reviewed_by = current_user.id
           appeal.application.reviewed_at = utc_now_naive()
           set_agent_role(appeal.application.user_id, True)
       else:
           set_agent_role(appeal.application.user_id, False)
       db.session.commit()
       app.logger.info(
           "agent_appeal_reviewed appeal_id=%s application_id=%s reviewer_id=%s decision=%s",
           appeal.id,
           appeal.application_id,
           current_user.id,
           decision,
       )
       flash(
           "Agent access restored and appeal approved."
           if decision == "approved"
           else "Appeal declined. The agent program remains revoked.",
           "success",
       )

   return redirect(url_for("admin_agent_applications", status="appeals"))


@app.route("/control-center/loans/<int:loan_id>/review", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def review_loan_request(loan_id):
   if not valid_csrf_request():
       abort(400)

   loan_request = LoanRequest.query.get_or_404(loan_id)
   # Lock the member before the request, matching the lock order used when
   # members submit loan requests and preventing approval against stale funds.
   loan_user = User.query.filter_by(id=loan_request.user_id).with_for_update().first()
   loan_request = LoanRequest.query.filter_by(id=loan_id).with_for_update().first_or_404()
   decision = request.form.get("decision", "").strip().lower()
   admin_note = request.form.get("admin_note", "").strip()
   current_loan_limit = 0
   current_qualifying_deposits = 0
   if decision in {"approved", "credit"} and loan_user is not None:
       current_wallet = wallet_summary_for_user(loan_user.id)
       current_qualifying_deposits = qualifying_loan_balance_for_wallet(current_wallet)
       current_loan_limit = maximum_loan_for_deposit_balance(current_qualifying_deposits)

   if decision not in {"approved", "declined", "revoked", "credit"}:
       flash("Choose approve, decline, revoke, or issue a credit to record a decision.", "danger")
   elif decision in {"approved", "declined"} and loan_request.status != "pending":
       flash("Only pending loan requests can be approved or declined.", "warning")
   elif decision == "revoked" and loan_request.status not in {"pending", "approved"}:
       flash("Only pending or approved loan requests can be revoked.", "warning")
   elif decision == "revoked" and not admin_note:
       flash("Enter a reason before revoking this loan request.", "danger")
   elif decision == "revoked" and loan_request.repaid_at is not None:
       flash("A repaid loan cannot be revoked or have its credit removed.", "warning")
   elif decision == "credit" and (loan_request.status != "approved" or loan_request.credit_awarded_at is not None):
       flash("Only approved loan requests without an account credit can be credited.", "warning")
   elif decision in {"approved", "credit"} and loan_user is None:
       flash("The member account for this loan request is unavailable.", "warning")
   elif decision in {"approved", "credit"} and loan_request.amount > current_loan_limit:
       db.session.rollback()
       flash(
               f"This request exceeds the member's current wallet-recharge band. Their qualifying "
           f"wallet-recharge balance is {format_user_money(current_qualifying_deposits)}, which supports "
           f"a maximum loan request of {format_loan_limit(current_loan_limit)}. "
           "The member must qualify for a higher band before this request can be approved or credited.",
           "warning",
       )
   elif len(admin_note) > 500:
       flash("The review note must be 500 characters or fewer.", "danger")
   else:
       previous_status = loan_request.status
       was_credited = loan_request.credit_awarded_at is not None
       if decision in {"approved", "declined", "revoked"}:
           loan_request.status = decision
       if decision in {"approved", "credit"}:
           credit_time = utc_now_naive()
           if not was_credited:
               loan_request.credit_awarded_at = credit_time
               if loan_request.loan_term_months:
                   loan_request.repayment_due_at = add_calendar_months(
                       credit_time,
                       loan_request.loan_term_months,
                   )
       loan_request.admin_note = admin_note or None
       loan_request.reviewed_by = current_user.id
       loan_request.reviewed_at = utc_now_naive()
       db.session.commit()
       app.logger.info(
           "loan_request_reviewed loan_id=%s reviewer_id=%s previous_status=%s decision=%s amount=%s",
           loan_request.id,
           current_user.id,
           previous_status,
           decision,
           loan_request.amount,
       )
       if decision == "approved":
           flash(f"Loan request #{loan_request.id} approved. KES {loan_request.amount:,} has been added to the account balance.", "success")
       elif decision == "credit":
           flash(f"KES {loan_request.amount:,} has been added to the account balance.", "success")
       elif decision == "revoked" and previous_status == "approved" and was_credited:
           flash(f"Loan request #{loan_request.id} revoked. KES {loan_request.amount:,} has been removed from the account balance.", "success")
       else:
           flash(f"Loan request #{loan_request.id} marked {decision}.", "success")

   return redirect(url_for("admin_loans", status=request.args.get("status", "pending")))


@app.route("/control-center/loans/<int:loan_id>/repayment", methods=["POST"])
@limiter.limit("30 per minute", key_func=get_client_ip)
@admin_required
def record_loan_repayment(loan_id):
   if not valid_csrf_request():
       abort(400)

   loan_request = LoanRequest.query.filter_by(id=loan_id).with_for_update().first_or_404()
   repayment_note = request.form.get("repayment_note", "").strip()
   selected_status = request.args.get("status", "all")
   if loan_request.status != "approved" or loan_request.credit_awarded_at is None:
       flash("Only approved and credited loans can be marked as repaid.", "warning")
   elif loan_request.repaid_at is not None:
       flash("Repayment has already been recorded for this loan.", "warning")
   elif not repayment_note or len(repayment_note) > 500:
       flash("Enter the repayment receipt or a short note confirming how it was received.", "danger")
   else:
       loan_request.repaid_at = utc_now_naive()
       loan_request.repayment_note = repayment_note
       db.session.commit()
       app.logger.info(
           "loan_repayment_recorded loan_id=%s reviewer_id=%s member_id=%s",
           loan_request.id,
           current_user.id,
           loan_request.user_id,
       )
       flash(f"Repayment recorded for loan #{loan_request.id}.", "success")

   return redirect(url_for("admin_loans", status=selected_status))

@app.route("/support")
@login_required
def support():
   is_kenyan = current_user.country_code == "KE"
   return render_template(
       "support.html",
       support_chat_menu=SUPPORT_CHAT_MENU,
       support_guided_responses=support_guided_responses_for_country(current_user.country_code),
       support_ai_enabled=bool(env_first("GEMINI_API_KEY")),
       support_is_kenyan=is_kenyan,
       task_withdrawal_wallet_activation_target=TASK_WITHDRAWAL_WALLET_ACTIVATION_TARGET,
       whatsapp_view_reward=WHATSAPP_VIEW_REWARD,
       packages=PACKAGE_OPTIONS,
   )


@app.route("/chatbot")
@login_required
def chatbot():
   return redirect(url_for("support"))

@app.route("/ask", methods=["POST"])
@login_required
@limiter.limit("5 per minute", key_func=authenticated_user_rate_key, methods=["POST"])
@limiter.limit("60 per hour", key_func=get_client_ip, methods=["POST"])
def ask():
   if not valid_csrf_request():
       return jsonify({"error": "Your session security token expired. Refresh this page and try again."}), 400

   data = request.get_json(silent=True)
   user_message = data.get("message") if isinstance(data, dict) else None
   if not isinstance(user_message, str):
       return jsonify({"error": "Send a text message."}), 400
   user_message = user_message.strip()

   if not user_message:
       return jsonify({"error": "No message provided"}), 400
   if len(user_message) > 4000:
       return jsonify({"error": "Message must be 4,000 characters or fewer."}), 413
   topic = data.get("topic") if isinstance(data.get("topic"), str) else None
   if topic not in SUPPORT_CHAT_MENU_BY_KEY:
       topic = None

   if support_message_contains_private_data(user_message):
       bot_response = SUPPORT_GUIDED_RESPONSES["sensitive"]
       provider = "guided"
       action = {"label": "Open Contact details", "href": url_for("contact")}
       quick_replies = support_follow_up_replies("contact")
   else:
       bot_response = gemini_support_reply(user_message, topic=topic)
       provider = "gemini" if bot_response else "guided"
       if not bot_response:
           bot_response = get_offline_chat_response(user_message, topic=topic)
       response_topic = topic or infer_support_topic(user_message)
       action = None
       action_config = SUPPORT_TOPIC_ACTIONS.get(response_topic)
       if action_config:
           endpoint, label = action_config
           action = {"label": label, "href": url_for(endpoint)}
       quick_replies = support_follow_up_replies(response_topic)

   return jsonify({
       "response": bot_response,
       "provider": provider,
       "action": action,
       "quick_replies": quick_replies,
   })



def account_password_change_rate_key():
   user_id = current_user.get_id() if current_user.is_authenticated else "anonymous"
   return f"password-change:{user_id}:{get_client_ip()}"


def account_closure_block_reason(user):
   pending_withdrawal = (
       WithdrawalRequest.query.filter(
           WithdrawalRequest.user_id == user.id,
           WithdrawalRequest.status.in_(("pending", "processing")),
       )
       .with_for_update()
       .first()
   )
   if pending_withdrawal:
       return "A withdrawal is still being processed. Contact support after it is resolved."

   open_loan = (
       LoanRequest.query.filter(
           LoanRequest.user_id == user.id,
           LoanRequest.status.in_(("pending", "approved")),
       )
       .with_for_update()
       .first()
   )
   if open_loan:
       return "A loan request or approved loan is still open. Contact support to resolve it before closing your account."

   open_agent_application = AgentApplication.query.filter(
       AgentApplication.user_id == user.id,
       AgentApplication.status.in_(("awaiting_payment", "pending")),
   ).with_for_update().first()
   if open_agent_application:
       return "Your agent application is still being processed. Contact support before closing your account."

   pending_task = TaskSubmission.query.filter_by(
       user_id=user.id,
       status="pending",
   ).with_for_update().first()
   if pending_task:
       return "A task submission is still awaiting review. Wait for the review or contact support before closing your account."

   return None


@app.route("/account-settings/close", methods=["POST"])
@login_required
@limiter.limit("3 per hour", key_func=account_password_change_rate_key, methods=["POST"])
@limiter.limit("10 per hour", key_func=get_client_ip, methods=["POST"])
def close_account():
   if not valid_csrf_request():
       abort(400)
   expire_stale_pending_wallet_deposits()

   current_password = request.form.get("current_password", "")
   confirmation = request.form.get("confirmation", "").strip()
   if not current_password or not bcrypt.check_password_hash(current_user.password_hash, current_password):
       log_security_event("account_closure_failed", current_user.email, "invalid_password")
       flash("Your password was not accepted. Your account is still open.", "danger")
       return redirect(url_for("account_settings"))
   if confirmation != "DELETE":
       flash('Type DELETE exactly to confirm account closure.', "danger")
       return redirect(url_for("account_settings"))

   user_id = current_user.id
   locked_user = User.query.filter_by(id=user_id).with_for_update().first()
   if not locked_user or not locked_user.is_active:
       logout_user()
       session.clear()
       session["_remember"] = "clear"
       return redirect(url_for("form2", next="login"))

   # Payment callbacks lock the payment before touching referral rows. Lock
   # outstanding payments here so deletion waits for any callback already in
   # progress. Pending provider checkouts remain in the de-identified ledger
   # and may still be reconciled by a later verified callback.
   Payment.query.filter_by(user_id=user_id, status="pending").with_for_update().all()

   # Close pending referral opportunities under row locks before the final
   # closure checks. A simultaneous referred-member deposit either finishes
   # first and remains in the de-identified ledger, or sees a closed referral.
   Referral.query.filter(
       db.or_(
           Referral.referrer_user_id == user_id,
           db.and_(Referral.referred_user_id == user_id, Referral.status != "rewarded"),
       )
   ).update({Referral.status: "closed"}, synchronize_session=False)

   blocker = account_closure_block_reason(locked_user)
   if blocker:
       db.session.rollback()
       flash(blocker, "warning")
       return redirect(url_for("account_settings"))

   closed_at = utc_now_naive()
   try:
       # Preserve financial ledgers and their internal account links, while
       # removing profile data and credentials that directly identify the user.
       Payment.query.filter_by(user_id=user_id).filter(
           db.or_(Payment.status != "pending", Payment.payment_method != "mpesa")
       ).update({
           Payment.phone: "",
           Payment.response_description: None,
           Payment.result_description: None,
           Payment.raw_request: None,
           Payment.raw_callback: None,
       }, synchronize_session=False)
       # Safaricom's verified STK callback validates the phone and amount.
       # Retain only the phone for a still-pending M-PESA attempt until that
       # callback arrives; the callback scrubs it as soon as the attempt is
       # settled. All other identity fields are removed below.
       Payment.query.filter_by(
           user_id=user_id,
           status="pending",
           payment_method="mpesa",
       ).update({
           Payment.response_description: None,
           Payment.result_description: None,
           Payment.raw_request: None,
           Payment.raw_callback: None,
       }, synchronize_session=False)
       WithdrawalRequest.query.filter_by(user_id=user_id).update({
           WithdrawalRequest.phone: "",
           WithdrawalRequest.payout_details: "",
           WithdrawalRequest.admin_note: None,
       }, synchronize_session=False)
       LoanRequest.query.filter_by(user_id=user_id).update({
           LoanRequest.phone: "",
           LoanRequest.purpose: "closed",
           LoanRequest.admin_note: None,
       }, synchronize_session=False)
       TaskSubmission.query.filter_by(user_id=user_id).update({
           TaskSubmission.proof: "[removed on account closure]",
           TaskSubmission.review_note: None,
       }, synchronize_session=False)

       application_ids = [row.id for row in AgentApplication.query.filter_by(user_id=user_id).all()]
       if application_ids:
           AgentAppeal.query.filter(
               AgentAppeal.application_id.in_(application_ids)
           ).delete(synchronize_session=False)
           AgentApplication.query.filter(
               AgentApplication.id.in_(application_ids)
           ).update({
               AgentApplication.phone: "",
               AgentApplication.whatsapp_phone: None,
               AgentApplication.whatsapp_contact_consent_at: None,
               AgentApplication.location: "",
               AgentApplication.admin_note: None,
               AgentApplication.fee_waiver_note: None,
           }, synchronize_session=False)

       # Remove API credentials, authentication links, and private user content.
       api_key_ids = [key_id for (key_id,) in db.session.query(APIKey.id).filter_by(user_id=user_id).all()]
       if api_key_ids:
           Referral.query.filter(Referral.referral_code_id.in_(api_key_ids)).update(
               {Referral.referral_code_id: None}, synchronize_session=False
           )
       EmailVerification.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       ActiveSession.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       UserRole.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       AdminMFA.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       OAuthIdentity.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       APIKey.query.filter_by(user_id=user_id).delete(synchronize_session=False)
       AnalysisHistory.query.filter_by(user_id=user_id).delete(synchronize_session=False)

       # Drop this account's identity from staff/reviewer attribution columns.
       WithdrawalRequest.query.filter_by(reviewed_by=user_id).update(
           {WithdrawalRequest.reviewed_by: None}, synchronize_session=False
       )
       LoanRequest.query.filter_by(reviewed_by=user_id).update(
           {LoanRequest.reviewed_by: None}, synchronize_session=False
       )
       AgentApplication.query.filter(
           db.or_(AgentApplication.reviewed_by == user_id, AgentApplication.fee_waived_by == user_id)
       ).update({
           AgentApplication.reviewed_by: None,
           AgentApplication.fee_waived_by: None,
       }, synchronize_session=False)
       AgentAppeal.query.filter_by(reviewed_by=user_id).update(
           {AgentAppeal.reviewed_by: None}, synchronize_session=False
       )
       TaskSubmission.query.filter_by(reviewed_by=user_id).update(
           {TaskSubmission.reviewed_by: None}, synchronize_session=False
       )
       TaskOffer.query.filter_by(created_by=user_id).update(
           {TaskOffer.created_by: None}, synchronize_session=False
       )

       locked_user.full_name = f"Closed member {user_id}"
       locked_user.email = f"closed-{user_id}@deleted.invalid"
       locked_user.password_hash = social_password_hash()
       locked_user.phone_number = None
       locked_user.country_code = None
       locked_user.reset_token = None
       locked_user.deleted_at = closed_at
       db.session.commit()
   except Exception:
       db.session.rollback()
       app.logger.exception("account_closure_failed user_id=%s", user_id)
       flash("We could not close your account right now. Please try again or contact support.", "danger")
       return redirect(url_for("account_settings"))

   logout_user()
   session.clear()
   session["_remember"] = "clear"
   app.logger.info("account_closed user_id=%s", user_id)
   flash(
       "Your account has been closed. Financial and transaction records may be retained in de-identified form where needed for accounting, disputes, outstanding obligations, or legal requirements.",
       "success",
   )
   return redirect(url_for("form2", next="login"))


@app.route("/account-settings", methods=["GET", "POST"])
@login_required
@limiter.limit("5 per hour", key_func=account_password_change_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def account_settings():
   expire_stale_pending_wallet_deposits()
   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)

       action = request.form.get("action", "password").strip().lower()
       if action != "password":
           flash("Only password changes are available in account settings.", "danger")
           return redirect(url_for("account_settings"))

       current_password = request.form.get("current_password", "").strip()
       new_password = request.form.get("new_password", "").strip()

       if not current_password or not new_password:
           flash("Enter your current password and a new password.", "danger")
       elif not bcrypt.check_password_hash(current_user.password_hash, current_password):
           log_security_event("password_change_failed", current_user.email, "incorrect_current_password")
           flash("Your current password is incorrect.", "danger")
       elif bcrypt.check_password_hash(current_user.password_hash, new_password):
           flash("Choose a new password that is different from your current password.", "warning")
       elif password_policy_error(new_password):
           flash(password_policy_error(new_password), "danger")
       else:
           current_user.password_hash = bcrypt.generate_password_hash(new_password).decode("utf-8")
           db.session.commit()
           app.logger.info("security_event=password_changed user_id=%s", current_user.id)
           flash("Your password has been updated.", "success")

       return redirect(url_for("account_settings"))

   return render_template("account_settings.html")


@app.route("/country-selection", methods=["GET", "POST"])
@login_required
def country_selection():
   if current_user.country_code:
       return redirect(url_for("dashboard"))

   detected_country = (
       detected_country_code()
       if app.config.get("COUNTRY_LOCATION_ENFORCEMENT") else None
   )
   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)
       country_code = request.form.get("country_code", "").strip().upper()
       if country_code not in COUNTRY_NAME_BY_CODE:
           flash("Choose a valid country to continue.", "danger")
       else:
           detected_country, country_error = validate_country_location(country_code)
           if country_error:
               reason = (
                   f"selected={country_code},detected={detected_country}"
                   if detected_country else "location_unavailable"
               )
               log_security_event("country_selection_check_failed", reason=reason)
               flash(country_error, "danger")
           else:
               current_user.country_code = country_code
               db.session.commit()
               flash(
                   f"Country saved. Your display currency is {current_currency_code()}.",
                   "success",
               )
               return redirect(url_for("dashboard"))
   return render_template(
       "country_selection.html",
       country_location_enforcement=app.config.get("COUNTRY_LOCATION_ENFORCEMENT"),
       detected_country_code=detected_country,
       detected_country_name=COUNTRY_NAME_BY_CODE.get(detected_country, "") if detected_country else "",
   )



# ----------------- LOGOUT -----------------
@app.route("/logout", methods=["POST"])
@login_required
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def logout():
   if not valid_csrf_request():
       abort(400)
   remove_current_presence_session()
   logout_user()
   session.pop("control_center_user_id", None)
   session.pop("control_center_authenticated_at", None)
   session.pop("admin_mfa_setup_user_id", None)
   session.pop("admin_mfa_setup_started_at", None)
   session.pop("passport_invite_token", None)
   # Flash messages are page-local. Do not carry notices from the previous
   # authenticated session into the next sign-in screen.
   session.pop("_flashes", None)
   return redirect(url_for("form2"))

# ----------------- EMAIL CONFIRMATION -----------------
@app.route("/confirm-email/<token>", methods=["GET", "POST"])
@limiter.limit("5 per hour", key_func=email_verification_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def confirm_email(token):
   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)
       token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
       verification = EmailVerification.query.filter_by(token_hash=token_hash).first()
       now = utc_now_naive()
       if (
           not verification
           or verification.verified_at is not None
           or verification.expires_at <= now
       ):
           flash(
               "That confirmation link is invalid or expired. Sign in and request a fresh email confirmation link.",
               "warning",
           )
           return redirect(url_for("form2", next="login"))

       verification.verified_at = now
       verification.token_hash = None
       db.session.commit()
       flash("Email confirmed. You can now sign in.", "success")
       return redirect(url_for("form2", next="login"))

   response = make_response(render_template("email_confirmation.html"))
   response.headers["Cache-Control"] = "no-store"
   response.headers["Referrer-Policy"] = "no-referrer"
   return response


# ----------------- RESET PASSWORD -----------------
@app.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("5 per hour", key_func=password_reset_rate_key, methods=["POST"])
@limiter.limit("20 per hour", key_func=get_client_ip, methods=["POST"])
def reset(token):
   try:
       email = serializer.loads(token, salt="reset-password", max_age=900)
   except (SignatureExpired, BadSignature):
       flash("Invalid or expired token", "danger")
       return redirect(url_for("form2"))

   user = User.query.filter_by(email=email, reset_token=token).first()

   if not user:
       flash("Invalid or already-used token", "danger")
       return redirect(url_for("form2"))

   if request.method == "POST":
       if not valid_csrf_request():
           abort(400)
       password = request.form.get("password", "").strip()
       confirm_password = request.form.get("confirmPassword", "").strip()

       if not password or not confirm_password:
           flash("Both password fields are required.", "danger")
           return redirect(url_for("reset", token=token))

       if password != confirm_password:
           flash("Passwords do not match.", "danger")
           return redirect(url_for("reset", token=token))

       password_error = password_policy_error(password)
       if password_error:
           flash(password_error, "danger")
           return redirect(url_for("reset", token=token))

       hashed = bcrypt.generate_password_hash(password).decode("utf-8")

       user.password_hash = hashed
       user.reset_token = None
       mark_email_verified(user)
       db.session.commit()

       flash("Password updated successfully!", "success")
       return redirect(url_for("form2"))

   return render_template("reset.html")

# ----------------- RUN -----------------
def upgrade_local_sqlite_loan_credit_column():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("loan_requests"):
       return

   columns = {column["name"] for column in inspector.get_columns("loan_requests")}
   if "credit_awarded_at" not in columns:
       with db.engine.begin() as connection:
           connection.execute(
               sqlalchemy_text("ALTER TABLE loan_requests ADD COLUMN credit_awarded_at DATETIME")
           )
       app.logger.info("Applied local SQLite loan-credit schema update")


def upgrade_local_sqlite_loan_repayment_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("loan_requests"):
       return

   columns = {column["name"] for column in inspector.get_columns("loan_requests")}
   definitions = {
       "loan_term_months": "INTEGER",
       "terms_accepted_at": "DATETIME",
       "repayment_due_at": "DATETIME",
       "repaid_at": "DATETIME",
       "repayment_note": "VARCHAR(500)",
   }
   with db.engine.begin() as connection:
       for column_name, sql_type in definitions.items():
           if column_name not in columns:
               connection.execute(sqlalchemy_text(
                   f"ALTER TABLE loan_requests ADD COLUMN {column_name} {sql_type}"
               ))
       connection.execute(sqlalchemy_text(
           "CREATE INDEX IF NOT EXISTS idx_loan_requests_repayment_due "
           "ON loan_requests (status, repayment_due_at, repaid_at)"
       ))
   app.logger.info("Applied local SQLite loan-repayment schema update")


def upgrade_local_sqlite_agent_program_fee_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("payments") or not inspector.has_table("agent_applications"):
       return

   payment_columns = {column["name"] for column in inspector.get_columns("payments")}
   application_columns = {column["name"] for column in inspector.get_columns("agent_applications")}
   with db.engine.begin() as connection:
       if "payment_type" not in payment_columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN payment_type VARCHAR(32) NOT NULL DEFAULT 'wallet_deposit'"
           ))
       connection.execute(sqlalchemy_text(
           "CREATE INDEX IF NOT EXISTS ix_payments_payment_type ON payments (payment_type)"
       ))
       if "fee_payment_id" not in application_columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN fee_payment_id INTEGER"
           ))
       if "fee_paid_at" not in application_columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN fee_paid_at DATETIME"
           ))
       connection.execute(sqlalchemy_text(
           "UPDATE agent_applications SET fee_paid_at = created_at "
           "WHERE status = 'pending' AND fee_paid_at IS NULL"
       ))
   app.logger.info("Applied local SQLite agent-program fee schema update")


def upgrade_local_sqlite_agent_fee_waiver_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("agent_applications"):
       return

   columns = {column["name"] for column in inspector.get_columns("agent_applications")}
   with db.engine.begin() as connection:
       if "fee_waived_at" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN fee_waived_at DATETIME"
           ))
       if "fee_waived_by" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN fee_waived_by INTEGER"
           ))
       if "fee_waiver_note" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN fee_waiver_note VARCHAR(500)"
           ))
   app.logger.info("Applied local SQLite agent fee-waiver schema update")


def upgrade_local_sqlite_agent_whatsapp_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("agent_applications"):
       return

   columns = {column["name"] for column in inspector.get_columns("agent_applications")}
   with db.engine.begin() as connection:
       if "whatsapp_phone" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN whatsapp_phone VARCHAR(20)"
           ))
       if "whatsapp_contact_consent_at" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE agent_applications ADD COLUMN whatsapp_contact_consent_at DATETIME"
           ))
   app.logger.info("Applied local SQLite agent WhatsApp contact schema update")


def upgrade_local_sqlite_package_task_rewards():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("task_offers") or not inspector.has_table("task_submissions"):
       return

   offer_columns = {column["name"] for column in inspector.get_columns("task_offers")}
   submission_columns = {column["name"] for column in inspector.get_columns("task_submissions")}
   added_package_key = "package_key" not in offer_columns
   with db.engine.begin() as connection:
       if added_package_key:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE task_offers ADD COLUMN package_key VARCHAR(40) "
               "NOT NULL DEFAULT 'whatsapp-earn'"
           ))
       if "reward_amount" not in submission_columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE task_submissions ADD COLUMN reward_amount INTEGER NOT NULL DEFAULT 0"
           ))
       connection.execute(sqlalchemy_text(
           "UPDATE task_submissions SET reward_amount = ("
           "SELECT task_offers.reward_amount FROM task_offers "
           "WHERE task_offers.id = task_submissions.task_id"
           ") WHERE reward_amount = 0"
       ))
       connection.execute(sqlalchemy_text(
           "CREATE INDEX IF NOT EXISTS idx_task_submissions_task_status "
           "ON task_submissions (task_id, status)"
       ))
       if added_package_key:
           connection.execute(sqlalchemy_text(
               "UPDATE task_offers SET reward_amount = 450 WHERE package_key = 'whatsapp-earn'"
           ))
   app.logger.info("Applied local SQLite package task reward schema update")


def upgrade_local_sqlite_task_pass_daily_rewards():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("task_offers") or not inspector.has_table("task_submissions"):
       return
   offer_columns = {column["name"] for column in inspector.get_columns("task_offers")}
   submission_columns = {
       column["name"] for column in inspector.get_columns("task_submissions")
   }
   if not {"task_type", "package_key", "reward_amount"}.issubset(offer_columns):
       return
   if not {"task_id", "status", "reward_amount", "user_id", "submitted_at"}.issubset(
       submission_columns
   ):
       return

   package_rewards = {
       "whatsapp-earn": 100,
       "authorized-agent": 250,
       "investment-plan": 350,
   }
   with db.engine.begin() as connection:
       for package_key, reward_amount in package_rewards.items():
           connection.execute(
               sqlalchemy_text(
                   "UPDATE task_offers SET reward_amount = :reward "
                   "WHERE task_type != 'whatsapp_views' AND package_key = :package"
               ),
               {"reward": reward_amount, "package": package_key},
           )
           connection.execute(
               sqlalchemy_text(
                   "UPDATE task_submissions SET reward_amount = :reward "
                   "WHERE status = 'pending' AND task_id IN ("
                   "SELECT id FROM task_offers WHERE task_type != 'whatsapp_views' "
                   "AND package_key = :package)"
               ),
               {"reward": reward_amount, "package": package_key},
           )
       connection.execute(sqlalchemy_text(
           "CREATE INDEX IF NOT EXISTS idx_task_submissions_user_status_submitted "
           "ON task_submissions (user_id, status, submitted_at)"
       ))
   app.logger.info("Applied local SQLite task-pass daily reward update")


def upgrade_local_sqlite_agent_earnings_index():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("referral_rewards"):
       return
   index_names = {index["name"] for index in inspector.get_indexes("referral_rewards")}
   if "idx_referral_rewards_role_created_recipient" in index_names:
       return

   with db.engine.begin() as connection:
       connection.execute(sqlalchemy_text(
           "CREATE INDEX idx_referral_rewards_role_created_recipient "
           "ON referral_rewards (recipient_role, created_at, recipient_user_id)"
       ))
   app.logger.info("Applied local SQLite agent earnings report index")


def upgrade_local_sqlite_paypal_payment_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("payments"):
       return
   columns = {column["name"] for column in inspector.get_columns("payments")}
   with db.engine.begin() as connection:
       if "payment_method" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN payment_method VARCHAR(16) "
               "NOT NULL DEFAULT 'mpesa'"
           ))
       if "provider_reference" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN provider_reference VARCHAR(80)"
           ))
       if "provider_amount" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN provider_amount NUMERIC(12, 2)"
           ))
       if "provider_currency" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN provider_currency VARCHAR(3)"
           ))
       if "exchange_rate" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE payments ADD COLUMN exchange_rate NUMERIC(12, 4)"
           ))
       connection.execute(sqlalchemy_text(
           "CREATE INDEX IF NOT EXISTS ix_payments_payment_method ON payments (payment_method)"
       ))
       connection.execute(sqlalchemy_text(
           "CREATE UNIQUE INDEX IF NOT EXISTS ix_payments_provider_reference "
           "ON payments (provider_reference)"
       ))
   app.logger.info("Applied local SQLite PayPal payment schema update")


def upgrade_local_sqlite_country_and_task_target_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   with db.engine.begin() as connection:
       if inspector.has_table("users"):
           user_columns = {column["name"] for column in inspector.get_columns("users")}
           if "country_code" not in user_columns:
               connection.execute(sqlalchemy_text(
                   "ALTER TABLE users ADD COLUMN country_code VARCHAR(2)"
               ))
               connection.execute(sqlalchemy_text(
                   "CREATE INDEX IF NOT EXISTS ix_users_country_code ON users (country_code)"
               ))
       if inspector.has_table("task_offers"):
           task_columns = {column["name"] for column in inspector.get_columns("task_offers")}
           if "target_country" not in task_columns:
               connection.execute(sqlalchemy_text(
                   "ALTER TABLE task_offers ADD COLUMN target_country VARCHAR(3) "
                   "NOT NULL DEFAULT 'ALL'"
               ))
               connection.execute(sqlalchemy_text(
                   "CREATE INDEX IF NOT EXISTS ix_task_offers_target_country "
                   "ON task_offers (target_country)"
               ))
   app.logger.info("Applied local SQLite country and task targeting schema update")


def upgrade_local_sqlite_selected_task_package_column():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("users"):
       return
   columns = {column["name"] for column in inspector.get_columns("users")}
   if "selected_task_package_key" in columns:
       return

   with db.engine.begin() as connection:
       connection.execute(sqlalchemy_text(
           "ALTER TABLE users ADD COLUMN selected_task_package_key VARCHAR(40)"
       ))
   app.logger.info("Applied local SQLite selected-task-package schema update")


def upgrade_local_sqlite_user_phone_column():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("users"):
       return
   columns = {column["name"] for column in inspector.get_columns("users")}
   if "phone_number" in columns:
       return

   with db.engine.begin() as connection:
       connection.execute(sqlalchemy_text(
           "ALTER TABLE users ADD COLUMN phone_number VARCHAR(20)"
       ))
   app.logger.info("Applied local SQLite user phone schema update")


def upgrade_local_sqlite_whatsapp_task_columns():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   table_columns = {
       table_name: (
           {column["name"] for column in inspector.get_columns(table_name)}
           if inspector.has_table(table_name)
           else set()
       )
       for table_name in ("agent_applications", "task_offers", "task_submissions")
   }
   statements = {
       "agent_applications": {
           "fee_waiver_requested_at": "ALTER TABLE agent_applications ADD COLUMN fee_waiver_requested_at DATETIME",
       },
       "task_offers": {
           "promo_image_url": "ALTER TABLE task_offers ADD COLUMN promo_image_url VARCHAR(500)",
           "audience": (
               "ALTER TABLE task_offers ADD COLUMN audience VARCHAR(20) "
               "NOT NULL DEFAULT 'all_members'"
           ),
       },
       "task_submissions": {
           "proof_url": "ALTER TABLE task_submissions ADD COLUMN proof_url VARCHAR(500)",
           "claimed_view_count": "ALTER TABLE task_submissions ADD COLUMN claimed_view_count INTEGER",
           "verified_view_count": "ALTER TABLE task_submissions ADD COLUMN verified_view_count INTEGER",
       },
   }
   changed = False
   with db.engine.begin() as connection:
       for table_name, column_statements in statements.items():
           if not table_columns[table_name]:
               continue
           for column_name, statement in column_statements.items():
               if column_name not in table_columns[table_name]:
                   connection.execute(sqlalchemy_text(statement))
                   changed = True
       if table_columns["task_offers"]:
           connection.execute(sqlalchemy_text(
               "UPDATE task_offers SET package_key = CASE package_key "
               "WHEN 'whatsapp-earn' THEN 'whatsapp-quantum' "
               "WHEN 'authorized-agent' THEN 'whatsapp-compliance' "
               "WHEN 'investment-plan' THEN 'whatsapp-dominance' END "
               "WHERE task_type = 'whatsapp_views' AND package_key IN "
               "('whatsapp-earn', 'authorized-agent', 'investment-plan')"
           ))
   if changed:
       app.logger.info("Applied local SQLite WhatsApp task and fee-waiver schema update")


def upgrade_local_sqlite_withdrawal_payout_fields():
   if is_production or db.engine.dialect.name != "sqlite":
       return

   inspector = sqlalchemy_inspect(db.engine)
   if not inspector.has_table("withdrawal_requests"):
       return

   columns = {column["name"] for column in inspector.get_columns("withdrawal_requests")}
   with db.engine.begin() as connection:
       if "payout_method" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE withdrawal_requests ADD COLUMN payout_method VARCHAR(16) "
               "NOT NULL DEFAULT 'mpesa'"
           ))
       if "payout_details" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE withdrawal_requests ADD COLUMN payout_details VARCHAR(254) "
               "NOT NULL DEFAULT ''"
           ))
       if "payout_amount" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE withdrawal_requests ADD COLUMN payout_amount NUMERIC(12, 2) "
               "NOT NULL DEFAULT 0"
           ))
       if "payout_currency" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE withdrawal_requests ADD COLUMN payout_currency VARCHAR(3) "
               "NOT NULL DEFAULT 'KES'"
           ))
       if "exchange_rate" not in columns:
           connection.execute(sqlalchemy_text(
               "ALTER TABLE withdrawal_requests ADD COLUMN exchange_rate NUMERIC(12, 4)"
           ))
       connection.execute(sqlalchemy_text(
           "UPDATE withdrawal_requests SET payout_details = phone "
           "WHERE payout_details = ''"
       ))
       connection.execute(sqlalchemy_text(
           "UPDATE withdrawal_requests SET payout_amount = amount "
           "WHERE payout_amount = 0"
       ))
   app.logger.info("Applied local SQLite withdrawal payout schema update")


def reconcile_agent_roles():
   mismatches = (
       db.session.query(AgentApplication.user_id, AgentApplication.status, UserRole)
       .outerjoin(
           UserRole,
           db.and_(
               UserRole.user_id == AgentApplication.user_id,
               UserRole.role_name == "agent",
           ),
       )
       .filter(
           db.or_(
               db.and_(AgentApplication.status == "approved", UserRole.id.is_(None)),
               db.and_(AgentApplication.status != "approved", UserRole.id.isnot(None)),
           )
       )
       .all()
   )
   for user_id, status, role in mismatches:
       if status == "approved" and role is None:
           db.session.add(UserRole(user_id=user_id, role_name="agent"))
       elif role is not None:
           db.session.delete(role)


def initialize_database():
   with app.app_context():
       auto_create_tables = env_bool("AUTO_CREATE_TABLES", default=not is_production)
       try:
           if auto_create_tables:
               db.create_all()
               upgrade_local_sqlite_loan_credit_column()
               upgrade_local_sqlite_loan_repayment_columns()
               upgrade_local_sqlite_agent_program_fee_columns()
               upgrade_local_sqlite_agent_fee_waiver_columns()
               upgrade_local_sqlite_agent_whatsapp_columns()
               upgrade_local_sqlite_package_task_rewards()
               upgrade_local_sqlite_task_pass_daily_rewards()
               upgrade_local_sqlite_agent_earnings_index()
               upgrade_local_sqlite_paypal_payment_columns()
               upgrade_local_sqlite_country_and_task_target_columns()
               upgrade_local_sqlite_selected_task_package_column()
               upgrade_local_sqlite_user_phone_column()
               upgrade_local_sqlite_whatsapp_task_columns()
               upgrade_local_sqlite_withdrawal_payout_fields()

           reconcile_agent_roles()
           reconcile_existing_agent_deposit_commissions()
           if db.session.new or db.session.dirty or db.session.deleted:
               db.session.commit()

           configured_emails = configured_admin_emails()
           if configured_emails:
               admin_users = User.query.filter(
                   db.func.lower(User.email).in_(configured_emails)
               ).all()
               for admin_user in admin_users:
                   grant_configured_admin_role(admin_user)
               if admin_users:
                   db.session.commit()
       except Exception:
           db.session.rollback()
           if auto_create_tables:
               raise
           app.logger.exception(
               "Database initialization failed; verify all required db-init migrations are applied"
           )

# Run once when the module is imported (including by Gunicorn)
initialize_database()

if __name__ == "__main__":
   app.run(
       host="0.0.0.0",
       port=int(env_first("PORT", default="5000")),
       debug=debug_mode,
   )