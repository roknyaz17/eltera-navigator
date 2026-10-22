"""Клиент CRM API МегаФон ВАТС (платформа ITooLabs).

Зачем он вообще нужен: ElevenLabs не может дозвониться до ВАТС снаружи — хост
платформы закрыт для зарубежных IP (подробности в README). Поэтому звонок
инициирует сама АТС, а ElevenLabs принимает его как входящий на добавочный номер.

Схема вызова makecall: АТС сначала поднимает внутренний номер (`user`), и только
потом набирает абонента (`phone`). Порядок ног зафиксирован в документации и
параметром не меняется — это важно для настройки агента, см. README.

На платформе сосуществуют две версии API. Здесь используется актуальная,
`/crmapi/v1/*` с заголовком X-API-KEY. Legacy-вариант `/sys/crm_api.wcgp`
(form-encoded, token в теле) тоже жив, но нужен в основном готовым CRM.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# Коды завершения звонка из cmd=history. Успех — только Success.
CALL_STATUSES = (
    "Success", "Missed", "Cancel", "Busy", "NotAvailable", "NotAllowed", "NotFound",
)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class PBXError(RuntimeError):
    """Ошибка CRM API ВАТС.

    Платформа отвечает то {"message": ...}, то {"error": ...} — разбираем оба.
    """

    def __init__(self, status_code: int, payload: Any, path: str) -> None:
        self.status_code = status_code
        self.payload = payload
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error") or str(payload)
        else:
            message = str(payload)[:400]

        hint = ""
        if status_code == 401:
            hint = (
                "\n    Проверьте PBX_API_KEY. Ключ берётся в кабинете ВАТС: "
                "Интеграция с CRM → Ваша CRM (REST API) → «Ключ для авторизации в АТС»."
            )
        elif status_code == 403:
            hint = (
                "\n    Доступ запрещён. Включите «Команда makeCall» в разделе команд API "
                "и проверьте, не ограничен ли доступ к аккаунту по IP."
            )
        super().__init__(f"{path} -> HTTP {status_code}: {message}{hint}")


class MegaPBX:
    """CRM API ВАТС: инициация звонков и история."""

    def __init__(
        self, base_url: str, api_key: str, timeout: float = 20.0, use_proxy: bool = False
    ) -> None:
        if not base_url:
            raise ValueError("Не задан PBX_BASE_URL")
        if not api_key:
            raise ValueError("Не задан PBX_API_KEY")
        # trust_env=False намеренно: хост ВАТС закрыт для зарубежных IP, а
        # HTTPS_PROXY на машине разработчика обычно уводит трафик за границу.
        # Тогда запрос упирается в тот же геофильтр, что и SIP. Ходим напрямую.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
            trust_env=use_proxy,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "MegaPBX":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(
        self, method: str, path: str, *, json_body: Any = None, params: dict | None = None
    ) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        for attempt in range(3):
            response = self._client.request(method, path, json=json_body, params=params)
            if response.status_code in RETRYABLE_STATUS and attempt < 2:
                time.sleep(2**attempt)
                continue
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                raise PBXError(response.status_code, payload, path)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return response.text
        raise RuntimeError("недостижимо")

    # --- команды -------------------------------------------------------------

    def users(self) -> list[dict[str, Any]]:
        """Сотрудники и их короткие номера. Полезно, чтобы проверить добавочный."""
        result = self._request("GET", "/crmapi/v1/users")
        if isinstance(result, dict):
            return result.get("users") or result.get("items") or []
        return result or []

    def make_call(
        self,
        *,
        phone: str,
        user: str | None = None,
        group: str | None = None,
        clid: str | None = None,
        show_phone: bool | None = None,
    ) -> dict[str, Any]:
        """Инициирует звонок и возвращает {"callid": ..., "clid": ...}.

        `phone` — внешний номер абонента (обязателен).
        `user`  — внутренний номер или логин, который поднимут первым.
        `clid`  — исходящий номер, который увидит абонент.

        Помните про порядок: сначала снимут трубку на `user`, потом наберут `phone`.
        """
        body = {
            "phone": phone,
            "user": user,
            "group": group,
            "clid": clid,
            "show_phone": show_phone,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self._request("POST", "/crmapi/v1/makecall", json_body=body) or {}

    def history(self, *, uid: str | None = None, limit: int = 20, **filters: Any) -> list[dict]:
        """История звонков. По uid отдаёт конкретный звонок из makecall."""
        params = {"uid": uid, "limit": limit, **filters}
        result = self._request("GET", "/crmapi/v1/history/json", params=params)
        if isinstance(result, dict):
            return result.get("history") or result.get("items") or []
        return result or []

    def wait_for_result(
        self, callid: str, *, timeout_secs: int = 300, interval_secs: float = 5.0
    ) -> dict[str, Any] | None:
        """Опрашивает историю, пока по звонку не появится запись с длительностью.

        Возвращает запись CDR или None, если за отведённое время ничего не нашлось.
        Пока звонок идёт, платформа может вообще не показывать его в истории.
        """
        deadline = time.monotonic() + timeout_secs
        while time.monotonic() < deadline:
            try:
                records = self.history(uid=callid)
            except PBXError:
                records = []
            for record in records:
                if record.get("status") or record.get("duration") is not None:
                    return record
            time.sleep(interval_secs)
        return None
