"""
Извлекатель вакансий Маркетстаффа из Google Таблицы «Объекты МС»
(лист «Объекты МО»): одна строка = одна позиция.

Особенности листа, из-за которых нельзя просто читать get_all_records():

1. Блоки объектов с объединёнными ячейками.
   Один объект занимает несколько строк (по строке на должность). Колонки
   «Объект», «Город», «График», «Адрес общежития», «Вычеты», «Особенности СНГ»
   заполнены ТОЛЬКО в первой строке блока — ниже пустые ячейки (merge).
   Поэтому значения протягиваются вниз в пределах блока (_forward_fill).
   Новый блок начинается, когда меняется «Объект» ИЛИ «Город»: у части блоков
   «Объект» повторяется в каждой строке (Холодильник.ру), у части — стоит только
   сверху (ОЗОН ФФ), а у одного блока (п. Быково) «Объект» вообще пустой и
   опознать блок можно лишь по смене города.

2. Колонка «Общие положения для всех обьектов» заполнена один раз на весь лист.
   Любая колонка с «для всех» в заголовке считается общелистовой и
   подмешивается в каждую строку.

3. Заголовки колонок 4–7 НЕ соответствуют содержимому части строк.
   В блоках Холодильник.ру / Артиленд / Самара порядок совпадает с шапкой
   («Срок вахты», «Возраст от 18», «Обучение», «опыт работы/требования»),
   а в блоках Быково / ОЗОН / Яндекс / Седрус те же четыре ячейки заполнены
   в другом порядке: обучение, доплаты по сменам, срок вахты, возраст.
   Доверять шапке на этих четырёх колонках нельзя, поэтому значения
   классифицируются ПО СОДЕРЖИМОМУ (_classify_unstable):
     - «35/45/60», «15/35/45/60»  → срок вахты в сменах;
     - «до 45 лет», «от 18 до 50 лет» → возраст;
     - остальное → нейтральная корзина «Обучение и требования».
   Так LLM не получает противоречивых подписей и min_shifts/age_from/age_to
   считаются верно независимо от того, как заполнен конкретный блок.

4. Активна только позиция с открытой заявкой.
   «Заявка Мужчкины»/«Заявка Женщины» = ДА или непустое «КОЛИЧЕСТВО» —
   значит идёт набор. «Нет/Нет» без количества — объект в каталоге есть,
   но набора сейчас нет. Явный стоп-маркер («заявка закрыта!») перебивает
   всё остальное. Неактивные строки не парсятся, а snapshot-lifecycle
   (deactivate_stale_vacancies) гасит их is_active=FALSE.

Подход к парсингу — как у AmetistSheetExtractor: одна строка → один LLM-вызов,
source/counterparty форсятся в «Маркетстафф», object_name при пустом ответе LLM
берётся из колонки «Объект».
"""

import asyncio
import re
from typing import Dict, List, Optional, Tuple

from loguru import logger

from registry.models import RawRequest
from registry.sources import SOURCE_MARKETSTAFF, unique_ref
from vacancy_parser import make_vacancy_id

# «35/45/60» — набор допустимых сроков вахты в сменах. Требуем минимум два
# числа и каждое >= 10, чтобы не спутать с графиком «6/1 или 7/0».
_SHIFT_TERM_RE = re.compile(r"^\d{1,3}(?:\s*/\s*\d{1,3})+$")
# «до 45 лет», «от 18 до 50 лет», «До 60 лет».
_AGE_RE = re.compile(r"(?:от\s*\d{1,2}\s*)?до\s*\d{1,2}\s*(?:лет|года?)", re.IGNORECASE)
# «с 09:00 до 21:00» — интервал смены в колонке «График».
_TIME_RANGE_RE = re.compile(r"с\s*(\d{1,2}):(\d{2})\s*до\s*(\d{1,2}):(\d{2})", re.IGNORECASE)


def _norm_space(value: str) -> str:
    """Схлопывает переводы строк и повторные пробелы в заголовках/ячейках.

    В шапке листа переносы внутри ячеек («Зарплата \\nза смену»), а в данных —
    висячие пробелы и пустые строки в конце.
    """
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def _key(value: str) -> str:
    """Ключ для сравнения «тот же объект/город или уже другой»."""
    return _norm_space(value).lower().replace("ё", "е")


class MarketstaffSheetExtractor:
    """Парсер таблицы Маркетстаффа: одна data-строка = одна вакансия."""

    SOURCE_NAME = "Маркетстафф"
    COUNTERPARTY = "Маркетстафф"
    DEFAULT_LLM_CONCURRENCY = 5

    # Ячейки этих колонок объединены по блоку объекта — тянем значение вниз.
    BLOCK_FILL_KEYS = ("schedule", "housing", "deductions", "cis")

    # «ДА» в колонках «Заявка Мужчкины» / «Заявка Женщины».
    YES_VALUES = {"да", "есть", "+", "да!", "yes"}
    # Явный стоп в «КОЛИЧЕСТВО» — перебивает даже «Заявка = ДА».
    CLOSED_MARKERS = ("закрыт", "стоп", "не набира", "пауза", "приостанов")

    def __init__(self, sheets_service, llm_parser):
        self.sheets = sheets_service
        self.parser = llm_parser

    async def collect_requests(
            self,
            marketstaff_spreadsheet_id: str,
            marketstaff_sheet_name: str = "Объекты МО",
            source_url: str = "",
    ) -> List[RawRequest]:
        """Отдаёт строки листа «Объекты МО» как заявки для реестра.

        Вся возня с объединёнными ячейками, общелистовыми колонками и
        переставленными заголовками (_read_sheet / _classify_unstable) остаётся
        здесь — реестр получает уже готовый текст позиции.
        """
        rows = await asyncio.to_thread(
            self._read_sheet, marketstaff_spreadsheet_id, marketstaff_sheet_name
        )
        if not rows:
            logger.info("[Маркетстафф] таблица пустая или не удалось прочитать")
            return []

        seen: set = set()
        out: List[RawRequest] = []
        skipped_closed: List[str] = []
        skipped_no_demand: List[str] = []
        for row in rows:
            label = self._row_label(row)
            state = self._demand_state(row)
            if state == "closed":
                skipped_closed.append(label)
                continue
            if state == "none":
                skipped_no_demand.append(label)
                continue

            object_name = (row.get("object") or "").strip()
            position = (row.get("position") or "").strip()
            city = (row.get("city") or "").strip()
            out.append(RawRequest(
                source=SOURCE_MARKETSTAFF,
                source_ref=unique_ref(f"{object_name}|{city}|{position}", seen),
                raw_text=self._row_to_text(row),
                source_name=self.SOURCE_NAME,
                source_url=source_url,
                counterparty_hint=self.COUNTERPARTY,
                raw_payload={k: v for k, v in row.items() if k != "extra"},
                # Должность, объект, формат и смена берутся из таблицы, а не из
                # разбора: они входят в ключ склейки позиции, и «плавающая»
                # формулировка модели заводила бы дубли (см. комментарий в
                # extract_and_save).
                field_overrides={
                    "counterparty": self.COUNTERPARTY,
                    "vacancy_name": position or None,
                    "object_name": object_name or city or None,
                    "work_format": "вахта",
                    "shift_type": self._shift_type_from_schedule(row.get("schedule") or ""),
                },
            ))

        logger.info(
            f"[Маркетстафф] в листе {len(rows)} позиций; "
            f"с открытой заявкой — {len(out)}, "
            f"без набора — {len(skipped_no_demand)}, "
            f"закрытых — {len(skipped_closed)}"
        )
        if skipped_no_demand:
            logger.info(f"[Маркетстафф] без набора: {'; '.join(skipped_no_demand)}")
        if skipped_closed:
            logger.info(f"[Маркетстафф] заявка закрыта: {'; '.join(skipped_closed)}")
        return out

    async def extract_and_save(
            self,
            marketstaff_spreadsheet_id: str,
            target_spreadsheet_id: str,
            target_sheet_name: str = "Лист1",
            marketstaff_sheet_name: str = "Объекты МО",
            source_url: str = "",
            llm_concurrency: int = None,
            sheets_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, int]:
        empty_stats = {"added": 0, "updated": 0, "skipped": 0, "deactivated": 0}

        rows = await asyncio.to_thread(
            self._read_sheet, marketstaff_spreadsheet_id, marketstaff_sheet_name
        )
        if not rows:
            logger.info("[Маркетстафф] таблица пустая или не удалось прочитать")
            return empty_stats

        # Отбираем только позиции с открытой заявкой. Пропущенные логируем
        # поимённо — иначе «взято 7 из 22» выглядит как потеря данных.
        items: List[Tuple[str, str, Dict]] = []  # (label, text, row)
        skipped_closed: List[str] = []
        skipped_no_demand: List[str] = []
        for row in rows:
            label = self._row_label(row)
            state = self._demand_state(row)
            if state == "closed":
                skipped_closed.append(label)
                continue
            if state == "none":
                skipped_no_demand.append(label)
                continue
            items.append((label, self._row_to_text(row), row))

        logger.info(
            f"[Маркетстафф] в листе {len(rows)} позиций; "
            f"с открытой заявкой — {len(items)}, "
            f"без набора — {len(skipped_no_demand)}, "
            f"закрытых — {len(skipped_closed)}"
        )
        if skipped_no_demand:
            logger.info(f"[Маркетстафф] без набора: {'; '.join(skipped_no_demand)}")
        if skipped_closed:
            logger.info(f"[Маркетстафф] заявка закрыта: {'; '.join(skipped_closed)}")

        if not items:
            logger.warning("[Маркетстафф] ни одной позиции с открытой заявкой")
            return empty_stats

        sem = asyncio.Semaphore(llm_concurrency or self.DEFAULT_LLM_CONCURRENCY)

        async def parse_one(label: str, text: str, row: Dict):
            async with sem:
                logger.info(f"[Маркетстафф] парсим: {label}")
                parsed = await self.parser.aparse(
                    text, source=self.SOURCE_NAME, source_url=source_url
                )
                return label, text, row, parsed

        results = await asyncio.gather(
            *[parse_one(l, t, r) for l, t, r in items]
        )

        all_vacancies: List[Dict] = []
        for label, text, row, parsed in results:
            if not parsed:
                logger.warning(f"[Маркетстафф] парсер вернул пустой результат: {label}")
                continue
            # Одна строка листа = ровно одна вакансия: у неё одна ставка и одно
            # КОЛИЧЕСТВО. LLM иногда всё же делит позицию на день/ночь по
            # правилу 5 своего промпта — и тогда КОЛИЧЕСТВО задваивается
            # (7 человек превращаются в 7 днём + 7 ночью). Берём первую.
            if len(parsed) > 1:
                logger.warning(
                    f"[Маркетстафф] LLM вернул {len(parsed)} вакансий на одну "
                    f"строку ({label}) — беру первую, чтобы не задваивать потребность"
                )
                parsed = parsed[:1]

            object_name = (row.get("object") or "").strip()
            shift_type = self._shift_type_from_schedule(row.get("schedule") or "")
            for vac in parsed:
                # Форсируем источник/контрагента и подменяем original_message
                # текстом строки. setdefault не годится: LLM возвращает
                # original_message пустой строкой, и при ре-апсерте у записи
                # осталось бы старое сообщение (см. AmetistSheetExtractor).
                vac["source"] = self.SOURCE_NAME
                vac["counterparty"] = self.COUNTERPARTY
                vac["source_url"] = source_url
                vac["original_message"] = text
                # vacancy_name, object_name, work_format и shift_type входят в
                # vacancy_id. Если оставить их на усмотрение LLM, id «плывёт»
                # между прогонами — на боевом прогоне одна и та же позиция
                # приехала сначала как «Комплектовщик», потом как
                # «Комплектовщик (сборка заказов)», и в целевой таблице завёлся
                # дубль. Поэтому берём их из таблицы: «Должность», «Объект»,
                # разобранный «График» плюс фиксированный формат.
                vac["vacancy_name"] = (row.get("position") or "").strip() or vac.get("vacancy_name")
                vac["object_name"] = (
                    object_name
                    or (vac.get("object_name") or "").strip()
                    or (row.get("city") or "")
                )
                vac["work_format"] = "вахта"
                vac["shift_type"] = shift_type
            all_vacancies.extend(parsed)

        if not all_vacancies:
            return empty_stats

        # Снимок: всё, чего нет в этом прогоне, гасим is_active=FALSE.
        # vacancy_id пересчитываем ОБЯЗАТЕЛЬНО: _enrich внутри парсера посчитал
        # его до того, как мы подменили object_name и shift_type, и без
        # пересчёта id остался бы завязан на «сырой» ответ LLM.
        keep_ids = set()
        for vac in all_vacancies:
            vid = make_vacancy_id(vac)
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

    # ---------- чтение листа ----------

    # Логическое имя поля -> предикат по нормализованному заголовку.
    # Заголовки в листе с опечатками («Мужчкины», «обьектов») и переносами,
    # поэтому матчим по устойчивым фрагментам, а не по точной строке.
    _COLUMN_MATCHERS = (
        ("object", lambda h: h == "объект"),
        ("city", lambda h: h == "город"),
        ("position", lambda h: h == "должность"),
        ("rate", lambda h: h.startswith("зарплата")),
        ("citizenship", lambda h: h.startswith("гражд")),
        ("sb", lambda h: h == "сб"),
        ("need_men", lambda h: h.startswith("заявка") and "муж" in h),
        ("need_women", lambda h: h.startswith("заявка") and "жен" in h),
        ("quantity", lambda h: h == "количество"),
        ("meals", lambda h: h == "питание"),
        ("schedule", lambda h: h == "график"),
        ("housing", lambda h: h.startswith("адрес")),
        ("duties", lambda h: h.startswith("должностные")),
        ("deductions", lambda h: h == "вычеты"),
        ("cis", lambda h: h.startswith("особенности снг")),
    )
    # Колонки, чьи подписи не соответствуют содержимому в части блоков.
    _UNSTABLE_MATCHERS = (
        lambda h: h.startswith("срок вахты"),
        lambda h: h.startswith("возраст"),
        lambda h: h.startswith("обучение"),
        lambda h: h.startswith("опыт работы"),
    )

    @classmethod
    def _build_column_map(cls, headers: List[str]) -> Dict[str, int]:
        """Заголовок -> индекс колонки по логическим именам."""
        lowered = [h.lower() for h in headers]
        cmap: Dict[str, int] = {}
        for name, matches in cls._COLUMN_MATCHERS:
            for i, h in enumerate(lowered):
                if h and matches(h) and name not in cmap:
                    cmap[name] = i
        return cmap

    @classmethod
    def _unstable_indexes(cls, headers: List[str]) -> List[int]:
        lowered = [h.lower() for h in headers]
        out = []
        for i, h in enumerate(lowered):
            if h and any(m(h) for m in cls._UNSTABLE_MATCHERS):
                out.append(i)
        return out

    def _read_sheet(self, spreadsheet_id: str, sheet_name: str) -> List[Dict]:
        """Читает лист и возвращает список нормализованных строк-словарей.

        Каждый словарь содержит логические ключи (object, city, position, ...),
        разобранную «нестабильную» группу (shift_term, age, training) и
        служебный `extra` — прочие колонки «как есть».
        """
        sh = self.sheets.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
        all_values = ws.get_all_values()
        if not all_values:
            return []

        headers = [_norm_space(h) for h in all_values[0]]
        if not any(headers):
            return []

        cmap = self._build_column_map(headers)
        for required in ("object", "city", "position"):
            if required not in cmap:
                logger.error(
                    f"[Маркетстафф] в шапке листа нет колонки {required!r}; "
                    f"шапка: {headers}"
                )
                return []
        unstable = self._unstable_indexes(headers)

        # Колонки «… для всех обьектов» — одно значение на весь лист.
        global_idx = [i for i, h in enumerate(headers) if "для всех" in h.lower()]
        global_values: Dict[int, str] = {}

        # Индексы колонок, которые тянем вниз внутри блока объекта.
        fill_idx = [cmap[k] for k in self.BLOCK_FILL_KEYS if k in cmap]

        rows: List[Dict] = []
        cur_object = cur_city = ""
        block_fill: Dict[int, str] = {}

        for raw in all_values[1:]:
            cells = [_norm_space(c) for c in raw]
            if len(cells) < len(headers):
                cells += [""] * (len(headers) - len(cells))
            if not any(cells):
                continue

            for i in global_idx:
                if cells[i] and i not in global_values:
                    global_values[i] = cells[i]

            obj_raw = cells[cmap["object"]]
            city_raw = cells[cmap["city"]]
            # Новый блок — когда явно указан другой объект или другой город.
            # Пустые ячейки означают продолжение текущего блока (merge).
            is_new_block = (
                (obj_raw and _key(obj_raw) != _key(cur_object))
                or (city_raw and _key(city_raw) != _key(cur_city))
            )
            if is_new_block:
                cur_object, cur_city = obj_raw, city_raw
                block_fill = {}
            else:
                cur_object = obj_raw or cur_object
                cur_city = city_raw or cur_city

            for i in fill_idx:
                if cells[i]:
                    block_fill[i] = cells[i]
                elif i in block_fill:
                    cells[i] = block_fill[i]

            position = cells[cmap["position"]]
            if not position:
                # Строка без должности — разделитель/остаток шапки, не вакансия.
                continue

            entry: Dict = {
                "object": cur_object,
                "city": cur_city,
                "position": position,
            }
            for name in ("rate", "citizenship", "sb", "need_men", "need_women",
                         "quantity", "meals", "schedule", "housing", "duties",
                         "deductions", "cis"):
                entry[name] = cells[cmap[name]] if name in cmap else ""

            entry.update(self._classify_unstable([cells[i] for i in unstable]))

            # Остальные колонки — как есть, под своими заголовками.
            used = set(cmap.values()) | set(unstable) | set(global_idx)
            entry["extra"] = {
                headers[i]: cells[i]
                for i in range(len(headers))
                if i not in used and headers[i] and cells[i]
            }
            entry["general"] = " ".join(
                global_values[i] for i in sorted(global_values) if global_values[i]
            )
            rows.append(entry)

        return rows

    @classmethod
    def _classify_unstable(cls, values: List[str]) -> Dict[str, str]:
        """Разбирает «нестабильную» четвёрку колонок по содержимому.

        Подписи этих колонок в листе не совпадают с данными для части блоков,
        поэтому опираемся на форму значения, а не на заголовок.
        """
        shift_term = ""
        age = ""
        training: List[str] = []
        for value in values:
            if not value:
                continue
            if not shift_term and cls._is_shift_term(value):
                shift_term = value
            elif not age and _AGE_RE.search(value):
                age = value
            else:
                training.append(value)
        return {
            "shift_term": shift_term,
            "age": age,
            "training": " | ".join(training),
        }

    @staticmethod
    def _shift_type_from_schedule(schedule: str) -> str:
        """'day' | 'night' | 'mixed' по колонке «График».

        В листе график задан интервалами: «6/1 или 7/0 с 08:00 до 20:00
        с 20:00 до 08:00» — то есть на объекте есть и дневная, и ночная смена.
        Считаем ночной сменой ту, что начинается с 18:00 и позже или до 06:00.
        Отдавать это на откуп LLM нельзя: shift_type входит в vacancy_id, и его
        разброс между прогонами плодит дубли в целевой таблице.
        """
        has_day = has_night = False
        for hour, _, _, _ in _TIME_RANGE_RE.findall(schedule or ""):
            start = int(hour)
            if start >= 18 or start < 6:
                has_night = True
            else:
                has_day = True
        if has_day and has_night:
            return "mixed"
        if has_night:
            return "night"
        if has_day:
            return "day"
        # Интервалы не указаны — ориентируемся на явную пометку «ночь».
        if "ноч" in (schedule or "").lower():
            return "night"
        return "day"

    @staticmethod
    def _is_shift_term(value: str) -> bool:
        """«35/45/60» — да; «6/1» (график) — нет; «3,700 руб» — нет."""
        if not _SHIFT_TERM_RE.match(value.strip()):
            return False
        numbers = [int(n) for n in re.findall(r"\d+", value)]
        return len(numbers) >= 2 and all(n >= 10 for n in numbers)

    # ---------- потребность ----------

    def _demand_state(self, row: Dict) -> str:
        """'closed' | 'none' | 'open' — есть ли сейчас набор по позиции."""
        quantity = (row.get("quantity") or "").strip()
        low = quantity.lower()
        if any(marker in low for marker in self.CLOSED_MARKERS):
            return "closed"
        men = (row.get("need_men") or "").strip().lower()
        women = (row.get("need_women") or "").strip().lower()
        if men in self.YES_VALUES or women in self.YES_VALUES:
            return "open"
        if quantity and re.search(r"\d", quantity):
            return "open"
        return "none"

    # ---------- текст для LLM ----------

    @staticmethod
    def _row_label(row: Dict) -> str:
        where = row.get("object") or row.get("city") or "без объекта"
        return f"{row.get('position') or '<без должности>'} — {where}"

    def _row_to_text(self, row: Dict) -> str:
        """Собирает текст одной позиции для LLM.

        Подписи здесь уже нормализованные: срок вахты и возраст вытащены по
        содержимому, всё прочее — под корректными заголовками листа.
        """
        lines = [
            f"Источник: {self.SOURCE_NAME}",
            "Формат работы: вахта",
            # Строка листа — всегда одна должность с одной ставкой и одним
            # количеством. Без этой оговорки LLM по своему правилу 5 делит
            # позицию на дневную и ночную и дублирует в каждую всю потребность.
            "Это ОДНА позиция: верни ровно один объект в JSON-массиве. "
            "Даже если в графике есть и дневная, и ночная смена — это одна "
            "и та же вакансия, потребность делить между ними не нужно.",
        ]
        pairs = [
            ("Объект", row.get("object")),
            ("Город", row.get("city")),
            ("Должность", row.get("position")),
            ("Зарплата за смену", row.get("rate")),
            ("Срок вахты в сменах (минимальный — первое число)", row.get("shift_term")),
            ("Возраст", row.get("age")),
            ("Обучение и требования", row.get("training")),
            ("Гражданство", row.get("citizenship")),
            ("Проверка СБ", row.get("sb")),
            ("Требуются мужчины", row.get("need_men")),
            ("Требуются женщины", row.get("need_women")),
            ("Количество (потребность)", row.get("quantity")),
            ("Питание", row.get("meals")),
            ("График", row.get("schedule")),
            ("Общежитие и доступность до объекта", row.get("housing")),
            ("Должностные обязанности и особенности объекта", row.get("duties")),
            ("Вычеты", row.get("deductions")),
            ("Особенности для СНГ", row.get("cis")),
        ]
        for label, value in pairs:
            if value:
                lines.append(f"{label}: {value}")
        for label, value in (row.get("extra") or {}).items():
            lines.append(f"{label}: {value}")
        if row.get("general"):
            lines.append(f"Общие положения для всех объектов: {row['general']}")
        return "\n".join(lines)
