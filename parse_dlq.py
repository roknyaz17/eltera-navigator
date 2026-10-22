"""Мёртвая очередь разбора: всё, что не доехало из заявки до реестра.

Зачем отдельный файл. Про упавший разбор в базе остаётся одна строка —
`requests.parse_status = 'failed'` и обрезанный до 500 символов `parse_error`.
Этого хватает, чтобы зажечь индикатор, и не хватает, чтобы понять причину:
исходный текст к тому времени уже перезаписан следующей ревизией, а у заявки,
которую контрагент убрал из таблицы, не остаётся вообще ничего. Так заявка
ЯППИ от 17 августа 2026 полтора месяца числилась сломанной, и восстановить по
ней было нечего.

Что сюда попадает — три места, где вакансия теряется:

    request_failed   разбор заявки упал целиком: исключение или «LLM не вернул
                     валидный JSON». Видно в базе, но без контекста.
    chunks_failed    разобралась часть кусков. Заявка сохраняется как удачная,
                     и потерянные куски раньше не оставляли следа нигде.
    vacancy_empty    LLM вернул объект, но после нормализации в нём нет ни
                     должности, ни объекта, ни города — `_is_meaningful`
                     выбрасывает такую вакансию молча.

Формат — JSON Lines: одна запись в строке. Так её одинаково удобно и грепать
(`grep ELT-2026-000526 logs/parse_dlq.log`), и разбирать машиной, и по ней же
можно переиграть разбор, когда сервис починят. Ротация месячная со сжатием —
как у основного лога, диск на сервере тесный.

Запись никогда не роняет прогон: любая ошибка внутри самой записи гасится.
"""

import json
import traceback as tb_mod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger

# Метка в extra, по которой сортируются записи: в DLQ-файл уходят только
# помеченные, а в обычный лог — только непомеченные.
DLQ_MARK = "parse_dlq"

# Сколько символов исходника кладём в запись. Посты Градуса со «снимком дня»
# доходят до десятков килобайт, и целиком они тут не нужны — нужен кусок,
# по которому видно, на чём споткнулся разбор.
RAW_LIMIT = 8000
CHUNK_LIMIT = 4000


def setup(log_dir: Path) -> None:
    """Поднимает файл мёртвой очереди. Зовётся из logging_config.setup_logging."""
    logger.add(
        log_dir / "parse_dlq.log",
        level="DEBUG",
        rotation="1 month",
        retention="12 months",
        compression="zip",
        encoding="utf-8",
        # В message уже лежит готовый JSON — своё оформление loguru не добавляет.
        format="{message}",
        filter=lambda rec: rec["extra"].get(DLQ_MARK) is True,
        enqueue=True,
    )


def _clip(value: Optional[str], limit: int) -> Dict[str, Any]:
    """Текст с честной пометкой, что его обрезали."""
    text = value or ""
    if len(text) <= limit:
        return {"text": text, "chars": len(text), "clipped": False}
    return {"text": text[:limit], "chars": len(text), "clipped": True}


def record(
        kind: str,
        source: str,
        request_id: str,
        raw: Any = None,
        error: str = "",
        exc: Optional[BaseException] = None,
        **extra: Any,
) -> None:
    """Кладёт одну потерянную вакансию (или заявку) в мёртвую очередь.

    :param kind: request_failed | chunks_failed | vacancy_empty | request_empty
    :param raw: RawRequest — из него берём происхождение заявки
    :param exc: исключение, если было; трассировка пойдёт в запись
    """
    try:
        entry: Dict[str, Any] = {
            "at": datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            "source": source,
            "request_id": request_id,
            "error": error or (str(exc) if exc else ""),
        }
        if exc is not None:
            entry["exc_type"] = type(exc).__name__
            entry["traceback"] = "".join(
                tb_mod.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:]
        if raw is not None:
            # Происхождение: по source_ref и source_url заявку находят в
            # исходнике — в таблице контрагента или в посте канала.
            entry["origin"] = {
                "source_name": getattr(raw, "source_name", "") or "",
                "source_ref": getattr(raw, "source_ref", "") or "",
                "source_url": getattr(raw, "source_url", "") or "",
                "counterparty_hint": getattr(raw, "counterparty_hint", "") or "",
                "content_hash": getattr(raw, "content_hash", "") or "",
                "received_at": str(getattr(raw, "received_at", "") or ""),
            }
            entry["raw_text"] = _clip(getattr(raw, "raw_text", ""), RAW_LIMIT)
            payload = getattr(raw, "raw_payload", None)
            if payload:
                entry["raw_payload"] = payload
        for key, value in extra.items():
            if key in ("chunk", "vacancy_raw"):
                entry[key] = _clip(value if isinstance(value, str)
                                   else json.dumps(value, ensure_ascii=False), CHUNK_LIMIT)
            else:
                entry[key] = value

        logger.bind(**{DLQ_MARK: True}).info(
            json.dumps(entry, ensure_ascii=False, default=str)
        )
        # Короткая строка в обычный лог: по ней видно, что потеря была, и
        # куда идти смотреть подробности.
        logger.warning(
            f"[DLQ:{kind}] {source}/{request_id} — {entry['error'] or 'подробности в logs/parse_dlq.log'}"
        )
    except Exception:  # noqa: BLE001 — мёртвая очередь не имеет права ронять прогон
        logger.exception(f"[DLQ] не удалось записать потерю {kind} по {request_id}")
