"""
In-memory кеш для отображения вакансий из Google Sheets.

Паттерн: stale-while-revalidate.
- Самый первый запрос (нет данных вообще) ждёт fetch синхронно.
- Дальше get() всегда возвращает данные мгновенно — из памяти.
- Когда TTL истёк, отдаём старые данные, а в фоне запускаем обновление.
- Пока фоновое обновление идёт, последующие get() возвращают старые данные.
- Параллельные триггеры обновления защищены asyncio.Lock — в один момент идёт максимум один fetch.
- На сам fetch навешен timeout, чтобы зависший Sheets API не подвесил кеш навсегда.
"""

import asyncio
import time
from typing import Dict, List, Optional

from loguru import logger

try:
    import metrics as M
    _METRICS_OK = True
except ImportError:
    _METRICS_OK = False


class VacanciesCache:
    FETCH_TIMEOUT_SECONDS = 30

    def __init__(
            self,
            sheets_service,
            spreadsheet_id: str,
            sheet_name: str = "Лист1",
            ttl_seconds: int = 120,
    ):
        self._sheets = sheets_service
        self._spreadsheet_id = spreadsheet_id
        self._sheet_name = sheet_name
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._data: Optional[List[Dict[str, str]]] = None
        self._timestamp: float = 0.0
        self._refresh_task: Optional[asyncio.Task] = None

    # -------------------------------------------------- internal fetch
    def _fetch_sync(self) -> List[Dict[str, str]]:
        """Синхронное чтение Sheets — крутится в worker-треде."""
        logger.info(f"[cache] fetch from Sheets ({self._spreadsheet_id}/{self._sheet_name})")
        sh = self._sheets.client.open_by_key(self._spreadsheet_id)
        ws = sh.worksheet(self._sheet_name)
        all_values = ws.get_all_values()
        if not all_values:
            return []
        headers = [(h or "").strip() for h in all_values[0]]
        rows: List[Dict[str, str]] = []
        for row in all_values[1:]:
            entry = {}
            for i, col in enumerate(headers):
                if not col:
                    continue
                entry[col] = row[i] if i < len(row) else ""
            if any((v or "").strip() for v in entry.values()):
                rows.append(entry)
        logger.info(f"[cache] получено {len(rows)} строк")
        return rows

    async def _refresh(self, mode: str = "foreground") -> None:
        """Перечитываем Sheets и обновляем _data + _timestamp.
        На ошибках только логируем — старые данные остаются.
        mode: 'foreground' (юзер ждёт) или 'background' (stale-while-revalidate)."""
        try:
            new_data = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_sync),
                timeout=self.FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(f"[cache] fetch таймаут после {self.FETCH_TIMEOUT_SECONDS}s — оставляем старые данные")
            if _METRICS_OK:
                M.CACHE_FETCH_FAILURES.labels(reason="timeout").inc()
            return
        except Exception:
            logger.exception("[cache] fetch упал — оставляем старые данные")
            if _METRICS_OK:
                M.CACHE_FETCH_FAILURES.labels(reason="exception").inc()
            return

        self._data = new_data
        self._timestamp = time.time()
        if _METRICS_OK:
            M.CACHE_REFRESHES.labels(mode=mode).inc()

    def _is_stale(self) -> bool:
        return time.time() - self._timestamp > self._ttl

    async def _background_refresh(self) -> None:
        """Запускается через create_task. Lock сериализует параллельные вызовы:
        второй ждёт первый, после освобождения видит свежий timestamp и сразу выходит."""
        async with self._lock:
            if self._is_stale():
                await self._refresh(mode="background")

    # ----------------------------------------------------------- public
    async def get(self, force_refresh: bool = False) -> List[Dict[str, str]]:
        """
        Возвращает кешированные строки.
        - Если данных ещё нет — ждёт первый fetch синхронно.
        - Если есть и не stale — возвращает мгновенно.
        - Если есть, но stale — возвращает старые мгновенно, в фоне обновляет.
        - force_refresh=True — ждём свежий fetch (используется редко, для отладки).
        """
        # Самый первый раз или принудительное обновление — ждём синхронно
        if self._data is None or force_refresh:
            async with self._lock:
                if self._data is None or force_refresh:
                    await self._refresh(mode="foreground")
            return self._data or []

        # Данные есть, но устарели — отдаём старые, обновление в фоне
        if self._is_stale():
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._background_refresh())
        else:
            if _METRICS_OK:
                M.CACHE_HITS.inc()

        return self._data

    def invalidate(self) -> None:
        """Помечает кеш как устаревший. Данные НЕ удаляются — пользователь
        получит их мгновенно, а в фоне запустится refresh при следующем get()."""
        self._timestamp = 0.0
