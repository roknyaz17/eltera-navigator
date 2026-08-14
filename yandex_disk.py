"""Чтение публичной папки Яндекс.Диска.

Публичная ссылка отдаётся через открытый REST API Яндекса, авторизация не
нужна — поэтому здесь нет ни токенов, ни OAuth: только public_key (сама
ссылка) и путь внутри неё.

Модуль намеренно знает только про транспорт: получить список, скачать файл,
достать из файла текст. Что с этим текстом делать — решает project_kb.py.
"""

import html
import io
import re
import time
import zipfile
from typing import Dict, List, Optional
from urllib.parse import quote

import requests
from loguru import logger

API_ROOT = "https://cloud-api.yandex.net/v1/disk/public/resources"

# Диск отвечает 429 при частых запросах. Полный обход — это ~80 листингов,
# поэтому хватает короткой паузы и пары повторов.
RETRY_PAUSES = (1.0, 3.0, 7.0)


class YandexDiskError(RuntimeError):
    """Диск недоступен или ответил ошибкой. Прогон из-за этого падать не должен."""


class PublicDisk:
    """Публичная папка Яндекс.Диска, доступная по ссылке.

    :param public_url: ссылка вида https://disk.yandex.ru/d/XXXXXXXX
    """

    def __init__(self, public_url: str, timeout: float = 30.0):
        self.public_url = public_url.strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "eltera-navigator/1.0"})

    # ------------------------------------------------------------------ API
    def _get(self, url: str, params: Optional[Dict] = None) -> Dict:
        last_error = ""
        for attempt, pause in enumerate((0.0,) + RETRY_PAUSES):
            if pause:
                time.sleep(pause)
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                continue
            if response.status_code == 200:
                return response.json()
            # 404 — папки нет: повторять бессмысленно.
            if response.status_code == 404:
                raise YandexDiskError(f"404 {params or url}")
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            logger.debug(f"[yadisk] попытка {attempt + 1}: {last_error}")
        raise YandexDiskError(last_error or "нет ответа")

    def listdir(self, path: str = "/", limit: int = 500) -> List[Dict]:
        """Содержимое одной папки. Вложенные папки не разворачивает."""
        items: List[Dict] = []
        offset = 0
        while True:
            data = self._get(API_ROOT, {
                "public_key": self.public_url,
                "path": path,
                "limit": limit,
                "offset": offset,
            })
            embedded = data.get("_embedded") or {}
            batch = embedded.get("items") or []
            items.extend(batch)
            total = embedded.get("total", len(items))
            offset += len(batch)
            if not batch or offset >= total:
                break
        return items

    def download(self, path: str) -> bytes:
        """Скачивает файл по пути внутри публичной папки.

        Ссылка на скачивание каждый раз ведёт на случайный узел хранилища
        Яндекса, и часть узлов отдаёт просроченный сертификат. Проверку
        сертификата это отключать не повод: достаточно попросить ссылку
        заново — выпадет другой узел.
        """
        last_error = ""
        for pause in (0.0,) + RETRY_PAUSES:
            if pause:
                time.sleep(pause)
            href = self._get(f"{API_ROOT}/download", {
                "public_key": self.public_url,
                "path": path,
            }).get("href")
            if not href:
                raise YandexDiskError(f"диск не дал ссылку на скачивание: {path}")
            try:
                response = self.session.get(href, timeout=max(self.timeout, 60.0))
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.debug(f"[yadisk] скачивание {path}: {last_error}")
                continue
            if response.status_code == 200:
                return response.content
            last_error = f"HTTP {response.status_code}"
        raise YandexDiskError(f"скачивание {path}: {last_error}")

    def folder_url(self, path: str) -> str:
        """Ссылка на папку для человека — её можно открыть из карточки заявки."""
        clean = path.lstrip("/")
        if not clean:
            return self.public_url
        return f"{self.public_url.rstrip('/')}/{quote(clean)}"


# ------------------------------------------------------------------- документы

def docx_to_text(blob: bytes) -> str:
    """Текст из .docx без внешних зависимостей.

    docx — это zip с word/document.xml; python-docx ради двух регулярок в
    зависимости тянуть не нужно. Разметка выкидывается, абзацы и табуляции
    сохраняются: описания проектов свёрстаны списками, и без переносов строк
    они склеиваются в кашу.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [n for n in archive.namelist() if n == "word/document.xml"]
        if not names:
            return ""
        xml = archive.read("word/document.xml").decode("utf-8", "replace")

    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br\b[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p\s*>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    text = html.unescape(xml)
    # Мягкие переносы и неразрывные пробелы из Word — в обычные.
    text = text.replace("\xa0", " ").replace("​", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def file_to_text(name: str, blob: bytes) -> str:
    """Текст файла, если формат читаемый. Иначе пустая строка.

    Картинки (а их на диске большинство) сюда не попадают осознанно: мы не
    распознаём фотографии, а лишь считаем их и показываем ссылку на альбом.
    """
    low = name.lower()
    try:
        if low.endswith(".docx"):
            return docx_to_text(blob)
        if low.endswith((".txt", ".md", ".csv")):
            return blob.decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        logger.warning(f"[yadisk] не прочитал {name}: {exc}")
    return ""


def is_readable(name: str) -> bool:
    return name.lower().endswith((".docx", ".txt", ".md", ".csv"))


def is_image(name: str) -> bool:
    return name.lower().endswith((".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"))
