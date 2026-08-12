"""
FastAPI-приложение со встроенным APScheduler и веб-страницей вакансий.

Запуск:
    uvicorn app:app --host 0.0.0.0 --port 8000

Эндпоинты:
    GET  /vacancies              — HTML со списком вакансий (Basic Auth).
    GET  /health                 — статус приложения и планировщика.
    GET  /jobs                   — расписание + время следующего запуска.
    POST /trigger/{name}         — запустить зарегистрированную задачу сейчас.
    POST /run?sources=...&reset= — запустить произвольную комбинацию.

Расписание (Europe/Moscow):
    09:30  vahtapro + aaaplus              с reset для них
    12:00  kpk + yappi + marketstaff       с reset для них
    13:00  vahtapro                        без reset
    13:30  ametist                         без reset (окно 14 дн., снимок «Обновляем потребность»)

Переменные окружения для веб-доступа:
    WEB_USER       — логин для Basic Auth (страница /vacancies)
    WEB_PASSWORD   — пароль
"""

import csv
import io
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from logging_config import setup_logging

setup_logging()

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import metrics as M
from pipeline import (
    ALL_SOURCES,
    REGISTRY_ENABLED,
    SOURCE_NAMES,
    TARGET_SHEET_NAME,
    TARGET_SPREADSHEET_ID,
    parse_sources,
    run_pipeline,
)
from registry import compat, db as registry_db, dictionaries as registry_dicts, queries as rq
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
        "description": "09:30 МСК — ВахтаПро + AAA+ с reset",
    },
    "noon_tables": {
        "trigger": CronTrigger(hour=12, minute=0, timezone="Europe/Moscow"),
        "sources": ["kpk", "yappi", "marketstaff"],
        "reset": True,
        "description": "12:00 МСК — КПК + Yappi + Маркетстафф с reset",
    },
    "afternoon_vahtapro": {
        "trigger": CronTrigger(hour=13, minute=0, timezone="Europe/Moscow"),
        "sources": ["vahtapro"],
        "reset": False,
        "description": "13:00 МСК — ВахтаПро без reset",
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _register_jobs()
    scheduler.start()
    M.APP_INFO.info({"version": "1.0", "jobs": ",".join(JOBS.keys())})
    logger.info("APScheduler стартовал. Задачи:")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.id}: next run = {job.next_run_time}")
    yield
    scheduler.shutdown()
    logger.info("APScheduler остановлен.")


app = FastAPI(title="Eltrea Bot", lifespan=lifespan)

# Статика для веб-фронта (логотипы Eltera и пр., используется /navigator).
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- Basic Auth ----------
security = HTTPBasic()


def verify_creds(creds: HTTPBasicCredentials = Depends(security)) -> str:
    expected_user = os.getenv("WEB_USER", "")
    expected_pwd = os.getenv("WEB_PASSWORD", "")
    if not expected_user or not expected_pwd:
        raise HTTPException(
            status_code=500,
            detail="WEB_USER / WEB_PASSWORD не настроены в .env",
        )
    user_ok = secrets.compare_digest(creds.username, expected_user)
    pwd_ok = secrets.compare_digest(creds.password, expected_pwd)
    if not (user_ok and pwd_ok):
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# ---------- Эндпоинты ----------
@app.get("/health")
async def health() -> dict:
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
async def metrics_endpoint() -> Response:
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
async def list_jobs() -> dict:
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
        user: str = Depends(verify_creds),
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
        user: str = Depends(verify_creds),
) -> dict:
    try:
        parsed = parse_sources(sources)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    background.add_task(_job_wrapper, f"manual({','.join(parsed)})", parsed, reset)
    return {"status": "started", "sources": parsed, "reset": reset}


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
    """Кандидат-фронт «Навигатор вакансий» (тёмная тема Eltera).

    Это статичный HTML+JS: данные он подтягивает с /api/vacancies и фильтрует
    на клиенте. Отдаём содержимое файла напрямую (без Jinja-рендеринга), чтобы
    фигурные скобки в JS/CSS гарантированно не интерпретировались как шаблон.
    """
    with open("templates/navigator.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


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
        user: str = Depends(verify_creds),
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
        user: str = Depends(verify_creds),
):
    with registry_db.connect() as conn:
        registry_dicts.confirm(conn, kind, alias, canonical)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.post("/registry/dictionaries/delete")
async def registry_dictionaries_delete(
        kind: str = Form(...),
        alias: str = Form(...),
        canonical: str = Form(""),
        user: str = Depends(verify_creds),
):
    with registry_db.connect() as conn:
        registry_dicts.remove(conn, kind, alias)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.post("/registry/dictionaries/confirm-all")
async def registry_dictionaries_confirm_all(
        kind: str = Form(...),
        user: str = Depends(verify_creds),
):
    with registry_db.connect() as conn:
        registry_dicts.confirm_all(conn, kind)
    return RedirectResponse(f"/registry/dictionaries?kind={kind}", status_code=303)


@app.get("/registry/manual", response_class=HTMLResponse)
async def registry_manual_form(request: Request, user: str = Depends(verify_creds)):
    return templates.TemplateResponse(
        request=request, name="registry_manual.html", context={"result": None, "error": None},
    )


@app.post("/registry/manual", response_class=HTMLResponse)
async def registry_manual_submit(
        request: Request,
        counterparty: str = Form(...),
        text: str = Form(...),
        channel: str = Form(""),
        user: str = Depends(verify_creds),
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
        raw_payload={"channel": channel, "entered_by": user},
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
        })
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
        },
    )


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
