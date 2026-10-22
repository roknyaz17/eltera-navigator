"""Тонкий HTTP-клиент ElevenLabs Agents API.

Здесь только транспорт: пути, тела запросов и разбор ошибок. Логика обзвона живёт
в zvonilka.py — так слой провайдера остаётся заменяемым.

Все пути и имена полей сверены с https://api.elevenlabs.io/openapi.json (04.09.2026).
Особенно легко ошибиться в двух местах, поэтому явно:
  * путь звонка — /v1/convai/sip-trunk/outbound-call, дефисы в обоих сегментах;
  * ответ SIP-звонка содержит sip_call_id (snake_case), а не callSid, как у Twilio.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import httpx

BASE_URL = "https://api.elevenlabs.io"

# Статусы разговора из openapi.json. Дефис в "in-progress" — не опечатка.
ALL_STATUSES = ("initiated", "in-progress", "processing", "done", "failed")
# После этих статусов опрашивать больше нечего.
TERMINAL_STATUSES = frozenset({"done", "failed"})

# Коды, которые имеет смысл повторить: троттлинг, таймаут и сбои на стороне сервиса.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ElevenLabsError(RuntimeError):
    """Ошибка API с разобранным телом ответа.

    ElevenLabs отдаёт два несовместимых формата: {"detail": {...}} для доменных
    ошибок и {"detail": [...]} для ошибок валидации (422). Разбираем оба.
    """

    def __init__(self, status_code: int, payload: Any, method: str, path: str) -> None:
        self.status_code = status_code
        self.payload = payload
        self.code: str | None = None

        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            self.code = detail.get("code") or detail.get("type")
            message = detail.get("message") or detail.get("type") or str(detail)
        elif isinstance(detail, list) and detail:
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in (item.get("loc") or []))
                    parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
                else:
                    parts.append(str(item))
            message = "; ".join(parts)
        else:
            message = str(payload)[:500]

        prefix = f"[{self.code}] " if self.code else ""
        super().__init__(f"{method} {path} -> HTTP {status_code}: {prefix}{message}")


def _prune(value: Any) -> Any:
    """Рекурсивно выбрасывает None и пустые словари.

    Документация ElevenLabs прямо просит не слать null в override-полях: пустое
    значение трактуется как «перезаписать пустым», а не «не трогать».
    """
    if isinstance(value, dict):
        cleaned = {k: _prune(v) for k, v in value.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v != {}}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


class ElevenLabsAgents:
    """Клиент раздела Agents Platform (/v1/convai/*)."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("Не задан ELEVENLABS_API_KEY")
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"xi-api-key": api_key},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ElevenLabsAgents":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- транспорт -----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.request(method, path, json=json_body, params=params)
            except httpx.TransportError as exc:  # сеть моргнула — имеет смысл повторить
                last_error = exc
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2**attempt)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries - 1:
                # Retry-After в этом API не документирован — используем свой бэкофф.
                time.sleep(2**attempt)
                continue

            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                raise ElevenLabsError(response.status_code, payload, method, path)

            if raw:
                return response.content
            if not response.content:
                return None
            return response.json()

        raise last_error if last_error else RuntimeError("недостижимо")

    # --- телефонные номера ---------------------------------------------------

    def create_sip_trunk_number(
        self,
        *,
        phone_number: str,
        label: str,
        address: str,
        username: str | None = None,
        password: str | None = None,
        transport: str = "tls",
        media_encryption: str = "allowed",
        agent_id: str | None = None,
        enabled_codecs: list[str] | None = None,
        headers: dict[str, str] | None = None,
        remote_domains: list[str] | None = None,
    ) -> str:
        """Регистрирует SIP-транк и возвращает phone_number_id.

        Обязательны только phone_number и label. `address` — hostname или IP, куда
        ElevenLabs шлёт INVITE: без схемы "sip:" и без порта.

        Если credentials не переданы, ElevenLabs переходит на ACL-авторизацию по
        IP источника, а её IP-адреса плавают — на практике это означает отказ.
        """
        credentials = None
        if username:
            credentials = {"username": username, "password": password}

        body = {
            "provider": "sip_trunk",  # шлём явно: в схеме это anyOf без discriminator
            "phone_number": phone_number,
            "label": label,
            "agent_id": agent_id,
            "outbound_trunk_config": {
                "address": address,
                "transport": transport,
                "media_encryption": media_encryption,
                "credentials": credentials,
                "enabled_codecs": enabled_codecs,
                "headers": headers,
            },
            "inbound_trunk_config": {
                "media_encryption": media_encryption,
                "credentials": credentials,
                "remote_domains": remote_domains,
            },
        }
        result = self._request("POST", "/v1/convai/phone-numbers", json_body=_prune(body))
        return result["phone_number_id"]

    def list_phone_numbers(self, provider: str | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/convai/phone-numbers", params={"provider": provider}) or []

    def get_phone_number(self, phone_number_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/convai/phone-numbers/{phone_number_id}")

    def update_phone_number(self, phone_number_id: str, **fields: Any) -> dict[str, Any]:
        """Обновляет номер. Так же назначается агент: update(..., agent_id=...).

        Ответ GET нельзя скормить обратно в PATCH: там inbound_trunk/outbound_trunk,
        а PATCH ждёт inbound_trunk_config/outbound_trunk_config.
        """
        return self._request(
            "PATCH", f"/v1/convai/phone-numbers/{phone_number_id}", json_body=_prune(fields)
        )

    def delete_phone_number(self, phone_number_id: str) -> None:
        self._request("DELETE", f"/v1/convai/phone-numbers/{phone_number_id}")

    # --- агенты --------------------------------------------------------------

    def list_agents(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/v1/convai/agents")
        if isinstance(result, dict):
            return result.get("agents", [])
        return result or []

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/convai/agents/{agent_id}")

    # --- исходящий звонок ----------------------------------------------------

    def outbound_call(
        self,
        *,
        agent_id: str,
        agent_phone_number_id: str,
        to_number: str,
        dynamic_variables: dict[str, Any] | None = None,
        ringing_timeout_secs: int | None = None,
        max_duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Инициирует звонок через SIP-транк.

        Возвращает {success, message, conversation_id, sip_call_id}. Ключи всегда
        присутствуют, но conversation_id и sip_call_id могут быть null.

        Осторожно: success=true означает лишь, что запрос принят. Отказ SIP-авторизации
        или недоступность АТС всплывут позже — смотрите get_conversation_sip_messages.
        """
        initiation: dict[str, Any] = {}
        if dynamic_variables:
            initiation["dynamic_variables"] = dynamic_variables
        if max_duration_seconds:
            # conversation.max_duration_seconds — override, требует включения
            # соответствующего тумблера в разделе Security у агента.
            initiation["conversation_config_override"] = {
                "conversation": {"max_duration_seconds": max_duration_seconds}
            }

        body = {
            "agent_id": agent_id,
            "agent_phone_number_id": agent_phone_number_id,
            "to_number": to_number,
            "conversation_initiation_client_data": initiation or None,
            "telephony_call_config": (
                {"ringing_timeout_secs": ringing_timeout_secs} if ringing_timeout_secs else None
            ),
        }
        return self._request(
            "POST", "/v1/convai/sip-trunk/outbound-call", json_body=_prune(body)
        )

    # --- разговоры -----------------------------------------------------------

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/convai/conversations/{conversation_id}")

    def get_conversation_audio(self, conversation_id: str) -> bytes:
        """Скачивает запись разговора (audio/mpeg).

        Перед вызовом проверяйте has_audio у разговора: отдельного статуса
        «записи нет» в документации не заявлено.
        """
        return self._request(
            "GET", f"/v1/convai/conversations/{conversation_id}/audio", raw=True
        )

    def list_conversations(
        self, *, agent_id: str | None = None, page_size: int = 30, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            "/v1/convai/conversations",
            params={"agent_id": agent_id, "page_size": page_size, "cursor": cursor},
        )

    # --- инструменты агента --------------------------------------------------

    def create_secret(self, name: str, value: str) -> str:
        """Кладёт секрет в воркспейс и возвращает secret_id.

        Нужен, чтобы токен нашей ручки не лежал в конфиге инструмента открытым:
        в заголовке указывается ссылка на секрет, а не само значение.
        """
        result = self._request(
            "POST", "/v1/convai/secrets",
            json_body={"type": "new", "name": name, "value": value},
        )
        return result["secret_id"]

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/v1/convai/tools")
        if isinstance(result, dict):
            return result.get("tools", [])
        return result or []

    def create_tool(self, tool_config: dict[str, Any]) -> str:
        """Создаёт инструмент. tool_config — тело без обёртки.

        Обёртка важна: response_mocks лежит рядом с tool_config, а не внутри,
        и вложенный вариант схема не принимает.
        """
        result = self._request(
            "POST", "/v1/convai/tools", json_body={"tool_config": tool_config}
        )
        return result["id"]

    def delete_tool(self, tool_id: str) -> None:
        self._request("DELETE", f"/v1/convai/tools/{tool_id}")

    def get_tool_executions(self, tool_id: str, page_size: int = 30) -> list[dict[str, Any]]:
        """Журнал вызовов инструмента — видно, с чем агент звал ручку."""
        result = self._request(
            "GET", f"/v1/convai/tools/{tool_id}/executions",
            params={"page_size": page_size},
        )
        if isinstance(result, dict):
            return result.get("executions") or result.get("items") or []
        return result or []

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/v1/convai/agents/{agent_id}", json_body=_prune(fields)
        )

    def attach_tools(self, agent_id: str, tool_ids: list[str]) -> None:
        """Привязывает инструменты к агенту.

        Список заменяется целиком, поэтому сначала читаем текущий: слепая
        запись отвязала бы всё, что настроено в дашборде руками.
        Одновременно слать tool_ids и устаревшее поле tools нельзя — ошибка.
        """
        agent = self.get_agent(agent_id)
        prompt = (
            (agent.get("conversation_config") or {}).get("agent") or {}
        ).get("prompt") or {}
        merged = list(dict.fromkeys((prompt.get("tool_ids") or []) + tool_ids))
        self.update_agent(
            agent_id,
            conversation_config={"agent": {"prompt": {"tool_ids": merged}}},
        )

    # --- диагностика SIP -----------------------------------------------------

    def get_conversation_sip_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        """Сырые SIP-сообщения разговора — главный инструмент отладки транка."""
        result = self._request(
            "GET",
            f"/v1/convai/conversations/{conversation_id}/sip-messages",
            params={"page_size": 20},
        )
        return (result or {}).get("sip_messages", [])

    def get_phone_number_sip_messages(self, phone_number_id: str) -> list[dict[str, Any]]:
        """SIP-сообщения номера: видны даже тогда, когда разговор не создался."""
        result = self._request(
            "GET",
            f"/v1/convai/phone-numbers/{phone_number_id}/sip-messages",
            params={"page_size": 20},
        )
        return (result or {}).get("sip_messages", [])

    # --- опрос до завершения -------------------------------------------------

    def poll_conversation(
        self, conversation_id: str, *, timeout_secs: int = 900, interval_secs: float = 2.0
    ) -> Iterator[dict[str, Any]]:
        """Опрашивает разговор, отдавая каждый снимок состояния.

        Останавливается на терминальном статусе или по таймауту. Вызывающий код сам
        решает, что делать с промежуточными снимками — например, печатать новые реплики.
        """
        deadline = time.monotonic() + timeout_secs
        while True:
            conversation = self.get_conversation(conversation_id)
            yield conversation
            if conversation.get("status") in TERMINAL_STATUSES:
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(interval_secs)
