import asyncio
from typing import Dict, List, Optional

from loguru import logger

from registry.models import RawRequest
from registry.sources import SOURCE_YAPPI, unique_ref


class YappiToVacanciesService:
    """Парсит таблицу Yappi (Seatable external link): одна строка = одна вакансия."""

    EMPTY_NEED_TOKENS = {"стоп", "заявка стоп", "нет", "0", "-", "—", ""}
    DEFAULT_LLM_CONCURRENCY = 5

    def __init__(self, seatable_client, sheets_service, llm_parser):
        """
        :param seatable_client: экземпляр SeatableExternalLinkClient
        :param sheets_service: GoogleSheetsService для записи в целевую таблицу
        :param llm_parser: VacancyParserService
        """
        self.seatable = seatable_client
        self.sheets = sheets_service
        self.parser = llm_parser

    async def collect_requests(
            self,
            table_name: str = None,
            counterparty_name: str = "Yappi",
            source_url: str = "",
    ) -> List[RawRequest]:
        """Отдаёт строки Seatable как заявки для реестра."""
        rows = await asyncio.to_thread(self.seatable.fetch_table_as_dicts, table_name)
        if not rows:
            logger.info("[Yappi] таблица пустая или не удалось прочитать")
            return []

        seen: set = set()
        out: List[RawRequest] = []
        for row in rows:
            need_raw = (row.get("ПОТРЕБНОСТЬ") or "").strip().lower()
            if not need_raw or any(stop in need_raw for stop in ("стоп", "заявка стоп")):
                continue
            if need_raw in self.EMPTY_NEED_TOKENS:
                continue
            project = (row.get("ПРОЕКТ") or "").strip()
            vacancy = (row.get("ВАКАНСИЯ") or "").strip()
            # У строк Seatable есть собственный _id — он и есть самый
            # устойчивый ключ документа. Если его нет, собираем из полей.
            ref = row.get("_id") or f"{project}|{vacancy}|{row.get('ГОРОД', '')}"
            out.append(RawRequest(
                source=SOURCE_YAPPI,
                source_ref=unique_ref(str(ref), seen),
                raw_text=self._row_to_text(row, counterparty_name),
                source_name=counterparty_name,
                source_url=source_url,
                counterparty_hint=counterparty_name,
                raw_payload=dict(row),
                field_defaults={"counterparty": counterparty_name},
            ))

        logger.info(f"[Yappi] из таблицы взято {len(out)} активных позиций")
        return out

    async def extract_and_save(
            self,
            target_spreadsheet_id: str,
            target_sheet_name: str = "Лист1",
            table_name: str = None,
            counterparty_name: str = "Yappi",
            source_url: str = "",
            llm_concurrency: int = None,
            sheets_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, int]:
        rows = await asyncio.to_thread(
            self.seatable.fetch_table_as_dicts, table_name
        )
        if not rows:
            return {"added": 0, "updated": 0, "skipped": 0}

        # Фильтруем строки без реальной потребности
        items = []
        for row in rows:
            need_raw = (row.get("ПОТРЕБНОСТЬ") or "").strip().lower()
            if not need_raw or any(stop in need_raw for stop in ("стоп", "заявка стоп")):
                continue
            if need_raw in self.EMPTY_NEED_TOKENS:
                continue
            text = self._row_to_text(row, counterparty_name)
            label = row.get("ВАКАНСИЯ") or row.get("ПРОЕКТ") or "<без названия>"
            items.append((label, text))

        if not items:
            return {"added": 0, "updated": 0, "skipped": 0}

        sem = asyncio.Semaphore(llm_concurrency or self.DEFAULT_LLM_CONCURRENCY)

        async def parse_one(label: str, text: str):
            async with sem:
                logger.info(f"[Yappi] парсим: {label}")
                parsed = await self.parser.aparse(text, source="Yappi", source_url=source_url)
                return label, text, parsed

        results = await asyncio.gather(*[parse_one(l, t) for l, t in items])

        all_vacancies: List[Dict] = []
        for label, text, parsed in results:
            if not parsed:
                logger.warning(f"[Yappi] парсер вернул пустой результат для строки: {label}")
                continue
            for vac in parsed:
                vac.setdefault("source", "Yappi")
                vac.setdefault("source_url", source_url)
                vac.setdefault("original_message", text)
            all_vacancies.extend(parsed)

        if not all_vacancies:
            return {"added": 0, "updated": 0, "skipped": 0}

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

    @staticmethod
    def _row_to_text(row: Dict[str, str], counterparty: str) -> str:
        lines = [f"Источник: {counterparty}"]
        for key, value in row.items():
            if not key or not value:
                continue
            lines.append(f"{key}: {value}")
        return "\n".join(lines)
