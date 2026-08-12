"""Выгрузка реестра в Google Таблицу.

Таблица перестала быть источником правды и стала витриной: тем, кто привык
смотреть данные в Sheets, ничего не меняется, но редактирование переехало в
/registry. Поэтому выгрузка — полная перезапись, а не upsert: слияние с
ручными правками больше не нужно, а с ним исчезает и класс ошибок, где правка
менеджера затиралась ночным прогоном.

Порядок записи выбран так, чтобы лист никогда не оказался пустым: сначала
перезаписываются строки данных, и только потом чистится хвост от прошлой,
более длинной выгрузки.
"""

from typing import List, Optional

from loguru import logger

from registry import db
from registry.compat import LEGACY_COLUMNS, legacy_cell
from registry.queries import all_positions
from sheets_adapter import _col_letter


def build_rows(conn) -> List[List[str]]:
    rows = [list(LEGACY_COLUMNS)]
    for position in all_positions(conn):
        rows.append([legacy_cell(position, column) for column in LEGACY_COLUMNS])
    return rows


def export_to_sheets(
        sheets_service,
        spreadsheet_id: str,
        sheet_name: str = "Лист1",
        db_path: Optional[str] = None,
) -> int:
    """Перезаписывает лист текущим содержимым реестра. Возвращает число позиций."""
    with db.connect(db_path) as conn:
        values = build_rows(conn)

    sheet = sheets_service.client.open_by_key(spreadsheet_id)
    worksheet = sheet.worksheet(sheet_name) if sheet_name else sheet.sheet1

    previous_rows = worksheet.row_count
    end_column = _col_letter(len(LEGACY_COLUMNS))
    needed_rows = len(values)

    if worksheet.row_count < needed_rows or worksheet.col_count < len(LEGACY_COLUMNS):
        worksheet.resize(
            rows=max(needed_rows, worksheet.row_count),
            cols=max(len(LEGACY_COLUMNS), worksheet.col_count),
        )

    worksheet.update(
        f"A1:{end_column}{needed_rows}", values, value_input_option="USER_ENTERED"
    )

    if previous_rows > needed_rows:
        worksheet.batch_clear([f"A{needed_rows + 1}:{end_column}{previous_rows}"])

    logger.info(f"[export] в таблицу записано {needed_rows - 1} позиций")
    return needed_rows - 1
