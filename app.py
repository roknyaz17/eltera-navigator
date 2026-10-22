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
    09:30  vahtapro + aaaplus + melior     с reset для них
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
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Literal, Optional
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
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
import agent_search
import bitrix
import nav_assistant
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
        "sources": ["vahtapro", "aaaplus", "melior"],
        "reset": True,
        "description": "09:30 МСК — Градус + AAA+ + МЕЛИОР с reset",
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


async def _crm_job() -> None:
    """Досылает в Битрикс то, что не ушло с первого раза.

    Раз в пять минут, а не сразу при звонке: портал может лежать, а
    постобработка звонка ждать его не должна. Пять минут задержки для лида
    незаметны, потерянный лид — нет.
    """
    if not bitrix.enabled():
        return
    import asyncio

    def _run():
        with registry_db.connect() as conn:
            return bitrix.deliver_pending(conn)

    try:
        summary = await asyncio.to_thread(_run)
        if summary.get("взято"):
            logger.info(f"[bitrix] {summary}")
    except Exception:
        logger.exception("[bitrix] проход не удался")


def _register_jobs() -> None:
    scheduler.add_job(
        _crm_job, trigger="interval", minutes=5, id="bitrix",
        name="Досылка лидов в Битрикс", replace_existing=True,
        misfire_grace_time=300,
    )
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


# Токен для машинных клиентов. Пока такой клиент один — голосовой агент, который
# во время звонка спрашивает подходящие вакансии.
#
# Отдельный заголовок, а не сессия и не Basic, по трём причинам. Сессию пришлось
# бы продлевать логином раз в неделю, а логин стоит 600k итераций PBKDF2 и
# оставляет запись в журнале входов. Basic ограничен BASIC_PATHS и его собираются
# убирать (SEC-14). И главное — обе схемы дают доступ ко всему сайту сразу, а
# агенту нужна ровно одна ручка, отзываемая одной переменной окружения.
AGENT_API_TOKEN = os.getenv("AGENT_API_TOKEN", "").strip()


def verify_agent_token(request: Request) -> str:
    """Пускает машинного клиента по заголовку X-Agent-Token."""
    if not AGENT_API_TOKEN:
        # Токен не задан — ручка выключена целиком. Так безопаснее, чем пускать
        # всех: пустой токен иначе совпал бы с пустым заголовком.
        raise HTTPException(503, "Доступ для агента не настроен")
    provided = request.headers.get("X-Agent-Token", "")
    if not secrets.compare_digest(provided, AGENT_API_TOKEN):
        logger.warning(f"[agent] неверный токен с {_client_ip(request)}")
        raise HTTPException(401, "Неверный токен")
    return "agent"


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    """Форма входа. Уже вошедшего сразу отправляем дальше.

    Проверка здесь обязана быть ровно такой же строгой, как в verify_creds.
    Раньше тут стоял голый read_session — только подпись и срок, без сверки
    с базой. Из-за этого cookie с устаревшим отпечатком пароля выглядела
    действительной для /login и недействительной для всех остальных роутов:
    /navigator отправлял на /login, /login отправлял обратно, и браузер
    зацикливался. Разная строгость на соседних роутах — это всегда петля.
    """
    if current_user(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)

    stale = bool(request.cookies.get(auth.COOKIE_NAME, ""))
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"next_url": _safe_next(next), "email": "", "error": "", "blocked": False},
    )
    # Форма входа не должна оседать в кеше браузера и промежуточных прокси.
    response.headers["Cache-Control"] = "no-store"
    if stale:
        # Cookie есть, но не действует: гасим, чтобы браузер перестал её носить
        # и не спотыкался о неё на каждом следующем запросе.
        response.delete_cookie(auth.COOKIE_NAME, path="/")
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
            request=request,
            name="login.html",
            context={"next_url": target, "email": email, "error": message, "blocked": blocked},
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

# Минимальная длина пароля. Четыре — решение заказчика (16.08.2026).
# Держится это на ограничителе попыток: пять с адреса, потом блокировка,
# плюс глобальный предохранитель от перебора с разных адресов. Пока
# приложение отдаётся по http без сертификата, длина — не главная дыра.
MIN_PASSWORD_LENGTH = 4


@app.get("/password", response_class=HTMLResponse)
async def password_form(request: Request, user: str = Depends(verify_creds)):
    person = getattr(request.state, "user", None)
    response = templates.TemplateResponse(
        request=request,
        name="password_change.html",
        context={"email": user, "error": "",
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
            request=request,
            name="password_change.html",
            context={"email": user, "error": message,
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
    """Стартовая страница — навигатор: сюда же ведёт вход без явного next."""
    target = "/navigator"
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
            is_admin = bool(person and person.is_admin)
            payload["me"] = {
                "email": person.email if person else user,
                "name": person.title if person else user,
                "role": (person.roles[0] if person and person.roles else ""),
                "isAdmin": is_admin,
            }
            # Ставка рекрутёра режется до его доли здесь, а не в шаблоне:
            # этот JSON открывается в браузере рекрутёра целиком.
            return navigator_api.apply_role_visibility(payload, is_admin=is_admin)

    payload = await asyncio.to_thread(_build)
    return JSONResponse(payload)


class AssistantAsk(BaseModel):
    """Тело POST /api/assistant: вопрос и разговор, к которому он относится."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(..., min_length=1, max_length=nav_assistant.MAX_QUESTION)
    session: str = Field("", max_length=64)


@app.post("/api/assistant")
async def api_assistant(
        payload: AssistantAsk,
        request: Request,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """Помощник подбора: вопрос словами → позиции из реестра.

    Каждый вопрос с ответом пишется в assistant_log (nav_assistant.ask) — и
    удачный, и упавший. Модель отвечает секунды, поэтому ask уходит в поток,
    а не держит цикл событий.
    """
    import asyncio

    if not _same_origin(request):
        raise HTTPException(403, "Запрос пришёл с чужой страницы")
    if not payload.q.strip():
        raise HTTPException(400, "Пустой вопрос")
    result = await asyncio.to_thread(
        nav_assistant.ask, payload.q, user=user, session=payload.session,
    )
    return JSONResponse(result)


@app.get("/api/assistant/log")
async def api_assistant_log(
        limit: int = Query(100, ge=1, le=1000),
        email: str = "",
        admin: "users_mod.User" = Depends(require_admin),
) -> JSONResponse:
    """Журнал помощника для администратора: вопросы, ответы, условия поиска."""
    with registry_db.connect() as conn:
        items = nav_assistant.recent_log(conn, limit, user=email.strip().lower())
    return JSONResponse({"items": items})


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


@app.get("/api/agent/payout_coverage")
async def api_agent_payout_coverage(
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Насколько актуальны ставки выплат за кандидата.

    Отдельная ручка, потому что молчаливое протухание — главный способ, каким
    эта механика ломается. Акции живут неделю; когда их перестают обновлять,
    все позиции получают нулевую ставку, порядок выдачи откатывается к
    прежнему, и снаружи ничего не меняется. Проверять глазами нечего — поэтому
    есть что запросить.
    """
    import asyncio

    import payouts as payout_rules

    def _report() -> dict:
        with registry_db.connect() as conn:
            return payout_rules.coverage(conn)

    return JSONResponse(await asyncio.to_thread(_report))


def _years(raw) -> Optional[int]:
    """Возраст из того, что прислала модель. None — «не сказано».

    Приходит и число, и «28 лет», и пустая строка, и «null» словом. Падать
    на любом из этих вариантов нельзя: ошибка вызова инструмента для человека
    в трубке выглядит как сломанная система, и агент начинает извиняться.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if not digits:
        return None
    years = int(digits[:3])
    # Трёхзначный возраст — это склеенные цифры или мусор, а не человек.
    return years if 10 <= years <= 99 else None


AGENT_PARAMS = ("profession", "city", "region", "citizenship", "age", "gender")


def _repair_params(request: Request) -> dict[str, str]:
    """Чинит опечатки в именах параметров подбора.

    Агент изредка пишет имя не так: на сотне вызовов приходили cittizenship,
    citisenship, категория, парметры. Незнакомый ключ FastAPI молча
    выбрасывает, и подбор идёт без гражданства — человеку читают вакансии, на
    которые его не возьмут, а в логе ни следа. Поэтому близкое имя принимаем,
    далёкое пишем в лог: пусть будет видно, что агент придумывает.
    """
    import difflib

    repaired: dict[str, str] = {}
    for name, value in request.query_params.items():
        if name in AGENT_PARAMS or name.startswith("system__") or not value:
            continue
        near = difflib.get_close_matches(name.lower(), AGENT_PARAMS, n=1, cutoff=0.8)
        if not near:
            logger.warning(f"[agent] незнакомый параметр {name!r}={value!r}")
        elif near[0] not in repaired:
            repaired[near[0]] = value
            logger.warning(f"[agent] параметр {name!r} принят как {near[0]!r}")
    return repaired


@app.get("/api/agent/vacancies")
async def api_agent_vacancies(
        request: Request,
        # Не обязателен намеренно. Обязательное поле голосовой агент заполняет
        # любой ценой — в тестовых звонках выдумывал профессию за кандидата.
        # Пустое значение agent_search вернёт с причиной «профессия не названа»,
        # и агент по ней задаёт вопрос вместо того, чтобы гадать.
        profession: str = "",
        city: str = "",
        region: str = "",
        citizenship: str = "",
        # Строкой, а не числом, намеренно. Типизированный integer выглядит
        # строже, но строгость тут достаётся не нам: ElevenLabs отбивает
        # несовпадение типа у себя, вызов до навигатора не доходит, и агент
        # вместо вакансий получает «Unable to execute function». Так мы
        # потеряли разговор с Ангелиной 06.10: модель прислала age=null,
        # потому что человек на вопрос про возраст ответил своим вопросом.
        # Разбираем сами: цифры берём, всё остальное считаем «не сказано».
        age: str = "",
        gender: str = "",
        has_medical_book: Optional[bool] = None,
        min_rate: Optional[int] = None,
        limit: int = 3,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Подбор вакансий для голосового агента во время звонка.

    Ручка узкая по умыслу: агент спрашивает человека и тут же зачитывает
    варианты вслух, поэтому наружу уходит белый список полей, которые кандидату
    можно назвать. Ни контрагента, ни объекта, ни адреса, ни ставки рекрутёра
    здесь нет — подбор в agent_search собирает ответ с нуля, а не вычищает
    карточку из /api/navigator.

    Отвечать надо быстро: у инструмента ElevenLabs минимальный таймаут 5 секунд,
    и всё это время в трубке тишина.
    """
    import asyncio

    fixed = _repair_params(request)
    profession = profession or fixed.get("profession", "")
    city = city or fixed.get("city", "")
    region = region or fixed.get("region", "")
    citizenship = citizenship or fixed.get("citizenship", "")
    gender = gender or fixed.get("gender", "")
    years = _years(age) or _years(fixed.get("age", ""))

    def _run() -> dict:
        with registry_db.connect(readonly=True) as conn:
            return agent_search.search(
                conn,
                profession=profession,
                city=city,
                region=region,
                citizenship=citizenship,
                age=years,
                gender=gender,
                has_medical_book=has_medical_book,
                min_rate=min_rate,
                limit=limit,
            )

    result = await asyncio.to_thread(_run)
    logger.info(
        f"[agent] подбор: {profession!r} город={city!r} → "
        f"найдено {result['total_matched']}"
    )
    return JSONResponse(result)


class AgentCardRequest(BaseModel):
    """Тело запроса на ручную сборку карточки. Кандидатам её шлёт не эта ручка."""

    # Идентификатор позиции — строка вида ELT-2026-000605-01, не число.
    position_id: str
    channel: Literal["telegram", "whatsapp"] = "telegram"
    phone: str = ""
    candidate_name: str = ""


class AgentCallRequest(BaseModel):
    """Итог звонка от звонилки.

    Разговор кладём целиком, а не разобранным по полям. Состав data collection
    у агента меняется на живом проекте, и если бы звонилка разбирала его сама,
    каждое новое поле требовало бы выкатки на машину обзвона. Разбирает сервер.
    """

    phone: str
    conversation: dict


class InboundMessage(BaseModel):
    """Входящее сообщение кандидата из любого канала."""

    channel: Literal["telegram", "whatsapp", "max"]
    peer: str = ""
    phone: str = ""
    external_id: str = ""
    kind: Literal["text", "photo", "document", "other"] = "text"
    text: str = ""
    media_type: str = ""
    media_size: Optional[int] = None
    media_sha256: str = ""
    media_path: str = ""
    received_at: str = ""


# Базовый набор документов. Держим здесь, а не только в мессенджере, потому
# что состав для конкретного кандидата считает навигатор: он один знает, какой
# контрагент за вакансией и что тот требует сверх обычного.
DEFAULT_DOCS = ("passport_main", "passport_registration", "snils")


class DocumentVerdict(BaseModel):
    """Разбор одного присланного файла."""

    phone: str
    kind: str
    outcome: Literal["accepted", "retry", "manual"]
    full_name: str = ""
    birth_date: str = ""
    sex: str = ""
    number: str = ""
    confidence: float = 0
    problems: str = ""
    media_path: str = ""
    inbound_id: Optional[int] = None


class StageUpdate(BaseModel):
    """Перевод кандидата на другую стадию переписки."""

    stage: str
    # Смена стадии означает, что человек снова с нами: счётчик напоминаний
    # сбрасывается, иначе один раз замолчавший навсегда остаётся без них.
    reset_reminders: bool = True
    needs_human: Optional[bool] = None


class OutboxMessage(BaseModel):
    """Готовый текст в очередь отправки."""

    phone: str
    channel: str
    text: str
    # reply — ответ человеку, который сам написал. Такие сообщения идут мимо
    # пауз и дневного лимита: это не рассылка, а разговор.
    kind: Literal["reply", "reminder"] = "reply"
    position_id: str = ""


class InboundStatus(BaseModel):
    status: Literal["handled", "ignored", "new"] = "handled"


class PeerLink(BaseModel):
    """Кто есть кто: собеседник в канале и его телефон."""

    channel: Literal["telegram", "whatsapp", "max"]
    peer: str
    phone: str


class SbChatRule(BaseModel):
    """Правило адресации: какой чат обслуживает какие вакансии."""

    # leads — не маршрутизация, а единственный адрес: чат, куда падают
    # удачные лиды. Живёт в той же таблице, потому что это ровно тот же вопрос
    # «куда писать», и разводить его по двум местам значило бы заводить второй
    # способ настроить чат.
    match_type: Literal["counterparty", "source", "default", "leads"]
    match_value: str = ""
    channel: Literal["telegram", "whatsapp", "max"] = "telegram"
    chat_id: str
    title: str = ""
    is_active: bool = True
    # Что этот контрагент требует сверх обычного паспорта со СНИЛСом.
    # Виды через запятую: "portrait" либо "portrait,inn".
    extra_docs: str = ""


class SbRequestCreate(BaseModel):
    """Заявка в службу безопасности по конкретному кандидату."""

    phone: str
    position_id: str = ""
    # ФИО из паспорта, а не из разговора: голосом кандидат называет только имя,
    # а службе безопасности нужны фамилия и отчество.
    full_name: str = ""
    documents: str = ""
    send: bool = False
    # Пакет без срока выезда получателю бесполезен: заезд по нему не поставить.
    # Отпускаем такой пакет только осознанно — когда кандидата уже спрашивали,
    # а он не ответил. По умолчанию выключено, чтобы защита работала сама.
    send_without_departure: bool = False


class DepartureUpdate(BaseModel):
    """Что кандидат ответил про срок выезда."""

    phone: str
    raw: str = ""
    # Разбор делает мессенджер: там же лежит словарь формулировок и туда же
    # приходит сообщение. Навигатор хранит результат, а не разбирает заново.
    date: str = ""
    precision: Literal["exact", "approx", "unknown"] = "unknown"
    # Вопрос задан, ответа пока нет. Пишем только сам факт вопроса: затирать
    # уже записанный срок пустотой нельзя, а знать, сколько раз спрашивали,
    # надо раньше, чем решать, отпускать ли пакет без даты.
    asked_only: bool = False


class SbVerdict(BaseModel):
    """Ответ заказчика по заявке."""

    verdict: Literal["approved", "rejected", "unclear"]
    answer_text: str = ""
    answered_by: str = ""


class OutboxReport(BaseModel):
    """Отчёт сервиса переписки о судьбе сообщения."""

    status: Literal["sent", "failed", "pending"]
    error: str = ""
    # Что человек реально увидел. Храним отдельно от тела сообщения: тело это
    # данные, а текст из них собирает мессенджер, и его формулировки меняются.
    # Без этой записи прошлая переписка переписывалась бы задним числом при
    # каждой правке шаблона.
    text: str = ""


def build_card(conn, position_id: str) -> Optional[dict]:
    """Карточка вакансии для кандидата или None, если позиции нет.

    Собирается из реестра через agent_search.project() — по тому же белому
    списку полей, что агент называет вслух. Пересказ модели сюда не попадает:
    в переписке у человека остаётся текст, на который он потом сошлётся, и он
    обязан совпадать с реестром.
    """
    row = conn.execute(
        "SELECT * FROM positions WHERE position_id = ?", (position_id,)
    ).fetchone()
    if row is None:
        return None
    city_regions = registry_dicts.load(conn, registry_dicts.KIND_CITY_REGION)
    vacancy = agent_search.project(row, city_regions)
    return {
        "вакансия": vacancy["profession"],
        "город": vacancy["city"] or vacancy["region"],
        "ставка": vacancy["pay_text"],
        "смен_минимум": vacancy["min_shifts"],
        "график": vacancy["schedule_text"],
        "проживание": vacancy["housing"],
        "питание": vacancy["meals"],
        "медкнижка": vacancy["medical_book"],
        "документы": "первый разворот паспорта, прописка, номер СНИЛС",
    }


@app.post("/api/agent/send_card")
async def api_agent_send_card(
        payload: AgentCardRequest,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Ручная сборка карточки — посмотреть глазами, а не отправить.

    Кандидатам карточки уходят из очереди candidate_messages, которую наполняет
    постобработка звонка. Ручка осталась, чтобы можно было проверить, что
    соберётся по конкретной позиции, не поднимая звонок.
    """
    import asyncio

    def _run():
        with registry_db.connect(readonly=True) as conn:
            return build_card(conn, payload.position_id)

    card = await asyncio.to_thread(_run)
    if card is None:
        return JSONResponse({"ok": False, "error": "позиция не найдена"}, status_code=404)
    return JSONResponse({"ok": True, "card": card})


# Поля, которые агент собирает после разговора (Data collection в ElevenLabs),
# и колонки карточки кандидата, куда они ложатся. Список здесь, а не в звонилке:
# добавить поле — правка на сервере, без выкатки на машину обзвона.
_CALL_FIELDS_TEXT = {
    "candidate_name": "name",
    # Возраст агент отдаёт строкой; колонка объявлена INTEGER, и SQLite сам
    # приведёт "38" к числу по affinity. Городить отдельную ветку разбора ради
    # этого не стоит: пустое и нечисловое значение отсеивается выше.
    "age": "age",
    "citizenship": "citizenship",
    "region": "region",
    "city": "city",
    "profession": "profession",
    "call_status": "status",
    "candidate_interest": "interest",
    "preferred_channel": "preferred_channel",
    "recommended_vacancy_id": "last_position_id",
    "desired_salary": "desired_salary",
    "desired_schedule": "desired_schedule",
    "available_start_date": "start_date",
    "objections": "objections",
    "candidate_questions": "questions",
    "callback_datetime": "callback_at",
    "next_action": "next_action",
    "summary": "summary",
}
_CALL_FIELDS_BOOL = {
    "has_medical_book": "has_medical_book",
    "ready_for_shift_work": "ready_for_shift",
    "ready_to_relocate": "ready_to_move",
    "callback_requested": "callback_requested",
    "needs_human_recruiter": "needs_human",
}


# Судимость приезжает одной строкой, а не двумя полями: у тарифа ElevenLabs
# потолок в 25 полей сбора, и он занят целиком. Разбираем здесь.
_NO_RECORD = {"нет", "не судим", "несудим", "отсутствует", "no", "none", "false"}


def _criminal(value) -> tuple:
    """«» → (не спрашивали, ''), «нет» → (нет судимости, ''), иначе → (есть, статья).

    Три состояния, а не два. «Вопрос не задавали» и «кандидат сказал, что нет» —
    разные вещи: во втором случае переспрашивать человека значит показывать
    недоверие, в первом спросить обязаны.
    """
    text = (value or "").strip()
    if not text:
        return None, ""
    if text.lower().strip(" .!") in _NO_RECORD:
        return 0, ""
    return 1, text


# Идентификаторы супергрупп Telegram имеют вид -100XXXXXXXXXX, но человек
# копирует их из интерфейса без префикса. Приводим к каноническому виду, иначе
# отправка молча уйдёт «пользователю» с таким id или не уйдёт никуда.
def normalize_chat_id(raw: str, channel: str = "telegram") -> str:
    value = (raw or "").strip()
    if channel != "telegram" or not value:
        return value
    if value.startswith("@") or value.startswith("-"):
        return value
    if value.isdigit() and len(value) >= 9:
        return "-100" + value
    return value


def resolve_sb_chat(conn, counterparty: str, source: str) -> Optional[dict]:
    """Куда слать пакет по этой вакансии.

    Порядок неслучаен: чат конкретного контрагента точнее, чем общий чат
    источника, а общий точнее, чем свалка по умолчанию. Первое совпадение
    выигрывает, дальше не смотрим.
    """
    for match_type, match_value in (
        ("counterparty", counterparty),
        ("source", source),
        ("default", ""),
    ):
        if match_type != "default" and not match_value:
            continue
        row = conn.execute(
            """SELECT channel, chat_id, title, match_type, match_value, extra_docs
               FROM sb_chats
               WHERE match_type = ? AND match_value = ? AND is_active = 1""",
            (match_type, match_value),
        ).fetchone()
        if row:
            return dict(row)
    return None


def build_sb_package(candidate, position, full_name: str, documents: str,
                     snils: str = "") -> str:
    """Текст заявки в чат заказчика.

    Формат снят с живой переписки в девяти чатах контрагентов, а не придуман.
    Настоящая заявка выглядит так:

        Лисовая Олеся Владимировна
        Птицефабрика Павлово
        Вахта 35 смен
        Приезд 2 июня
        +7 (906) 380-45-88
        на проверку СБ

    Отсюда четыре вещи, каждая из которых в прежней версии была не так.

    Телефон нужен. Раньше его тут не было: я решил, что проверяющему он ни к
    чему, а разойдётся по чужим чатам навсегда. В 18 заявках из 39 он есть, и
    Маркет Стафф прямо перечисляет, что ждёт: «Паспорт прописка, когда готова
    заселиться и телефон».

    Смены обязательны — «вахта 35 смен» стоит в 26 заявках из 39.

    Контрагента не пишут. Мы писали «Контрагент: Перфетти» в чат самого
    Перфетти.

    Пишут объект, а не должность: «Птицефабрика Павлово», «Яндекс Самара».
    Должность проверяющему не нужна, ему нужно понять, куда человек едет.

    «на проверку СБ» стоит в конце, а не заголовком: это не название документа,
    а просьба, и читается она последней.
    """
    name = (full_name or candidate["name"] or "").strip() or "имя не указано"
    age = candidate["age"]
    citizenship = candidate["citizenship"] or ""
    first = name + (f" {age} лет" if age else "") + (f", {citizenship}" if citizenship else "")
    lines = [first]

    if position is not None:
        # Объект, а не вакансия. Города в объекте часто уже есть («Яндекс
        # Самара»), поэтому добавляем его отдельной частью только когда он не
        # звучит в названии.
        where = (position["object_name"] or "").strip()
        city = (position["city"] or "").strip()
        if not where:
            where = (position["vacancy_name"] or "").strip()
        if city and city.lower() not in where.lower():
            where = f"{where}, {city}" if where else city
        if where:
            lines.append(where)

        shifts = position["min_shifts"]
        if shifts:
            lines.append(f"Вахта {shifts} смен")

    departure = _departure_line(candidate)
    if departure:
        lines.append(departure)

    phone = (candidate["phone"] or "").strip()
    if phone:
        lines.append(phone)

    if snils:
        lines.append(f"СНИЛС {snils}")

    if candidate["has_criminal_record"]:
        article = candidate["criminal_article"] or "статья не названа"
        lines.append(f"Судимость: {article}")

    if documents:
        lines.append(f"Документы: {documents}")

    lines += ["", "на проверку СБ"]
    return "\n".join(lines)


def _departure_line(candidate) -> str:
    """Строка про срок выезда для пакета в чат заказчика.

    Приблизительный срок печатаем вместе со словами кандидата. Заезд планирует
    человек на той стороне, и «через недельку», превращённое разбором ровно в
    плюс семь дней, он должен видеть как оценку, а не как назначенное число.

    Порядок веток — сначала наличие даты, потом точность. Клиент вправе
    прислать precision без самой даты, и проверка «по точности сначала»
    напечатала бы «Готов выехать:» с пустым местом.
    """
    iso = (candidate["departure_date"] or "").strip()
    precision = (candidate["departure_precision"] or "").strip()
    raw = " ".join((candidate["departure_raw"] or "").split())[:120]

    human = iso
    try:
        human = datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        pass

    # «Приезд 2 июня» — так пишут в живых заявках. Слово «Приезд» короче и
    # точнее «Готов выехать»: заказчику важно, когда человек будет у него, а не
    # когда тронется.
    if iso and precision == "exact":
        return f"Приезд {human}"
    if iso:
        return f"Приезд ориентировочно {human}" + (f" (со слов: «{raw}»)" if raw else "")
    if raw:
        return f"Приезд уточняется (со слов: «{raw}»)"
    return "Приезд уточняется"


# Кого показываем людям. Согласие — не единственный признак интереса: тот, кто
# просил перезвонить, думает или кому не подошла вакансия, хочет работать ровно
# так же — мы просто не нашли ему места. Для менеджера это лид. Мусор здесь
# только отказ, чужой номер, автоответчик и уже трудоустроенный.
INTERESTED = tuple(bitrix.AGREED) + ("thinking", "callback", "vacancy_mismatch",
                                     "human_recruiter_needed")


# Ниже этого числа реплик разговора не было. Четыре — это «алло», приветствие
# агента, ответ человека и следующий вопрос: минимум, на котором вообще можно
# что-то узнать. Порог нужен потому, что итог ставится и по обрывку: десять
# секунд и две реплики уже приезжали в воронку как «думает», и менеджер
# получал карточку, в которой нет ни имени, ни города — звонить не с чем.
MIN_TURNS_FOR_LEAD = 4


# Итоги, которые проходят без порога по длине разговора.
#
# Согласие — потому что человек сказал «да», и терять это из-за формальности
# нельзя. Просьба перезвонить — потому что она почти всегда и звучит коротко:
# «я сейчас не могу, перезвоните» это две реплики и семь секунд. Такие
# разговоры порог отсекал, и люди оставались вообще без следа: карточки нет,
# лида нет, перезвонить некому. За сентябрь так потерялось полтора десятка
# человек, которые сами просили с ними связаться.
ALWAYS_WORTH = tuple(bitrix.AGREED) + ("callback",)


def _worth_a_lead(collected: dict, conversation: dict) -> bool:
    """Стоит ли показывать этот разговор людям."""
    status = str(collected.get("call_status") or "")
    if status in ALWAYS_WORTH:
        return True
    if status not in INTERESTED:
        return False
    return len(conversation.get("transcript") or []) >= MIN_TURNS_FOR_LEAD


def _lead_body(conn, phone: str, position_id: str, channel: str,
               collected: dict, talk: str = "") -> dict:
    """Карточка лида — одна и та же для чата и для воронки.

    Общая намеренно. Раньше её собирали два разных места, и одно и то же поле
    успело называться там «профессия», а тут «специальность»; строка молча
    исчезала из карточки, и поймать это можно было только глазами.

    Про готовность к вахте и переезду. Агент проставляет их и тогда, когда не
    спрашивал, — тогда выходит «нет». Отличить такое «нет» от настоящего
    отказа нечем, поэтому пишем только «да»: утверждение, которого человек не
    делал, стоит менеджеру ненабранного номера.
    """
    vacancy = conn.execute(
        """SELECT vacancy_name, object_name,
                  COALESCE(NULLIF(city, ''), region, '') AS place, shift_rate
           FROM positions WHERE position_id = ?""", (position_id,)
    ).fetchone()
    person = conn.execute(
        """SELECT name, age, citizenship, profession, objections, questions,
                  desired_salary, desired_schedule, start_date, next_action,
                  summary, interest, status, callback_at, calls_count,
                  has_medical_book, has_criminal_record, criminal_article,
                  ready_for_shift, ready_to_move,
                  COALESCE(NULLIF(city, ''), region, '') AS place
           FROM candidates WHERE phone = ?""", (phone,)
    ).fetchone()

    def field(name, default=""):
        return (person[name] if person else default) or default

    ready = [text for text, flag in (("на вахту", field("ready_for_shift", 0)),
                                     ("к переезду", field("ready_to_move", 0))) if flag]

    return {
        "имя": field("name"),
        "телефон": phone,
        "канал": channel,
        "возраст": field("age"),
        "гражданство": field("citizenship"),
        "откуда": field("place"),
        "профессия": field("profession"),
        "готов": ", ".join(ready),
        "медкнижка": {1: "есть", 0: "нет"}.get(field("has_medical_book", None), ""),
        "судимость": (field("criminal_article") or "есть") if field("has_criminal_record", 0)
                     else ("нет" if field("has_criminal_record", None) == 0 else ""),
        "хочет получать": field("desired_salary"),
        "график": field("desired_schedule"),
        "готов выйти": field("start_date") or str(collected.get("available_start_date") or ""),
        "перезвонить": field("callback_at"),
        "возражения": field("objections"),
        "вопросы": field("questions"),
        "что дальше": field("next_action"),
        "пересказ": field("summary") or str(collected.get("summary") or ""),
        "интерес": field("interest") or str(collected.get("candidate_interest") or ""),
        "итог": field("status") or str(collected.get("call_status") or ""),
        "звонков": field("calls_count", 0),
        # Расшифровка едет в карточку чата, но показывается только когда
        # показывать больше нечего. Бывает так: человек назвал имя, возраст и
        # город, сказал «подумаю» — и всё, менеджер видит четыре строки и не
        # знает, о чём с ним говорить. Сам разговор в таком случае полезнее
        # любого поля. Хвост, а не начало: решение человек произносит в конце.
        "разговор": (talk or "")[-1800:],
        "вакансия": (vacancy["vacancy_name"] if vacancy else "") or "",
        "код": position_id,
        "объект": (vacancy["object_name"] if vacancy else "") or "",
        "город": (vacancy["place"] if vacancy else "") or "",
        "ставка": (vacancy["shift_rate"] if vacancy else "") or "",
    }


def _queue_lead(conn, phone: str, body: dict, now: str) -> None:
    """Кладёт лид в общий чат.

    Молчим, если чат не заведён. Лид — вещь полезная, но не обязательная, и
    ронять из-за неё постобработку звонка нельзя: без неё пропадёт карточка
    кандидата, а это уже потеря.
    """
    rule = conn.execute(
        """SELECT chat_id, channel FROM sb_chats
           WHERE match_type = 'leads' AND is_active = 1 LIMIT 1"""
    ).fetchone()
    if rule is None or not rule["chat_id"]:
        return

    conn.execute(
        """INSERT INTO candidate_messages
           (phone, channel, kind, position_id, body, chat_id, created_at, updated_at)
           VALUES (?, ?, 'lead', ?, ?, ?, ?, ?)""",
        (phone, rule["channel"], body.get("код", ""),
         json.dumps(body, ensure_ascii=False), rule["chat_id"], now, now),
    )


def _e164(raw: str) -> str:
    """Российский номер в виде +7XXXXXXXXXX. Пусто — если это не номер.

    Пусто, а не «как есть»: неопознанная строка в поле телефона заводит
    кандидата-двойника и тихо ломает отправку, и лучше пропустить её вовсе,
    оставшись с тем номером, который мы набирали.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return ""


def _call_facts(conversation: dict) -> dict:
    """Что из разговора едет в ленту лида: текст, дата и длительность.

    Дата с длительностью не украшение: обещание «выйду через неделю», данное
    три недели назад, значит совсем не то же самое, что вчерашнее, а по одной
    расшифровке возраст разговора не виден.
    """
    metadata = conversation.get("metadata") or {}
    started = ""
    stamp = metadata.get("start_time_unix_secs")
    if stamp:
        try:
            started = datetime.fromtimestamp(int(stamp)).strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError, OSError):
            started = ""
    return {
        "расшифровка": _call_transcript(conversation),
        "длительность": metadata.get("call_duration_secs") or 0,
        "когда": started,
    }


def _queue_crm(conn, phone: str, body: dict, now: str, call=None) -> None:
    """Ставит лид в очередь на отправку в Битрикс.

    Отложенно, через таблицу: постобработка звонка не должна ни ждать портал,
    ни падать из-за него. Карточка кандидата важнее, а недоехавший лид можно
    дослать — обратный порядок означал бы, что минута недоступности Битрикса
    стоит нам потерянного звонка.
    """
    payload = dict(body)
    payload.update(call or {})

    # Тот же человек по той же вакансии — тот же лид, сколько бы раз он ни
    # перезвонил. Обновляем содержимое, но не создаём второй.
    conn.execute(
        """INSERT INTO crm_leads (phone, position_id, payload, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(phone, position_id) DO UPDATE SET
             payload = excluded.payload, updated_at = excluded.updated_at""",
        (phone, payload.get("код", ""), json.dumps(payload, ensure_ascii=False), now, now),
    )


def _collected(conversation: dict) -> dict:
    """Плоский словарь собранных агентом полей."""
    results = ((conversation.get("analysis") or {}).get("data_collection_results")) or {}
    return {key: (item or {}).get("value") for key, item in results.items()}


def _call_transcript(conversation: dict) -> str:
    lines = []
    for turn in conversation.get("transcript") or []:
        message = turn.get("message")
        if not message:
            continue
        who = "Агент" if turn.get("role") == "agent" else "Кандидат"
        lines.append(who + ": " + message)
    return "\n".join(lines)


@app.post("/api/agent/candidates/call")
async def api_agent_candidate_call(
        payload: AgentCallRequest,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Постобработка звонка: карточка кандидата, история, очередь сообщений.

    Вызывает звонилка сразу после разговора. Номер берётся из её поля, а не из
    разговора: в режиме релея ElevenLabs видит только добавочный 710 и
    настоящего номера кандидата не знает в принципе. Единственное исключение —
    кандидат вслух продиктовал ДРУГОЙ номер, тогда агент кладёт его в поле
    contact_phone_override.

    Идемпотентно по conversation_id: повторный пуш не задваивает ни историю,
    ни отправку карточки.
    """
    import asyncio

    conversation = payload.conversation
    conversation_id = conversation.get("conversation_id") or ""
    if not conversation_id:
        raise HTTPException(400, "в разговоре нет conversation_id")

    collected = _collected(conversation)
    # Продиктованный вслух номер приводим к единому виду. Агент записывает его
    # как услышал — «9532647070», «8 953…», с пробелами, — и такой ключ заводит
    # второго кандидата на того же человека, а мессенджер на него не отправит
    # ничего: ни Telegram, ни WhatsApp номер без кода страны не принимают.
    phone = _e164(collected.get("contact_phone_override") or "") or payload.phone.strip()
    if not phone:
        raise HTTPException(400, "не передан номер кандидата")

    metadata = conversation.get("metadata") or {}
    analysis = conversation.get("analysis") or {}
    now = datetime.now().isoformat(timespec="seconds")

    def _store() -> dict:
        with registry_db.connect() as conn:
            seen = conn.execute(
                "SELECT 1 FROM candidate_calls WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()
            if seen:
                return {"ok": True, "phone": phone, "duplicate": True, "message_id": None}

            conn.execute(
                "INSERT OR IGNORE INTO candidates (phone, created_at, updated_at) VALUES (?, ?, ?)",
                (phone, now, now),
            )

            # Пустые значения не затирают уже известное: в коротком звонке агент
            # соберёт три поля из шестнадцати, и обнулять ими карточку, набранную
            # прошлыми разговорами, нельзя.
            updates, params = [], []
            for field, column in _CALL_FIELDS_TEXT.items():
                value = collected.get(field)
                if value is None or str(value).strip() == "":
                    continue
                updates.append(column + " = ?")
                params.append(str(value).strip())
            for field, column in _CALL_FIELDS_BOOL.items():
                value = collected.get(field)
                if value is None:
                    continue
                updates.append(column + " = ?")
                params.append(1 if value else 0)

            has_record, article = _criminal(collected.get("criminal_record"))
            if has_record is not None:
                updates.append("has_criminal_record = ?")
                params.append(has_record)
            if article:
                updates.append("criminal_article = ?")
                params.append(article)

            updates.append("calls_count = calls_count + 1")
            updates.append("updated_at = ?")
            params.append(now)
            params.append(phone)
            conn.execute("UPDATE candidates SET " + ", ".join(updates) + " WHERE phone = ?", params)

            conn.execute(
                """INSERT INTO candidate_calls
                   (phone, conversation_id, started_at, duration_secs, call_successful,
                    termination_reason, status, transcript, payload, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    phone, conversation_id,
                    str(metadata.get("start_time_unix_secs") or ""),
                    metadata.get("call_duration_secs"),
                    str(analysis.get("call_successful") or ""),
                    str(metadata.get("termination_reason") or ""),
                    str(collected.get("call_status") or ""),
                    _call_transcript(conversation),
                    json.dumps(conversation, ensure_ascii=False),
                    now,
                ),
            )

            # Карточку ставим в очередь, только если человек выбрал мессенджер
            # и агент дошёл до конкретной вакансии. Нет одного из двух — слать
            # нечего или некуда.
            channel = (collected.get("preferred_channel") or "").strip().lower()
            position_id = (collected.get("recommended_vacancy_id") or "").strip()
            queued = None
            if channel in ("telegram", "whatsapp") and position_id:
                card = build_card(conn, position_id)
                if card is None:
                    logger.warning(
                        "[candidates] позиция " + position_id + " не найдена, карточка не поставлена"
                    )
                else:
                    cursor = conn.execute(
                        """INSERT INTO candidate_messages
                           (phone, channel, kind, position_id, body, created_at, updated_at)
                           VALUES (?, ?, 'vacancy_card', ?, ?, ?, ?)""",
                        (phone, channel, position_id,
                         json.dumps(card, ensure_ascii=False), now, now),
                    )
                    queued = cursor.lastrowid
                    # С этой минуты идёт отсчёт молчания: карточка ушла, и
                    # дальше мы ждём от человека документы. Без отметки
                    # напоминать было бы не от чего.
                    conn.execute(
                        """UPDATE candidates
                           SET stage = 'card_sent', stage_since = ?,
                               reminders_sent = 0, last_reminder_at = ''
                           WHERE phone = ?""",
                        (now, phone),
                    )

            # Лид людям — за пределами ветки с карточкой. Выбор мессенджера
            # ничего не говорит об интересе: попросивший перезвонить хочет
            # работать ровно так же, а внутри ветки он терялся бы молча.
            if _worth_a_lead(collected, conversation):
                facts = _call_facts(conversation)
                body = _lead_body(conn, phone, position_id, channel, collected,
                                  talk=facts.get("расшифровка") or "")
                _queue_lead(conn, phone, body, now)
                _queue_crm(conn, phone, body, now, facts)
            conn.commit()
            return {"ok": True, "phone": phone, "duplicate": False, "message_id": queued}

    result = await asyncio.to_thread(_store)
    if result.get("duplicate"):
        logger.info("[candidates] " + phone + ": разговор " + conversation_id + " уже сохранён")
    else:
        logger.info(
            "[candidates] " + phone
            + " статус=" + str(collected.get("call_status"))
            + " канал=" + str(collected.get("preferred_channel"))
            + " сообщение=" + str(result.get("message_id"))
        )
    return JSONResponse(result)


@app.post("/api/agent/peers")
async def api_agent_peer_link(
        link: PeerLink,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Записывает соответствие собеседника и телефона.

    Вызывается в момент отправки, пока связь ещё известна. Позже её взять
    неоткуда: Telegram-драйвер удаляет импортированный контакт сразу после
    сообщения, и ответ кандидата приходит от идентификатора без номера.
    """
    import asyncio

    def _store() -> None:
        with registry_db.connect() as conn:
            conn.execute(
                """INSERT INTO channel_peers (channel, peer, phone, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(channel, peer) DO UPDATE SET phone = excluded.phone""",
                (link.channel, link.peer, link.phone,
                 datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()

    await asyncio.to_thread(_store)
    return JSONResponse({"ok": True})


@app.post("/api/agent/inbound")
async def api_agent_inbound(
        message: InboundMessage,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Принимает входящее сообщение кандидата.

    Телефон восстанавливаем по собеседнику, если канал его не дал. Не нашли —
    строку всё равно сохраняем: потерять присланный человеком паспорт из-за
    того, что не сматчили номер, хуже, чем разобрать это потом руками.

    Повторы отсекаются по паре «канал + идентификатор сообщения»: мессенджеры
    переприсылают события при переподключении.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _store() -> dict:
        with registry_db.connect() as conn:
            phone = message.phone.strip()
            if not phone and message.peer:
                row = conn.execute(
                    "SELECT phone FROM channel_peers WHERE channel = ? AND peer = ?",
                    (message.channel, message.peer),
                ).fetchone()
                if row:
                    phone = row["phone"]

            if message.external_id:
                seen = conn.execute(
                    "SELECT inbound_id FROM candidate_inbound WHERE channel = ? AND external_id = ?",
                    (message.channel, message.external_id),
                ).fetchone()
                if seen:
                    return {"ok": True, "duplicate": True, "inbound_id": seen["inbound_id"],
                            "phone": phone, "matched": bool(phone)}

            cursor = conn.execute(
                """INSERT INTO candidate_inbound
                   (phone, channel, peer, external_id, kind, text,
                    media_type, media_size, media_sha256, media_path,
                    received_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (phone, message.channel, message.peer, message.external_id,
                 message.kind, message.text, message.media_type, message.media_size,
                 message.media_sha256, message.media_path,
                 message.received_at or now, now),
            )
            conn.commit()
            return {"ok": True, "duplicate": False, "inbound_id": cursor.lastrowid,
                    "phone": phone, "matched": bool(phone)}

    result = await asyncio.to_thread(_store)
    if not result["duplicate"]:
        who = result["phone"] or f"{message.channel}:{message.peer} (номер не сопоставлен)"
        logger.info(f"[inbound] {who}: {message.kind} {message.text[:60]!r}")
    return JSONResponse(result)


@app.get("/api/agent/inbound")
async def api_agent_inbound_list(
        status: str = "new",
        limit: int = 50,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Неразобранные входящие. Читает тот, кто будет вести диалог."""
    import asyncio

    take = max(1, min(limit, 200))

    def _read() -> list:
        with registry_db.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT inbound_id, phone, channel, peer, kind, text,
                          media_type, media_size, media_path, received_at
                   FROM candidate_inbound WHERE status = ?
                   ORDER BY received_at LIMIT ?""",
                (status, take),
            ).fetchall()
            return [dict(row) for row in rows]

    return JSONResponse({"messages": await asyncio.to_thread(_read)})


@app.post("/api/agent/inbound/{inbound_id}")
async def api_agent_inbound_status(
        inbound_id: int,
        update: InboundStatus,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Помечает входящее разобранным. Без этого оно разбиралось бы вечно."""
    import asyncio

    def _mark() -> bool:
        with registry_db.connect() as conn:
            cursor = conn.execute(
                "UPDATE candidate_inbound SET status = ? WHERE inbound_id = ?",
                (update.status, inbound_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    if not await asyncio.to_thread(_mark):
        raise HTTPException(404, "входящее не найдено")
    return JSONResponse({"ok": True})


# --- присланные файлы --------------------------------------------------------
#
# Хранилище лежит рядом с базой, в том же volume: переживает пересборку
# контейнера. Файлами, а не колонкой в базе — паспорт в SQLite не кладут,
# база целиком уезжает в бэкап, и удалить оттуда одну картинку по требованию
# человека нельзя, а файл удаляется одной строкой.
MEDIA_STORE = os.getenv("MEDIA_STORE_DIR", "").strip() or os.path.join(
    os.path.dirname(registry_db.DEFAULT_DB_PATH) or ".", "candidate_media")

# Фотография паспорта с телефона весит 2–6 МБ, скан в PDF редко больше десяти.
# Всё, что крупнее, — либо ошибка, либо попытка забить диск.
MEDIA_MAX_BYTES = 25 * 1024 * 1024

# Расширение берём по типу, а не по присланному имени: имя приходит снаружи,
# и класть на диск «паспорт.jpg.php» ради удобства мы не станем.
MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "application/pdf": ".pdf",
}


def _media_file(name: str) -> str:
    """Путь к файлу в хранилище. Только имя, без каталогов из базы."""
    return os.path.join(MEDIA_STORE, os.path.basename(name or ""))


def _forget_media(conn, inbound_id) -> bool:
    """Стирает копию файла у навигатора и забывает о ней в базе.

    Сначала файл, потом запись — по той же причине, что и у мессенджера:
    обратный порядок при сбое оставил бы паспорт на диске без единого
    упоминания в базе.
    """
    row = conn.execute(
        "SELECT media_file FROM candidate_inbound WHERE inbound_id = ?", (inbound_id,)
    ).fetchone()
    name = (row["media_file"] if row else "") or ""
    if not name:
        return False
    try:
        os.remove(_media_file(name))
    except FileNotFoundError:
        pass  # уже стёрли руками — запись всё равно надо почистить
    except OSError as exc:
        logger.error(f"[media] не стёр {name}: {exc}")
        return False
    conn.execute(
        "UPDATE candidate_inbound SET media_file = '' WHERE inbound_id = ?", (inbound_id,)
    )
    return True


@app.post("/api/agent/inbound/{inbound_id}/media")
async def api_agent_inbound_media(
        inbound_id: int,
        file: UploadFile = File(...),
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Принимает сам файл, а не путь к нему.

    Раньше мессенджер присылал только media_path — путь на СВОЁМ диске.
    Навигатор его записывал, а менеджер в переписке видел вместо паспорта
    строку «открывается в мессенджере»: файл лежал на другой машине, отдать
    его браузеру было неоткуда. То есть документ, ради которого вся переписка
    и затевалась, до человека не доезжал.

    Имя файла на диске — номер входящего: он уникален, а повторная присылка
    того же сообщения перезапишет файл вместо того, чтобы наплодить копий.
    """
    import asyncio
    import hashlib

    def _kind() -> str:
        with registry_db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT media_type FROM candidate_inbound WHERE inbound_id = ?",
                (inbound_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "входящее не найдено")
        return row["media_type"] or ""

    known_type = await asyncio.to_thread(_kind)
    media_type = (known_type or file.content_type or "").split(";")[0].strip().lower()

    # Читаем кусками и считаем размер по ходу: read() целиком принял бы в
    # память сколько угодно раньше, чем мы успели бы отказать.
    digest = hashlib.sha256()
    size = 0
    parts: List[bytes] = []
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        size += len(chunk)
        if size > MEDIA_MAX_BYTES:
            raise HTTPException(413, f"файл больше {MEDIA_MAX_BYTES // (1024 * 1024)} МБ")
        digest.update(chunk)
        parts.append(chunk)
    if not size:
        raise HTTPException(400, "пустой файл")

    def _store() -> str:
        os.makedirs(MEDIA_STORE, exist_ok=True)
        name = f"{inbound_id}{MEDIA_SUFFIXES.get(media_type, '.bin')}"
        target = _media_file(name)
        # Пишем во временный и переименовываем: менеджер не должен однажды
        # открыть наполовину долитую фотографию.
        tmp = target + ".part"
        with open(tmp, "wb") as handle:
            for part in parts:
                handle.write(part)
        os.chmod(tmp, 0o600)  # это чужой паспорт, а не картинка сайта
        os.replace(tmp, target)
        with registry_db.connect() as conn:
            conn.execute(
                """UPDATE candidate_inbound
                   SET media_file = ?, media_sha256 = ?,
                       media_type = CASE WHEN media_type = '' THEN ? ELSE media_type END,
                       media_size = COALESCE(media_size, ?)
                   WHERE inbound_id = ?""",
                (name, digest.hexdigest(), media_type, size, inbound_id),
            )
            conn.commit()
        return name

    name = await asyncio.to_thread(_store)
    logger.info(f"[inbound] №{inbound_id}: файл принят ({size // 1024} КБ) → {name}")
    return JSONResponse({"ok": True, "inbound_id": inbound_id, "file": name, "size": size})


@app.get("/api/agent/media/missing")
async def api_agent_media_missing(
        days: int = 7,
        limit: int = 50,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Входящие, чей файл до навигатора ещё не доехал.

    Страховка на случай, когда в момент приёма навигатор лежал или мессенджер
    перезапустили между приёмом сообщения и отправкой файла. Без неё одна
    неудачная попытка навсегда оставляла бы менеджера без фотографии, а узнали
    бы мы об этом от него же — через день.

    Старше срока хранения не просим: эти файлы и так подлежат удалению.
    """
    import asyncio

    edge = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    take = max(1, min(limit, 200))

    def _read() -> list:
        with registry_db.connect(readonly=True) as conn:
            return [
                {"inbound_id": r["inbound_id"], "path": r["media_path"]}
                for r in conn.execute(
                    """SELECT inbound_id, media_path FROM candidate_inbound
                       WHERE media_path != '' AND media_file = '' AND received_at >= ?
                       ORDER BY received_at LIMIT ?""", (edge, take))
            ]

    return JSONResponse({"files": await asyncio.to_thread(_read)})


@app.get("/api/agent/dialogue/{phone}")
async def api_agent_dialogue(
        phone: str,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Всё, что нужно для ответа кандидату, одним запросом.

    Одним, а не пятью: диалог отвечает человеку, который ждёт, и пять походов
    по сети на каждое сообщение — это секунды, которые он видит как задержку.
    """
    import asyncio

    def _read() -> dict:
        with registry_db.connect(readonly=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE phone = ?", (phone,)
            ).fetchone()
            if candidate is None:
                raise HTTPException(404, "кандидат не найден")

            documents = [dict(row) for row in conn.execute(
                """SELECT kind, outcome, full_name, birth_date, sex, number,
                          confidence, problems, media_path, created_at
                   FROM candidate_documents WHERE phone = ?
                   ORDER BY created_at""",
                (phone,),
            )]

            # Карточка вакансии нужна модели, чтобы отвечать на вопросы про
            # условия по данным реестра, а не по памяти. Внутреннего в ней нет:
            # project() отдаёт только то, что можно назвать вслух.
            vacancy = None
            # Что просить у этого кандидата. Базовый набор один на всех, а
            # сверх него — то, что требует конкретный контрагент: Маркет Стафф
            # без фото по пояс не отправляет на проверку вовсе. Спрашивать это
            # надо вместе с паспортом, а не после возврата заявки.
            required = list(DEFAULT_DOCS)
            if candidate["last_position_id"]:
                vacancy = build_card(conn, candidate["last_position_id"])
                position = conn.execute(
                    "SELECT counterparty, source FROM positions WHERE position_id = ?",
                    (candidate["last_position_id"],),
                ).fetchone()
                if position is not None:
                    rule = resolve_sb_chat(conn, position["counterparty"] or "",
                                           position["source"] or "")
                    for kind in ((rule or {}).get("extra_docs") or "").split(","):
                        kind = kind.strip()
                        if kind and kind not in required:
                            required.append(kind)

            return {
                "phone": phone,
                "required_docs": required,
                "name": candidate["name"] or "",
                "stage": candidate["stage"] or "",
                "stage_since": candidate["stage_since"] or "",
                "reminders_sent": candidate["reminders_sent"] or 0,
                "last_reminder_at": candidate["last_reminder_at"] or "",
                "needs_human": bool(candidate["needs_human"]),
                "preferred_channel": candidate["preferred_channel"] or "",
                "position_id": candidate["last_position_id"] or "",
                "departure": {
                    "raw": candidate["departure_raw"] or "",
                    "date": candidate["departure_date"] or "",
                    "precision": candidate["departure_precision"] or "",
                    "asks": candidate["departure_asks"] or 0,
                },
                "documents": documents,
                "vacancy": vacancy,
            }

    return JSONResponse(await asyncio.to_thread(_read))


@app.post("/api/agent/documents")
async def api_agent_document(
        verdict: DocumentVerdict,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Записывает разбор присланного файла.

    Принятый документ заменяет прежний того же вида: человек мог прислать
    паспорт трижды, и в работу должен идти последний годный. Неудачные попытки
    копятся — по ним считается, сколько раз мы уже просили переснять.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _store() -> dict:
        with registry_db.connect() as conn:
            if verdict.outcome == "accepted":
                conn.execute(
                    "DELETE FROM candidate_documents WHERE phone = ? AND kind = ? AND outcome = 'accepted'",
                    (verdict.phone, verdict.kind),
                )
            cursor = conn.execute(
                """INSERT INTO candidate_documents
                   (phone, kind, outcome, full_name, birth_date, sex, number,
                    confidence, problems, media_path, inbound_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (verdict.phone, verdict.kind, verdict.outcome, verdict.full_name,
                 verdict.birth_date, verdict.sex, verdict.number, verdict.confidence,
                 verdict.problems, verdict.media_path, verdict.inbound_id, now),
            )
            retries = conn.execute(
                """SELECT COUNT(*) FROM candidate_documents
                   WHERE phone = ? AND kind = ? AND outcome = 'retry'""",
                (verdict.phone, verdict.kind),
            ).fetchone()[0]
            conn.commit()
            return {"ok": True, "document_id": cursor.lastrowid, "retries": retries}

    result = await asyncio.to_thread(_store)
    logger.info(f"[документ] {verdict.phone}: {verdict.kind} → {verdict.outcome}")
    return JSONResponse(result)


@app.post("/api/agent/candidates/{phone}/stage")
async def api_agent_stage(
        phone: str,
        update: StageUpdate,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Переводит кандидата на другую стадию переписки."""
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _set() -> dict:
        with registry_db.connect() as conn:
            fields = ["stage = ?", "stage_since = ?", "updated_at = ?"]
            values = [update.stage, now, now]
            if update.reset_reminders:
                fields += ["reminders_sent = 0", "last_reminder_at = ''"]
            if update.needs_human is not None:
                fields.append("needs_human = ?")
                values.append(1 if update.needs_human else 0)
            values.append(phone)
            cursor = conn.execute(
                f"UPDATE candidates SET {', '.join(fields)} WHERE phone = ?", values
            )
            conn.commit()
            return {"ok": cursor.rowcount > 0, "stage": update.stage}

    result = await asyncio.to_thread(_set)
    if not result["ok"]:
        raise HTTPException(404, "кандидат не найден")
    logger.info(f"[стадия] {phone}: {update.stage}")
    return JSONResponse(result)


@app.post("/api/agent/outbox")
async def api_agent_outbox_put(
        message: OutboxMessage,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Кладёт готовый текст в очередь отправки.

    Текст готовый, а не данные: остальные виды сообщений собираются из полей
    уже в мессенджере, но ответ в диалоге рождается там же, где принимается
    решение, и пересобирать его по частям было бы лишним кругом.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _put() -> int:
        with registry_db.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO candidate_messages
                   (phone, channel, kind, position_id, body, chat_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, '', ?, ?)""",
                (message.phone, message.channel, message.kind, message.position_id,
                 json.dumps({"text": message.text}, ensure_ascii=False), now, now),
            )
            conn.commit()
            return cursor.lastrowid

    message_id = await asyncio.to_thread(_put)
    return JSONResponse({"ok": True, "message_id": message_id})


@app.get("/api/agent/silent")
async def api_agent_silent(
        first_hours: int = 4,
        next_hours: int = 20,
        limit: int = 20,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Кому пора напомнить: молчит дольше порога и напоминали меньше двух раз.

    Два напоминания и всё. Третье — уже давление, после которого человек
    перестаёт отвечать совсем, а не начинает.

    Порог считается от последнего события: от ухода карточки, если не
    напоминали, и от прошлого напоминания, если напоминали. Иначе оба
    напоминания ушли бы подряд через четыре часа.
    """
    import asyncio

    now = datetime.now()
    first_before = (now - timedelta(hours=first_hours)).isoformat(timespec="seconds")
    next_before = (now - timedelta(hours=next_hours)).isoformat(timespec="seconds")

    def _read() -> list:
        with registry_db.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT phone FROM candidates
                   WHERE stage IN ('card_sent', 'docs', 'date')
                     AND needs_human = 0
                     AND reminders_sent < 2
                     AND stage_since != ''
                     AND ((reminders_sent = 0 AND stage_since <= ?)
                          OR (reminders_sent = 1 AND last_reminder_at <= ?))
                   ORDER BY stage_since
                   LIMIT ?""",
                (first_before, next_before, max(1, min(limit, 100))),
            ).fetchall()
            return [row["phone"] for row in rows]

    return JSONResponse({"phones": await asyncio.to_thread(_read)})


@app.post("/api/agent/candidates/{phone}/reminded")
async def api_agent_reminded(
        phone: str,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Отмечает, что напоминание ушло. Считает их навигатор, а не мессенджер.

    Считает здесь, потому что мессенджер может перезапуститься между решением
    и отправкой, и тогда счётчик в его памяти обнулился бы, а человек получил
    бы напоминание заново.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _mark() -> int:
        with registry_db.connect() as conn:
            conn.execute(
                """UPDATE candidates
                   SET reminders_sent = reminders_sent + 1, last_reminder_at = ?,
                       updated_at = ?
                   WHERE phone = ?""",
                (now, now, phone),
            )
            row = conn.execute(
                "SELECT reminders_sent FROM candidates WHERE phone = ?", (phone,)
            ).fetchone()
            conn.commit()
            return row["reminders_sent"] if row else 0

    sent = await asyncio.to_thread(_mark)
    return JSONResponse({"ok": True, "reminders_sent": sent})


@app.get("/api/agent/stale_media")
async def api_agent_stale_media(
        days: int = 7,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Файлы, которые пора стереть с диска.

    Паспорта не должны лежать у нас дольше, чем нужно для работы. Неделя —
    окно, в которое заказчик успевает попросить прислать заново, если что-то
    потерял; дальше это просто чужие сканы на чужом сервере.
    """
    import asyncio

    edge = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    def _read() -> list:
        with registry_db.connect(readonly=True) as conn:
            rows = [
                {"source": "documents", "id": r["document_id"], "path": r["media_path"]}
                for r in conn.execute(
                    """SELECT document_id, media_path FROM candidate_documents
                       WHERE media_path != '' AND created_at <= ?""", (edge,))
            ]
            rows += [
                {"source": "inbound", "id": r["inbound_id"], "path": r["media_path"]}
                for r in conn.execute(
                    """SELECT inbound_id, media_path FROM candidate_inbound
                       WHERE media_path != '' AND received_at <= ?""", (edge,))
            ]
            return rows

    return JSONResponse({"files": await asyncio.to_thread(_read), "older_than": edge})


@app.post("/api/agent/stale_media")
async def api_agent_stale_media_done(
        cleaned: dict,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Чистит пути к файлам, которые мессенджер уже стёр с диска.

    Порядок именно такой — сначала файл, потом запись. Обратный порядок при
    сбое оставил бы паспорт на диске без единого упоминания в базе, и найти
    его было бы нечем.

    Заодно стираем СВОЮ копию входящего: с тех пор как файл доезжает до
    навигатора, срок хранения у него общий с мессенджером. Иначе паспорт,
    стёртый у мессенджера через неделю, у нас лежал бы вечно.
    """
    import asyncio

    def _clear() -> int:
        done = 0
        with registry_db.connect() as conn:
            for item in cleaned.get("files") or []:
                table, key = ("candidate_documents", "document_id") \
                    if item.get("source") == "documents" else ("candidate_inbound", "inbound_id")
                if table == "candidate_inbound":
                    _forget_media(conn, item.get("id"))
                cursor = conn.execute(
                    f"UPDATE {table} SET media_path = '' WHERE {key} = ?", (item.get("id"),)
                )
                done += cursor.rowcount
            conn.commit()
        return done

    return JSONResponse({"ok": True, "cleared": await asyncio.to_thread(_clear)})


# --- переписка для менеджеров -------------------------------------------------
#
# Эти ручки живут под обычным входом в навигатор, а не под агентским токеном:
# ими пользуется человек из браузера, и права у него свои.


class ManagerMessage(BaseModel):
    """Сообщение, которое менеджер пишет кандидату руками."""

    text: str
    channel: str = ""


def _chat_rows(conn, phone: str) -> list:
    """Переписка с одним человеком: его сообщения и наши, вперемешку по времени."""
    outgoing = [
        {
            "from": "manager" if row["author"] else "bot",
            "author": row["author"] or "",
            "kind": row["kind"],
            "status": row["status"],
            # До отправки показываем, что сообщение только собрано: текст
            # родится в мессенджере в момент отправки.
            "text": row["sent_text"] or "",
            "at": row["sent_at"] or row["created_at"],
            "error": row["last_error"] or "",
        }
        for row in conn.execute(
            """SELECT kind, status, sent_text, author, sent_at, created_at, last_error
               FROM candidate_messages WHERE phone = ?""", (phone,))
    ]
    incoming = [
        {
            "from": "candidate",
            "author": "",
            "kind": row["kind"],
            "status": row["status"],
            "text": row["text"] or "",
            "at": row["received_at"],
            # Ссылка, если файл у нас есть, и путь мессенджера, если его
            # прислали до того, как файлы стали доезжать: по пути вёрстка
            # честно скажет, что смотреть надо в мессенджере.
            "media": (f"/api/chats/media/{row['inbound_id']}" if row["media_file"]
                      else (row["media_path"] or "")),
            "media_type": row["media_type"] or "",
        }
        for row in conn.execute(
            """SELECT inbound_id, kind, status, text, media_path, media_file,
                      media_type, received_at
               FROM candidate_inbound WHERE phone = ?""", (phone,))
    ]
    return sorted(outgoing + incoming, key=lambda item: item["at"] or "")


@app.get("/api/chats")
async def api_chats(
        query: str = "",
        limit: int = 200,
        _: str = Depends(verify_creds),
) -> JSONResponse:
    """Список кандидатов с последней репликой.

    Показываем всех, а не только написавших: менеджер должен иметь возможность
    написать первым — карточка могла уйти, а человек молчит.
    """
    import asyncio

    def _read() -> list:
        with registry_db.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT phone, name, stage, needs_human, preferred_channel,
                          last_position_id, updated_at
                   FROM candidates ORDER BY updated_at DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()

            chats = []
            for row in rows:
                if query:
                    haystack = f"{row['phone']} {row['name'] or ''}".lower()
                    if query.lower() not in haystack:
                        continue
                last = conn.execute(
                    """SELECT text, received_at AS at, 'candidate' AS side
                       FROM candidate_inbound WHERE phone = ?
                       UNION ALL
                       SELECT sent_text AS text,
                              COALESCE(NULLIF(sent_at, ''), created_at) AS at,
                              CASE WHEN author != '' THEN 'manager' ELSE 'bot' END AS side
                       FROM candidate_messages WHERE phone = ?
                       ORDER BY at DESC LIMIT 1""",
                    (row["phone"], row["phone"]),
                ).fetchone()
                # Непрочитанное — это то, что кандидат прислал, а диалог ещё
                # не разобрал. Для менеджера это и есть «требует внимания».
                unread = conn.execute(
                    "SELECT COUNT(*) FROM candidate_inbound WHERE phone = ? AND status = 'new'",
                    (row["phone"],),
                ).fetchone()[0]
                chats.append({
                    "phone": row["phone"],
                    "name": row["name"] or "",
                    "stage": row["stage"] or "",
                    "needs_human": bool(row["needs_human"]),
                    "channel": row["preferred_channel"] or "",
                    "position_id": row["last_position_id"] or "",
                    "last_text": (last["text"] if last else "") or "",
                    "last_at": (last["at"] if last else "") or row["updated_at"],
                    "last_side": (last["side"] if last else ""),
                    "unread": unread,
                })
            chats.sort(key=lambda item: item["last_at"] or "", reverse=True)
            return chats

    return JSONResponse({"chats": await asyncio.to_thread(_read)})


@app.get("/api/chats/{phone}")
async def api_chat(phone: str, _: str = Depends(verify_creds)) -> JSONResponse:
    """Вся переписка с одним кандидатом."""
    import asyncio

    def _read() -> dict:
        with registry_db.connect(readonly=True) as conn:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE phone = ?", (phone,)
            ).fetchone()
            if candidate is None:
                raise HTTPException(404, "кандидат не найден")
            documents = [
                {"kind": r["kind"], "outcome": r["outcome"], "at": r["created_at"]}
                for r in conn.execute(
                    """SELECT kind, outcome, created_at FROM candidate_documents
                       WHERE phone = ? ORDER BY created_at""", (phone,))
            ]
            return {
                "phone": phone,
                "name": candidate["name"] or "",
                "stage": candidate["stage"] or "",
                "needs_human": bool(candidate["needs_human"]),
                "channel": candidate["preferred_channel"] or "",
                "position_id": candidate["last_position_id"] or "",
                "departure": {
                    "raw": candidate["departure_raw"] or "",
                    "date": candidate["departure_date"] or "",
                    "precision": candidate["departure_precision"] or "",
                },
                "documents": documents,
                "messages": _chat_rows(conn, phone),
            }

    return JSONResponse(await asyncio.to_thread(_read))


@app.get("/api/chats/media/{inbound_id}")
async def api_chat_media(inbound_id: int, _: str = Depends(verify_creds)) -> Response:
    """Отдаёт менеджеру файл, который прислал кандидат.

    Под обычным входом в навигатор, а не открытой ссылкой: это паспорта, и
    ссылка на них, разошедшаяся по перепискам, живёт дольше самих файлов.
    """
    import asyncio

    def _find() -> tuple:
        with registry_db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT kind, media_file, media_type FROM candidate_inbound
                   WHERE inbound_id = ?""", (inbound_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "входящее не найдено")
        return row["media_file"] or "", row["media_type"] or "", row["kind"] or "file"

    name, media_type, kind = await asyncio.to_thread(_find)
    if not name:
        # Либо файл пришёл до того, как мессенджер научился их отдавать, либо
        # срок хранения вышел. Для вёрстки это разные случаи только по тексту.
        raise HTTPException(404, "файла у навигатора нет")
    path = _media_file(name)
    if not os.path.exists(path):
        raise HTTPException(404, "файл удалён")

    return FileResponse(
        path,
        media_type=media_type or "application/octet-stream",
        headers={
            # inline — чтобы фотография открывалась в самой переписке, а не
            # скачивалась к менеджеру на диск лишней копией паспорта.
            "Content-Disposition": f'inline; filename="{kind}-{inbound_id}{os.path.splitext(name)[1]}"',
            "Cache-Control": "private, max-age=300",
        },
    )


@app.post("/api/chats/{phone}")
async def api_chat_send(
        phone: str,
        message: ManagerMessage,
        request: Request,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """Менеджер пишет кандидату. Бот с этого момента молчит.

    Иначе на один вопрос человек получит два ответа — от менеджера и от бота,
    и они почти наверняка разойдутся. Вернуть диалог боту можно кнопкой, это
    осознанное действие, а не побочный эффект.
    """
    import asyncio

    text = " ".join((message.text or "").split("\n"))
    text = (message.text or "").strip()
    if not text:
        raise HTTPException(400, "пустое сообщение")

    now = datetime.now().isoformat(timespec="seconds")

    def _send() -> dict:
        with registry_db.connect() as conn:
            candidate = conn.execute(
                "SELECT preferred_channel, last_position_id FROM candidates WHERE phone = ?",
                (phone,),
            ).fetchone()
            if candidate is None:
                raise HTTPException(404, "кандидат не найден")

            channel = message.channel or candidate["preferred_channel"] or ""
            if channel not in ("telegram", "whatsapp"):
                raise HTTPException(409, "у кандидата не выбран канал связи")

            cursor = conn.execute(
                """INSERT INTO candidate_messages
                   (phone, channel, kind, position_id, body, author, created_at, updated_at)
                   VALUES (?, ?, 'manual', ?, ?, ?, ?, ?)""",
                (phone, channel, candidate["last_position_id"] or "",
                 json.dumps({"text": text}, ensure_ascii=False), user, now, now),
            )
            conn.execute(
                """UPDATE candidates SET stage = 'human', needs_human = 1,
                       stage_since = ?, updated_at = ? WHERE phone = ?""",
                (now, now, phone),
            )
            conn.commit()
            return {"ok": True, "message_id": cursor.lastrowid, "channel": channel}

    result = await asyncio.to_thread(_send)
    logger.info(f"[переписка] {user} → {phone}: {text[:80]}")
    return JSONResponse(result)


@app.post("/api/chats/{phone}/handback")
async def api_chat_handback(
        phone: str,
        user: str = Depends(verify_creds),
) -> JSONResponse:
    """Возвращает диалог боту.

    Стадию восстанавливаем по фактам, а не по памяти: что уже собрано в
    документах и есть ли срок выезда, видно в базе, и по ним однозначно
    понятно, на каком шаге человек остановился.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _back() -> dict:
        with registry_db.connect() as conn:
            candidate = conn.execute(
                "SELECT departure_date FROM candidates WHERE phone = ?", (phone,)
            ).fetchone()
            if candidate is None:
                raise HTTPException(404, "кандидат не найден")

            accepted = {r["kind"] for r in conn.execute(
                """SELECT kind FROM candidate_documents
                   WHERE phone = ? AND outcome = 'accepted'""", (phone,))}
            required = {"passport_main", "passport_registration", "snils"}

            sent = conn.execute(
                "SELECT COUNT(*) FROM sb_requests WHERE phone = ? AND status = 'sent'",
                (phone,),
            ).fetchone()[0]

            if sent:
                stage = "sb"
            elif required <= accepted:
                stage = "date" if not candidate["departure_date"] else "sb"
            elif accepted:
                stage = "docs"
            else:
                stage = "card_sent"

            conn.execute(
                """UPDATE candidates SET stage = ?, needs_human = 0, stage_since = ?,
                       reminders_sent = 0, last_reminder_at = '', updated_at = ?
                   WHERE phone = ?""",
                (stage, now, now, phone),
            )
            conn.commit()
            return {"ok": True, "stage": stage}

    result = await asyncio.to_thread(_back)
    logger.info(f"[переписка] {user} вернул {phone} боту, стадия {result['stage']}")
    return JSONResponse(result)


@app.get("/api/agent/crm")
async def api_agent_crm(_: str = Depends(verify_agent_token)) -> JSONResponse:
    """Что с очередью лидов в Битрикс.

    Отдельная ручка, потому что молчание тут неотличимо от порядка: если
    вебхук не задан или токен протух, лиды копятся, а снаружи ничего не видно.
    """
    import asyncio

    def _read() -> dict:
        with registry_db.connect(readonly=True) as conn:
            rows = list(conn.execute(
                "SELECT status, COUNT(*) n FROM crm_leads GROUP BY status"))
            last = conn.execute(
                """SELECT phone, status, bitrix_id, attempts, last_error, updated_at
                   FROM crm_leads ORDER BY updated_at DESC LIMIT 10""").fetchall()
            return {
                "интеграция включена": bitrix.enabled(),
                "по статусам": {r["status"]: r["n"] for r in rows},
                "последние": [dict(r) for r in last],
            }

    return JSONResponse(await asyncio.to_thread(_read))


@app.post("/api/agent/crm/deliver")
async def api_agent_crm_deliver(_: str = Depends(verify_agent_token)) -> JSONResponse:
    """Досылает очередь прямо сейчас, не дожидаясь расписания."""
    import asyncio

    def _run() -> dict:
        with registry_db.connect() as conn:
            return bitrix.deliver_pending(conn)

    return JSONResponse(await asyncio.to_thread(_run))


# Владелец служебных сообщений в очереди. Номер с кодом 000 не существует,
# так что набрать его нельзя даже по ошибке.
SERVICE_PHONE = "+70000000000"


class Announce(BaseModel):
    text: str
    # Пинговать участников чата. На старте прогона — да: менеджеры должны
    # узнать, что сейчас придут лиды, до того как те придут. На сводке в конце
    # — нет: она информационная, и дёргать ею всех значит приучить к тому, что
    # упоминание можно игнорировать.
    ping: bool = False


@app.post("/api/agent/announce")
async def api_agent_announce(
        request: Announce,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Сообщение от робота в общий чат менеджеров.

    Нужна потому, что обзвон живёт на другой машине и сессии мессенджера у
    него нет: написать в чат он может только через эту очередь. Адрес берём
    из той же настройки, что и лиды, — отдельный «чат для оповещений» означал
    бы, что однажды он разойдётся с чатом, куда реально смотрят.
    """
    import asyncio

    text = (request.text or "").strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")

    def _queue() -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        with registry_db.connect() as conn:
            rule = conn.execute(
                """SELECT chat_id, channel FROM sb_chats
                   WHERE match_type = 'leads' AND is_active = 1 LIMIT 1"""
            ).fetchone()
            if rule is None or not rule["chat_id"]:
                return {"ok": False, "reason": "чат для лидов не заведён"}
            # Очередь сообщений ссылается на кандидата внешним ключом, а у
            # оповещения кандидата нет: оно про прогон, а не про человека.
            # Поэтому держим одну служебную запись-владельца. Телефон у неё
            # заведомо ненабираемый — код 000 не существует, и если такая
            # строка однажды утечёт в обзвон, звонок просто не состоится.
            conn.execute(
                """INSERT OR IGNORE INTO candidates (phone, created_at, updated_at,
                                                     name, status)
                   VALUES (?, ?, ?, 'Жанна (служебное)', 'service')""",
                (SERVICE_PHONE, now, now),
            )
            cur = conn.execute(
                """INSERT INTO candidate_messages
                   (phone, channel, kind, position_id, body, chat_id,
                    created_at, updated_at)
                   VALUES (?, ?, 'announce', '', ?, ?, ?, ?)""",
                (SERVICE_PHONE, rule["channel"],
                 json.dumps({"text": text, "ping": bool(request.ping)},
                            ensure_ascii=False),
                 rule["chat_id"], now, now),
            )
            conn.commit()
            return {"ok": True, "message_id": cur.lastrowid, "chat_id": rule["chat_id"]}

    result = await asyncio.to_thread(_queue)
    logger.info(f"[announce] {result}")
    return JSONResponse(result)


class LeadPushItem(BaseModel):
    phone: str
    # На что человек откликался. В базе этого нет: отклик живёт в выгрузке
    # площадки, а к нам приезжает только номер. Поэтому принимаем снаружи.
    отклик: str = ""
    перезвонить: str = ""


class LeadPush(BaseModel):
    items: list[LeadPushItem] = []
    to_chat: bool = True
    to_crm: bool = True


def _lead_payload(conn, phone: str, item: LeadPushItem) -> dict:
    """Карточка для ручной отправки: та же, что у автоматической, плюс отклик."""
    person = conn.execute(
        "SELECT last_position_id, preferred_channel FROM candidates WHERE phone = ?",
        (phone,)).fetchone()
    if person is None:
        raise HTTPException(404, f"кандидата {phone} нет в базе")

    payload = _lead_body(conn, phone, person["last_position_id"] or "",
                         person["preferred_channel"] or "", {})
    payload["отклик"] = item.отклик.strip()
    if item.перезвонить.strip():
        payload["перезвонить"] = item.перезвонить.strip()

    # Расшифровка последнего разговора — она уедет в ленту лида Битрикса.
    call = conn.execute(
        """SELECT payload FROM candidate_calls WHERE phone = ?
           ORDER BY created_at DESC LIMIT 1""", (phone,)).fetchone()
    if call:
        try:
            payload.update(_call_facts(json.loads(call["payload"] or "{}")))
        except ValueError:
            logger.warning(f"[leads] {phone}: разговор не читается, шлём без расшифровки")
    return payload


@app.post("/api/agent/leads/push")
async def api_agent_leads_push(
        request: LeadPush,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Отправляет названных кандидатов в чат и в воронку вручную.

    Обычный путь узкий и правильно узкий: сам собой лид уходит только за
    согласившегося. Но человек, который смотрит на результаты обзвона, видит
    то, чего не видит правило: швею, под которую у нас просто нет объекта, или
    того, кто попросил перезвонить в два часа. Таких отправляет он, и тогда
    проверка на «согласен» здесь не нужна — решение уже принято.
    """
    import asyncio

    def _run() -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        done = []
        with registry_db.connect() as conn:
            rule = conn.execute(
                """SELECT chat_id, channel FROM sb_chats
                   WHERE match_type = 'leads' AND is_active = 1 LIMIT 1""").fetchone()

            for item in request.items:
                phone = item.phone.strip()
                payload = _lead_payload(conn, phone, item)
                result = {"телефон": phone, "имя": payload["имя"]}

                if request.to_chat and rule and rule["chat_id"]:
                    cursor = conn.execute(
                        """INSERT INTO candidate_messages
                           (phone, channel, kind, position_id, body, chat_id, created_at, updated_at)
                           VALUES (?, ?, 'lead', ?, ?, ?, ?, ?)""",
                        (phone, rule["channel"], payload["код"],
                         json.dumps(payload, ensure_ascii=False), rule["chat_id"], now, now))
                    result["сообщение"] = cursor.lastrowid
                elif request.to_chat:
                    result["сообщение"] = "чат для лидов не заведён"

                if request.to_crm:
                    # Тот же человек по той же вакансии — тот же лид. Но если
                    # он уже уехал в Битрикс, второй раз не шлём: дубль в
                    # воронке хуже, чем отсутствие повтора.
                    seen = conn.execute(
                        """SELECT crm_id, status, bitrix_id FROM crm_leads
                           WHERE phone = ? AND position_id = ?""",
                        (phone, payload["код"])).fetchone()
                    if seen and seen["status"] == "sent":
                        result["воронка"] = f"уже лид {seen['bitrix_id']}"
                    else:
                        conn.execute(
                            """INSERT INTO crm_leads (phone, position_id, payload, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?)
                               ON CONFLICT(phone, position_id) DO UPDATE SET
                                 payload = excluded.payload, status = 'pending',
                                 attempts = 0, last_error = '', updated_at = excluded.updated_at""",
                            (phone, payload["код"],
                             json.dumps(payload, ensure_ascii=False), now, now))
                        result["воронка"] = "в очереди"
                done.append(result)
            conn.commit()
        return {"отправлено": done}

    return JSONResponse(await asyncio.to_thread(_run))


class CrmRefresh(BaseModel):
    """Какие уже созданные лиды освежить в портале."""

    phones: list[str] = []
    fields: bool = False


@app.post("/api/agent/crm/refresh")
async def api_agent_crm_refresh(
        request: CrmRefresh,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Досылает расшифровку к лидам, которые уже в воронке.

    Обычный путь — приложить её в момент создания, но он не помогает в двух
    случаях: лид создан до того, как расшифровку научились прикладывать, и
    комментарий не прошёл, о чём в очереди осталась пометка. Пересоздавать
    лид ради этого нельзя — в воронке появится дубль, а это хуже, чем
    разговор, которого в ленте нет.

    Карточку лида по умолчанию не трогаем: COMMENTS наше поле, но менеджер
    мог дописать туда своё, и молча затирать его нечем оправдать. Нужно —
    просите явно через fields.
    """
    import asyncio

    def _run() -> dict:
        def digits(raw: str) -> str:
            return "".join(ch for ch in raw if ch.isdigit())[-10:]

        wanted = {digits(p) for p in request.phones if p.strip()}
        done, problems = [], []
        with registry_db.connect() as conn:
            rows = conn.execute(
                """SELECT crm_id, phone, bitrix_id, payload FROM crm_leads
                   WHERE status = 'sent' AND bitrix_id IS NOT NULL""").fetchall()
            for row in rows:
                if wanted and digits(row["phone"]) not in wanted:
                    continue
                lead = json.loads(row["payload"] or "{}")
                try:
                    if request.fields:
                        bitrix.update_fields(row["bitrix_id"], lead)
                    bitrix.attach_transcript(row["bitrix_id"], lead, check_existing=True)
                except Exception as exc:  # noqa: BLE001 — портал, сеть, токен
                    problems.append({"телефон": row["phone"], "ошибка": str(exc)[:200]})
                    continue
                conn.execute(
                    "UPDATE crm_leads SET last_error = '', updated_at = ? WHERE crm_id = ?",
                    (datetime.now().isoformat(timespec="seconds"), row["crm_id"]),
                )
                done.append({"телефон": row["phone"], "лид": row["bitrix_id"]})
            conn.commit()
        return {"обновлено": done, "не вышло": problems}

    return JSONResponse(await asyncio.to_thread(_run))


class CrmBackfill(BaseModel):
    """Какие прошлые разговоры поднять в очередь."""

    phones: list[str] = []
    dry_run: bool = True


@app.post("/api/agent/crm/backfill")
async def api_agent_crm_backfill(
        request: CrmBackfill,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Поднимает согласившихся из уже состоявшихся разговоров.

    Очередь наполняется в момент постобработки звонка, поэтому всё, с кем
    поговорили до появления интеграции — а это весь первый обзвон, — в воронку
    не попало бы никогда. То же самое повторится, если Битрикс отключат на
    неделю: разговоры идут, лиды в них есть, а ставить их в очередь некому.

    По умолчанию только показывает, кого нашла: заливка чужой воронки —
    действие односторонее, и списком лучше сначала посмотреть глазами.
    Список номеров сужает выборку — например, чтобы не тащить в продовую
    воронку собственные тестовые звонки.
    """
    import asyncio

    def _run() -> dict:
        with registry_db.connect() as conn:
            rows = conn.execute(
                """SELECT phone, conversation_id, payload, created_at
                   FROM candidate_calls WHERE status IN (%s)
                   ORDER BY created_at""" % ", ".join("?" * len(INTERESTED)), INTERESTED
            ).fetchall()

            # Сверяем по цифрам: в отборе номер напишут как придётся, а в базе
            # он лежит в одном виде. Ошибиться тут дороже, чем написать четыре
            # лишних слова: не сойдётся — и заливка молча сделает ничего.
            def digits(raw: str) -> str:
                return "".join(ch for ch in raw if ch.isdigit())[-10:]

            wanted = {digits(p) for p in request.phones if p.strip()}
            found, queued = [], 0
            for row in rows:
                if wanted and digits(row["phone"]) not in wanted:
                    continue
                try:
                    conversation = json.loads(row["payload"] or "{}")
                except ValueError:
                    logger.warning(f"[bitrix] {row['phone']}: разговор не читается, пропускаю")
                    continue

                collected = _collected(conversation)
                found.append({
                    "телефон": row["phone"],
                    "статус": str(collected.get("call_status") or ""),
                    "вакансия": str(collected.get("recommended_vacancy_id") or ""),
                    "когда": row["created_at"],
                })
                if request.dry_run:
                    continue
                body = _lead_body(
                    conn, row["phone"],
                    (collected.get("recommended_vacancy_id") or "").strip(),
                    (collected.get("preferred_channel") or "").strip().lower(),
                    collected)
                _queue_crm(conn, row["phone"], body, row["created_at"],
                           _call_facts(conversation))
                queued += 1

            if not request.dry_run:
                conn.commit()
            return {"найдено": found, "поставлено в очередь": queued,
                    "режим": "показ" if request.dry_run else "заливка"}

    return JSONResponse(await asyncio.to_thread(_run))


@app.get("/api/agent/sb_chats")
async def api_agent_sb_chats(_: str = Depends(verify_agent_token)) -> JSONResponse:
    """Все правила адресации. Заодно показывает, для кого чата ещё нет."""
    import asyncio

    def _read() -> dict:
        with registry_db.connect(readonly=True) as conn:
            rules = [dict(r) for r in conn.execute(
                "SELECT * FROM sb_chats ORDER BY match_type, match_value"
            )]
            covered = {r["match_value"] for r in rules if r["match_type"] == "counterparty"}
            sources = {r["match_value"] for r in rules if r["match_type"] == "source"}
            # Контрагенты, по которым реально можно кого-то трудоустроить прямо
            # сейчас: неактивные позиции в чат всё равно не поедут.
            active = [
                (r["counterparty"], r["source"]) for r in conn.execute(
                    """SELECT DISTINCT counterparty, source FROM positions
                       WHERE is_active = 1 AND need_total > 0"""
                )
            ]
            uncovered = sorted({
                cp for cp, srs in active
                if cp and cp not in covered and (srs or "") not in sources
            })
            has_default = any(r["match_type"] == "default" for r in rules)
            return {"rules": rules, "uncovered": uncovered, "has_default": has_default}

    return JSONResponse(await asyncio.to_thread(_read))


@app.post("/api/agent/sb_chats")
async def api_agent_sb_chat_upsert(
        rule: SbChatRule,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Заводит или обновляет правило адресации."""
    import asyncio

    chat_id = normalize_chat_id(rule.chat_id, rule.channel)
    now = datetime.now().isoformat(timespec="seconds")

    def _store() -> None:
        with registry_db.connect() as conn:
            conn.execute(
                """INSERT INTO sb_chats
                   (match_type, match_value, channel, chat_id, title, is_active,
                    extra_docs, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(match_type, match_value) DO UPDATE SET
                     channel = excluded.channel, chat_id = excluded.chat_id,
                     title = excluded.title, is_active = excluded.is_active,
                     extra_docs = excluded.extra_docs,
                     updated_at = excluded.updated_at""",
                (rule.match_type, rule.match_value, rule.channel, chat_id,
                 rule.title, 1 if rule.is_active else 0, rule.extra_docs, now, now),
            )
            conn.commit()

    await asyncio.to_thread(_store)
    return JSONResponse({"ok": True, "chat_id": chat_id, "normalized": chat_id != rule.chat_id})


@app.post("/api/agent/sb_requests")
async def api_agent_sb_request(
        payload: SbRequestCreate,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Собирает заявку в СБ и, если велено, ставит её в очередь отправки.

    По умолчанию заявка создаётся черновиком: пакет собран, текст видно, но
    никуда он не уходит. Отправка — отдельное решение, потому что за ней стоит
    передача персональных данных третьему лицу.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _build() -> dict:
        with registry_db.connect() as conn:
            candidate = conn.execute(
                "SELECT * FROM candidates WHERE phone = ?", (payload.phone,)
            ).fetchone()
            if candidate is None:
                raise HTTPException(404, "кандидат не найден")

            position_id = payload.position_id or candidate["last_position_id"]
            # Заявке нужна сырая позиция, а не проекция для кандидата: в
            # проекции нет объекта — она отдаёт только то, что можно назвать
            # вслух, а объект как раз внутренняя кухня. Заказчику же именно он
            # и нужен, ему в своём чате скрывать от себя нечего.
            position, counterparty, source = None, "", ""
            if position_id:
                position = conn.execute(
                    "SELECT * FROM positions WHERE position_id = ?", (position_id,)
                ).fetchone()
                if position is not None:
                    counterparty = position["counterparty"] or ""
                    source = position["source"] or ""

            # Номер СНИЛС берём из разобранного документа: в живых заявках его
            # пишут прямо в тексте, и проверяющему это избавляет от чтения
            # фотографии ради одиннадцати цифр.
            snils_row = conn.execute(
                """SELECT number FROM candidate_documents
                   WHERE phone = ? AND kind = 'snils' AND outcome = 'accepted'
                     AND number != '' ORDER BY created_at DESC LIMIT 1""",
                (payload.phone,),
            ).fetchone()

            chat = resolve_sb_chat(conn, counterparty, source)
            body = build_sb_package(candidate, position, payload.full_name,
                                    payload.documents,
                                    snils=snils_row["number"] if snils_row else "")

            status = "draft"
            queued = None
            if payload.send:
                if chat is None:
                    raise HTTPException(
                        409,
                        f"нет чата для контрагента {counterparty!r} и источника {source!r}",
                    )
                # Порядок «документы, потом срок, потом отправка» держится не на
                # аккуратности мессенджера, а на этой проверке. Без неё пакет
                # уйдёт со строкой «уточняется», и получатель снова не поставит
                # заезд — то есть ровно та беда, ради которой порядок и меняли.
                if not (candidate["departure_date"] or "").strip() \
                        and not payload.send_without_departure:
                    raise HTTPException(409, {
                        "error": "departure_missing",
                        "message": "срок выезда не записан: спросите кандидата "
                                   "(POST /api/agent/departure) либо пришлите "
                                   "send_without_departure=true",
                        "asks": candidate["departure_asks"],
                    })
                status = "sent"

            cursor = conn.execute(
                """INSERT INTO sb_requests
                   (phone, position_id, counterparty, source, channel, chat_id,
                    body, status, created_at, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload.phone, position_id, counterparty, source,
                 (chat or {}).get("channel", ""), (chat or {}).get("chat_id", ""),
                 body, status, now, now if payload.send else None),
            )
            request_row_id = cursor.lastrowid

            if payload.send:
                # Фотографии документов уходят вместе с текстом. Без них
                # заявка бессмысленна: в живых чатах проверяющие спрашивают
                # «паспорта высылали?», а список названий документов им
                # ничего не даёт — им нужно на них посмотреть.
                files = [
                    row["media_path"] for row in conn.execute(
                        """SELECT media_path FROM candidate_documents
                           WHERE phone = ? AND outcome = 'accepted' AND media_path != ''
                           ORDER BY created_at""", (payload.phone,))
                ]
                cursor = conn.execute(
                    """INSERT INTO candidate_messages
                       (phone, channel, kind, position_id, body, chat_id, created_at, updated_at)
                       VALUES (?, ?, 'sb_request', ?, ?, ?, ?, ?)""",
                    (payload.phone, chat["channel"], position_id,
                     json.dumps({"text": body, "files": files}, ensure_ascii=False),
                     chat["chat_id"], now, now),
                )
                queued = cursor.lastrowid

            conn.commit()
            return {
                "ok": True,
                "request_id": request_row_id,
                "status": status,
                "chat": chat,
                "counterparty": counterparty,
                "source": source,
                "body": body,
                "message_id": queued,
            }

    return JSONResponse(await asyncio.to_thread(_build))


@app.post("/api/agent/departure")
async def api_agent_departure(
        payload: DepartureUpdate,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Записывает срок выезда со слов кандидата.

    Дату спрашивают сразу после сбора документов, не дожидаясь службы
    безопасности: проверка идёт своим ходом, а знать срок надо раньше, иначе
    одобренный кандидат сутки лежит без плана.

    Приблизительный ответ — тоже ответ. «Через неделю» лучше, чем молчание, и
    отказывать в нём нельзя: у человека может не быть точной даты по причинам,
    к нам не относящимся.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _store() -> dict:
        with registry_db.connect() as conn:
            row = conn.execute(
                "SELECT departure_asks FROM candidates WHERE phone = ?", (payload.phone,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "кандидат не найден")

            if payload.asked_only:
                # Счётчик считает заданные вопросы, а не полученные ответы.
                # Иначе молчащий кандидат навсегда остаётся с нулём, и правило
                # «спросили дважды — отпускаем пакет без даты» не сработает
                # ровно в том случае, ради которого оно написано.
                conn.execute(
                    """UPDATE candidates SET departure_asked_at = ?,
                           departure_asks = departure_asks + 1, updated_at = ?
                       WHERE phone = ?""",
                    (now, now, payload.phone),
                )
            else:
                # departure_asked_at здесь не трогаем: оно про момент вопроса.
                # Сдвинув его ответом, мы перезапустили бы отсчёт молчания.
                conn.execute(
                    """UPDATE candidates SET departure_raw = ?, departure_date = ?,
                           departure_precision = ?, updated_at = ?
                       WHERE phone = ?""",
                    (payload.raw, payload.date, payload.precision, now, payload.phone),
                )

            conn.commit()
            return {"ok": True,
                    "asks": row["departure_asks"] + (1 if payload.asked_only else 0),
                    "date": payload.date, "precision": payload.precision}

    result = await asyncio.to_thread(_store)
    if payload.asked_only:
        logger.info(f"[departure] {payload.phone}: спросили про срок "
                    f"(всего {result['asks']})")
    else:
        logger.info(f"[departure] {payload.phone}: {payload.raw!r} → "
                    f"{payload.date or '—'} ({payload.precision})")
    return JSONResponse(result)


@app.post("/api/agent/sb_requests/{request_row_id}/verdict")
async def api_agent_sb_verdict(
        request_row_id: int,
        payload: SbVerdict,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Записывает ответ заказчика и готовит сообщение кандидату.

    Три исхода, а не два. «Непонятно» — это когда в чате ответили что-то, из
    чего вердикт не следует: «а он точно с правами?», «на следующей неделе
    посмотрим». Гадать в такой ситуации нельзя, потому что цена ошибки —
    сказать человеку «вы прошли», когда его не брали. Такие заявки ждут
    человека и кандидату о них ничего не пишут.

    Сообщение не отправляется отсюда, а кладётся в ту же очередь, что и
    карточка вакансии: отправкой занимается мессенджер, и знать про него
    навигатору незачем.
    """
    import asyncio

    now = datetime.now().isoformat(timespec="seconds")

    def _apply() -> dict:
        with registry_db.connect() as conn:
            request = conn.execute(
                "SELECT * FROM sb_requests WHERE request_row_id = ?", (request_row_id,)
            ).fetchone()
            if request is None:
                raise HTTPException(404, "заявка не найдена")

            conn.execute(
                """UPDATE sb_requests SET status = ?, answer_text = ?, answered_by = ?,
                       answered_at = ? WHERE request_row_id = ?""",
                (payload.verdict, payload.answer_text[:2000], payload.answered_by,
                 now, request_row_id),
            )

            if payload.verdict == "unclear":
                conn.commit()
                return {"ok": True, "verdict": payload.verdict, "message_id": None,
                        "note": "ответ неоднозначный, кандидату не пишем"}

            candidate = conn.execute(
                "SELECT * FROM candidates WHERE phone = ?", (request["phone"],)
            ).fetchone()

            vacancy = None
            if request["position_id"]:
                row = conn.execute(
                    "SELECT * FROM positions WHERE position_id = ?", (request["position_id"],)
                ).fetchone()
                if row is not None:
                    city_regions = registry_dicts.load(conn, registry_dicts.KIND_CITY_REGION)
                    vacancy = agent_search.project(row, city_regions)

            # В тело кладём данные, а не готовый текст. Формулировки живут в
            # мессенджере рядом с остальными сообщениями кандидату — иначе они
            # расползутся по двум сервисам и разойдутся между собой.
            body = {
                "имя": (candidate["name"] if candidate else "") or "",
                "вакансия": (vacancy or {}).get("profession", ""),
                "город": (vacancy or {}).get("city") or (vacancy or {}).get("region") or "",
                "дата": (candidate["departure_date"] if candidate else "") or "",
                "точность": (candidate["departure_precision"] if candidate else "") or "",
            }
            kind = "sb_approved" if payload.verdict == "approved" else "sb_rejected"
            channel = (candidate["preferred_channel"] if candidate else "") or ""
            if channel not in ("telegram", "whatsapp"):
                conn.commit()
                return {"ok": True, "verdict": payload.verdict, "message_id": None,
                        "note": "канал связи неизвестен, сообщение не поставлено"}

            cursor = conn.execute(
                """INSERT INTO candidate_messages
                   (phone, channel, kind, position_id, body, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request["phone"], channel, kind, request["position_id"],
                 json.dumps(body, ensure_ascii=False), now, now),
            )
            conn.commit()
            return {"ok": True, "verdict": payload.verdict,
                    "message_id": cursor.lastrowid, "body": body}

    result = await asyncio.to_thread(_apply)
    logger.info(f"[sb] заявка {request_row_id}: {payload.verdict}, "
                f"сообщение {result.get('message_id')}")
    return JSONResponse(result)


@app.get("/api/agent/expected_fields")
async def api_agent_expected_fields(_: str = Depends(verify_agent_token)) -> JSONResponse:
    """Какие поля сбора данных навигатор умеет разложить по карточке кандидата.

    Нужна, чтобы расхождение между настройкой агента и ожиданиями сервера
    ловилось командой, а не обнаруживалось через месяц по пустым колонкам.
    Список живёт здесь, в одном месте с самой раскладкой: дублировать его в
    звонилке значило бы завести вторую правду, которая разойдётся с первой.
    """
    return JSONResponse({
        "text": sorted(_CALL_FIELDS_TEXT),
        "boolean": sorted(_CALL_FIELDS_BOOL),
        # Разбираются отдельно, а не таблицей отображений: судимость приезжает
        # одной строкой и раскладывается на две колонки, номер для связи
        # подменяет тот, по которому звонили.
        "parsed": ["criminal_record", "contact_phone_override"],
    })


@app.get("/api/agent/outbox")
async def api_agent_outbox(
        limit: int = 20,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Выдаёт пачку неотправленных сообщений сервису переписки.

    Забранные сразу переводятся в sending, чтобы два воркера не отправили одно
    и то же дважды. Если сервис упадёт, не отчитавшись, сообщение зависнет в
    sending — это видно в базе и лечится возвратом в pending, а вот два
    одинаковых сообщения человеку уже не отменишь.
    """
    import asyncio

    take = max(1, min(limit, 100))

    def _take() -> list:
        with registry_db.connect() as conn:
            rows = conn.execute(
                """SELECT message_id, phone, channel, kind, position_id, body,
                          chat_id, attempts
                   FROM candidate_messages WHERE status = 'pending'
                   ORDER BY created_at LIMIT ?""",
                (take,),
            ).fetchall()
            if rows:
                stamp = datetime.now().isoformat(timespec="seconds")
                conn.executemany(
                    """UPDATE candidate_messages
                       SET status = 'sending', attempts = attempts + 1, updated_at = ?
                       WHERE message_id = ?""",
                    [(stamp, row["message_id"]) for row in rows],
                )
                conn.commit()
            return [
                {
                    "message_id": row["message_id"],
                    "phone": row["phone"],
                    "channel": row["channel"],
                    "kind": row["kind"],
                    "position_id": row["position_id"],
                    "card": json.loads(row["body"] or "{}"),
                    # Пусто — пишем кандидату по номеру. Заполнено — это чат
                    # заказчика, и туда по номеру не попасть в принципе.
                    "chat_id": row["chat_id"] or "",
                    "attempts": row["attempts"] + 1,
                }
                for row in rows
            ]

    return JSONResponse({"messages": await asyncio.to_thread(_take)})


@app.post("/api/agent/outbox/{message_id}")
async def api_agent_outbox_report(
        message_id: int,
        report: OutboxReport,
        _: str = Depends(verify_agent_token),
) -> JSONResponse:
    """Сервис переписки отчитывается: отправлено, не вышло или вернуть в очередь."""
    import asyncio

    def _report() -> bool:
        with registry_db.connect() as conn:
            stamp = datetime.now().isoformat(timespec="seconds")
            cursor = conn.execute(
                """UPDATE candidate_messages
                   SET status = ?, last_error = ?, updated_at = ?,
                       sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                       sent_text = CASE WHEN ? != '' THEN ? ELSE sent_text END
                   WHERE message_id = ?""",
                (report.status, report.error[:500], stamp, report.status, stamp,
                 report.text, report.text, message_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    if not await asyncio.to_thread(_report):
        raise HTTPException(404, "сообщение не найдено")
    if report.status == "failed":
        logger.warning("[outbox] сообщение " + str(message_id) + " не отправлено: " + report.error[:200])
    return JSONResponse({"ok": True})


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
