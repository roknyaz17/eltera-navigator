"""Внутренние проекты — наши собственные вакансии в «Навигаторе».

Кроме вакансий контрагентов (Аметист, МЕЛИОР, ЯППИ…) у компании есть свои
проекты — ВентПро, Интер: заказчик работает с нами напрямую, посредника нет.
Потока заявок у них нет: менеджеры ведут их в шпаргалке Avito рядом с
объявлениями контрагентов. Наша строка там отличается одним — в колонке
«Контрагент» пусто или «Не указан».

В реестр они идут отдельным источником `internal` («Внутренние проекты»):
так пометка видна везде, где виден контрагент, — в карточке, на вкладках
«Контрагенты» и «Ставки», в поиске, — а прогон гасит только свои позиции.

Строка шпаргалки — объявление, а не вакансия: ВентПро в Михнево размещён в 25
городах привлечения. Позиция реестра — место работы, поэтому строки
складываются по (проект, место, должность), а города объявлений уходят в
текст заявки: кандидат звонит из Рыбинска, и рекрутёр видит, откуда он.

LLM не нужен: таблица уже разложена по колонкам. Всё кладётся в
field_overrides, а «разбор» возвращает одну пустую вакансию.

Прогон — полный снимок источника: проект, которого больше нет в шпаргалке,
гаснет. Если наших строк не нашлось вовсе, прогон останавливается — пустой
снимок погасил бы всё разом из-за переименованной колонки.

Запуск (на сервере — `docker exec -it eltrea-bot python -m scripts.internal_projects`):
    python -m scripts.internal_projects --dry-run        # что будет заведено
    python -m scripts.internal_projects                  # завести / обновить
    python -m scripts.internal_projects <id или ссылка>  # другая шпаргалка
"""

import argparse
import asyncio
import os
import re
import sys
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from registry import db  # noqa: E402
from registry.dictionaries import norm_key  # noqa: E402
from registry.ingest import RegistryIngestor  # noqa: E402
from registry.labels import SOURCE_TITLES  # noqa: E402
from registry.models import RawRequest  # noqa: E402
from registry.sources import SOURCE_INTERNAL  # noqa: E402

# «Шпаргалка_менеджерам_Avito» — её же читают менеджеры.
DEFAULT_SPREADSHEET_ID = os.getenv(
    "INTERNAL_PROJECTS_SPREADSHEET_ID", "1fCfzRcYMWTcqNCf_9JmRvVBxp281svCYpxl2s6Q5nWA",
)

TITLE = SOURCE_TITLES[SOURCE_INTERNAL]

COL_PLACE = "Проект и место"
COL_AD = "Объявление / вакансия"
COL_PAY = "Оплата работнику"
COL_COUNTERPARTY = "Контрагент"
COL_AD_ADDRESS = "Адрес в файле Avito"
REQUIRED_COLUMNS = (COL_PLACE, COL_AD, COL_PAY, COL_COUNTERPARTY)

# Чем в колонке «Контрагент» помечена наша строка. Контрагента у неё нет —
# заказчик наш собственный.
INTERNAL_MARKS = {"", "-", "—", "не указан", "нет", "внутренний проект", "внутренние проекты"}

# Первая часть места, которая означает регион, а не населённый пункт.
_REGION_RE = re.compile(r"област|край|республик|автономн", re.IGNORECASE)
FEDERAL_CITIES = {"москва", "санкт-петербург", "севастополь"}

# Вторая строка ячейки объявления: как должность названа в заявке.
_IN_REQUEST_RE = re.compile(r"^\s*в (?:заявке|навигаторе)\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _cell(row: Sequence[str], index: Optional[int]) -> str:
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _lines(text: str) -> List[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def is_internal(counterparty: str) -> bool:
    return counterparty.strip().lower() in INTERNAL_MARKS


def split_place(place: str) -> Tuple[str, str]:
    """«Московская область, Михнево» → (регион, город).

    Регион стоит первым, если это область, край, республика или город
    федерального значения с уточнением («Москва, Внуково» — Внуково, Москва:
    так Внуково уже лежит в реестре у Градуса).
    """
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if not parts:
        return "", ""
    first = parts[0]
    if len(parts) > 1 and (_REGION_RE.search(first) or first.lower() in FEDERAL_CITIES):
        return first, parts[1]
    return "", first


def split_ad(cell: str) -> Tuple[str, str]:
    """Ячейка объявления → (заголовок объявления, должность из заявки)."""
    lines = _lines(cell)
    title = lines[0] if lines else ""
    vacancy = ""
    for line in lines[1:]:
        match = _IN_REQUEST_RE.match(line)
        if match:
            vacancy = match.group(1)
            break
    return title, vacancy or re.sub(r",\s*вахта\s*$", "", title, flags=re.IGNORECASE)


def find_header(values: Sequence[Sequence[str]]) -> Optional[Tuple[int, Dict[str, int]]]:
    """Строка шапки и номера колонок. Над шапкой в шпаргалке — заголовок и пояснения."""
    for index, row in enumerate(values):
        names = {str(cell or "").strip(): pos for pos, cell in enumerate(row)}
        if all(column in names for column in REQUIRED_COLUMNS):
            return index, names
    return None


def collect(sheets: Sequence[Tuple[str, Sequence[Sequence[str]]]]) -> List[Dict[str, Any]]:
    """Наши строки со всех листов, сложенные по месту работы.

    Отложенное объявление (лист «Отложены») — всё ещё наша вакансия: Avito
    не пропустил текст, а люди на объект нужны по-прежнему.
    """
    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for sheet_title, values in sheets:
        found = find_header(values)
        if not found:
            continue
        header_at, cols = found
        for row in values[header_at + 1:]:
            if not any(str(cell or "").strip() for cell in row):
                continue
            if not is_internal(_cell(row, cols.get(COL_COUNTERPARTY))):
                continue
            place_lines = _lines(_cell(row, cols[COL_PLACE]))
            if not place_lines:
                continue
            project = place_lines[0]
            place = ", ".join(place_lines[1:])
            title, vacancy = split_ad(_cell(row, cols[COL_AD]))
            key = norm_key(" | ".join([project, place, vacancy])) or "row"
            group = groups.setdefault(key, {
                "ref": key,
                "project": project,
                "place": place,
                "title": title,
                "vacancy": vacancy,
                "pay": _cell(row, cols[COL_PAY]),
                "ads": [],
                "sheets": [],
            })
            ad_place = _cell(row, cols.get(COL_AD_ADDRESS))
            if ad_place and ad_place not in group["ads"]:
                group["ads"].append(ad_place)
            if sheet_title not in group["sheets"]:
                group["sheets"].append(sheet_title)
    return list(groups.values())


def raw_text(group: Dict[str, Any], spreadsheet_title: str) -> str:
    lines = [
        f"{TITLE}: «{group['project']}» — наша вакансия, контрагента нет.",
        f"Работа: {group['place'] or 'место не указано'}.",
        f"Объявление: {group['title']}",
        f"В заявке: {group['vacancy']}",
    ]
    if group["pay"]:
        lines.append(f"Оплата: {group['pay']}")
    if group["ads"]:
        lines.append(
            f"Объявления Avito ({len(group['ads'])}) — города привлечения, "
            f"а не место работы: " + "; ".join(group["ads"])
        )
    if spreadsheet_title:
        lines.append(f"Источник: «{spreadsheet_title}».")
    return "\n".join(lines)


def build_request(group: Dict[str, Any], spreadsheet_title: str, url: str) -> RawRequest:
    region, city = split_place(group["place"])
    overrides: Dict[str, Any] = {
        "counterparty": TITLE,
        "object_name": group["project"],
        "vacancy_name": group["vacancy"],
        "city": city,
        "region": region,
        "object_address": group["place"],
        "duties": re.sub(r",\s*вахта\s*$", "", group["title"], flags=re.IGNORECASE),
        "shift_rate": group["pay"],
    }
    if "вахт" in group["title"].lower():
        overrides["schedule"] = "вахта"
    return RawRequest(
        source=SOURCE_INTERNAL,
        source_ref=group["ref"],
        raw_text=raw_text(group, spreadsheet_title),
        source_name=TITLE,
        source_url=url,
        counterparty_hint=TITLE,
        raw_payload={k: group[k] for k in ("project", "place", "title", "vacancy", "pay", "ads", "sheets")},
        field_overrides={k: v for k, v in overrides.items() if v},
        expects_vacancies=True,
    )


class TableParser:
    """«Разбор» без модели: всё уже лежит в field_overrides заявки."""

    async def aparse_raw_ex(self, text: str, context: Optional[str] = None):
        return [{}], 0, 0


def spreadsheet_id(value: str) -> str:
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value.strip()


def read_spreadsheet(key: str) -> Tuple[str, List[Tuple[str, List[List[str]]]]]:
    from sheets_adapter import GoogleSheetsService

    book = GoogleSheetsService("credentials.json").client.open_by_key(key)
    return book.title, [(ws.title, ws.get_all_values()) for ws in book.worksheets()]


async def ingest(raws: List[RawRequest], db_path: Optional[str] = None):
    ingestor = RegistryIngestor(TableParser(), db_path=db_path)
    return await ingestor.ingest(SOURCE_INTERNAL, raws, snapshot=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Наши вакансии из шпаргалки Avito → «Навигатор»")
    ap.add_argument("spreadsheet", nargs="?", default=DEFAULT_SPREADSHEET_ID,
                    help="id или ссылка на шпаргалку")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не писать")
    args = ap.parse_args()

    key = spreadsheet_id(args.spreadsheet)
    title, sheets = read_spreadsheet(key)
    url = f"https://docs.google.com/spreadsheets/d/{key}/edit"
    groups = collect(sheets)

    print(f"«{title}»: листов {len(sheets)}, наших вакансий {len(groups)}")
    for group in groups:
        region, city = split_place(group["place"])
        print(f"  {group['project']} · {group['vacancy']} · {city}"
              f"{' (' + region + ')' if region else ''} · {group['pay']} · "
              f"объявлений {len(group['ads'])}")
    if not groups:
        print("Наших строк нет — снимок пустым не пишу, иначе погаснут все внутренние проекты.")
        return 1
    if args.dry_run:
        return 0

    raws = [build_request(group, title, url) for group in groups]
    stats = asyncio.run(ingest(raws))
    print(stats.summary())
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT position_id, object_name, vacancy_name, city, shift_rate, is_active "
            "FROM positions WHERE source = ? ORDER BY is_active DESC, position_id",
            (SOURCE_INTERNAL,),
        ).fetchall()
    for row in rows:
        state = "" if row["is_active"] else "  (погашена)"
        print(f"  {row['position_id']}  {row['object_name']} · {row['vacancy_name']} · "
              f"{row['city']} · {row['shift_rate']} ₽/смена{state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
