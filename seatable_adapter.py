"""
Адаптер для чтения публичной "external link" таблицы Seatable.

Доступ к Yappi реализован так:
1. Запрашиваем HTML страницы внешней ссылки.
2. Из встроенного JS-конфига вытаскиваем dtable_uuid и короткоживущий JWT
   (accessToken c permission='r').
3. Ходим в SeaTable API Gateway:
   - GET /api-gateway/api/v2/dtables/<uuid>/metadata/
   - GET /api-gateway/api/v2/dtables/<uuid>/rows/?table_name=<name>

Значения select-полей в строках приходят как ID опций — мы их резолвим в текст
по metadata.
"""

import html as html_module
import re
from typing import Dict, List, Optional

import requests


class SeatableExternalLinkClient:
    """Клиент для одной публичной external-link таблицы Seatable."""

    DEFAULT_SERVER = "https://cloud.seatable.io"

    def __init__(self, external_link_url: str, server: str = DEFAULT_SERVER):
        """
        :param external_link_url: URL вида
            https://cloud.seatable.io/dtable/external-links/custom/<slug>/
            (query-параметры tid/vid игнорируются)
        :param server: базовый URL Seatable
        """
        self.server = server.rstrip("/")
        self.link_url = external_link_url.split("?")[0]
        self._uuid: Optional[str] = None
        self._token: Optional[str] = None

    # ------------------------------------------------------------------ auth
    def authorize(self) -> None:
        """Получает свежий JWT и dtable_uuid с HTML-страницы внешней ссылки."""
        r = requests.get(self.link_url, timeout=15)
        r.raise_for_status()
        uuid_m = re.search(r"dtableUuid:\s*'([^']+)'", r.text)
        token_m = re.search(r"accessToken:\s*'([^']+)'", r.text)
        if not uuid_m or not token_m:
            raise RuntimeError(
                f"Не удалось извлечь dtableUuid/accessToken со страницы {self.link_url}"
            )
        self._uuid = uuid_m.group(1)
        self._token = token_m.group(1)

    @property
    def _headers(self) -> Dict[str, str]:
        if not self._token:
            self.authorize()
        return {"Authorization": f"Token {self._token}"}

    @property
    def dtable_uuid(self) -> str:
        if not self._uuid:
            self.authorize()
        return self._uuid  # type: ignore[return-value]

    # ------------------------------------------------------------------ api
    def get_metadata(self) -> Dict:
        url = f"{self.server}/api-gateway/api/v2/dtables/{self.dtable_uuid}/metadata/"
        r = requests.get(url, headers=self._headers, timeout=15)
        r.raise_for_status()
        return r.json()["metadata"]

    def get_rows(self, table_name: str, limit: int = 1000) -> List[Dict]:
        url = f"{self.server}/api-gateway/api/v2/dtables/{self.dtable_uuid}/rows/"
        r = requests.get(
            url,
            headers=self._headers,
            params={"table_name": table_name, "limit": limit},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("rows", [])

    # ----------------------------------------------------------- high-level
    def fetch_table_as_dicts(
            self, table_name: Optional[str] = None
    ) -> List[Dict[str, str]]:
        """
        Возвращает строки таблицы как список словарей {имя_колонки: человекочитаемая_строка}.
        Если table_name не указан — берётся первая таблица в base.
        Все значения сводятся к строке: select-опции резолвятся в имена,
        markdown-форматирование и HTML-сущности убираются.
        """
        meta = self.get_metadata()
        tables = meta.get("tables", [])
        if not tables:
            return []

        if table_name is None:
            table = tables[0]
        else:
            table = next((t for t in tables if t["name"] == table_name), None)
            if table is None:
                raise ValueError(f"Таблица '{table_name}' не найдена в base")

        # Карта key -> (name, type, options{id: option_name})
        col_map: Dict[str, Dict] = {}
        for col in table["columns"]:
            options = {}
            for opt in (col.get("data") or {}).get("options", []) or []:
                options[opt["id"]] = opt["name"]
            col_map[col["key"]] = {
                "name": col["name"],
                "type": col["type"],
                "options": options,
            }

        rows = self.get_rows(table["name"])
        result: List[Dict[str, str]] = []
        for row in rows:
            decoded: Dict[str, str] = {}
            for key, value in row.items():
                if key.startswith("_") or key not in col_map:
                    continue
                col = col_map[key]
                rendered = self._render_value(value, col)
                if rendered:
                    decoded[col["name"]] = rendered
            if decoded:
                result.append(decoded)
        return result

    # ------------------------------------------------------------ rendering
    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = html_module.unescape(text)
        # **bold** / __italic__ → plain
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"__(.+?)__", r"\1", text)
        text = re.sub(r"[*_]{1,2}", "", text)
        # zero-width / спецсимволы
        text = text.replace("​", "").replace("\xa0", " ")
        # схлопываем пустые строки
        lines = [ln.strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln)

    @classmethod
    def _render_value(cls, value, col: Dict) -> str:
        if value is None or value == "":
            return ""

        col_type = col["type"]
        options = col["options"]

        if col_type == "single-select":
            return options.get(value, str(value))

        if col_type == "multiple-select":
            if not isinstance(value, list):
                value = [value]
            names = [options.get(v, str(v)) for v in value]
            return ", ".join(n for n in names if n)

        if col_type in ("text", "long-text", "url", "email"):
            return cls._strip_markdown(str(value))

        if col_type in ("number", "rate", "duration"):
            return str(value)

        if col_type in ("file", "image"):
            # Не тянем файлы в LLM — просто пометим, что есть прикрепления
            if isinstance(value, list) and value:
                return f"<{len(value)} файл(ов)>"
            return ""

        # Все прочие типы — превращаем как умеем
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return cls._strip_markdown(str(value))
