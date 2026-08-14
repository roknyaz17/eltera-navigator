"""
Извлекатель вакансий Аметиста из Google Таблицы.

Раньше Аметист парсился из Telegram-чата (см. ametist_message_processor.py),
но в чате один пост содержит десятки позиций («📅 Обновляем потребность…»),
которые LLM плохо делит — разные проекты (Ламода/Мираторг/Мултон) сливались
в одну вакансию. Контрагенты ведут эту же информацию в отдельной Google
Таблице, по одной строке на позицию — это надёжнее.

Особенности листа:
- Шапка: «Номер, Объект, Потребность, Срок вахты, Территориально/общежитие,
  Граждансво, колличество общее, Возраст, СБ, Должность, График, Ставка в
  смену, Питание, Проживание, ЛМК, Форма».
- «Строки-разделители регионов» (заполнена ТОЛЬКО первая ячейка) —
  «Ростовская область», «САРАТОВСКАЯ область», «Санкт-Петербург»,
  «Москва и МО». Эти строки задают регион для всех data-строк ниже,
  пока не встретится следующий разделитель.
- В data-строке поле «Номер» может быть пустым — это вторая (третья…)
  позиция того же объекта (другая должность/ставка). Считаем её
  самостоятельной вакансией, в vacancy_id её отличит «Должность».
- «Потребность=0» (или прочее «пусто/нет/—») — позиция временно неактивна:
  пропускаем парсинг. Snapshot-lifecycle такие пометит is_active=FALSE.

- Колонка «Ссылка на описание вакансии» ведёт на пост в Telegram-канале
  Аметиста. В строке — структура (объект, потребность, ставка), в посте —
  подробности, которых в строке физически нет: адрес, часы смен, оплата по
  ролям, что выдают, условия заселения. Пост забирается ДО вызова LLM и
  подаётся разбору вместе со строкой (см. telegram_post_fetcher).

Подход к парсингу — как у MarketstaffSheetExtractor: одна строка → один
LLM-вызов, полученные вакансии форсятся в source=«Аметист»,
counterparty=«Аметист», а object_name (если LLM не определил) — берётся из
столбца «Объект».
"""

import asyncio
from typing import Dict, List, Optional, Tuple

from loguru import logger

from registry.models import RawRequest
from registry.sources import SOURCE_AMETIST, unique_ref
from vacancy_parser import make_vacancy_id


class AmetistSheetExtractor:
    """Парсер таблицы Аметиста: одна data-строка = одна вакансия."""

    EMPTY_NEEDS = {"", "0", "0м", "0ж", "0м/ж", "стоп", "нет", "-", "—"}
    DEFAULT_LLM_CONCURRENCY = 5
    SOURCE_NAME = "Аметист"
    COUNTERPARTY = "Аметист"
    # Заголовок колонки со ссылкой на пост. Ищем по подстроке: контрагент уже
    # переименовывал колонки, а «ссылка» в названии остаётся.
    LINK_COLUMN_HINT = "ссылк"

    def __init__(self, sheets_service, llm_parser, post_fetcher=None):
        self.sheets = sheets_service
        self.parser = llm_parser
        # Без фетчера всё работает как раньше — по одной строке таблицы.
        self.post_fetcher = post_fetcher

    async def collect_requests(
            self,
            ametist_spreadsheet_id: str,
            ametist_sheet_name: str = "Потребность ",
            source_url: str = "",
    ) -> List[RawRequest]:
        """Отдаёт строки листа как заявки для реестра.

        Читаем тем же _read_sheet, что и старый путь: разбор особенностей
        листа (строки-разделители регионов, пустой «Номер» у второй позиции
        объекта) остаётся здесь, реестр про них знать не должен.
        """
        rows_with_region = await asyncio.to_thread(
            self._read_sheet, ametist_spreadsheet_id, ametist_sheet_name
        )
        if not rows_with_region:
            logger.info("[Аметист] таблица пустая или не удалось прочитать")
            return []

        posts = await self._fetch_posts(rows_with_region)

        seen: set = set()
        out: List[RawRequest] = []
        for region, row in rows_with_region:
            need_str = (row.get("Потребность") or "").strip().lower()
            if need_str in self.EMPTY_NEEDS:
                continue
            object_name = (row.get("Объект") or "").strip()
            if not object_name:
                continue
            position = (row.get("Должность") or "").strip()
            out.append(RawRequest(
                source=SOURCE_AMETIST,
                source_ref=unique_ref(f"{object_name}|{position}", seen),
                raw_text=self._row_to_text(row, region, posts.get(self._row_link(row), "")),
                source_name=self.SOURCE_NAME,
                source_url=source_url,
                counterparty_hint=self.COUNTERPARTY,
                raw_payload={"region": region, **row},
                field_overrides={"counterparty": self.COUNTERPARTY},
                # Объект из колонки — только если модель ничего не нашла:
                # в тексте позиции он обычно есть и сформулирован точнее.
                field_defaults={"object_name": object_name},
            ))

        logger.info(f"[Аметист] из таблицы взято {len(out)} активных позиций")
        return out

    async def extract_and_save(
            self,
            ametist_spreadsheet_id: str,
            target_spreadsheet_id: str,
            target_sheet_name: str = "Лист1",
            ametist_sheet_name: str = "Потребность ",
            source_url: str = "",
            llm_concurrency: int = None,
            sheets_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, int]:
        empty_stats = {"added": 0, "updated": 0, "skipped": 0, "deactivated": 0}

        rows_with_region = await asyncio.to_thread(
            self._read_sheet, ametist_spreadsheet_id, ametist_sheet_name
        )
        if not rows_with_region:
            logger.info("[Аметист] таблица пустая или не удалось прочитать")
            return empty_stats

        posts = await self._fetch_posts(rows_with_region)

        # Готовим набор data-строк для LLM
        items: List[Tuple[str, str, str]] = []  # (label, text, object_name)
        for region, row in rows_with_region:
            need_str = (row.get("Потребность") or "").strip().lower()
            if need_str in self.EMPTY_NEEDS:
                continue
            object_name = (row.get("Объект") or "").strip()
            if not object_name:
                continue
            text = self._row_to_text(row, region, posts.get(self._row_link(row), ""))
            label = f"{object_name} ({region or 'без региона'})"
            items.append((label, text, object_name))

        logger.info(f"[Аметист] из таблицы взято {len(items)} активных позиций")
        if not items:
            return empty_stats

        sem = asyncio.Semaphore(llm_concurrency or self.DEFAULT_LLM_CONCURRENCY)

        async def parse_one(label: str, text: str, object_name: str):
            async with sem:
                logger.info(f"[Аметист] парсим: {label}")
                parsed = await self.parser.aparse(
                    text, source=self.SOURCE_NAME, source_url=source_url
                )
                return label, text, object_name, parsed

        results = await asyncio.gather(
            *[parse_one(l, t, o) for l, t, o in items]
        )

        all_vacancies: List[Dict] = []
        for label, text, object_name, parsed in results:
            if not parsed:
                logger.warning(f"[Аметист] парсер вернул пустой результат: {label}")
                continue
            for vac in parsed:
                # Форсируем единое наименование источника и контрагента, плюс
                # перезаписываем original_message нашим строковым представлением
                # строки из таблицы. setdefault тут НЕ годится: LLM возвращает
                # ключ original_message с пустой строкой, и setdefault считает,
                # что значение уже есть, — в результате при ре-апсерте у строки
                # сохраняется старое (например, от прошлого TG-парсинга) сообщение.
                vac["source"] = self.SOURCE_NAME
                vac["counterparty"] = self.COUNTERPARTY
                vac["source_url"] = source_url
                vac["original_message"] = text
                # Если LLM не определил object_name — кладём «Объект» из таблицы.
                # Это критично: object_name участвует в vacancy_id и в поиске.
                if not (vac.get("object_name") or "").strip():
                    vac["object_name"] = object_name
            all_vacancies.extend(parsed)

        if not all_vacancies:
            return empty_stats

        # Считаем vacancy_id заранее, чтобы знать актуальный snapshot
        # (то, что есть в таблице на момент прогона). Всё, чего нет здесь,
        # пометим is_active=FALSE через deactivate_stale_vacancies.
        keep_ids = set()
        for vac in all_vacancies:
            vid = (vac.get("vacancy_id") or "").strip() or make_vacancy_id(vac)
            vac["vacancy_id"] = vid
            keep_ids.add(vid)

        lock = sheets_lock or asyncio.Lock()
        async with lock:
            result = await asyncio.to_thread(
                self.sheets.upsert_vacancies,
                spreadsheet_id=target_spreadsheet_id,
                vacancies=all_vacancies,
                sheet_name=target_sheet_name,
                original_msg=None,
            )
            deactivated = await asyncio.to_thread(
                self.sheets.deactivate_stale_vacancies,
                spreadsheet_id=target_spreadsheet_id,
                source=self.SOURCE_NAME,
                keep_ids=keep_ids,
                sheet_name=target_sheet_name,
            )
        result["deactivated"] = deactivated
        return result

    # ---------- internals ----------

    def _read_sheet(self, spreadsheet_id: str, sheet_name: str) -> List[Tuple[str, Dict[str, str]]]:
        """Читает лист и возвращает [(current_region, {col: val})] для data-строк.

        Разделители регионов — строки, где заполнена ТОЛЬКО первая ячейка.
        Хранится в running-variable current_region и подмешивается к каждой
        следующей data-строке, пока не сменится.
        """
        sh = self.sheets.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
        all_values = ws.get_all_values()
        if not all_values:
            return []

        headers = [(h or "").strip() for h in all_values[0]]
        if not headers:
            return []

        result: List[Tuple[str, Dict[str, str]]] = []
        current_region = ""
        for raw in all_values[1:]:
            cells = [(c or "").strip() for c in raw]
            if not any(cells):
                continue
            # «Разделитель региона»: только первая ячейка непустая
            if cells[0] and not any(cells[1:]):
                current_region = cells[0]
                continue
            entry: Dict[str, str] = {}
            for i, col in enumerate(headers):
                if not col:
                    continue
                entry[col] = cells[i] if i < len(cells) else ""
            result.append((current_region, entry))
        return result

    def _row_link(self, row: Dict[str, str]) -> str:
        """Ссылка на пост из строки: по названию колонки, иначе по значению."""
        for key, value in row.items():
            if key and self.LINK_COLUMN_HINT in key.lower() and (value or "").strip():
                return value.strip()
        for value in row.values():
            if value and "t.me/" in value:
                return value.strip()
        return ""

    async def _fetch_posts(self, rows_with_region: List[Tuple[str, Dict[str, str]]]) -> Dict[str, str]:
        """Тексты постов по всем ссылкам листа за одно подключение к Telegram."""
        if not self.post_fetcher:
            return {}
        links = {self._row_link(row) for _, row in rows_with_region}
        links.discard("")
        if not links:
            return {}
        posts = await self.post_fetcher.fetch_many(links)
        found = sum(1 for link in links if posts.get(link))
        logger.info(f"[Аметист] описаний из Telegram: {found} из {len(links)} ссылок")
        return posts

    def _row_to_text(self, row: Dict[str, str], region: str, post_text: str = "") -> str:
        """Собирает текст одной позиции для LLM.

        Источник и регион подаём явно, чтобы LLM не пытался угадывать.
        Прочие поля идут «как есть» с русскими названиями колонок — LLM
        понимает их без отдельного маппинга.

        Описание из Telegram кладём отдельным блоком и прямо говорим, что
        строка таблицы главнее: в посте бывает несколько ролей и старые ставки,
        а потребность и актуальная ставка ведутся именно в таблице.
        """
        lines = [f"Источник: {self.SOURCE_NAME}"]
        if region:
            lines.append(f"Регион: {region}")
        for key, value in row.items():
            if not key or not value:
                continue
            lines.append(f"{key}: {value}")
        if post_text:
            lines.append("")
            lines.append(
                "Ниже — описание вакансии из Telegram-канала контрагента. Оно ДОПОЛНЯЕТ "
                "строку таблицы, а не заменяет её: всё, что указано в строке выше "
                "(питание, медкнижка, проживание, гражданство, потребность, требования, "
                "ставка), обязательно должно попасть в ответ. Из описания бери то, чего "
                "в строке нет. При расхождении значений верна строка таблицы."
            )
            lines.append(post_text)
        return "\n".join(lines)
