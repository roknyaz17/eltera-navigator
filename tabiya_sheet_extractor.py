"""Приёмник заявок Табии из Google Таблицы (лист «Вакансии»).

Одна строка листа — один объект — одна заявка. В ячейке «ВАКАНСИИ И
ПОТРЕБНОСТЬ» перечислено несколько должностей со своими числами, поэтому из
одной заявки рождается несколько позиций. Делит их модель, а не этот модуль, —
и это главное решение здесь.

Почему приёмник НЕ разбирает текст сам. Первая версия пыталась вытащить
должности, потребность и город регулярками. На настоящем листе это раз за
разом ломалось: пол, написанный словом («Грузчик - 4 муж»), терял вакансию;
сноска в скобках с числом («(обучение - 2 человека в группе)») становилась
фантомной вакансией с потребностью 2; «Смена с 08:00 до 20:00» читалось как
потребность 0; а город брался из хвоста ячейки и оказывался «СРОЧНО» или
«ПРОИЗВОДСТВО», потому что лист написан капсом и проверка «с заглавной буквы»
ничего не отсекала. Разбирать живой человеческий текст — работа модели, она
для этого и стоит в конвейере (vacancy_parser.py). Приёмник отдаёт ей строку
целиком и форсит ровно то, что обязан держать сам.

Что форсим и почему. В ключ тождества позиции входят шесть полей
(registry/models.py, FINGERPRINT_FIELDS), и любое «плавающее» из них плодит
дубли между прогонами. Из них приёмник надёжно знает два: контрагента и
объект — объект берётся первой строкой ячейки «ОБЪЕКТ» и не зависит от того,
как модель перескажет заголовок. Город НЕ форсим: в ячейке он записан
по-разному («МО, г. Мытищи», «м. Озерная, г. Москва», «Рязанская обл., г. …»),
и модель разбирает такое лучше любой регулярки, а registry/normalize.py потом
приводит его к канону.

Служебные строки. В листе есть врезка с адресом офиса и ссылкой на запись —
у неё пустая ячейка «ОБЪЕКТ». По этому признаку её и пропускаем: он
содержательный и не зависит ни от ссылок (контрагент вправе положить ссылку на
карту в строку объекта), ни от наличия номера (номера в листе идут с
пропусками и иногда отсутствуют вовсе).
"""

import asyncio
import re
from typing import Any, Dict, List, Set

from loguru import logger

from registry.models import RawRequest
from registry.sources import SOURCE_TABIYA, unique_ref

# Позиционно важны только три первые колонки: номер объекта, «ОБЪЕКТ» и
# «ВАКАНСИИ И ПОТРЕБНОСТЬ». Остальные уезжают модели под своими подписями из
# шапки, поэтому вставка колонки в середину листа ничего не ломает.
COL_NUMBER = 0
COL_OBJECT = 1
COL_VACANCIES = 2

# Минимум, без которого строку не прочитать. Выше поднимать нельзя: лист ведут
# руками, и лишняя строгость превращается в молчаливый отказ источника.
MIN_COLUMNS = 3

_SPACES_RE = re.compile(r"[ \t ]+")


def _clean_cell(value: Any) -> str:
    """Чистит ячейку, СОХРАНЯЯ переносы строк.

    Переносы несут привязку: в «ТАРИФНОЙ СТАВКЕ» одна строка относится к
    грузчику, другая к водителю. Схлопнешь их в пробел — получится простыня, и
    модель припишет ставку не той должности.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


# Что сказать модели про эту таблицу. Две оговорки тут несущие.
# Про день и ночь — потому что shift_type входит в ключ позиции: без неё модель
# по своему правилу вернёт две вакансии вместо одной и поделит между ними
# потребность (ровно это пришлось оговаривать и Маркетстаффу).
# Про ноль — потому что в листе 19 объектов из 27 стоят в нулях: это живой
# каталог без набора, и позиции по ним нужны, иначе объект исчезнет из реестра.
ПОЯСНЕНИЕ = """
Как читать эту заявку:
- Это ОДИН объект. В ячейке «ВАКАНСИИ И ПОТРЕБНОСТЬ» перечислено несколько
  должностей, у каждой своё число — сколько человек нужно. Верни отдельную
  вакансию на каждую должность.
- Строки в скобках и строки без числа — это пояснение к должности выше
  (график, документы, условия). Отдельной вакансией они НЕ являются.
- Число 0 означает, что набор на эту должность сейчас закрыт. Позицию всё
  равно верни, потребность у неё 0.
- Если в графике работы есть и дневная, и ночная смена — это ОДНА вакансия.
  Не дели её на две и не дели между ними потребность.
- Ставки, обязанности и условия в ячейках расписаны по должностям: соотноси их
  по названию должности, а не по порядку строк.
""".strip()


class TabiyaSheetExtractor:
    """Парсер листа «Вакансии» Табии: одна строка = один объект = одна заявка."""

    SOURCE_NAME = "Табия"
    COUNTERPARTY = "Табия"

    def __init__(self, sheets_service, llm_parser=None):
        self.sheets = sheets_service
        self.parser = llm_parser

    async def collect_requests(
            self,
            tabiya_spreadsheet_id: str,
            tabiya_sheet_name: str = "Вакансии",
            source_url: str = "",
    ) -> List[RawRequest]:
        values = await asyncio.to_thread(
            self._read_values, tabiya_spreadsheet_id, tabiya_sheet_name
        )
        if not values:
            # Пустая пачка снимком не считается, поэтому позиции Табии не
            # погаснут — но и обновляться перестанут. Это обязано быть видно.
            logger.error(
                f"[Табия] лист «{tabiya_sheet_name}» пуст или не прочитан — "
                "заявок не отдаём, позиции останутся с прежними данными"
            )
            return []
        return self.build_requests(values, source_url=source_url)

    def _read_values(self, spreadsheet_id: str, sheet_name: str) -> List[List[str]]:
        """Сырые строки листа. Разбор вынесен в build_requests, чтобы его можно
        было проверить тестом на фикстуре, без сети и ключей."""
        sh = self.sheets.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name)
        return ws.get_all_values()

    # ------------------------------------------------------------- разбор

    def build_requests(self, values: List[List[str]], source_url: str = "") -> List[RawRequest]:
        if len(values) < 2:
            logger.error("[Табия] в листе нет строк данных — заявок не отдаём")
            return []

        width = max(len(row) for row in values)
        if width < MIN_COLUMNS:
            logger.error(
                f"[Табия] в листе {width} колонок, нужно минимум {MIN_COLUMNS} "
                f"(номер, объект, вакансии) — заявок не отдаём. Шапка: {values[0]}"
            )
            return []

        labels = [_clean_cell(c).replace("\n", " ") for c in values[0]]
        labels += [f"Колонка {i + 1}" for i in range(len(labels), width)]

        requests: List[RawRequest] = []
        seen: Set[str] = set()
        skipped = 0

        for index, raw_row in enumerate(values[1:], start=2):
            row = [_clean_cell(c) for c in raw_row]
            row += [""] * (width - len(row))

            object_cell = row[COL_OBJECT]
            if not object_cell:
                # Врезка с адресом офиса и прочие служебные строки: объекта нет,
                # заводить нечего. Пишем в лог с содержимым — молчаливый пропуск
                # строки с настоящим объектом был бы потерей потребности.
                skipped += 1
                digest = " | ".join(c.replace("\n", " ")[:40] for c in row[:3] if c)
                logger.info(f"[Табия] строка {index} без объекта, пропущена: {digest}")
                continue

            object_name = object_cell.split("\n")[0].strip()
            number = row[COL_NUMBER].replace("\n", " ").strip()
            # Ключ заявки: номер объекта, если он есть, иначе само название.
            # Разделитель «/» выбран намеренно — norm_key его сохраняет, а «|»
            # превращает в пробел, и два разных ключа схлопнулись бы в один.
            base = f"{number}/{object_name}" if number else object_name
            source_ref = unique_ref(base, seen)

            payload: Dict[str, Any] = {
                labels[i]: row[i] for i in range(width) if row[i]
            }
            requests.append(RawRequest(
                source=SOURCE_TABIYA,
                source_ref=source_ref,
                raw_text=self._row_to_text(labels, row, width),
                source_name=self.SOURCE_NAME,
                source_url=source_url,
                counterparty_hint=self.COUNTERPARTY,
                raw_payload=payload,
                # Контрагент и объект входят в ключ тождества позиции и потому
                # не отдаются на откуп модели: от прогона к прогону они обязаны
                # быть побуквенно одинаковыми, иначе позиция заводится заново.
                field_overrides={
                    "counterparty": self.COUNTERPARTY,
                    "object_name": object_name,
                },
            ))

        logger.info(
            f"[Табия] объектов в заявки: {len(requests)}, пропущено строк без объекта: {skipped}"
        )
        return requests

    @staticmethod
    def _row_to_text(labels: List[str], row: List[str], width: int) -> str:
        """Строка листа как текст для модели: «ПОДПИСЬ: значение» по колонкам."""
        lines = ["Источник: Табия", "Контрагент: Табия", ""]
        for i in range(width):
            value = row[i]
            if not value:
                continue
            label = labels[i] or f"Колонка {i + 1}"
            if "\n" in value:
                lines.append(f"{label}:")
                lines.extend(value.split("\n"))
            else:
                lines.append(f"{label}: {value}")
        lines += ["", ПОЯСНЕНИЕ]
        return "\n".join(lines)
