"""
FastAPI-приложение со встроенным APScheduler и веб-страницей вакансий.

Запуск:
    uvicorn app:app --host 0.0.0.0 --port 8000

Эндпоинты:
    GET  /login, POST /login     — вход по почте и паролю (форма, сессия в cookie).
    POST /logout                 — выход.
    GET  /navigator, /registry   — рабочие экраны (нужна сессия).
    GET  /health                 — живость процесса, единственный открытый роут.
    GET  /health/details         — состояние планировщика и задач.
    GET  /jobs                   — расписание + время следующего запуска.
    GET  /metrics                — метрики Prometheus.
    POST /trigger/{name}         — запустить зарегистрированную задачу сейчас.
    POST /run?sources=...&reset= — запустить произвольную комбинацию.

Расписание (Europe/Moscow):
    09:30  vahtapro + aaaplus              с reset для них
    12:00  kpk + yappi + marketstaff       с reset для них
    13:00  vahtapro                        без reset
    13:30  ametist                         без reset (окно 14 дн., снимок «Обновляем потребность»)

Доступ. Людям — вход по форме, машинам — Basic.

    SECRET_KEY          ключ подписи сессии: openssl rand -hex 32
    AUTH_EMAIL          почта учётной записи
    AUTH_PASSWORD_HASH  хеш пароля: python scripts/set_password.py
    SESSION_DAYS        срок сессии, по умолчанию 7
    LOGIN_MAX_ATTEMPTS  попыток входа с одного IP, по умолчанию 5
    SESSION_COOKIE_SECURE=1  когда приложение за https

    WEB_USER, WEB_PASSWORD   Basic для машинных клиентов (Prometheus → /metrics)
    WEB_BASIC_ENABLED=0      выключить Basic, когда машины переведены
    ENABLE_API_DOCS=1        включить /docs и /openapi.json на время отладки
"""

import csv
import io
import json
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import List, Literal, Optional
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from logging_config import setup_logging

setup_logging()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import auth
import metrics as M
import users as users_mod
import navigator_api
from pipeline import (
    ALL_SOURCES,
    REGISTRY_ENABLED,
    SOURCE_NAMES,
    TARGET_SHEET_NAME,
    TARGET_SPREADSHEET_ID,
    parse_sources,
    run_pipeline,
)
from registry import (
    compat,
    db as registry_db,
    dictionaries as registry_dicts,
    queries as rq,
    rates,
)
from registry.ingest import RegistryIngestor
from registry.labels import FIELD_GROUPS, FIELD_LABELS, SOURCE_TITLES, display
from registry.models import DATA_FIELDS, RawRequest
from registry.sources import SOURCE_MANUAL
from sheets_adapter import GoogleSheetsService
from vacancies_cache import VacanciesCache

CACHE_TTL_SECONDS = 120

JOBS = {
    "morning_telegram": {
        "trigger": CronTrigger(hour=9, minute=30, timezone="Europe/Moscow"),
        "sources": ["vahtapro", "aaaplus"],
        "reset": True,
        "description": "09:30 МСК — Градус + AAA+ с reset",
    },
    "noon_tables": {
        "trigger": CronTrigger(hour=12, minute=0, timezone="Europe/Moscow"),
        "sources": ["kpk", "yappi", "marketstaff"],
        "reset": True,
        "description": "12:00 МСК — КНК + ЯППИ + Маркетстафф с reset",
    },
    "afternoon_vahtapro": {
        "trigger": CronTrigger(hour=13, minute=0, timezone="Europe/Moscow"),
        "sources": ["vahtapro"],
        "reset": False,
        "description": "13:00 МСК — Градус без reset",
    },
    "afternoon_ametist": {
        "trigger": CronTrigger(hour=13, minute=30, timezone="Europe/Moscow"),
        "sources": ["ametist"],
        "reset": False,
        "description": "13:30 МСК — Аметист без reset (окно 14 дн.)",
    },
}


# ---------- Сервисы инициализируются один раз ----------
_sheets_service = GoogleSheetsService("credentials.json")
_cache = VacanciesCache(
    sheets_service=_sheets_service,
    spreadsheet_id=TARGET_SPREADSHEET_ID,
    sheet_name=TARGET_SHEET_NAME,
    ttl_seconds=CACHE_TTL_SECONDS,
)

templates = Jinja2Templates(directory="templates")


async def _legacy_rows(active_only: bool = False) -> list:
    """Строки в прежнем формате для /vacancies и /navigator.

    Источник правды — реестр (SQLite). Прежний путь через кеш Google Sheets
    остаётся только на случай аварийного отката по REGISTRY_ENABLED=0.
    """
    if REGISTRY_ENABLED:
        import asyncio

        return await asyncio.to_thread(compat.legacy_rows, active_only)
    rows = await _cache.get()
    if active_only:
        return [r for r in rows if (r.get("is_active") or "").upper() == "TRUE"]
    return rows


# ---------- APScheduler-обвязка ----------
scheduler = AsyncIOScheduler()


async def _job_wrapper(job_name: str, sources: list, reset: bool) -> None:
    logger.info(f"[scheduler] start job={job_name}")
    try:
        await run_pipeline(sources, reset=reset)
        # После прогона данные в Sheets изменились — сбрасываем кеш
        _cache.invalidate()
        logger.info(f"[scheduler] done  job={job_name}")
    except Exception:
        logger.exception(f"[scheduler] FAILED job={job_name}")


def _register_jobs() -> None:
    for name, cfg in JOBS.items():
        scheduler.add_job(
            _job_wrapper,
            trigger=cfg["trigger"],
            args=[name, cfg["sources"], cfg["reset"]],
            id=name,
            name=cfg["description"],
            replace_existing=True,
            misfire_grace_time=600,
        )


# ---------- Проверка окружения на старте ----------
#
# Раньше отсутствие WEB_USER / WEB_PASSWORD всплывало как HTTP 500 на первом же
# запросе — то есть через часы после деплоя и в виде «сервер сломался».
# Приложение, которое не может никого впустить, не должно подниматься вообще.

REQUIRED_ENV = {
    "SECRET_KEY": "ключ подписи сессии (openssl rand -hex 32)",
    "AUTH_EMAIL": "почта учётной записи для входа",
    "AUTH_PASSWORD_HASH": "хеш пароля (python scripts/set_password.py)",
}

# Basic остаётся для машинных клиентов: им негде хранить cookie.
# Сегодня это Prometheus, который скрейпит /metrics.
# Выключается WEB_BASIC_ENABLED=0, когда людей переведут на вход по форме.
BASIC_ENABLED = os.getenv("WEB_BASIC_ENABLED", "1").strip().lower() not in ("0", "false", "no")

# Пароли, с которыми нельзя выходить в сеть: это значения из примеров и
# инструкций, а не выбранные человеком.
WEAK_PASSWORDS = {
    "change_me_please", "change-me", "changeme", "change_me",
    "password", "admin", "secret", "12345", "123456", "qwerty",
}


def check_required_env() -> None:
    """Падает на старте, если приложение заведомо не сможет работать."""
    missing = [f"{name} — {why}" for name, why in REQUIRED_ENV.items() if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Не заданы обязательные переменные окружения:\n  "
            + "\n  ".join(missing)
            + "\nЗаполните .env (см. .env.example) и перезапустите."
        )
    password = os.getenv("WEB_PASSWORD", "")
    if password.strip().lower() in WEAK_PASSWORDS:
        if BASIC_ENABLED:
            raise RuntimeError(
                "WEB_PASSWORD — значение из примера, а Basic включён. Этой парой "
                "закрыты /metrics, /jobs и /health/details, и она доступна из сети.\n"
                "Либо смените WEB_PASSWORD, либо выключите Basic: WEB_BASIC_ENABLED=0"
            )
        logger.warning("[auth] WEB_PASSWORD — значение из примера, но Basic выключен")
    if BASIC_ENABLED and password:
        logger.warning(
            "[auth] Basic-доступ по WEB_USER/WEB_PASSWORD ещё включён. "
            "Он нужен Prometheus для /metrics; для людей есть вход по форме. "
            "Когда машинные клиенты переведены — WEB_BASIC_ENABLED=0."
        )
    secret = os.getenv("SECRET_KEY", "").strip()
    if len(secret) < 32:
        raise RuntimeError(
            f"SECRET_KEY короче 32 символов (сейчас {len(secret)}). Этим ключом "
            "подписывается сессия: слабый ключ подбирается, и вход обходится "
            "целиком. Сгенерируйте новый: openssl rand -hex 32"
        )
    if secret.lower() in WEAK_PASSWORDS or len(set(secret)) < 8:
        raise RuntimeError(
            "SECRET_KEY слишком однообразный или взят из примера. "
            "Сгенерируйте: openssl rand -hex 32"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_required_env()
    _register_jobs()
    scheduler.start()
    M.APP_INFO.info({"version": "1.0", "jobs": ",".join(JOBS.keys())})
    logger.info("APScheduler стартовал. Задачи:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.id}: next run = {job.next_run_time}")
    yield
    scheduler.shutdown()
    logger.info("APScheduler остановлен.")


# Схема API наружу не публикуется: она описывает все роуты и тела запросов.
# Включается на время отладки переменной ENABLE_API_DOCS=1.
_API_DOCS = os.getenv("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="Eltrea Bot",
    lifespan=lifespan,
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)

# Статика для веб-фронта (логотипы Eltera и пр., используется /navigator).
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Вход ----------
#
# Два способа попасть внутрь:
#   * сессия — подписанная cookie, её ставит форма входа. Для людей.
#   * Basic — для машинных клиентов, которым негде хранить cookie.
#     Сегодня это Prometheus со скрейпом /metrics.
#
# Раньше был только Basic, одна пара на всех, пароль в окружении открытым
# текстом. Он остаётся включённым до тех пор, пока машинные клиенты не
# переведены, и гасится WEB_BASIC_ENABLED=0.

security = HTTPBasic(auto_error=False)

# Лимит на адрес. Раньше LOGIN_MAX_ATTEMPTS объявлялся, читался в конфиг —
# и не доходил сюда: всегда действовала зашитая пятёрка.
_throttle = auth.LoginThrottle(max_attempts=auth.load_config().max_attempts)

# Предохранитель от перебора с разных адресов. Лимит на IP от ботнета не спасает:
# каждый запрос приходит с нового адреса и упирается в свежий счётчик.
# Порог намеренно щедрый — офис за одним внешним адресом не должен его задевать.
_global_throttle = auth.LoginThrottle(
    max_attempts=max(50, auth.load_config().max_attempts * 20),
    window_sec=900,
    block_sec=300,
)
GLOBAL_KEY = "*"


# X-Forwarded-For клиент подставляет сам. Пока приложение слушает напрямую,
# доверять заголовку нельзя: любой перебирающий пароль просто меняет его на
# каждом запросе и лимит попыток перестаёт существовать. Включается только
# когда впереди действительно стоит прокси, который этот заголовок переписывает.
TRUST_PROXY = os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes")


def _client_ip(request: Request) -> str:
    """IP клиента. Заголовку прокси верим, только если это разрешено явно."""
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _wants_html(request: Request) -> bool:
    """Браузеру показываем форму входа, программе отвечаем 401."""
    if request.url.path.startswith("/api/") or request.url.path in ("/metrics", "/jobs"):
        return False
    return "text/html" in request.headers.get("accept", "")


# Роуты, куда пускаем по Basic. Это ровно то, что забирают программы:
# Prometheus скрейпит /metrics, мониторинг смотрит /health/details и /jobs.
# Раньше Basic открывал ВСЁ приложение — то есть слабый общий пароль давал
# доступ к реестру и правилам ставок в обход формы входа и лимита попыток.
BASIC_PATHS = {"/metrics", "/jobs", "/health/details"}


def _basic_ok(request: Request, creds: Optional[HTTPBasicCredentials]) -> bool:
    if not BASIC_ENABLED or creds is None:
        return False
    if request.url.path not in BASIC_PATHS:
        return False
    expected_user = os.getenv("WEB_USER", "")
    expected_pwd = os.getenv("WEB_PASSWORD", "")
    if not expected_user or not expected_pwd:
        return False
    return (
        secrets.compare_digest(creds.username, expected_user)
        and secrets.compare_digest(creds.password, expected_pwd)
    )


def current_user(request: Request) -> Optional["users_mod.User"]:
    """Сотрудник из сессии, если она действительна.

    Пользователь читается из базы на каждом запросе, а не берётся из cookie:
    отключение сотрудника и смена роли должны действовать немедленно, а не
    через неделю, когда истечёт его сессия.
    """
    session = auth.read_session(request.cookies.get(auth.COOKIE_NAME, ""))
    if not session:
        return None
    email = str(session.get("email") or "")
    if not email:
        return None
    with registry_db.connect() as conn:
        person = users_mod.get_by_email(conn, email)
    if person is None or not person.is_active:
        return None
    # Отпечаток пароля: смена пароля гасит все ранее выданные cookie, иначе
    # украденная сессия переживает смену пароля и живёт весь свой срок.
    if session.get("epoch") != auth.password_epoch(person.password_hash):
        return None
    return person


def verify_creds(
        request: Request,
        creds: Optional[HTTPBasicCredentials] = Depends(security),
) -> str:
    """Кто это. Сессия или Basic; иначе — на форму входа либо 401."""
    person = current_user(request)
    if person is not None:
        if person.must_change_password and not request.url.path.startswith("/password"):
            # Пока временный пароль не сменён, дальше не пускаем: иначе человек
            # так и работает под паролем, который знает администратор.
            raise HTTPException(
                status_code=303,
                detail="Требуется смена пароля",
                headers={"Location": "/password"},
            )
        request.state.user = person
        return person.email

    if _basic_ok(request, creds):
        return creds.username

    if creds is not None and BASIC_ENABLED and request.url.path in BASIC_PATHS:
        # Перебор по Basic раньше не ограничивался и не оставлял следов
        # в журнале: подбор пароля к /metrics был полностью невидим.
        ip = _client_ip(request)
        if _throttle.blocked_for(ip):
            raise HTTPException(429, "Слишком много попыток")
        _throttle.register_failure(ip)
        _global_throttle.register_failure(GLOBAL_KEY)
        auth.log_login(ok=False, email=creds.username, ip=ip, reason="basic: неверная пара")

    if _wants_html(request):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=303,
            detail="Требуется вход",
            headers={"Location": f"/login?next={quote(target, safe='')}"},
        )

    headers = {"WWW-Authenticate": "Basic"} if BASIC_ENABLED else {}
    raise HTTPException(status_code=401, detail="Требуется вход", headers=headers)


def require_admin(request: Request, user: str = Depends(verify_creds)) -> "users_mod.User":
    """Роут только для администратора.

    Проверка на сервере, а не скрытие кнопки в вёрстке: рекрутеру закрыты
    справочники, ручной ввод, запуск прогонов и правила ставок
    (docs/OPEN-QUESTIONS.md, C1a).
    """
    person = getattr(request.state, "user", None)
    if person is None:
        # Вошли по Basic — это машинный клиент, ему в админские роуты нельзя.
        raise HTTPException(403, "Доступ только для администратора")
    if not person.is_admin:
        logger.warning(f"[auth] {person.email} без роли администратора пытался открыть {request.url.path}")
        raise HTTPException(403, "Доступ только для администратора")
    return person


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    """Форма входа. Уже вошедшего сразу отправляем дальше."""
    if auth.read_session(request.cookies.get(auth.COOKIE_NAME, "")):
        return RedirectResponse(_safe_next(next), status_code=303)
    response = templates.TemplateResponse(
        "login.html",
        {"request": request, "next_url": _safe_next(next), "email": "", "error": "", "blocked": False},
    )
    # Форма входа не должна оседать в кеше браузера и промежуточных прокси.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        next: str = Form("/"),
):
    import asyncio

    config = auth.load_config()
    ip = _client_ip(request)
    target = _safe_next(next)

    # Форму входа можно отправить с чужой страницы: сам вход она не даст,
    # но пятью запросами выбьет офисный адрес в блокировку. Сверяем источник.
    if not _same_origin(request):
        auth.log_login(ok=False, email=email, ip=ip, reason="запрос с чужого источника")
        raise HTTPException(403, "Запрос пришёл с чужой страницы")

    def _fail(message: str, *, blocked: bool = False, status: int = 401):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next_url": target, "email": email, "error": message, "blocked": blocked},
            status_code=status,
        )

    wait = _throttle.blocked_for(ip) or _global_throttle.blocked_for(GLOBAL_KEY)
    if wait:
        auth.log_login(ok=False, email=email, ip=ip, reason="превышен лимит попыток")
        return _fail(
            f"Слишком много попыток. Попробуйте через {wait // 60 + 1} мин.",
            blocked=True, status=429,
        )

    # PBKDF2 на 600 000 итераций — это ~0,3 с процессорного времени. В event
    # loop такая проверка блокирует ВСЁ приложение: десяток запросов на /login
    # от неавторизованного клиента кладёт и реестр, и прогоны.
    def _check():
        with registry_db.connect() as conn:
            users_mod.bootstrap_from_env(conn)
            person, why = users_mod.authenticate(conn, email, password)
            users_mod.record_login(
                conn, ok=person is not None, email=email, ip=ip, reason=why,
                user_agent=request.headers.get("user-agent", ""),
            )
            if person is not None:
                users_mod.touch_login(conn, person.user_id)
            return person, why

    person, reason = await asyncio.to_thread(_check)
    ok = person is not None
    if not ok:
        blocked_for = _throttle.register_failure(ip)
        _global_throttle.register_failure(GLOBAL_KEY)
        auth.log_login(ok=False, email=email, ip=ip, reason=reason)
        if blocked_for:
            return _fail(
                f"Слишком много попыток. Попробуйте через {blocked_for // 60} мин.",
                blocked=True, status=429,
            )
        left = _throttle.attempts_left(ip)
        tail = f" Осталось попыток: {left}." if left <= 2 else ""
        # Не уточняем, что именно неверно: иначе форма подсказывает,
        # существует ли такая почта.
        return _fail(f"Неверная почта или пароль.{tail}")

    _throttle.register_success(ip)
    auth.log_login(ok=True, email=person.email, ip=ip)
    # Временный пароль сменить обязательно, поэтому ведём не на запрошенную
    # страницу, а на смену пароля: verify_creds всё равно туда развернёт.
    if person.must_change_password:
        target = "/password"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(
            person.email,
            days=config.session_days,
            epoch=auth.password_epoch(person.password_hash),
        ),
        max_age=config.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=config.secure_cookie,
        path="/",
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    """Выход: удаляем cookie у браузера.

    Сессия не хранится на сервере, поэтому «выход» здесь — это указание
    браузеру забыть cookie. Уже украденная копия останется действительной до
    истечения срока; чтобы погасить её немедленно, надо сменить пароль —
    отпечаток в сессии перестанет совпадать (см. auth.password_epoch).
    Серверное хранилище сессий заведено отдельной задачей.
    """
    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


def _same_origin(request: Request) -> bool:
    """Пришёл ли POST со страницы этого же приложения.

    Полноценных CSRF-токенов в проекте пока нет (заведено отдельной задачей).
    Сверка Origin закрывает практическую часть: чужая страница не сможет ни
    выбить адрес в блокировку через /login, ни разлогинить через /logout.
    Запрос без Origin и без Referer пропускаем: так ходят curl и мониторинг,
    а браузер эти заголовки на POST-форме шлёт всегда.
    """
    origin = request.headers.get("origin", "")
    if origin:
        return origin.rstrip("/") == str(request.base_url).rstrip("/")
    referer = request.headers.get("referer", "")
    if referer:
        return referer.startswith(str(request.base_url))
    return True


def _safe_next(value: str) -> str:
    """Только внутренние адреса: иначе форма входа станет открытым редиректом.

    Отбрасываем не только «//evil.com», но и «/\\evil.com»: часть браузеров
    трактует обратный слэш как прямой, и такой адрес уводит на чужой домен.
    Управляющие символы режем, чтобы нельзя было подклеить заголовок к ответу.
    """
    value = (value or "/").strip()
    if not value.startswith("/"):
        return "/"
    if value.startswith("//") or value.startswith("/\\"):
        return "/"
    if any(ch in value for ch in ("\r", "\n", "\t", "\x00")):
        return "/"
    return value


# ---------- Эндпоинты ----------
@app.get("/health")
async def health() -> dict:
    """Живость процесса. Открыт без авторизации — его дёргает healthcheck Docker.

    Состав ответа намеренно минимальный: прежняя версия отдавала наружу список
    фоновых задач, то есть состав источников и расписание прогонов.
    Подробности — на /health/details, под авторизацией.
    """
    return {"status": "ok"}


@app.get("/health/details")
async def health_details(user: str = Depends(verify_creds)) -> dict:
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "jobs": [j.id for j in scheduler.get_jobs()],
    }


def _refresh_snapshot_metrics(all_rows: list) -> None:
    """Пересчитывает gauges из текущего состояния кеша."""
    from collections import defaultdict

    M.SNAP_VACANCIES.clear()
    M.SNAP_NEED_TOTAL.clear()
    M.SNAP_AVG_RATE.clear()
    M.SNAP_SHIFT_TYPE.clear()
    M.SNAP_MIN_SHIFTS.clear()

    by_source_active = defaultdict(int)
    need_by_source = defaultdict(int)
    rate_sum_by_source = defaultdict(float)
    rate_count_by_source = defaultdict(int)
    by_shift_type = defaultdict(int)
    by_min_shifts = defaultdict(int)

    for v in all_rows:
        src = (v.get("source") or "—").strip() or "—"
        active = "TRUE" if (v.get("is_active") or "").upper() == "TRUE" else "FALSE"
        by_source_active[(src, active)] += 1

        try:
            need_by_source[src] += int(float((v.get("need_total") or "0") or 0))
        except (ValueError, TypeError):
            pass

        rate_raw = (v.get("shift_rate") or "").strip()
        if rate_raw:
            try:
                rate_sum_by_source[src] += float(rate_raw)
                rate_count_by_source[src] += 1
            except (ValueError, TypeError):
                pass

        st = (v.get("shift_type") or "").strip() or "—"
        by_shift_type[st] += 1

        ms = (v.get("min_shifts") or "").strip()
        if ms in ("15", "20", "30", "45"):
            by_min_shifts[ms] += 1
        elif ms:
            by_min_shifts["other"] += 1
        else:
            by_min_shifts["—"] += 1

    for (src, active), cnt in by_source_active.items():
        M.SNAP_VACANCIES.labels(source=src, is_active=active).set(cnt)
    for src, total in need_by_source.items():
        M.SNAP_NEED_TOTAL.labels(source=src).set(total)
    for src in rate_count_by_source:
        avg = rate_sum_by_source[src] / rate_count_by_source[src]
        M.SNAP_AVG_RATE.labels(source=src).set(round(avg, 2))
    for st, cnt in by_shift_type.items():
        M.SNAP_SHIFT_TYPE.labels(shift_type=st).set(cnt)
    for ms, cnt in by_min_shifts.items():
        M.SNAP_MIN_SHIFTS.labels(min_shifts=ms).set(cnt)


@app.get("/metrics")
async def metrics_endpoint(user: str = Depends(verify_creds)) -> Response:
    """Prometheus scrape endpoint. Перед отдачей обновляем snapshot-gauges."""
    try:
        all_rows = await _legacy_rows()
        _refresh_snapshot_metrics(all_rows)
    except Exception:
        logger.exception("[metrics] не удалось обновить snapshot")
    if REGISTRY_ENABLED:
        try:
            _refresh_registry_metrics()
        except Exception:
            logger.exception("[metrics] не удалось обновить метрики реестра")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _refresh_registry_metrics() -> None:
    """Gauges реестра: наполненность, очередь справочников, объём."""
    with registry_db.connect() as conn:
        M.REG_POSITIONS.clear()
        for row in rq.positions_by_source(conn):
            M.REG_POSITIONS.labels(
                source=row["source"],
                is_active="TRUE" if row["is_active"] else "FALSE",
            ).set(row["cnt"])

        M.REG_EMPTY_FIELDS.clear()
        for field_name, ratio in rq.fill_ratio(conn).items():
            M.REG_EMPTY_FIELDS.labels(field=field_name).set(ratio)

        M.REG_DICT_PENDING.clear()
        for kind, count in registry_dicts.pending_counts(conn).items():
            M.REG_DICT_PENDING.labels(kind=kind).set(count)


@app.get("/jobs")
async def list_jobs(user: str = Depends(verify_creds)) -> dict:
    return {
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            }
            for job in scheduler.get_jobs()
        ]
    }


@app.post("/trigger/{name}")
async def trigger_job(
        name: str,
        background: BackgroundTasks,
        admin: "users_mod.User" = Depends(require_admin),
) -> dict:
    if name not in JOBS:
        raise HTTPException(404, f"Неизвестная задача {name!r}. Доступны: {list(JOBS.keys())}")
    cfg = JOBS[name]
    background.add_task(_job_wrapper, name, cfg["sources"], cfg["reset"])
    return {"status": "started", "job": name, "sources": cfg["sources"], "reset": cfg["reset"]}


@app.post("/run")
async def run_custom(
        background: BackgroundTasks,
        sources: Optional[str] = None,
        reset: bool = False,
        admin: "users_mod.User" = Depends(require_admin),
) -> dict:
    try:
        parsed = parse_sources(sources)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background.add_task(_job_wrapper, f"manual({','.join(parsed)})", parsed, reset)
    return {"status": "started", "sources": parsed, "reset": reset}


# ---------- Смена пароля ----------

MIN_PASSWORD_LENGTH = 12


@app.get("/password", response_class=HTMLResponse)
async def password_form(request: Request, user: str = Depends(verify_creds)):
    person = getattr(request.state, "user", None)
    response = templates.TemplateResponse(
        "password_change.html",
        {"request": request, "email": user, "error": "",
         "min_length": MIN_PASSWORD_LENGTH,
         "forced": bool(person and person.must_change_password)},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/password", response_class=HTMLResponse)
async def password_submit(
        request: Request,
        password: str = Form(""),
        password2: str = Form(""),
        user: str = Depends(verify_creds),
):
    import asyncio

    person = getattr(request.state, "user", None)
    if person is None:
        raise HTTPException(403, "Смена пароля недоступна машинным клиентам")
    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")

    def _fail(message: str):
        return templates.TemplateResponse(
            "password_change.html",
            {"request": request, "email": user, "error": message,
             "min_length": MIN_PASSWORD_LENGTH, "forced": person.must_change_password},
            status_code=400,
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        return _fail(f"Пароль короче {MIN_PASSWORD_LENGTH} символов")
    if password != password2:
        return _fail("Пароли не совпали")
    if auth.verify_password(password, person.password_hash):
        return _fail("Это прежний пароль. Задайте другой")

    def _save():
        with registry_db.connect() as conn:
            users_mod.set_password(conn, person.user_id, password)
            return users_mod.get(conn, person.user_id)

    updated = await asyncio.to_thread(_save)
    # Пароль сменился — прежние сессии обесценились, включая нашу.
    # Выдаём новую сразу, чтобы человек не входил повторно.
    config = auth.load_config()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.issue_session(updated.email, days=config.session_days,
                           epoch=auth.password_epoch(updated.password_hash)),
        max_age=config.session_days * 86400,
        httponly=True, samesite="lax", secure=config.secure_cookie, path="/",
    )
    return response


# ---------- Доступы ----------

def _temp_password() -> str:
    """Временный пароль, который не стыдно продиктовать вслух."""
    return secrets.token_urlsafe(12)


# ---------- Доступы: данные для вкладки «Доступы» в админке ----------
#
# Отдельная ручка, а не часть /api/navigator: список сотрудников и журнал
# входов не должны уезжать рекрутеру вообще, даже если он их не увидит
# на экране. Кто не администратор — тот этих данных не получает.

def _people_payload(conn) -> dict:
    def one(person) -> dict:
        return {
            "id": person.user_id,
            "email": person.email,
            "name": person.name,
            "role": person.roles[0] if person.roles else "",
            "active": person.is_active,
            "temp": person.must_change_password,
            "expires": person.password_expires_at,
            "lastLogin": person.last_login_at,
            "createdAt": person.created_at,
            "createdBy": person.created_by,
            "disabledAt": person.disabled_at,
        }

    return {
        "people": [one(p) for p in users_mod.list_users(conn)],
        "journal": [
            {"at": r["at"], "email": r["email"], "ip": r["ip"],
             "ok": bool(r["ok"]), "reason": r["reason"]}
            for r in users_mod.recent_logins(conn, limit=50)
        ],
        "roles": [
            {"key": users_mod.ROLE_RECRUITER, "title": "Рекрутер"},
            {"key": users_mod.ROLE_ADMIN, "title": "Администратор"},
        ],
        "ttlHours": users_mod.TEMP_PASSWORD_TTL_HOURS,
    }


@app.get("/api/users")
async def api_users(admin: "users_mod.User" = Depends(require_admin)) -> JSONResponse:
    import asyncio

    def _work():
        with registry_db.connect() as conn:
            payload = _people_payload(conn)
            payload["me"] = admin.user_id
            return payload

    return JSONResponse(await asyncio.to_thread(_work))


def _temp_password() -> str:
    """Временный пароль, который не стыдно продиктовать вслух."""
    return secrets.token_urlsafe(12)


@app.post("/api/users")
async def api_users_create(
        request: Request,
        payload: dict,
        admin: "users_mod.User" = Depends(require_admin),
) -> JSONResponse:
    import asyncio

    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")
    password = _temp_password()

    def _work():
        with registry_db.connect() as conn:
            person = users_mod.create_user(
                conn,
                email=str(payload.get("email") or ""),
                name=str(payload.get("name") or ""),
                role=str(payload.get("role") or users_mod.ROLE_RECRUITER),
                temp_password=password,
                created_by=admin.email,
            )
            data = _people_payload(conn)
            data["me"] = admin.user_id
            # Пароль отдаётся ровно один раз, в ответ на создание. Больше он
            # нигде не появится: в базе хеш, в журнале его нет.
            data["issued"] = {"email": person.email, "password": password,
                              "expires": person.password_expires_at}
            return data

    try:
        return JSONResponse(await asyncio.to_thread(_work))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/users/{user_id}/reset")
async def api_users_reset(
        request: Request, user_id: str,
        admin: "users_mod.User" = Depends(require_admin),
) -> JSONResponse:
    import asyncio

    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")
    password = _temp_password()

    def _work():
        with registry_db.connect() as conn:
            if users_mod.get(conn, user_id) is None:
                return None
            users_mod.issue_temp_password(conn, user_id, temp_password=password, by=admin.email)
            person = users_mod.get(conn, user_id)
            data = _people_payload(conn)
            data["me"] = admin.user_id
            data["issued"] = {"email": person.email, "password": password,
                              "expires": person.password_expires_at}
            return data

    data = await asyncio.to_thread(_work)
    if data is None:
        raise HTTPException(404, f"Сотрудник {user_id} не найден")
    return JSONResponse(data)


@app.post("/api/users/{user_id}/toggle")
async def api_users_toggle(
        request: Request, user_id: str,
        admin: "users_mod.User" = Depends(require_admin),
) -> JSONResponse:
    import asyncio

    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")

    def _work():
        with registry_db.connect() as conn:
            person = users_mod.get(conn, user_id)
            if person is None:
                return "Сотрудник не найден", None
            if person.user_id == admin.user_id:
                return "Нельзя отключить самого себя", None
            if person.is_active and person.is_admin and users_mod.count_admins(conn) <= 1:
                # Иначе система останется без администратора, а вкладка
                # доступов закрыта этой же ролью — завести нового некому.
                return "Это последний администратор — сначала назначьте другого", None
            users_mod.set_active(conn, user_id, not person.is_active, by=admin.email)
            data = _people_payload(conn)
            data["me"] = admin.user_id
            return "", data

    problem, data = await asyncio.to_thread(_work)
    if problem:
        raise HTTPException(400, problem)
    return JSONResponse(data)


@app.post("/api/users/{user_id}/role")
async def api_users_role(
        request: Request, user_id: str, payload: dict,
        admin: "users_mod.User" = Depends(require_admin),
) -> JSONResponse:
    import asyncio

    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")
    role = str(payload.get("role") or "")

    def _work():
        with registry_db.connect() as conn:
            person = users_mod.get(conn, user_id)
            if person is None:
                return "Сотрудник не найден", None
            if (person.is_admin and role != users_mod.ROLE_ADMIN
                    and users_mod.count_admins(conn) <= 1):
                return "Это последний администратор — сначала назначьте другого", None
            try:
                users_mod.set_role(conn, user_id, role, by=admin.email)
            except ValueError as exc:
                return str(exc), None
            data = _people_payload(conn)
            data["me"] = admin.user_id
            return "", data

    problem, data = await asyncio.to_thread(_work)
    if problem:
        raise HTTPException(400, problem)
    return JSONResponse(data)


@app.get("/", response_class=HTMLResponse)
async def root_redirect(user: str = Depends(verify_creds)):
    """Стартовая страница — реестр заявок: тот самый «один раздел»."""
    target = "/registry" if REGISTRY_ENABLED else "/vacancies"
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0; url={target}">',
        status_code=200,
    )


@app.get("/navigator", response_class=HTMLResponse)
async def navigator(user: str = Depends(verify_creds)):
    """Навигатор: подбор позиций и админ-очередь (тёмная тема Eltera).

    Это статичный HTML+JS: данные он подтягивает с /api/navigator и фильтрует
    на клиенте. Отдаём содержимое файла напрямую (без Jinja-рендеринга), чтобы
    фигурные скобки в JS/CSS гарантированно не интерпретировались как шаблон.
    """
    with open("templates/navigator.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/api/navigator")
async def api_navigator(
        request: Request,
        active_only: bool = True,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """Данные для экрана «Навигатор» в модели интерфейса.

    Отдельно от /api/vacancies: тот отдаёт плоские строки старого формата, а
    здесь позиция собрана блоками (проживание, питание, проезд, удержания) и
    рядом лежат исходники заявок, города с координатами, источники и слой
    мотивации рекрутера. Перевод из реестра — в navigator_api.
    """
    import asyncio

    def _build() -> dict:
        with registry_db.connect() as conn:
            payload = navigator_api.build_payload(conn, active_only=active_only)
            # Кто смотрит и что ему можно. Раньше экран решал это сам: пароль
            # администратора лежал в клиентском коде, а признак входа — в
            # localStorage. Любой вошедший открывал админку через консоль.
            # Теперь роль приходит с сервера и там же проверяется на роутах.
            person = getattr(request.state, "user", None)
            payload["me"] = {
                "email": person.email if person else user,
                "name": person.title if person else user,
                "role": (person.roles[0] if person and person.roles else ""),
                "isAdmin": bool(person and person.is_admin),
            }
            return payload

    payload = await asyncio.to_thread(_build)
    return JSONResponse(payload)


class RateTierIn(BaseModel):
    """Ступень лестницы «от N смен»."""

    model_config = ConfigDict(populate_by_name=True)

    min_shifts: int = Field(0, alias="minShifts", ge=0, le=999)
    amount: int = Field(0, ge=0, le=1_000_000)


class RatesPayload(BaseModel):
    """Тело POST /api/rates.

    Раньше принимался сырой dict, и нечисловая сумма роняла обработчик с 500
    вместо понятного 400: int('') падает уже внутри сборки правил.
    Имена полей — как их шлёт форма админки (camelCase), поэтому alias.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source: str
    strategy: Literal["all", "shifts", "clients"] = "all"
    amount: int = Field(0, ge=0, le=1_000_000)
    tiers: List[RateTierIn] = Field(default_factory=list)
    clients: List[str] = Field(default_factory=list)
    vacancy: str = ""
    note: str = ""
    payout: str = ""
    valid_from: str = Field("", alias="validFrom")
    valid_to: str = Field("", alias="validTo")
    dry_run: bool = Field(False, alias="dryRun")


@app.post("/api/rates")
async def api_rates_save(payload: RatesPayload, admin: "users_mod.User" = Depends(require_admin)) -> JSONResponse:
    """Выставление ставок рекрутёра из админки «Навигатора».

    Три стратегии — это три области действия одного и того же правила:

        all      одна ставка на всего контрагента
        shifts   лестница «от N смен» на всего контрагента
        clients  выбранные объекты (и, если задана, конкретная должность)

    `dryRun` считает, скольких позиций это коснётся, и ничего не пишет: форма
    показывает это до сохранения, чтобы руководитель не выставлял ставку
    вслепую.
    """
    import asyncio

    source = payload.source.strip()
    if source not in ALL_SOURCES:
        raise HTTPException(400, f"Неизвестный контрагент: {source or '—'}")
    strategy = payload.strategy

    def _work() -> dict:
        with registry_db.connect() as conn:
            rules = _rate_rules_from_payload(source, strategy, payload)
            if not rules:
                raise HTTPException(400, "Нечего сохранять: не заданы суммы")
            affected = _positions_affected(conn, rules)
            if payload.dry_run:
                return {"ok": True, "dryRun": True, "rules": len(rules), "positions": affected}
            # Область переписывается целиком: в новой сетке контрагента может
            # не быть прежних ступеней, и они иначе остались бы висеть рядом.
            for scope in {(rule.source, rule.client, rule.vacancy) for rule in rules}:
                rates.clear_scope(conn, scope[0], scope[1], scope[2], author=admin.email)
            saved = rates.save_rules(conn, rules, author=admin.email)
            return {"ok": True, "rules": saved, "positions": affected}

    return JSONResponse(await asyncio.to_thread(_work))


@app.delete("/api/rates/{rule_id}")
async def api_rates_delete(rule_id: int, admin: "users_mod.User" = Depends(require_admin)) -> JSONResponse:
    import asyncio

    def _work() -> dict:
        with registry_db.connect() as conn:
            return {"ok": rates.delete_rule(conn, rule_id, author=admin.email)}

    result = await asyncio.to_thread(_work)
    if not result["ok"]:
        raise HTTPException(404, f"Правило {rule_id} не найдено")
    return JSONResponse(result)


def _rate_rules_from_payload(source: str, strategy: str, payload: "RatesPayload") -> List[rates.RateRule]:
    """Форма админки → список правил. Ничего не додумывает: нет суммы — нет правила."""
    common = {
        "note": payload.note.strip(),
        "payout": payload.payout.strip(),
        "valid_from": payload.valid_from.strip(),
        "valid_to": payload.valid_to.strip(),
    }
    vacancy = payload.vacancy.strip()
    tiers = [(tier.min_shifts, tier.amount) for tier in payload.tiers if tier.amount]
    amount = payload.amount

    if strategy == "all":
        if amount <= 0:
            return []
        return [rates.RateRule(source=source, amount=amount, **common)]

    if strategy == "shifts":
        return [
            rates.RateRule(source=source, min_shifts=shifts, amount=value, **common)
            for shifts, value in tiers if value > 0 and shifts > 0
        ]

    clients = [name.strip() for name in payload.clients if name.strip()]
    if not clients:
        raise HTTPException(400, "Не выбран ни один объект")
    out: List[rates.RateRule] = []
    for client in clients:
        if tiers:
            out += [
                rates.RateRule(source=source, client=client, vacancy=vacancy,
                               min_shifts=shifts, amount=value, **common)
                for shifts, value in tiers if value > 0 and shifts > 0
            ]
        elif amount > 0:
            out.append(rates.RateRule(source=source, client=client, vacancy=vacancy,
                                      amount=amount, **common))
    return out


def _positions_affected(conn, rules: List[rates.RateRule]) -> int:
    """Сколько активных позиций попадёт под эти правила.

    Считаем тем же подбором, что и на витрине: правило по объекту может
    перебить правило по контрагенту, и «затронуто 56» вместо реальных 4 —
    это ровно та цифра, ради которой предпросмотр и делается.
    """
    scopes = {(rule.client, rule.vacancy) for rule in rules}
    rows = conn.execute(
        "SELECT counterparty, object_name, vacancy_name FROM positions "
        "WHERE source = ? AND is_active = 1", (rules[0].source,),
    ).fetchall()
    count = 0
    for row in rows:
        client = rates.client_key(row["counterparty"] or "", row["object_name"] or "")
        vacancy = (row["vacancy_name"] or "").strip()
        if any(
            (not scope_client or scope_client == client)
            and (not scope_vacancy or scope_vacancy == vacancy)
            for scope_client, scope_vacancy in scopes
        ):
            count += 1
    return count


@app.get("/api/vacancies")
async def api_vacancies(
        active_only: bool = True,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """JSON-фид вакансий для фронта /navigator.

    Отдаёт строки прямо из общего кеша (ключи = заголовки целевой таблицы),
    т.е. фронт получает те же нормализованные поля, что и страница /vacancies,
    без публичного CSV-доступа к Google Sheets.
    """
    rows = await _legacy_rows(active_only=active_only)
    if REGISTRY_ENABLED:
        with registry_db.connect() as conn:
            raw_updated = rq.overview(conn)["updated_at"]
        updated_at = raw_updated[:16].replace("T", " ") if raw_updated else ""
    else:
        updated_at = (
            time.strftime("%d.%m.%Y %H:%M", time.localtime(_cache._timestamp))
            if _cache._timestamp else ""
        )
    return JSONResponse({"rows": rows, "count": len(rows), "updated_at": updated_at})


# ---------- /registry: единый реестр заявок ----------
#
# Порядок объявления маршрутов важен: /registry/{request_id} — «жадный»
# шаблон, и если объявить его раньше, он перехватит /registry/manual и
# /registry/dictionaries.

REGISTRY_SHIFTS_OPTIONS = [15, 20, 30, 45]


def _registry_filters(params: dict) -> dict:
    """Собирает фильтры из query-параметров, отбрасывая пустые."""
    def text(name: str) -> Optional[str]:
        value = (params.get(name) or "").strip()
        return value or None

    def number(name: str) -> Optional[int]:
        try:
            return int(params.get(name))
        except (TypeError, ValueError):
            return None

    is_active = params.get("is_active") or "true"
    if is_active not in ("true", "false", "all"):
        is_active = "true"

    return {
        "q": text("q"),
        "source": text("source"),
        "counterparty": text("counterparty"),
        "city": text("city"),
        "region": text("region"),
        "vacancy_name": text("vacancy_name"),
        "shift_type": text("shift_type"),
        "sb_policy": text("sb_policy"),
        "status": text("status"),
        "rate_min": number("rate_min"),
        "rate_max": number("rate_max"),
        "max_shifts": number("max_shifts"),
        "age": number("age"),
        "date_from": text("date_from"),
        "date_to": text("date_to"),
        "has_gaps": bool(params.get("has_gaps")),
        "needs_review": bool(params.get("needs_review")),
        "is_active": is_active,
    }


def _registry_qs(filters: dict, extra: dict = None) -> dict:
    params = {k: v for k, v in filters.items() if v not in (None, "", False)}
    params.update(extra or {})
    return params


@app.get("/registry", response_class=HTMLResponse)
async def registry_page(
        request: Request,
        sort: str = rq.DEFAULT_SORT,
        order: str = rq.DEFAULT_ORDER,
        page: int = 1,
        per_page: int = 50,
        user: str = Depends(verify_creds),
):
    filters = _registry_filters(dict(request.query_params))
    if sort not in rq.SORTABLE:
        sort = rq.DEFAULT_SORT
    if order not in ("asc", "desc"):
        order = rq.DEFAULT_ORDER

    with registry_db.connect() as conn:
        items, total = rq.search(conn, filters, sort, order, page, per_page)
        facets = rq.facets(conn, active_only=(filters["is_active"] == "true"))
        overview = rq.overview(conn)

    per_page = max(1, min(per_page, 500))
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))

    def pagination_qs(new_page: int) -> str:
        return urlencode(_registry_qs(filters, {
            "sort": sort, "order": order, "per_page": per_page, "page": new_page,
        }), doseq=True)

    def sort_link(column: str) -> str:
        new_order = "desc"
        if column == sort:
            new_order = "asc" if order == "desc" else "desc"
        return "?" + urlencode(_registry_qs(filters, {
            "sort": column, "order": new_order, "per_page": per_page, "page": 1,
        }), doseq=True)

    def sort_arrow(column: str) -> str:
        if column != sort:
            return ""
        return " ▼" if order == "desc" else " ▲"

    return templates.TemplateResponse(
        request=request,
        name="registry.html",
        context={
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "filters": filters,
            "facets": facets,
            "overview": overview,
            "source_titles": SOURCE_TITLES,
            "shifts_options": REGISTRY_SHIFTS_OPTIONS,
            "sort": sort,
            "order": order,
            "pagination_qs": pagination_qs,
            "sort_link": sort_link,
            "sort_arrow": sort_arrow,
            "export_qs": urlencode(_registry_qs(filters), doseq=True),
        },
    )


@app.get("/api/registry")
async def api_registry(
        request: Request,
        sort: str = rq.DEFAULT_SORT,
        order: str = rq.DEFAULT_ORDER,
        page: int = 1,
        per_page: int = 100,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """JSON-срез реестра под теми же фильтрами, что и страница."""
    filters = _registry_filters(dict(request.query_params))
    with registry_db.connect() as conn:
        items, total = rq.search(conn, filters, sort, order, page, per_page)
        overview = rq.overview(conn)
    return JSONResponse({
        "rows": [dict(row) for row in items],
        "count": len(items),
        "total": total,
        "updated_at": overview["updated_at"],
    })


@app.get("/registry/export.csv")
async def registry_export_csv(request: Request, user: str = Depends(verify_creds)) -> Response:
    """Выгрузка текущей выборки — ровно то, что менеджер видит на экране."""
    filters = _registry_filters(dict(request.query_params))
    columns = ["position_id", "request_id", "source_name"] + DATA_FIELDS + ["is_active", "last_seen_at"]
    max_rows = 20000

    with registry_db.connect() as conn:
        collected = []
        page = 1
        total = 0
        while len(collected) < max_rows:
            rows, total = rq.search(conn, filters, per_page=500, page=page)
            if not rows:
                break
            collected.extend(rows)
            if len(collected) >= total:
                break
            page += 1

    if total > len(collected):
        logger.warning(
            f"[registry] выгрузка обрезана: {len(collected)} из {total} строк "
            f"(предел {max_rows})"
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([FIELD_LABELS.get(c, c) for c in columns])
    for row in collected:
        writer.writerow([display(row[c] if c in row.keys() else "", c) for c in columns])

    # BOM — чтобы Excel открыл кириллицу без плясок с кодировкой.
    payload = "﻿" + buffer.getvalue()
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="registry.csv"'},
    )


@app.get("/registry/dictionaries", response_class=HTMLResponse)
async def registry_dictionaries(
        request: Request,
        kind: str = registry_dicts.KIND_JOB_TITLE,
        admin: "users_mod.User" = Depends(require_admin),
):
    if kind not in registry_dicts.KINDS:
        kind = registry_dicts.KIND_JOB_TITLE
    with registry_db.connect() as conn:
        entries = registry_dicts.entries(conn, kind)
        counts = registry_dicts.pending_counts(conn)
        canonical = registry_dicts.canonical_values(conn, kind)
    return templates.TemplateResponse(
        request=request,
        name="registry_dictionaries.html",
        context={
            "kind": kind,
            "kind_titles": registry_dicts.KIND_TITLES,
            "entries": entries,
            "pending_counts": counts,
            "canonical_values": canonical,
        },
    )


@app.post("/registry/dictionaries/confirm")
async def registry_dictionaries_confirm(
        kind: str = Form(...),
        alias: str = Form(...),
        canonical: str = Form(""),
        admin: "users_mod.User" = Depends(require_admin),
):
    with registry_db.connect() as conn:
        registry_dicts.confirm(conn, kind, alias, canonical)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.post("/registry/dictionaries/delete")
async def registry_dictionaries_delete(
        kind: str = Form(...),
        alias: str = Form(...),
        canonical: str = Form(""),
        admin: "users_mod.User" = Depends(require_admin),
):
    with registry_db.connect() as conn:
        registry_dicts.remove(conn, kind, alias)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.post("/registry/dictionaries/confirm-all")
async def registry_dictionaries_confirm_all(
        kind: str = Form(...),
        admin: "users_mod.User" = Depends(require_admin),
):
    with registry_db.connect() as conn:
        registry_dicts.confirm_all(conn, kind)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.get("/registry/manual", response_class=HTMLResponse)
async def registry_manual_form(request: Request, admin: "users_mod.User" = Depends(require_admin)):
    return templates.TemplateResponse(
        request=request, name="registry_manual.html", context={"result": None, "error": None},
    )


@app.post("/registry/manual", response_class=HTMLResponse)
async def registry_manual_submit(
        request: Request,
        counterparty: str = Form(...),
        text: str = Form(...),
        channel: str = Form(""),
        admin: "users_mod.User" = Depends(require_admin),
):
    """Заявка, вставленная руками, идёт через тот же приём, что и автоматические."""
    from vacancy_parser import VacancyParserService

    context = {"result": None, "error": None}
    raw_text = (text or "").strip()
    if not raw_text:
        context["error"] = "Пустой текст заявки"
        return templates.TemplateResponse(
            request=request, name="registry_manual.html", context=context,
        )

    # Ключ документа — по содержимому: у ручной заявки нет внешнего id, а
    # повторная вставка того же письма не должна плодить дубли.
    raw = RawRequest(
        source=SOURCE_MANUAL,
        source_ref="",
        raw_text=raw_text,
        source_name=f"Вручную ({channel})" if channel else "Вручную",
        counterparty_hint=counterparty.strip(),
        raw_payload={"channel": channel, "entered_by": admin.email},
        field_defaults={"counterparty": counterparty.strip()},
    )
    raw.source_ref = f"manual:{raw.content_hash[:16]}"

    try:
        ingestor = RegistryIngestor(VacancyParserService())
        # snapshot=False: одна заявка — это не полный список потребностей,
        # гасить по ней всё остальное нельзя.
        await ingestor.ingest(SOURCE_MANUAL, [raw], snapshot=False)
        with registry_db.connect() as conn:
            row = conn.execute(
                "SELECT request_id FROM requests WHERE source = ? AND source_ref = ?",
                (SOURCE_MANUAL, raw.source_ref),
            ).fetchone()
            request_id = row["request_id"] if row else ""
            positions = rq.positions_of_request(conn, request_id) if request_id else []
        context["result"] = {"request_id": request_id, "positions": len(positions)}
    except Exception as exc:  # noqa: BLE001 — показываем причину прямо в форме
        logger.exception("[registry] ручная заявка не принята")
        context["error"] = f"Не удалось принять заявку: {exc}"

    return templates.TemplateResponse(
        request=request, name="registry_manual.html", context=context,
    )


@app.get("/registry/position/{position_id}", response_class=HTMLResponse)
async def registry_position(
        request: Request,
        position_id: str,
        user: str = Depends(verify_creds),
):
    """Панель «Как пришло / Как распозналось» — грузится в модальное окно."""
    with registry_db.connect() as conn:
        position = rq.get_position(conn, position_id)
        if position is None:
            raise HTTPException(404, f"Позиция {position_id} не найдена")
        history = rq.history_of_position(conn, position_id)

    values = {name: display(position[name], name) for name in DATA_FIELDS}
    filled = sum(1 for name in DATA_FIELDS if values.get(name))

    return templates.TemplateResponse(
        request=request,
        name="registry_position.html",
        context={
            "position": position,
            "values": values,
            "present": [name for name in DATA_FIELDS if values.get(name)],
            "filled": filled,
            "total_fields": len(DATA_FIELDS),
            "field_groups": FIELD_GROUPS,
            "labels": FIELD_LABELS,
            "history": history,
            "source_titles": SOURCE_TITLES,
        },
    )


@app.post("/registry/position/{position_id}")
async def registry_position_save(
        position_id: str,
        status: str = Form(""),
        priority: str = Form(""),
        responsible_manager: str = Form(""),
        recruiter_comment: str = Form(""),
        market_rate: str = Form(""),
        user: str = Depends(verify_creds),
):
    try:
        market_rate_value = int(market_rate) if market_rate.strip() else None
    except ValueError:
        market_rate_value = None
    with registry_db.connect() as conn:
        rq.update_manager_fields(conn, position_id, {
            "status": status.strip() or None,
            "priority": priority.strip() or None,
            "responsible_manager": responsible_manager.strip() or None,
            "recruiter_comment": recruiter_comment.strip() or None,
            "market_rate": market_rate_value,
        }, author=user)
    return RedirectResponse("/registry", status_code=303)


@app.get("/registry/{request_id}", response_class=HTMLResponse)
async def registry_request(
        request: Request,
        request_id: str,
        user: str = Depends(verify_creds),
):
    with registry_db.connect() as conn:
        request_row = rq.get_request(conn, request_id)
        if request_row is None:
            raise HTTPException(404, f"Заявка {request_id} не найдена")
        positions = rq.positions_of_request(conn, request_id)
        revisions = rq.revisions_of_request(conn, request_id)

    return templates.TemplateResponse(
        request=request,
        name="registry_request.html",
        context={
            "request_row": request_row,
            "positions": positions,
            "revisions": revisions,
            "source_titles": SOURCE_TITLES,
            # Какие справки с Яндекс.Диска подмешивались к разбору. Менеджер
            # должен видеть не только «что распозналось», но и по какому
            # описанию проекта, и мочь открыть ту же папку.
            "kb_projects": _kb_projects(request_row),
        },
    )


def _kb_projects(request_row) -> list:
    """Проекты базы знаний, привязанные к заявке (см. project_kb.py)."""
    try:
        payload = json.loads(request_row["raw_payload"] or "{}")
    except (ValueError, TypeError):
        return []
    items = payload.get("kb") or []
    return items if isinstance(items, list) else []


# ---------- /vacancies ----------
GENDER_OPTIONS = ["мужчины", "женщины", "любые"]
AGE_OPTIONS = [30, 35, 40, 45, 50, 55, 60]
SHIFTS_OPTIONS = [15, 20, 30, 45]
GEO_OPTIONS = {
    "moscow_mo": "Москва + МО",
    "regions": "Регионы",
    "all": "Все",
}


def _canonicalize_sb_policy(raw: str) -> str:
    """
    Сводит варианты sb_policy от LLM к канонической форме.

    Проблема: LLM может вернуть «легкие» и «лёгкие», «Проверка СБ» и
    «проверка СБ», «Без тяжких» и «без тяж.статей» — это всё одно и то же,
    но в селекте на странице видно как разные значения, и фильтр их не объединяет.
    """
    if not raw:
        return ""
    s = raw.strip().lower().replace("ё", "е")
    if "тяж" in s:
        return "без тяж.статей"
    if "легк" in s:
        return "лёгкие статьи допускаются"
    if "судим" in s and ("без" in s or "стро" in s):
        return "без судимостей"
    if "выбороч" in s:
        return "выборочная"
    if "провер" in s and ("сб" in s or "безопас" in s):
        return "проверка СБ"
    if s == "нет" or s == "без сб" or s == "нет сб":
        return "нет"
    return raw.strip()


def _to_int(value, default=None):
    try:
        return int(float((value or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return default


def _matches(v: dict, filters: dict) -> bool:
    if filters["source"] and v.get("source") != filters["source"]:
        return False
    if filters["city"]:
        city = (v.get("city") or "").lower()
        if filters["city"].lower() not in city:
            return False
    if filters["q"]:
        name = (v.get("vacancy_name") or "").lower()
        if filters["q"].lower() not in name:
            return False
    if filters["is_active"] != "all":
        wanted = "TRUE" if filters["is_active"] == "true" else "FALSE"
        if (v.get("is_active") or "").upper() != wanted:
            return False

    if filters["gender"]:
        if (v.get("gender") or "").lower().strip() != filters["gender"]:
            return False

    if filters["age"]:
        age_n = _to_int(filters["age"])
        if age_n is not None:
            age_from = _to_int(v.get("age_from"), 0) or 0
            age_to = _to_int(v.get("age_to"), 100)
            if age_to is None:
                age_to = 100
            if not (age_from <= age_n <= age_to):
                return False

    if filters["geo"] and filters["geo"] != "all":
        region = (v.get("region") or "").lower()
        city = (v.get("city") or "").lower()
        is_moscow = ("москва" in region) or ("моско" in region) or (city == "москва")
        if filters["geo"] == "moscow_mo" and not is_moscow:
            return False
        if filters["geo"] == "regions" and is_moscow:
            return False
        if filters["geo"] == "regions" and not (region or city):
            # без указания региона/города — не относим к «регионам»
            return False

    if filters["sb_policy"]:
        # Сравниваем с нормализованной формой — «легкие» и «лёгкие» считаются одним.
        canon = _canonicalize_sb_policy(v.get("sb_policy") or "")
        if canon not in filters["sb_policy"]:
            return False

    if filters["max_shifts"]:
        max_n = _to_int(filters["max_shifts"])
        if max_n is not None:
            ms = _to_int(v.get("min_shifts"))
            # Если у вакансии min_shifts не указан — показываем (не отсекаем).
            # Если указан и больше выбранного — кандидат не готов столько отрабатывать.
            if ms is not None and ms > max_n:
                return False

    return True


# Колонки таблицы, по которым разрешена сортировка. Ключ совпадает с именем
# поля в данных Sheets, или это виртуальный ключ (city_object) — обрабатывается
# в _sort_key как city. Для NUMERIC значения парсятся как float.
SORTABLE_COLUMNS = {
    "vacancy_name", "city", "shift_rate", "need_men", "need_women",
    "need_couples", "need_total", "shift_type", "min_shifts",
    "requires_tsd", "sb_policy", "source", "last_updated_at", "is_active",
}
NUMERIC_COLUMNS = {
    "shift_rate", "need_men", "need_women", "need_couples", "need_total", "min_shifts",
}
DEFAULT_SORT = "last_updated_at"
DEFAULT_ORDER = "desc"


def _is_empty_for_sort(v: dict, col: str) -> bool:
    """True если значение колонки в этой записи нужно считать «пустым»
    и отправлять в конец сортировки независимо от asc/desc."""
    raw = (v.get(col) or "").strip()
    if col in NUMERIC_COLUMNS:
        try:
            float(raw)
            return False
        except (ValueError, TypeError):
            return True
    return raw == ""


def _sort_key(col: str):
    """Ключ для list.sort() — применяется ТОЛЬКО к не-пустым записям."""
    def key(v: dict):
        raw = (v.get(col) or "").strip()
        if col in NUMERIC_COLUMNS:
            return float(raw)
        return raw.lower()
    return key


@app.get("/vacancies", response_class=HTMLResponse)
async def vacancies(
        request: Request,
        source: Optional[str] = None,
        city: Optional[str] = None,
        q: Optional[str] = None,
        is_active: str = "true",
        gender: Optional[str] = None,
        age: Optional[str] = None,
        geo: Optional[str] = None,
        sb_policy: Optional[List[str]] = Query(None),
        max_shifts: Optional[str] = None,
        sort: str = DEFAULT_SORT,
        order: str = DEFAULT_ORDER,
        page: int = 1,
        per_page: int = 50,
        user: str = Depends(verify_creds),
):
    all_rows = await _legacy_rows()
    cache_age = 0 if REGISTRY_ENABLED else (
        int(time.time() - _cache._timestamp) if _cache._timestamp else 0
    )

    # Нормализуем gender — иногда LLM возвращает с заглавной
    gender_norm = (gender or "").lower().strip() or None
    if gender_norm and gender_norm not in GENDER_OPTIONS:
        gender_norm = None

    geo_norm = (geo or "").lower().strip() or None
    if geo_norm and geo_norm not in GEO_OPTIONS:
        geo_norm = None

    # Чистим sb_policy: убираем пустые/None из списка
    sb_policy_norm = [s.strip() for s in (sb_policy or []) if s and s.strip()]

    filters = {
        "source": source,
        "city": city,
        "q": q,
        "is_active": is_active if is_active in ("true", "false", "all") else "true",
        "gender": gender_norm,
        "age": age,
        "geo": geo_norm,
        "sb_policy": sb_policy_norm,
        "max_shifts": max_shifts,
    }

    # ---- Метрики UI ----
    M.UI_PAGE_VIEWS.inc()
    # Каждый явно заданный фильтр — инкремент. is_active=true (дефолт) не считаем.
    if source:
        M.UI_FILTER_USED.labels(filter="source").inc()
        M.UI_FILTER_VALUE.labels(filter="source", value=source).inc()
    if city:
        M.UI_FILTER_USED.labels(filter="city").inc()
    if q:
        M.UI_FILTER_USED.labels(filter="q").inc()
    if gender_norm:
        M.UI_FILTER_USED.labels(filter="gender").inc()
        M.UI_FILTER_VALUE.labels(filter="gender", value=gender_norm).inc()
    if age:
        M.UI_FILTER_USED.labels(filter="age").inc()
        M.UI_FILTER_VALUE.labels(filter="age", value=age).inc()
    if geo_norm:
        M.UI_FILTER_USED.labels(filter="geo").inc()
        M.UI_FILTER_VALUE.labels(filter="geo", value=geo_norm).inc()
    if max_shifts:
        M.UI_FILTER_USED.labels(filter="max_shifts").inc()
        M.UI_FILTER_VALUE.labels(filter="max_shifts", value=max_shifts).inc()
    if sb_policy_norm:
        M.UI_FILTER_USED.labels(filter="sb_policy").inc()
        for val in sb_policy_norm:
            M.UI_FILTER_VALUE.labels(filter="sb_policy", value=val).inc()
    if is_active and is_active != "true":
        M.UI_FILTER_USED.labels(filter="is_active").inc()
        M.UI_FILTER_VALUE.labels(filter="is_active", value=is_active).inc()

    filtered = [v for v in all_rows if _matches(v, filters)]

    if sort not in SORTABLE_COLUMNS:
        sort = DEFAULT_SORT
    if order not in ("asc", "desc"):
        order = DEFAULT_ORDER
    # Считаем только явные сортировки (не дефолт)
    if sort != DEFAULT_SORT or order != DEFAULT_ORDER:
        M.UI_SORT_USED.labels(column=sort, order=order).inc()
    # Пустые значения сортируемой колонки уезжают в конец — в любом направлении.
    non_empty = [v for v in filtered if not _is_empty_for_sort(v, sort)]
    empty = [v for v in filtered if _is_empty_for_sort(v, sort)]
    non_empty.sort(key=_sort_key(sort), reverse=(order == "desc"))
    filtered = non_empty + empty

    total = len(filtered)
    per_page = max(1, min(per_page, 500))
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    items = filtered[start:start + per_page]

    # Наполнение селектов считаем по строкам, которые вообще может показать
    # текущий режим is_active, а не по всей таблице. Иначе в фильтре «Источник»
    # навсегда остаются отключённые провайдеры: строки удалённого ГСР лежат в
    # таблице как история, и он торчал бы в списке, хотя ни одной активной
    # вакансии у него нет.
    if filters["is_active"] == "all":
        visible_rows = all_rows
    else:
        wanted_active = "TRUE" if filters["is_active"] == "true" else "FALSE"
        visible_rows = [
            v for v in all_rows
            if (v.get("is_active") or "").upper() == wanted_active
        ]

    available_sources = sorted({(v.get("source") or "").strip() for v in visible_rows if v.get("source")})
    available_sb = sorted({
        _canonicalize_sb_policy(v.get("sb_policy") or "")
        for v in visible_rows
        if (v.get("sb_policy") or "").strip()
    } - {""})

    # Базовые query-параметры (фильтры + sort/order/per_page) — сохраняются при кликах.
    # doseq=True ниже корректно сериализует списки в "key=v1&key=v2".
    def _base_params() -> dict:
        params = {}
        for k, val in filters.items():
            if val is None or val == "" or val == []:
                continue
            params[k] = val
        params["per_page"] = per_page
        return params

    def pagination_qs(new_page: int) -> str:
        params = _base_params()
        params["sort"] = sort
        params["order"] = order
        params["page"] = new_page
        return urlencode(params, doseq=True)

    def sort_link(col: str) -> str:
        """URL для клика по заголовку таблицы. Переключает asc<->desc, либо ставит default-order для новой колонки."""
        params = _base_params()
        if col == sort:
            params["order"] = "asc" if order == "desc" else "desc"
        else:
            params["order"] = "desc" if col in NUMERIC_COLUMNS or col == "last_updated_at" else "asc"
        params["sort"] = col
        params["page"] = 1
        return "?" + urlencode(params, doseq=True)

    def sort_arrow(col: str) -> str:
        if col != sort:
            return ""
        return " ▼" if order == "desc" else " ▲"

    return templates.TemplateResponse(
        request=request,
        name="vacancies.html",
        context={
            "items": items,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "filters": filters,
            "available_sources": available_sources,
            "available_sb": available_sb,
            "gender_options": GENDER_OPTIONS,
            "age_options": AGE_OPTIONS,
            "shifts_options": SHIFTS_OPTIONS,
            "geo_options": GEO_OPTIONS,
            "cache_age": cache_age,
            "cache_ttl": CACHE_TTL_SECONDS,
            "pagination_qs": pagination_qs,
            "sort_link": sort_link,
            "sort_arrow": sort_arrow,
            "sort": sort,
            "order": order,
        },
    )
