"""
Универсальный процессор сообщений Telegram-каналов о вакансиях.

Один класс, который через конфиг (source_name, snapshot_marker, signal_keywords и т.д.)
обслуживает разные каналы (Градус, AAA+, и любые следующие). Логика разделения
сообщений и сегментации намеренно мягкая: «эта вакансия?» решается по содержимому,
эмодзи-разделитель используется только для нарезки длинных постов на куски.

Принципы:
1. Сообщения обрабатываются по возрастанию даты (важно для логики снимков).
2. Решение «есть в сообщении вакансия» — по словарю сигналов (см. SIGNAL_KEYWORDS,
   PROFESSION_HINTS). Эмодзи не используется как детектор.
3. Если в сообщении есть SNAPSHOT_MARKER — это «снимок дня». В конце пачки
   деактивируются вакансии этого source, не встретившиеся в снимке + апдейтах,
   пришедших после него.
4. LLM-парсинг сегментов внутри сообщения — параллельный (Semaphore).
5. Запись в Sheets — один батч на сообщение, под общим asyncio.Lock (если передан).
"""

import asyncio
from datetime import datetime
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from registry.models import RawRequest


class TelegramChannelProcessor:
    """Универсальный обработчик одного Telegram-канала-источника вакансий."""

    DEFAULT_LLM_CONCURRENCY = 5

    # Дефолтные словари — пригодны для большинства каналов про вахту.
    # Можно перегрузить через конструктор.
    DEFAULT_SIGNAL_KEYWORDS: Sequence[str] = (
        "руб", "₽",
        "вахта", "вахт",
        "смен",
        "р/час", "р/ч", "р/смена",
        "руб/смена", "руб/час",
        "👉",
    )
    DEFAULT_PROFESSION_HINTS: Sequence[str] = (
        "комплектовщик", "грузчик", "упаковщик", "уборщик", "уборщиц",
        "оператор", "сборщик", "водитель", "разнорабоч", "кладовщик",
        "фасовщик", "маркировщик", "кассир", "спк", "вэш",
        "транспортировщик", "укладчик", "мойщик", "работник",
        "жиловщик", "навесчик", "транспортир",
    )

    def __init__(
            self,
            sheets_service,
            llm_parser,
            *,
            source_name: str,
            source_url_fallback: str = "",
            snapshot_marker: Optional[str] = None,
            signal_keywords: Optional[Sequence[str]] = None,
            profession_hints: Optional[Sequence[str]] = None,
            segment_emoji: Optional[str] = "🚀",
            counterparty_override: Optional[str] = None,
            context_provider: Optional[Callable[[str], Tuple[Optional[str], Optional[Dict]]]] = None,
    ):
        """
        :param source_name: значение для поля `source` в таблице (например, "Градус", "AAA+").
        :param source_url_fallback: URL канала, если он не передан в process_messages.
        :param snapshot_marker: подстрока, идентифицирующая «снимок дня»
            (например, "Описание проектов и актуальная потребность"). Если None
            — деактивация устаревших не запускается, только апсёрт.
        :param signal_keywords: ключевые слова — «есть вакансия». По умолчанию — наш общий список.
        :param profession_hints: список профессий-маркеров. По умолчанию — общий.
        :param segment_emoji: разделитель сегментов внутри одного сообщения.
            None или "" — не сегментировать, отдавать всё сообщение целиком.
        :param counterparty_override: если задано — принудительно ставить это значение
            в поле counterparty каждой распарсенной вакансии (то, что LLM туда вытащил,
            попадает в object_name, если object_name пуст).
        :param context_provider: `текст → (справка по проекту, чем она подтверждена)`.
            Через него канал получает описание проекта из базы знаний контрагента
            (см. project_kb.py). Ничего не нашлось — (None, None), и заявка идёт
            в разбор ровно как раньше.
        """
        self.sheets = sheets_service
        self.parser = llm_parser
        self.source_name = source_name
        self.source_url_fallback = source_url_fallback
        self.snapshot_marker = snapshot_marker
        self.signal_keywords = tuple(signal_keywords or self.DEFAULT_SIGNAL_KEYWORDS)
        self.profession_hints = tuple(profession_hints or self.DEFAULT_PROFESSION_HINTS)
        self.segment_emoji = segment_emoji
        self.counterparty_override = counterparty_override
        self.context_provider = context_provider

    # -------------------------------------------------------- сбор для реестра
    def collect_requests(
            self,
            messages: List[Dict],
            source: str,
            source_url: str = "",
    ) -> Tuple[List[RawRequest], bool]:
        """Превращает сообщения канала в заявки реестра.

        Одно сообщение = одна заявка: пост в канале и есть тот документ,
        который прислал контрагент. Нарезка по «🚀» никуда не делась, но
        теперь это parse_chunks — подсказка модели, как читать длинный пост, а
        не повод завести два десятка отдельных заявок с одинаковым исходником.

        Возвращает (заявки, был ли среди них «снимок дня»). Второе значение
        решает, гасить ли позиции, которых в пачке нет: по отдельным апдейтам
        канала так делать нельзя — выключилось бы всё, кроме сегодняшнего.
        """
        url = source_url or self.source_url_fallback
        out: List[RawRequest] = []
        had_snapshot = False
        with_context = 0

        for msg in sorted(messages, key=lambda m: m["date"]):
            text = (msg.get("text") or "").strip()
            msg_id = msg.get("id")
            if not self._has_vacancy_signals(text):
                preview = text[:60].replace("\n", " ")
                logger.info(f"[{self.source_name} #{msg_id}] service: {preview!r}")
                continue

            if self.snapshot_marker and self.snapshot_marker in text:
                had_snapshot = True

            chunks = self._iter_segments(text)
            if not chunks:
                continue

            received_at = msg.get("date")
            if isinstance(received_at, str):
                try:
                    received_at = datetime.fromisoformat(received_at)
                except ValueError:
                    received_at = None

            overrides = {}
            if self.counterparty_override:
                overrides["counterparty"] = self.counterparty_override

            contexts, kb = self._collect_contexts(chunks)
            if kb:
                with_context += 1

            payload = {"message_id": msg_id, "channel_id": msg.get("channel_id")}
            if kb:
                # Что именно подмешано к разбору, видно в карточке заявки: у
                # менеджера должна быть возможность открыть ту же папку на
                # диске и проверить, туда ли мы попали.
                payload["kb"] = kb

            out.append(RawRequest(
                source=source,
                source_ref=f"msg:{msg_id}",
                raw_text=text,
                source_name=self.source_name,
                source_url=self._message_url(msg, url),
                counterparty_hint=self.counterparty_override or "",
                raw_payload=payload,
                received_at=received_at,
                field_overrides=overrides,
                parse_chunks=chunks if len(chunks) > 1 else None,
                extra_context=contexts[0] if len(chunks) == 1 else None,
                chunk_contexts=contexts if len(chunks) > 1 else None,
            ))

        logger.info(
            f"[{self.source_name}] заявок из канала: {len(out)}"
            + (f", со справкой по проекту: {with_context}" if self.context_provider else "")
            + (", среди них снимок дня" if had_snapshot else "")
        )
        return out, had_snapshot

    def _collect_contexts(
            self, chunks: List[str],
    ) -> Tuple[List[Optional[str]], List[Dict]]:
        """Справка по каждому куску поста + сведения о найденных проектах."""
        if not self.context_provider:
            return [None] * len(chunks), []
        contexts: List[Optional[str]] = []
        found: List[Dict] = []
        for chunk in chunks:
            try:
                context, meta = self.context_provider(chunk)
            except Exception as exc:  # noqa: BLE001 — база знаний не должна ронять сбор
                logger.warning(f"[{self.source_name}] справка по проекту не собрана: {exc}")
                context, meta = None, None
            contexts.append(context)
            if meta:
                found.append(meta)
        return contexts, found

    def _message_url(self, msg: Dict, fallback: str) -> str:
        """Ссылка на конкретный пост, а не на канал целиком."""
        msg_id = msg.get("id")
        channel_id = msg.get("channel_id") or 0
        if not msg_id:
            return fallback
        abs_id = abs(int(channel_id))
        if abs_id > 10 ** 12:
            abs_id -= 10 ** 12
        return f"https://t.me/c/{abs_id}/{msg_id}"

    # ------------------------------------------------------------- async
    async def aprocess_messages(
            self,
            messages: List[Dict],
            target_spreadsheet_id: str,
            target_sheet_name: str = "Лист1",
            source_url: str = "",
            llm_concurrency: Optional[int] = None,
            sheets_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, int]:
        url = source_url or self.source_url_fallback
        sem = asyncio.Semaphore(llm_concurrency or self.DEFAULT_LLM_CONCURRENCY)
        messages = sorted(messages, key=lambda m: m["date"])

        had_snapshot = False
        snapshot_ids: Set[str] = set()
        post_snapshot_ids: Set[str] = set()

        stats = {
            "added": 0, "updated": 0, "skipped": 0,
            "deactivated": 0,
            "snapshot_segments": 0, "update_segments": 0,
            "service_messages": 0,
            "needs_review": 0,
        }

        async def parse_one(seg: str):
            async with sem:
                return seg, await self.parser.aparse(
                    seg, source=self.source_name, source_url=url
                )

        lock = sheets_lock or asyncio.Lock()

        for msg in messages:
            text = (msg.get("text") or "").strip()
            msg_id = msg.get("id")
            msg_date = msg.get("date")
            channel_id = msg.get("channel_id") or 0
            # Для private-канала chat_id выглядит как -1002610083978.
            # Ссылка на конкретное сообщение: t.me/c/<abs_id_без_префикса_100>/<msg_id>
            abs_id = abs(int(channel_id))
            if abs_id > 10 ** 12:
                abs_id -= 10 ** 12
            msg_url = f"https://t.me/c/{abs_id}/{msg_id}" if msg_id else url

            if not self._has_vacancy_signals(text):
                stats["service_messages"] += 1
                preview = text[:60].replace("\n", " ")
                logger.info(f"[{self.source_name} #{msg_id}] service: {preview!r}")
                continue

            is_snapshot = bool(self.snapshot_marker) and self.snapshot_marker in text
            if is_snapshot:
                # Новый снимок сбрасывает накопленные id
                snapshot_ids = set()
                post_snapshot_ids = set()
                had_snapshot = True

            segments = self._iter_segments(text)
            if not segments:
                continue

            # Параллельный LLM по сегментам этого сообщения
            results = await asyncio.gather(*[parse_one(s) for s in segments])

            # Аккумулируем все вакансии в один батч на сообщение
            all_vacancies: List[Dict] = []
            for seg, parsed in results:
                if not parsed:
                    stats["needs_review"] += 1
                    preview = seg[:200].replace("\n", " | ")
                    logger.warning(f"[{self.source_name} #{msg_id} {msg_date}] "
                                   f"NEEDS_MANUAL_REVIEW: {preview!r}")
                    continue
                # Принудительная замена counterparty + перенос исходного значения
                # в object_name. Делается ДО пересчёта vacancy_id, иначе id не
                # будет соответствовать таблице.
                if self.counterparty_override:
                    for v in parsed:
                        original_cp = v.get("counterparty")
                        if original_cp and original_cp != self.counterparty_override:
                            v.setdefault("object_name", original_cp)
                        v["counterparty"] = self.counterparty_override
                        # Пересчитываем vacancy_id, т.к. counterparty/object_name изменились
                        from vacancy_parser import make_vacancy_id
                        v["vacancy_id"] = make_vacancy_id(v)

                ids = {v["vacancy_id"] for v in parsed if v.get("vacancy_id")}
                if is_snapshot:
                    snapshot_ids |= ids
                    stats["snapshot_segments"] += 1
                else:
                    post_snapshot_ids |= ids
                    stats["update_segments"] += 1
                for v in parsed:
                    v.setdefault("original_message", seg)
                    # Перезаписываем source_url на ссылку конкретного сообщения,
                    # а не на канал целиком — чтобы кнопка в UI открывала пост.
                    v["source_url"] = msg_url
                all_vacancies.extend(parsed)

            if all_vacancies:
                async with lock:
                    result = await asyncio.to_thread(
                        self.sheets.upsert_vacancies,
                        spreadsheet_id=target_spreadsheet_id,
                        vacancies=all_vacancies,
                        sheet_name=target_sheet_name,
                        original_msg=None,
                    )
                for k in ("added", "updated", "skipped"):
                    stats[k] += result.get(k, 0)

        if had_snapshot:
            keep_ids = snapshot_ids | post_snapshot_ids
            async with lock:
                stats["deactivated"] = await asyncio.to_thread(
                    self.sheets.deactivate_stale_vacancies,
                    spreadsheet_id=target_spreadsheet_id,
                    sheet_name=target_sheet_name,
                    source=self.source_name,
                    keep_ids=keep_ids,
                )

        return stats

    # ----------------------------------------------------------- detection
    def _has_vacancy_signals(self, text: str) -> bool:
        if not text:
            return False
        low = text.lower()
        if any(kw in low for kw in self.signal_keywords):
            return True
        if any(prof in low for prof in self.profession_hints):
            return True
        return False

    # ------------------------------------------------------- segmentation
    def _iter_segments(self, text: str) -> List[str]:
        emoji = self.segment_emoji
        if not emoji or text.count(emoji) < 2:
            return [text]

        parts = text.split(emoji)
        segments: List[str] = []
        # Шапка (текст до первого emoji) обычно "Описание..." / "Регионы:" —
        # самостоятельной вакансии в ней нет, пропускаем.
        for raw in parts[1:]:
            seg = emoji + raw.strip()
            if seg.strip() and self._has_vacancy_signals(seg):
                segments.append(seg)
        return segments if segments else [text]
