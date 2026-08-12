from typing import Dict, List

import gspread
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials

from vacancy_parser import make_vacancy_id

load_dotenv()

# Колонки целевой таблицы — в строгом порядке.
# Изменения: убраны advance_* (4), rotation_rate, monthly_rate.
# Добавлены shift_type, min_shifts, requires_tsd, sb_policy.
TARGET_COLUMNS = [
    "vacancy_id", "created_at", "updated_at", "counterparty", "vacancy_name",
    "vacancy_category", "city", "region", "object_name", "object_address",
    "work_format", "shift_type", "schedule", "min_shifts", "shift_hours",
    "shift_rate", "duties", "requirements", "requires_tsd", "gender",
    "age_from", "age_to", "citizenship_requirements",
    "need_men", "need_women", "need_couples", "need_total",
    "housing_available", "housing_free", "housing_deduction", "housing_conditions",
    "meals_available", "meals_free", "meals_deduction", "meals_times_per_day",
    "medical_book_required", "medical_book_payer", "can_start_without_medical_book",
    "uniform_available", "uniform_free", "uniform_deduction",
    "transport_paid", "transport_fully_paid", "transport_terms",
    "market_rate", "market_deviation",
    "advantages", "risks", "sb_policy",
    "recruiter_comment", "sales_script", "objections",
    "status", "priority", "last_updated_at",
    "source", "source_url", "responsible_manager",
    "needs_review", "is_active", "original_message",
]

# Поля, которые может менять менеджер вручную, — на upsert не перетираем.
PROTECTED_FIELDS = {
    "status", "priority", "responsible_manager",
    "recruiter_comment", "sales_script", "objections",
    "market_rate", "market_deviation",
}


def _serialize(value):
    """Приводит значение к строке, удобной для Google Sheets."""
    if isinstance(value, bool):
        return str(value).upper()
    if value is None:
        return ""
    return str(value)


def _col_letter(idx_1based: int) -> str:
    """1 -> A, 27 -> AA — для построения A1-нотации диапазонов."""
    s = ""
    n = idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


class GoogleSheetsService:
    """Сервис для чтения и записи данных в Google Таблицы."""

    def __init__(self, credentials_path: str):
        """
        :param credentials_path: путь к JSON-файлу с ключами сервисного аккаунта
        """
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        self.client = gspread.authorize(creds)

    def get_sheet_data(self, spreadsheet_id: str, sheet_name: str = None) -> List[Dict]:
        """Возвращает содержимое указанной таблицы как список словарей."""
        sh = self.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
        return ws.get_all_records()

    def upsert_vacancies(
            self,
            spreadsheet_id: str,
            vacancies: List[Dict],
            sheet_name: str = None,
            original_msg: str = None,
    ) -> Dict[str, int]:
        """
        Идемпотентная запись вакансий: по `vacancy_id` обновляет существующие
        строки, остальное добавляет.

        Возвращает {"added": N, "updated": M, "skipped": K}.
        """
        sh = self.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1

        all_values = ws.get_all_values()
        headers, data_rows = self._ensure_headers(ws, all_values)

        col_idx = {name: i for i, name in enumerate(headers)}
        if "vacancy_id" not in col_idx:
            raise RuntimeError(
                "В целевой таблице первая строка не содержит колонку vacancy_id. "
                "Очистите лист полностью (выделите все строки → Delete) и запустите снова — "
                "правильная шапка из 61 колонки запишется автоматически."
            )
        vid_col = col_idx["vacancy_id"]

        # Карта vacancy_id -> (1-based sheet row index, existing values).
        # Строка 1 — заголовок, поэтому первая строка данных = 2.
        existing_by_id: Dict[str, "tuple[int, List[str]]"] = {}
        for i, row in enumerate(data_rows):
            if len(row) > vid_col:
                vid = (row[vid_col] or "").strip()
                if not vid:
                    # Бекфилл id для старых записей: считаем по тем же полям
                    legacy_row = {
                        col: row[col_idx[col]] if col_idx[col] < len(row) else ""
                        for col in TARGET_COLUMNS if col in col_idx
                    }
                    vid = make_vacancy_id(legacy_row)
                if vid:
                    existing_by_id[vid] = (i + 2, row)

        rows_to_append: List[List[str]] = []
        batch_updates: List[Dict] = []
        seen_in_batch: set = set()
        added = updated = skipped = 0

        for vac in vacancies:
            if original_msg and not vac.get("original_message"):
                vac["original_message"] = original_msg

            vid = (vac.get("vacancy_id") or "").strip()
            if not vid:
                vid = make_vacancy_id(vac)
                vac["vacancy_id"] = vid

            if vid in seen_in_batch:
                skipped += 1
                continue
            seen_in_batch.add(vid)

            if vid in existing_by_id:
                sheet_row_num, existing_row = existing_by_id[vid]
                merged = self._merge_row(existing_row, headers, vac)
                end_col_letter = _col_letter(len(headers))
                batch_updates.append({
                    "range": f"A{sheet_row_num}:{end_col_letter}{sheet_row_num}",
                    "values": [merged],
                })
                updated += 1
            else:
                rows_to_append.append([_serialize(vac.get(col)) for col in headers])
                added += 1

        if batch_updates:
            ws.batch_update(batch_updates, value_input_option="USER_ENTERED")
        if rows_to_append:
            ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")

        return {"added": added, "updated": updated, "skipped": skipped}

    # Обратная совместимость: старые места кода могут звать append_vacancies.
    def append_vacancies(self, *args, **kwargs) -> int:
        result = self.upsert_vacancies(*args, **kwargs)
        return result["added"] + result["updated"]

    @staticmethod
    def _ensure_headers(ws, all_values):
        """
        Гарантирует, что в листе есть корректная шапка.
        Сценарии:
        - лист пустой → пишем TARGET_COLUMNS целиком;
        - первая строка вся из пустых ячеек → перезаписываем TARGET_COLUMNS;
        - первая строка содержит vacancy_id, но короче нашего набора →
          дописываем недостающие колонки справа (старые данные сохраняем);
        - первая строка содержит vacancy_id и достаточная по ширине →
          оставляем как есть.
        Возвращает (headers, data_rows).
        """
        # Пустой лист
        if not all_values:
            ws.append_row(TARGET_COLUMNS, value_input_option="USER_ENTERED")
            return list(TARGET_COLUMNS), []

        first_row = all_values[0] if all_values else []
        # Первая строка из одних пустых ячеек — это «мусор», переписываем шапку
        if not any((cell or "").strip() for cell in first_row):
            end_col = _col_letter(len(TARGET_COLUMNS))
            ws.update(f"A1:{end_col}1", [TARGET_COLUMNS], value_input_option="USER_ENTERED")
            return list(TARGET_COLUMNS), all_values[1:]

        headers = [(h or "").strip() for h in first_row]
        if "vacancy_id" not in headers:
            # Не наша шапка вовсе — возвращаем как есть, в upsert_vacancies
            # бросится понятная ошибка.
            return headers, all_values[1:]

        # Шапка наша, но возможно неполная (старая 63-кол. версия или короче).
        # Дописываем недостающие колонки справа.
        missing = [c for c in TARGET_COLUMNS if c not in headers]
        if missing:
            new_headers = headers + missing
            end_col = _col_letter(len(new_headers))
            ws.update(f"A1:{end_col}1", [new_headers], value_input_option="USER_ENTERED")
            return new_headers, all_values[1:]

        return headers, all_values[1:]

    def mark_sources_inactive(
            self,
            spreadsheet_id: str,
            sources: List[str],
            sheet_name: str = None,
    ) -> int:
        """
        Помечает is_active=FALSE у всех строк указанных source.
        Если sources пуст или None — сбрасываются ВСЕ строки (полный reset).

        Менеджерские поля (status, priority, recruiter_comment и т.д.) не
        затрагиваются.

        :return: количество строк, переключённых из TRUE в FALSE.
        """
        sh = self.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
        all_values = ws.get_all_values()
        if not all_values or len(all_values) < 2:
            return 0

        headers = [(h or "").strip() for h in all_values[0]]
        try:
            active_col = headers.index("is_active")
        except ValueError:
            return 0

        source_col = None
        if sources:
            try:
                source_col = headers.index("source")
            except ValueError:
                return 0
        sources_norm = {s.strip().lower() for s in (sources or [])}

        active_letter = _col_letter(active_col + 1)
        batch_updates: List[Dict] = []
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) <= active_col:
                continue
            # Фильтр по source (если задан список)
            if sources_norm:
                if source_col is None or len(row) <= source_col:
                    continue
                row_source = (row[source_col] or "").strip().lower()
                if row_source not in sources_norm:
                    continue
            current = (row[active_col] or "").strip().upper()
            if current == "FALSE":
                continue
            batch_updates.append({
                "range": f"{active_letter}{i}:{active_letter}{i}",
                "values": [["FALSE"]],
            })

        if batch_updates:
            ws.batch_update(batch_updates, value_input_option="USER_ENTERED")
        return len(batch_updates)

    # Алиас для обратной совместимости (полный сброс).
    def mark_all_inactive(self, spreadsheet_id: str, sheet_name: str = None) -> int:
        return self.mark_sources_inactive(spreadsheet_id, sources=[], sheet_name=sheet_name)

    def deactivate_stale_vacancies(
            self,
            spreadsheet_id: str,
            source: str,
            keep_ids: set,
            sheet_name: str = None,
    ) -> int:
        """
        Помечает is_active=FALSE у всех вакансий указанного source,
        чей vacancy_id НЕ входит в keep_ids.

        Используется для обработки полного «снимка» источника
        (например, ежедневная сводка от ВахтаПро).

        :return: количество деактивированных строк.
        """
        sh = self.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
        all_values = ws.get_all_values()
        if not all_values or len(all_values) < 2:
            return 0

        headers = [(h or "").strip() for h in all_values[0]]
        col_idx = {name: i for i, name in enumerate(headers)}
        for required in ("vacancy_id", "source", "is_active"):
            if required not in col_idx:
                # Шапка ещё не инициализирована или повреждена —
                # деактивировать нечего, тихо выходим.
                return 0

        vid_col = col_idx["vacancy_id"]
        source_col = col_idx["source"]
        active_col = col_idx["is_active"]
        active_letter = _col_letter(active_col + 1)

        normalized_source = source.strip().lower()
        batch_updates: List[Dict] = []
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) <= max(vid_col, source_col, active_col):
                continue
            row_source = (row[source_col] or "").strip().lower()
            if row_source != normalized_source:
                continue
            vid = (row[vid_col] or "").strip()
            if vid and vid in keep_ids:
                continue
            current = (row[active_col] or "").strip().upper()
            if current == "FALSE":
                continue
            batch_updates.append({
                "range": f"{active_letter}{i}:{active_letter}{i}",
                "values": [["FALSE"]],
            })

        if batch_updates:
            ws.batch_update(batch_updates, value_input_option="USER_ENTERED")
        return len(batch_updates)

    @staticmethod
    def _merge_row(
            existing_row: List[str],
            headers: List[str],
            vac: Dict,
    ) -> List[str]:
        """
        Сливает существующую строку и новые данные:
        - PROTECTED_FIELDS и `created_at` — сохраняем старое значение;
        - `needs_review` — если менеджер уже сбросил в FALSE, не возвращаем в TRUE;
        - `is_active` — при upsert считаем активной (вакансия снова встретилась);
        - всё прочее — берём новое, при пустом новом сохраняем старое.
        """
        new_row: List[str] = []
        for i, col in enumerate(headers):
            existing = existing_row[i] if i < len(existing_row) else ""
            new_value = vac.get(col)
            new_str = _serialize(new_value)

            if col in PROTECTED_FIELDS or col == "created_at":
                new_row.append(existing if existing else new_str)
            elif col == "needs_review":
                if (existing or "").strip().upper() == "FALSE":
                    new_row.append("FALSE")
                else:
                    new_row.append(new_str)
            elif col == "is_active":
                new_row.append("TRUE")
            else:
                new_row.append(new_str if new_str else existing)
        return new_row

    # Остальные методы get_vacancy_table, get_priority_table и т.д.

    def get_project_matrix_data_all(self, spreadsheet_id: str, sheet_name: str = None) -> Dict[str, Dict]:
        sh = self.client.open_by_key(spreadsheet_id)
        ws = sh.worksheet(sheet_name) if sheet_name else sh.sheet1
        all_data = ws.get_all_values()
        if not all_data or len(all_data) < 3:
            return {}

        # Вторая строка — это города (первая ячейка 'ГОРОД ')
        city_headers = all_data[1]
        # Строки с параметрами — начиная с индекса 2
        data_rows = all_data[2:]

        result = {}

        # Собираем общие поля из первого столбца (индекс 1, город Владимир)
        first_col_values = {}
        for row in data_rows:
            if len(row) > 1 and row[0].strip():
                first_col_values[row[0].strip()] = row[1].strip() if len(row) > 1 else ""

        # Перебираем столбцы, начиная со второго (индекс 1), потому что первый — названия параметров
        for col_idx in range(1, len(city_headers)):
            city = city_headers[col_idx].strip()
            if not city or city == 'ГОРОД':
                continue

            properties = {}
            for row in data_rows:
                if len(row) <= col_idx:
                    continue
                label = row[0].strip()
                if not label:
                    continue
                value = row[col_idx].strip() if len(row) > col_idx else ""
                properties[label] = value

            # Подставляем общие поля из первого столбца, если они пустые
            for key, value in properties.items():
                if not value and key in first_col_values:
                    properties[key] = first_col_values[key]

            result[city] = properties

        return result
