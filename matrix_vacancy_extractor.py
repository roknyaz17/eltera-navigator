import asyncio
from typing import Dict, List, Optional

from loguru import logger

from registry.models import RawRequest
from registry.sources import SOURCE_KPK, unique_ref


class MatrixToVacanciesService:
    """Преобразует матрицу проектов в вакансии через LLM. Берёт содержимое столбцов как есть."""

    DEFAULT_LLM_CONCURRENCY = 5

    def __init__(self, sheets_service, llm_parser):
        self.sheets = sheets_service
        self.parser = llm_parser

    async def collect_requests(
            self,
            matrix_spreadsheet_id: str,
            matrix_sheet_name: str = None,
            counterparty_name: str = "КНК",
            source_url: str = "",
    ) -> List[RawRequest]:
        """Отдаёт столбцы матрицы (один город = одна заявка) для реестра."""
        matrix_data = await asyncio.to_thread(
            self.sheets.get_project_matrix_data_all,
            matrix_spreadsheet_id, matrix_sheet_name,
        )
        if not matrix_data:
            logger.info("[КНК] матрица пустая или не удалось прочитать")
            return []

        seen: set = set()
        out: List[RawRequest] = []
        for city, raw_props in matrix_data.items():
            need_str = (raw_props.get("Потребность") or "").strip()
            if need_str in ("0", ""):
                continue
            out.append(RawRequest(
                source=SOURCE_KPK,
                source_ref=unique_ref(city, seen),
                raw_text=self._props_to_text(city, raw_props, counterparty_name),
                source_name=counterparty_name,
                source_url=source_url,
                counterparty_hint=counterparty_name,
                raw_payload={"city": city, **raw_props},
                field_defaults={"counterparty": counterparty_name, "city": city},
            ))

        logger.info(f"[КНК] из матрицы взято {len(out)} городов с потребностью")
        return out

    async def extract_and_save(
            self,
            matrix_spreadsheet_id: str,
            target_spreadsheet_id: str,
            target_sheet_name: str = "Лист1",
            matrix_sheet_name: str = None,
            counterparty_name: str = "КНК",
            source_url: str = "",
            llm_concurrency: int = None,
            sheets_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, int]:
        # Чтение Sheets — sync, выносим в thread
        matrix_data = await asyncio.to_thread(
            self.sheets.get_project_matrix_data_all,
            matrix_spreadsheet_id, matrix_sheet_name,
        )
        if not matrix_data:
            return {"added": 0, "updated": 0, "skipped": 0}

        # Отфильтровываем города с нулевой потребностью
        items = []
        for city, raw_props in matrix_data.items():
            need_str = (raw_props.get("Потребность") or "").strip()
            if need_str in ("0", ""):
                continue
            text = self._props_to_text(city, raw_props, counterparty_name)
            items.append((city, text))

        if not items:
            return {"added": 0, "updated": 0, "skipped": 0}

        sem = asyncio.Semaphore(llm_concurrency or self.DEFAULT_LLM_CONCURRENCY)

        async def parse_one(city: str, text: str):
            async with sem:
                logger.info(f"[КНК] парсим: {city}")
                parsed = await self.parser.aparse(text, source="КНК", source_url=source_url)
                return city, text, parsed

        results = await asyncio.gather(*[parse_one(c, t) for c, t in items])

        # Собираем все вакансии в один батч
        all_vacancies: List[Dict] = []
        for city, text, parsed in results:
            if not parsed:
                logger.warning(f"[КНК] парсер вернул пустой результат для {city}")
                continue
            for vac in parsed:
                vac.setdefault("counterparty", counterparty_name)
                vac.setdefault("source", "КНК Матрица")
                vac.setdefault("source_url", source_url)
                vac.setdefault("original_message", text)
            all_vacancies.extend(parsed)

        if not all_vacancies:
            return {"added": 0, "updated": 0, "skipped": 0}

        # Один батч-upsert под общим lock
        lock = sheets_lock or asyncio.Lock()
        async with lock:
            result = await asyncio.to_thread(
                self.sheets.upsert_vacancies,
                spreadsheet_id=target_spreadsheet_id,
                vacancies=all_vacancies,
                sheet_name=target_sheet_name,
                original_msg=None,
            )
        return result

    def _props_to_text(self, city: str, props: Dict, counterparty: str) -> str:
        """
        Склеивает все пары «ключ: значение» из словаря в один текст.
        """
        lines = [
            f"Город: {city}",
            f"Контрагент: {counterparty}",
        ]
        for key, value in props.items():
            if key.startswith('_'):
                continue
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}: {value}")
        return "\n".join(lines)
