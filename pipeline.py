"""
Единая async-функция запуска пайплайна источников.

Используется и из main.py (CLI), и из app.py (FastAPI/APScheduler).
"""

import asyncio
import os
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from loguru import logger

try:
    import metrics as M
    _METRICS_OK = True
except ImportError:
    _METRICS_OK = False

from aaaplus_message_processor import AaaPlusMessageProcessor
from ametist_sheet_extractor import AmetistSheetExtractor
from marketstaff_sheet_extractor import MarketstaffSheetExtractor
from matrix_vacancy_extractor import MatrixToVacanciesService
from registry.export_sheets import export_to_sheets
from registry.ingest import RegistryIngestor
from registry.models import RawRequest
from seatable_adapter import SeatableExternalLinkClient
from sheets_adapter import GoogleSheetsService
from telegram_userbot import TelegramUserbot
from vacancy_parser import VacancyParserService
from vahtapro_message_processor import VahtaProMessageProcessor
from yappi_vacancy_extractor import YappiToVacanciesService

# Реестр — основной путь. Переменная оставлена как аварийный тумблер: при
# REGISTRY_ENABLED=0 прогон идёт по прежнему маршруту, пишущему напрямую в
# Google Sheets.
REGISTRY_ENABLED = os.getenv("REGISTRY_ENABLED", "1").strip().lower() not in ("0", "false", "no")
# Выгружать ли реестр в Google Таблицу после прогона. Таблица теперь
# read-only витрина: менеджерские правки живут в /registry.
SHEETS_EXPORT_ENABLED = os.getenv("SHEETS_EXPORT_ENABLED", "1").strip().lower() not in ("0", "false", "no")

# Конфиг (можно вынести в env при необходимости)
TARGET_SPREADSHEET_ID = "1HiwitcTGsFcc7OFfLt4s93s38yy-AicqvRN6H14KZbk"
TARGET_SHEET_NAME = "Лист1"

KPK_MATRIX_ID = "18dIE1dyMFR41FCmgjdfMgUNe0aaIkebDtf9Cf1R8UOc"
KPK_MATRIX_URL = f"https://docs.google.com/spreadsheets/d/{KPK_MATRIX_ID}"

# Маркетстафф — таблица «Объекты МС», лист «Объекты МО»: одна строка = одна
# позиция, блоки объектов с объединёнными ячейками. См.
# marketstaff_sheet_extractor.py.
MARKETSTAFF_SPREADSHEET_ID = "1boGa7hDrxTIf-hIaA6NVCdZkDZpNZ1qu1Ti-GBYCJvI"
MARKETSTAFF_URL = f"https://docs.google.com/spreadsheets/d/{MARKETSTAFF_SPREADSHEET_ID}"
MARKETSTAFF_SHEET_NAME = "Объекты МО"

# Аметист — теперь не из Telegram-чата, а из общей таблицы (одна строка =
# одна позиция). См. ametist_sheet_extractor.py.
AMETIST_SPREADSHEET_ID = "1mnupysp96GdAZ5UEHFafOdyiqyqMgZt3-z0WBFM2Akg"
AMETIST_URL = f"https://docs.google.com/spreadsheets/d/{AMETIST_SPREADSHEET_ID}"

# Верхний предел сообщений за обход (cap). Реальный фильтр — по дате (с 07:00 МСК).
TG_LIMIT = 20
# С какого часа МСК берём сегодняшние сообщения Telegram.
TG_SINCE_HOUR_MSK = 7
MSK = timezone(timedelta(hours=3))

# Аметист переведён с Telegram-чата на Google Таблицу (см. AMETIST_SPREADSHEET_ID),
# поэтому AMETIST_LIMIT / AMETIST_LOOKBACK_DAYS больше не нужны.


def _since_today_msk() -> datetime:
    """Возвращает datetime «сегодня TG_SINCE_HOUR_MSK:00 МСК» (timezone-aware).
    Если сейчас раньше этого часа — берём ту же точку вчерашнего дня,
    чтобы ночной/ранний прогон не получил пустую выборку."""
    now = datetime.now(MSK)
    since = now.replace(hour=TG_SINCE_HOUR_MSK, minute=0, second=0, microsecond=0)
    if now < since:
        since -= timedelta(days=1)
    return since


SOURCE_NAMES: Dict[str, str] = {
    "kpk": "КПК",
    "yappi": "Yappi",
    "vahtapro": "ВахтаПро",
    "aaaplus": "AAA+",
    "ametist": "Аметист",
    "marketstaff": "Маркетстафф",
}
ALL_SOURCES = list(SOURCE_NAMES.keys())


def parse_sources(raw: Optional[str]) -> List[str]:
    """'kpk,yappi' -> ['kpk', 'yappi']. None или пустая строка -> все источники."""
    if not raw:
        return list(ALL_SOURCES)
    items = [s.strip().lower() for s in raw.split(",") if s.strip()]
    invalid = [s for s in items if s not in ALL_SOURCES]
    if invalid:
        raise ValueError(
            f"Неизвестные источники: {invalid}. "
            f"Допустимые: {', '.join(ALL_SOURCES)}"
        )
    return items


async def _read_telegram(sources: List[str]) -> Dict[str, Optional[List[dict]]]:
    """Читает сообщения каналов в одном async-сеансе userbot'а.

    Аметист переехал на таблицу, поэтому здесь только ВахтаПро и AAA+.
    """
    need_vahtapro = "vahtapro" in sources
    need_aaaplus = "aaaplus" in sources
    out: Dict[str, Optional[List[dict]]] = {
        "vahtapro": None, "aaaplus": None,
    }

    if not (need_vahtapro or need_aaaplus):
        return out

    since = _since_today_msk()
    logger.info(f"=== Чтение Telegram (сообщения с {since:%Y-%m-%d %H:%M %Z}) ===")
    async with TelegramUserbot() as bot:
        me = await bot.whoami()
        logger.info(f"Userbot: {me['first_name']} {me.get('last_name') or ''} "
                    f"(@{me.get('username') or '—'}, id={me['id']})")
        if need_vahtapro:
            chat_id = int(os.environ["TELEGRAM_VAHTAPRO_CHAT_ID"])
            out["vahtapro"] = await bot.get_messages(chat_id, limit=TG_LIMIT, after=since)
            logger.info(f"ВахтаПро: получено {len(out['vahtapro'])} сообщений за период")
        if need_aaaplus:
            chat_id = int(os.environ["TELEGRAM_AAAPLUS_CHAT_ID"])
            out["aaaplus"] = await bot.get_messages(chat_id, limit=TG_LIMIT, after=since)
            logger.info(f"AAA+: получено {len(out['aaaplus'])} сообщений за период")
    return out


def _fmt_table(stats: dict) -> str:
    out = (f"added={stats.get('added', 0)}, "
           f"updated={stats.get('updated', 0)}, "
           f"skipped={stats.get('skipped', 0)}")
    # Источники со snapshot-lifecycle (Аметист, Маркетстафф) гасят
    # пропавшие позиции — без этого счётчика прогон выглядит неполным.
    if "deactivated" in stats:
        out += f", deactivated={stats['deactivated']}"
    return out


def _fmt_telegram(stats: dict) -> str:
    return (f"added={stats.get('added', 0)}, "
            f"updated={stats.get('updated', 0)}, "
            f"skipped={stats.get('skipped', 0)}, "
            f"deactivated={stats.get('deactivated', 0)}, "
            f"needs_review={stats.get('needs_review', 0)}, "
            f"service={stats.get('service_messages', 0)}")


async def _collect_requests(
        sources: List[str],
        sheets: GoogleSheetsService,
        vacancy_parser: VacancyParserService,
        tg_msgs: Dict[str, Optional[List[dict]]],
        reset: bool,
) -> Dict[str, tuple]:
    """Собирает входящие документы всех источников. {alias: (заявки, снимок?)}.

    Экстракторы читают свои форматы ровно как раньше — меняется только то, что
    они отдают наружу: не готовые вакансии для записи в таблицу, а исходные
    заявки для реестра.
    """
    tasks = []
    aliases = []

    if "kpk" in sources:
        matrix_ex = MatrixToVacanciesService(sheets, vacancy_parser)
        tasks.append(matrix_ex.collect_requests(
            matrix_spreadsheet_id=KPK_MATRIX_ID,
            matrix_sheet_name="Таблица",
            counterparty_name="КПК",
            source_url=KPK_MATRIX_URL,
        ))
        aliases.append("kpk")

    if "marketstaff" in sources:
        marketstaff_ex = MarketstaffSheetExtractor(sheets, vacancy_parser)
        tasks.append(marketstaff_ex.collect_requests(
            marketstaff_spreadsheet_id=MARKETSTAFF_SPREADSHEET_ID,
            marketstaff_sheet_name=MARKETSTAFF_SHEET_NAME,
            source_url=MARKETSTAFF_URL,
        ))
        aliases.append("marketstaff")

    if "yappi" in sources:
        yappi_url = os.environ["SEATABLE_TABLE_YAPPI"]
        yappi_client = SeatableExternalLinkClient(yappi_url)
        yappi_ex = YappiToVacanciesService(yappi_client, sheets, vacancy_parser)
        tasks.append(yappi_ex.collect_requests(
            counterparty_name="Yappi",
            source_url=yappi_url,
        ))
        aliases.append("yappi")

    if "ametist" in sources:
        ametist_ex = AmetistSheetExtractor(sheets, vacancy_parser)
        tasks.append(ametist_ex.collect_requests(
            ametist_spreadsheet_id=AMETIST_SPREADSHEET_ID,
            ametist_sheet_name="Потребность ",
            source_url=AMETIST_URL,
        ))
        aliases.append("ametist")

    collected = await asyncio.gather(*tasks, return_exceptions=True)

    out: Dict[str, tuple] = {}
    for alias, result in zip(aliases, collected):
        if isinstance(result, Exception):
            logger.exception(f"[{alias}] сбор заявок упал: {result}")
            continue
        # Таблицы всегда приходят целиком, поэтому это полный снимок:
        # позиции, которых в нём нет, гасятся.
        out[alias] = (result, True)

    # Telegram: сообщения читаются одним сеансом заранее (см. _read_telegram).
    for alias, processor_cls in (
            ("vahtapro", VahtaProMessageProcessor),
            ("aaaplus", AaaPlusMessageProcessor),
    ):
        if alias not in sources or tg_msgs.get(alias) is None:
            continue
        chat_id = int(os.environ[f"TELEGRAM_{alias.upper()}_CHAT_ID"])
        url = f"https://t.me/c/{abs(chat_id) - 1000000000000}"
        processor = processor_cls(sheets, vacancy_parser)
        requests, had_snapshot = processor.collect_requests(
            messages=tg_msgs[alias], source=alias, source_url=url,
        )
        # Без «снимка дня» в пачке гасить нечего: в канале за день приходят
        # только отдельные апдейты, и снапшот-логика выключила бы всё
        # остальное. reset=True в задании планировщика означает ровно то же,
        # что и снимок, — считать пачку полным состоянием источника.
        out[alias] = (requests, had_snapshot or reset)

    return out


async def run_registry_pipeline(sources: List[str], reset: bool = False) -> Dict[str, Dict[str, int]]:
    """Прогон через единый реестр заявок."""
    vacancy_parser = VacancyParserService()
    sheets = GoogleSheetsService("credentials.json")

    tg_msgs = await _read_telegram(sources)
    batches = await _collect_requests(sources, sheets, vacancy_parser, tg_msgs, reset)
    if not batches:
        logger.warning("Ни один источник не отдал заявок")
        return {}

    ingestor = RegistryIngestor(vacancy_parser)
    start_time = _time.time()
    stats = await ingestor.ingest_batches(batches)
    total_duration = _time.time() - start_time

    out: Dict[str, Dict[str, int]] = {}
    for alias, item in stats.items():
        out[alias] = item.as_dict()
        if _METRICS_OK:
            M.PIPE_RUNS.labels(source=alias, status="success").inc()
            M.PIPE_ADDED.labels(source=alias).inc(item.positions_added)
            M.PIPE_UPDATED.labels(source=alias).inc(item.positions_updated)
            M.PIPE_DEACTIVATED.labels(source=alias).inc(item.positions_deactivated)
            M.PIPE_DURATION.labels(source=alias).observe(total_duration / max(len(stats), 1))
            M.REG_REQUESTS.labels(source=alias, kind="new").inc(item.requests_new)
            M.REG_REQUESTS.labels(source=alias, kind="changed").inc(item.requests_changed)
            M.REG_REQUESTS.labels(source=alias, kind="unchanged").inc(item.requests_unchanged)
            M.REG_REQUESTS.labels(source=alias, kind="failed").inc(item.requests_failed)
            M.REG_LLM_CALLS_SAVED.labels(source=alias).inc(item.llm_calls_saved)

    logger.info(f"=== LLM usage: {vacancy_parser.usage.summary()} ===")
    if _METRICS_OK:
        u = vacancy_parser.usage
        label = ",".join(stats.keys())
        M.LLM_REQUESTS.labels(source=label).inc(u.requests)
        M.LLM_TOKENS.labels(source=label, type="input").inc(u.input)
        M.LLM_TOKENS.labels(source=label, type="output").inc(u.output)
        M.LLM_COST.labels(source=label).inc(u.cost_rub)

    if SHEETS_EXPORT_ENABLED:
        try:
            exported = await asyncio.to_thread(
                export_to_sheets, sheets, TARGET_SPREADSHEET_ID, TARGET_SHEET_NAME,
            )
            logger.info(f"=== Выгрузка в Google Sheets: {exported} строк ===")
        except Exception:
            logger.exception("Выгрузка в Google Sheets не удалась — данные реестра не пострадали")

    return out


async def run_pipeline(sources: List[str], reset: bool = False) -> Dict[str, Dict[str, int]]:
    """
    Прогоняет указанные источники.

    :param sources: подмножество ALL_SOURCES
    :param reset: если True — пометить is_active=FALSE для этих source перед прогоном
    :return: {source_alias: stats} — статистика по каждому отработавшему источнику
    """
    if not sources:
        logger.warning("run_pipeline вызван с пустым списком источников — нечего делать")
        return {}

    if REGISTRY_ENABLED:
        logger.info(f"=== Запуск пайплайна (реестр): sources={sources}, reset={reset} ===")
        return await run_registry_pipeline(sources, reset=reset)

    logger.info(f"=== Запуск пайплайна (прежний путь): sources={sources}, reset={reset} ===")

    # Параллельно с подготовкой Telegram — инициализируем сервисы (синхронно, быстро)
    vacancy_parser = VacancyParserService()
    sheets = GoogleSheetsService("credentials.json")
    sheets_lock = asyncio.Lock()

    # Получаем телеграм-сообщения (если нужны)
    tg_msgs = await _read_telegram(sources)
    vahtapro_msgs = tg_msgs["vahtapro"]
    aaaplus_msgs = tg_msgs["aaaplus"]

    # Сброс is_active=FALSE для выбранных source
    if reset:
        source_names = [SOURCE_NAMES[s] for s in sources]
        logger.info(f"=== Сброс is_active=FALSE для source: {source_names} ===")
        deactivated = await asyncio.to_thread(
            sheets.mark_sources_inactive,
            spreadsheet_id=TARGET_SPREADSHEET_ID,
            sources=source_names,
            sheet_name=TARGET_SHEET_NAME,
        )
        logger.info(f"Сброшено в FALSE: {deactivated} строк")

    # Готовим задачи для всех источников. Параллельно идут только те,
    # которые сейчас в списке.
    tasks = []
    aliases = []

    if "kpk" in sources:
        matrix_ex = MatrixToVacanciesService(sheets, vacancy_parser)
        tasks.append(matrix_ex.extract_and_save(
            matrix_spreadsheet_id=KPK_MATRIX_ID,
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            counterparty_name="КПК",
            matrix_sheet_name="Таблица",
            source_url=KPK_MATRIX_URL,
            sheets_lock=sheets_lock,
        ))
        aliases.append("kpk")

    if "marketstaff" in sources:
        marketstaff_ex = MarketstaffSheetExtractor(sheets, vacancy_parser)
        tasks.append(marketstaff_ex.extract_and_save(
            marketstaff_spreadsheet_id=MARKETSTAFF_SPREADSHEET_ID,
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            marketstaff_sheet_name=MARKETSTAFF_SHEET_NAME,
            source_url=MARKETSTAFF_URL,
            sheets_lock=sheets_lock,
        ))
        aliases.append("marketstaff")

    if "yappi" in sources:
        yappi_url = os.environ["SEATABLE_TABLE_YAPPI"]
        yappi_client = SeatableExternalLinkClient(yappi_url)
        yappi_ex = YappiToVacanciesService(yappi_client, sheets, vacancy_parser)
        tasks.append(yappi_ex.extract_and_save(
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            counterparty_name="Yappi",
            source_url=yappi_url,
            sheets_lock=sheets_lock,
        ))
        aliases.append("yappi")

    if "vahtapro" in sources and vahtapro_msgs is not None:
        chat_id = int(os.environ["TELEGRAM_VAHTAPRO_CHAT_ID"])
        url = f"https://t.me/c/{abs(chat_id) - 1000000000000}"
        proc = VahtaProMessageProcessor(sheets, vacancy_parser)
        tasks.append(proc.aprocess_messages(
            messages=vahtapro_msgs,
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            source_url=url,
            sheets_lock=sheets_lock,
        ))
        aliases.append("vahtapro")

    if "aaaplus" in sources and aaaplus_msgs is not None:
        chat_id = int(os.environ["TELEGRAM_AAAPLUS_CHAT_ID"])
        url = f"https://t.me/c/{abs(chat_id) - 1000000000000}"
        proc = AaaPlusMessageProcessor(sheets, vacancy_parser)
        tasks.append(proc.aprocess_messages(
            messages=aaaplus_msgs,
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            source_url=url,
            sheets_lock=sheets_lock,
        ))
        aliases.append("aaaplus")

    if "ametist" in sources:
        ametist_ex = AmetistSheetExtractor(sheets, vacancy_parser)
        tasks.append(ametist_ex.extract_and_save(
            ametist_spreadsheet_id=AMETIST_SPREADSHEET_ID,
            target_spreadsheet_id=TARGET_SPREADSHEET_ID,
            target_sheet_name=TARGET_SHEET_NAME,
            ametist_sheet_name="Потребность ",
            source_url=AMETIST_URL,
            sheets_lock=sheets_lock,
        ))
        aliases.append("ametist")

    if not tasks:
        logger.warning("Нет ни одной задачи для запуска")
        return {}

    start_time = _time.time()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    total_duration = _time.time() - start_time

    out: Dict[str, Dict[str, int]] = {}
    for alias, stats in zip(aliases, results):
        if isinstance(stats, Exception):
            logger.exception(f"[{alias}] упал с ошибкой: {stats}")
            out[alias] = {"error": str(stats)}
            if _METRICS_OK:
                M.PIPE_RUNS.labels(source=alias, status="failed").inc()
            continue
        fmt = _fmt_telegram(stats) if alias in ("vahtapro", "aaaplus") else _fmt_table(stats)
        logger.info(f"{SOURCE_NAMES[alias]}: {fmt}")
        out[alias] = stats

        if _METRICS_OK:
            M.PIPE_RUNS.labels(source=alias, status="success").inc()
            M.PIPE_ADDED.labels(source=alias).inc(stats.get("added", 0))
            M.PIPE_UPDATED.labels(source=alias).inc(stats.get("updated", 0))
            M.PIPE_SKIPPED.labels(source=alias).inc(stats.get("skipped", 0))
            M.PIPE_DEACTIVATED.labels(source=alias).inc(stats.get("deactivated", 0))
            M.PIPE_NEEDS_REVIEW.labels(source=alias).inc(stats.get("needs_review", 0))
            # Время на этот конкретный источник точно не знаем (gather), приближаем
            # средним по всем — для группы из 3 заняло total/3 в среднем.
            M.PIPE_DURATION.labels(source=alias).observe(total_duration / max(len(aliases), 1))

    # Сводка расхода LLM-токенов за этот прогон
    logger.info(f"=== LLM usage: {vacancy_parser.usage.summary()} ===")
    if _METRICS_OK:
        # На этом прогоне LLM мог обработать несколько источников. Считаем сумарно.
        u = vacancy_parser.usage
        label = ",".join(aliases)
        M.LLM_REQUESTS.labels(source=label).inc(u.requests)
        M.LLM_TOKENS.labels(source=label, type="input").inc(u.input)
        M.LLM_TOKENS.labels(source=label, type="output").inc(u.output)
        M.LLM_COST.labels(source=label).inc(u.cost_rub)

    return out
