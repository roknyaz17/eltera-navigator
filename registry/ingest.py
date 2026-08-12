"""Приём заявок в реестр: дедупликация → разбор → унификация → запись.

Схема прогона одного источника:

1. Фаза подготовки (синхронная, одна транзакция). Для каждой входящей заявки
   ищем её по (source, source_ref). Не нашли — заводим новую с внутренним ID.
   Нашли и содержимое совпало — LLM не трогаем вообще: это главная экономия,
   при четырёх прогонах в день большинство строк не меняется. Содержимое
   изменилось — прежняя версия уезжает в request_revisions, заявка идёт на
   повторный разбор.
2. Фаза разбора (асинхронная, параллельная). Только те заявки, которым он
   действительно нужен.
3. Фаза записи (синхронная, одна транзакция). Нормализация, upsert позиций с
   историей изменений, обновление поискового индекса и снапшот-жизненный цикл.

Разделение на фазы неслучайно: соединение с SQLite не удерживается через
await, а весь ввод-вывод в базу идёт двумя транзакциями на источник.
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from registry import db
from registry.ids import next_position_id, next_request_id
from registry.models import DATA_FIELDS, IngestStats, RawRequest
from registry.normalize import Normalizer, clean_text, fingerprint

# Поля, попадающие в полнотекстовый индекс вместе с исходным текстом заявки.
SEARCHABLE_FIELDS = [
    "counterparty",
    "counterparty_raw",
    "vacancy_name",
    "vacancy_name_raw",
    "vacancy_category",
    "city",
    "city_raw",
    "region",
    "object_name",
    "object_address",
    "schedule",
    "requirements",
    "duties",
    "advantages",
    "risks",
    "sb_policy",
    "citizenship_requirements",
]

DEFAULT_LLM_CONCURRENCY = 5


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class _Pending:
    """Заявка, прошедшая фазу подготовки."""

    __slots__ = ("request_id", "raw", "needs_parse", "parsed", "tokens_in", "tokens_out", "error")

    def __init__(self, request_id: str, raw: RawRequest, needs_parse: bool):
        self.request_id = request_id
        self.raw = raw
        self.needs_parse = needs_parse
        self.parsed: Optional[List[Dict[str, Any]]] = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.error = ""


class RegistryIngestor:
    """Единая точка входа для всех источников.

    :param parser: VacancyParserService (или совместимый объект с aparse_raw_ex)
    :param db_path: путь к базе; None — из REGISTRY_DB_PATH
    :param learn: пополнять ли очередь справочников новыми вариантами написания
    """

    def __init__(
            self,
            parser,
            db_path: Optional[str] = None,
            llm_concurrency: int = DEFAULT_LLM_CONCURRENCY,
            learn: bool = True,
    ):
        self.parser = parser
        self.db_path = db_path
        self.llm_concurrency = llm_concurrency
        self.learn = learn

    # ------------------------------------------------------------------ public
    async def ingest(
            self,
            source: str,
            raws: Sequence[RawRequest],
            snapshot: bool = True,
    ) -> IngestStats:
        """Принимает пачку заявок одного источника.

        :param snapshot: считать пачку полным снимком источника. Позиции,
            которых в ней нет, гасятся is_active=0. Для источников, которые
            присылают только дельту (отдельные сообщения в чате без «сводки
            дня»), передавать False, иначе выключится всё, кроме сегодняшнего.
        """
        result = await self.ingest_batches({source: (list(raws), snapshot)})
        return result.get(source, IngestStats())

    async def ingest_batches(
            self,
            batches: Dict[str, Tuple[Sequence[RawRequest], bool]],
    ) -> Dict[str, IngestStats]:
        """Принимает заявки сразу нескольких источников.

        Фазы намеренно идут «по горизонтали», а не источник за источником:
        подготовка и запись — это быстрые транзакции к SQLite, а разбор —
        долгие сетевые вызовы. Сложив все требующие разбора заявки в один
        параллельный этап, мы сохраняем прежнюю одновременность источников,
        но при этом не держим соединение с базой открытым через await.
        """
        stats: Dict[str, IngestStats] = {source: IngestStats() for source in batches}
        prepared: Dict[str, Tuple[List[_Pending], List[str], bool]] = {}

        for source, (raws, snapshot) in batches.items():
            if not raws:
                logger.info(f"[{source}] нечего принимать")
                prepared[source] = ([], [], False)
                continue
            pending, keep_ids = self._prepare(source, raws, stats[source])
            prepared[source] = (pending, keep_ids, snapshot)

        all_pending = [item for pending, _, _ in prepared.values() for item in pending]
        await self._parse(all_pending)

        for source, (pending, keep_ids, snapshot) in prepared.items():
            if not pending and not keep_ids:
                continue
            self._store(source, pending, keep_ids, stats[source], snapshot)
            logger.info(f"[{source}] {stats[source].summary()}")
        return stats

    # ------------------------------------------------- фаза 1: подготовка
    def _prepare(
            self,
            source: str,
            raws: Sequence[RawRequest],
            stats: IngestStats,
    ) -> Tuple[List[_Pending], List[str]]:
        pending: List[_Pending] = []
        keep_ids: List[str] = []
        now = _now()

        with db.connect(self.db_path) as conn:
            for raw in raws:
                row = conn.execute(
                    "SELECT * FROM requests WHERE source = ? AND source_ref = ?",
                    (source, raw.source_ref),
                ).fetchone()

                if row is None:
                    request_id = self._insert_request(conn, source, raw, now)
                    pending.append(_Pending(request_id, raw, needs_parse=True))
                    stats.requests_new += 1
                    continue

                request_id = row["request_id"]
                unchanged = row["content_hash"] == raw.content_hash
                # Заявка, разбор которой в прошлый раз не удался, считается
                # требующей разбора даже при неизменившемся тексте — иначе
                # единичный сбой LLM навсегда оставил бы её без позиций.
                if unchanged and row["parse_status"] == "ok":
                    conn.execute(
                        "UPDATE requests SET last_seen_at = ? WHERE request_id = ?",
                        (now, request_id),
                    )
                    keep_ids.extend(self._position_ids_of(conn, request_id))
                    stats.requests_unchanged += 1
                    stats.llm_calls_saved += 1
                    continue

                if not unchanged:
                    self._archive_revision(conn, row, now)
                self._update_request(conn, request_id, raw, now, bump=not unchanged)
                pending.append(_Pending(request_id, raw, needs_parse=True))
                if not unchanged:
                    stats.requests_changed += 1

        return pending, keep_ids

    def _insert_request(
            self,
            conn: sqlite3.Connection,
            source: str,
            raw: RawRequest,
            now: str,
    ) -> str:
        request_id, year, seq = next_request_id(conn)
        counterparty = clean_text(raw.counterparty_hint) or ""
        conn.execute(
            """
            INSERT INTO requests (
                request_id, year, seq, source, source_ref, source_name, source_url,
                counterparty, counterparty_raw, raw_text, raw_payload, content_hash,
                revision, received_at, first_seen_at, last_seen_at, parse_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 'pending')
            """,
            (
                request_id, year, seq, source, raw.source_ref,
                raw.source_name or source, raw.source_url,
                counterparty, counterparty, raw.raw_text,
                json.dumps(raw.raw_payload, ensure_ascii=False),
                raw.content_hash,
                raw.received_at.isoformat(timespec="seconds") if raw.received_at else None,
                now, now,
            ),
        )
        return request_id

    @staticmethod
    def _archive_revision(conn: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
        conn.execute(
            """
            INSERT INTO request_revisions
                (request_id, revision, raw_text, raw_payload, content_hash, replaced_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["request_id"], row["revision"], row["raw_text"],
                row["raw_payload"], row["content_hash"], now,
            ),
        )

    @staticmethod
    def _update_request(
            conn: sqlite3.Connection,
            request_id: str,
            raw: RawRequest,
            now: str,
            bump: bool,
    ) -> None:
        conn.execute(
            f"""
            UPDATE requests SET
                raw_text = ?, raw_payload = ?, content_hash = ?,
                source_url = ?, last_seen_at = ?,
                revision = revision + {1 if bump else 0}
            WHERE request_id = ?
            """,
            (
                raw.raw_text,
                json.dumps(raw.raw_payload, ensure_ascii=False),
                raw.content_hash,
                raw.source_url,
                now,
                request_id,
            ),
        )

    @staticmethod
    def _position_ids_of(conn: sqlite3.Connection, request_id: str) -> List[str]:
        rows = conn.execute(
            "SELECT position_id FROM request_positions WHERE request_id = ?",
            (request_id,),
        ).fetchall()
        return [row["position_id"] for row in rows]

    # ----------------------------------------------------- фаза 2: разбор
    async def _parse(self, pending: List[_Pending]) -> None:
        targets = [p for p in pending if p.needs_parse]
        if not targets:
            return
        sem = asyncio.Semaphore(self.llm_concurrency)

        async def parse_chunk(text: str) -> Tuple[Optional[List[Dict[str, Any]]], int, int]:
            async with sem:
                return await self.parser.aparse_raw_ex(text)

        async def parse_one(item: _Pending) -> None:
            chunks = item.raw.parse_chunks or [item.raw.raw_text]
            try:
                results = await asyncio.gather(*[parse_chunk(chunk) for chunk in chunks])
            except Exception as exc:  # noqa: BLE001 — падение одной заявки не должно ронять прогон
                logger.exception(f"[{item.request_id}] разбор упал: {exc}")
                item.error = str(exc)
                return

            collected: List[Dict[str, Any]] = []
            failed = 0
            for parsed, t_in, t_out in results:
                item.tokens_in += t_in
                item.tokens_out += t_out
                if parsed is None:
                    failed += 1
                    continue
                collected.extend(parsed)

            if failed == len(results):
                item.error = "LLM не вернул валидный JSON"
                return
            if failed:
                # Часть кусков разобралась — заявку сохраняем, но помечаем, что
                # она неполная: молча потерять половину поста нельзя.
                item.error = ""
                logger.warning(
                    f"[{item.request_id}] не разобрано {failed} из {len(results)} фрагментов"
                )
            item.parsed = collected

        await asyncio.gather(*[parse_one(item) for item in targets])

    # ------------------------------------------------------ фаза 3: запись
    def _store(
            self,
            source: str,
            pending: List[_Pending],
            keep_ids: List[str],
            stats: IngestStats,
            snapshot: bool,
    ) -> None:
        now = _now()
        keep: set = set(keep_ids)

        with db.connect(self.db_path) as conn:
            normalizer = Normalizer(conn, learn=self.learn)

            for item in pending:
                if item.error:
                    stats.requests_failed += 1
                    conn.execute(
                        "UPDATE requests SET parse_status = 'failed', parse_error = ?, "
                        "parsed_at = ?, llm_tokens_in = ?, llm_tokens_out = ? "
                        "WHERE request_id = ?",
                        (item.error[:500], now, item.tokens_in, item.tokens_out, item.request_id),
                    )
                    # Позиции прошлой удачной версии не гасим: сбой разбора не
                    # повод считать, что потребности больше нет.
                    keep.update(self._position_ids_of(conn, item.request_id))
                    continue

                parsed = item.parsed or []
                position_ids: List[str] = []
                for vac in parsed:
                    merged = dict(vac)
                    for name, value in (item.raw.field_defaults or {}).items():
                        if not merged.get(name):
                            merged[name] = value
                    merged.update(item.raw.field_overrides or {})
                    fields = normalizer.normalize(merged)
                    if not self._is_meaningful(fields):
                        continue
                    position_id, created = self._upsert_position(
                        conn, item.request_id, source, fields, now, claimed=keep
                    )
                    position_ids.append(position_id)
                    # Сразу помечаем занятой: следующая позиция этой же заявки
                    # (например, ночная смена того же объекта) не должна
                    # опознаться как та же самая.
                    keep.add(position_id)
                    self._reindex(conn, position_id, item.request_id, item.raw.raw_text, fields)
                    if created:
                        stats.positions_added += 1
                    else:
                        stats.positions_updated += 1

                conn.execute("DELETE FROM request_positions WHERE request_id = ?", (item.request_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO request_positions (request_id, position_id) VALUES (?, ?)",
                    [(item.request_id, pid) for pid in position_ids],
                )
                keep.update(position_ids)

                # Контрагент заявки: из позиции он точнее, чем подсказка
                # источника, — там уже применён справочник.
                counterparty = self._pick_counterparty(conn, position_ids)
                conn.execute(
                    "UPDATE requests SET parse_status = 'ok', parse_error = '', parsed_at = ?, "
                    "llm_tokens_in = ?, llm_tokens_out = ?, llm_model = ?, "
                    "counterparty = COALESCE(NULLIF(?, ''), counterparty) "
                    "WHERE request_id = ?",
                    (
                        now, item.tokens_in, item.tokens_out,
                        getattr(getattr(self.parser, "llm", None), "model_name", "") or "",
                        counterparty or "",
                        item.request_id,
                    ),
                )

            if snapshot:
                stats.positions_deactivated = self._deactivate_stale(conn, source, keep, now)

    @staticmethod
    def _is_meaningful(fields: Dict[str, Any]) -> bool:
        """Отсекает пустышки.

        После запрета на додумывание модель иногда возвращает объект, в котором
        нет ни должности, ни объекта, ни города — по сути ничего. Заводить под
        это позицию в реестре бессмысленно.
        """
        return any(fields.get(name) for name in ("vacancy_name", "object_name", "city"))

    def _upsert_position(
            self,
            conn: sqlite3.Connection,
            request_id: str,
            source: str,
            fields: Dict[str, Any],
            now: str,
            claimed: Optional[set] = None,
    ) -> Tuple[str, bool]:
        fp = fingerprint(fields)
        row = conn.execute(
            "SELECT * FROM positions WHERE source = ? AND fingerprint = ?",
            (source, fp),
        ).fetchone()
        if row is None:
            row = self._rescue_match(conn, source, fields, fp, claimed)

        if row is None:
            position_id, seq = next_position_id(conn, request_id)
            columns = ["position_id", "seq", "first_request_id", "last_request_id",
                       "source", "fingerprint", "is_active",
                       "first_seen_at", "last_seen_at", "updated_at"] + DATA_FIELDS
            values = [position_id, seq, request_id, request_id, source, fp, 1, now, now, now]
            values += [fields.get(name) for name in DATA_FIELDS]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO positions ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
            return position_id, True

        position_id = row["position_id"]
        changes = []
        for name in DATA_FIELDS:
            old = row[name]
            new = fields.get(name)
            # Пустое новое значение не затирает известное старое: в снимке
            # источника поле могло просто не повториться.
            if new is None:
                continue
            if _differs(old, new):
                changes.append((name, old, new))

        if changes:
            assignments = ", ".join(f"{name} = ?" for name, _, _ in changes)
            params = [new for _, _, new in changes]
            params += [request_id, now, now, position_id]
            conn.execute(
                f"UPDATE positions SET {assignments}, last_request_id = ?, "
                f"last_seen_at = ?, updated_at = ?, is_active = 1 WHERE position_id = ?",
                params,
            )
            conn.executemany(
                "INSERT INTO position_history "
                "(position_id, request_id, field, old_value, new_value, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (position_id, request_id, name,
                     None if old is None else str(old),
                     None if new is None else str(new), now)
                    for name, old, new in changes
                ],
            )
        else:
            conn.execute(
                "UPDATE positions SET last_request_id = ?, last_seen_at = ?, is_active = 1 "
                "WHERE position_id = ?",
                (request_id, now, position_id),
            )
        return position_id, False

    @staticmethod
    def _rescue_match(
            conn: sqlite3.Connection,
            source: str,
            fields: Dict[str, Any],
            new_fingerprint: str,
            claimed: Optional[set] = None,
    ) -> Optional[sqlite3.Row]:
        """Ищет позицию по сути, когда fingerprint не совпал.

        Fingerprint включает work_format и shift_type, а они меняются не
        только когда меняется реальность: после запрета на додумывание смена
        у части позиций стала пустой вместо «day». Без этой подстраховки
        каждая такая позиция завелась бы заново с новым ID, а прежняя
        погасла бы как исчезнувшая — то есть у половины реестра оборвалась бы
        история ровно на изменении промпта.

        Ищем по «контрагент + город + должность + объект», причём пустое поле
        считается совместимым с любым значением, а не отдельным значением.
        Это принципиально: на боевом прогоне у Аметиста город стал пустым (в
        его таблице нет такой колонки, а выводить город из названия объекта
        модели больше нельзя), и сравнение «пусто = пусто» не находило
        «Грузчик / ДНС Пушкино / Пушкино» — заводился дубль.

        Три ограничения, без которых подстраховка превращается в склейку:

        1. Смена и формат работы должны быть СОВМЕСТИМЫ: пусто с любым, но
           «день» с «ночью» — нет. Иначе ночная позиция «Миксита» усыновляет
           дневную, и потребность двух смен схлопывается в одну.
        2. Позиции, уже занятые в этом же прогоне, исключаются: они заведомо
           другие, их только что подтвердила эта же или соседняя заявка.
        3. Совпадение должно быть ЕДИНСТВЕННЫМ.
        """
        identity = ("counterparty", "city", "vacancy_name", "object_name")
        if not fields.get("vacancy_name") and not fields.get("object_name"):
            return None

        conditions = []
        params: List[Any] = [source]
        for name in identity:
            value = fields.get(name)
            if value is None:
                continue
            conditions.append(f"({name} = ? OR {name} IS NULL)")
            params.append(value)
        if not conditions:
            return None

        # Различающие поля: пусто совместимо с чем угодно, разные значения — нет.
        for name in ("shift_type", "work_format"):
            value = fields.get(name)
            if value is None:
                continue
            conditions.append(f"({name} IS NULL OR {name} = ?)")
            params.append(value)

        if claimed:
            placeholders = ", ".join("?" for _ in claimed)
            conditions.append(f"position_id NOT IN ({placeholders})")
            params.extend(claimed)

        rows = conn.execute(
            f"SELECT * FROM positions WHERE source = ? AND {' AND '.join(conditions)} LIMIT 2",
            params,
        ).fetchall()
        if len(rows) != 1:
            return None

        row = rows[0]
        conn.execute(
            "UPDATE positions SET fingerprint = ? WHERE position_id = ?",
            (new_fingerprint, row["position_id"]),
        )
        logger.info(
            f"[{row['position_id']}] fingerprint обновлён "
            f"({row['fingerprint']} → {new_fingerprint}), позиция сохранена"
        )
        return row

    @staticmethod
    def _reindex(
            conn: sqlite3.Connection,
            position_id: str,
            request_id: str,
            raw_text: str,
            fields: Dict[str, Any],
    ) -> None:
        """Обновляет полнотекстовый индекс позиции.

        В тело индекса идёт и исходный текст заявки: менеджер ищет «Молком»
        или «Хостел Березка», не зная, попало это слово в распознанное поле
        или осталось только в исходнике.
        """
        parts = [raw_text or ""]
        parts += [str(fields.get(name)) for name in SEARCHABLE_FIELDS if fields.get(name)]
        conn.execute("DELETE FROM search_index WHERE position_id = ?", (position_id,))
        conn.execute(
            "INSERT INTO search_index (position_id, request_id, body) VALUES (?, ?, ?)",
            (position_id, request_id, "\n".join(parts)),
        )

    @staticmethod
    def _pick_counterparty(conn: sqlite3.Connection, position_ids: List[str]) -> str:
        if not position_ids:
            return ""
        placeholders = ", ".join("?" for _ in position_ids)
        row = conn.execute(
            f"SELECT counterparty, COUNT(*) AS cnt FROM positions "
            f"WHERE position_id IN ({placeholders}) AND counterparty IS NOT NULL "
            f"GROUP BY counterparty ORDER BY cnt DESC LIMIT 1",
            position_ids,
        ).fetchone()
        return row["counterparty"] if row else ""

    @staticmethod
    def _deactivate_stale(
            conn: sqlite3.Connection,
            source: str,
            keep: set,
            now: str,
    ) -> int:
        """Гасит позиции источника, которых нет в текущем снимке."""
        if keep:
            placeholders = ", ".join("?" for _ in keep)
            sql = (
                f"UPDATE positions SET is_active = 0, updated_at = ? "
                f"WHERE source = ? AND is_active = 1 AND position_id NOT IN ({placeholders})"
            )
            params = [now, source] + list(keep)
        else:
            sql = (
                "UPDATE positions SET is_active = 0, updated_at = ? "
                "WHERE source = ? AND is_active = 1"
            )
            params = [now, source]
        return conn.execute(sql, params).rowcount


def _differs(old: Any, new: Any) -> bool:
    """Сравнение старого и нового значения поля.

    SQLite возвращает числа числами, а из нормализации они тоже приходят
    числами — но после миграции из Sheets в колонке может лежать строка.
    Сравниваем по строковому представлению, чтобы не плодить пустые записи в
    истории изменений.
    """
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    if isinstance(old, float) or isinstance(new, float):
        try:
            return abs(float(old) - float(new)) > 1e-9
        except (TypeError, ValueError):
            pass
    return str(old).strip() != str(new).strip()
