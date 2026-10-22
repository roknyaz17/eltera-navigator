#!/usr/bin/env python
"""Звонилка: исходящие звонки голосовым агентом ElevenLabs через SIP-транк.

Диалог целиком ведёт ElevenLabs — распознавание, LLM, синтез и перехват реплик.
Медиапоток идёт напрямую между ElevenLabs и вашей АТС, минуя эту машину. Скрипт
только инициирует звонки и забирает результат: транскрипт и запись.

Команды:
    python zvonilka.py check                    проверить конфигурацию
    python zvonilka.py setup-trunk              завести SIP-транк в ElevenLabs
    python zvonilka.py numbers                  список номеров
    python zvonilka.py agents                   список агентов
    python zvonilka.py call-relay +79991234567  один звонок (основной режим)
    python zvonilka.py call-list numbers.csv    обзвон списка
    python zvonilka.py fetch conv_xxx           забрать транскрипт и запись
    python zvonilka.py sip-log                  сырые SIP-сообщения для отладки

Режимы набора различаются тем, кто инициирует вызов.

`call-relay` — основной. Релей на российском VPS сам набирает абонента через ВАТС
и подключает агента только по SIP 200 OK, то есть по факту снятия трубки.

`call-pbx` — запасной, через click-to-call ВАТС. Оставлен для отладки: там АТС
поднимает добавочный первым и успевает наговорить агенту служебное объявление.

`call` — прямой транк ElevenLabs. Не работает с МегаФон ВАТС: её хост закрыт для
зарубежных IP. Пригодится при переезде на оператора, доступного снаружи.

Подробности и диагностика — в README.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import secrets
import string
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from elevenlabs_api import ElevenLabsAgents, ElevenLabsError
from megapbx import MegaPBX, PBXError

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


# --- конфигурация ------------------------------------------------------------


@dataclass
class Config:
    api_key: str
    agent_id: str
    phone_number_id: str
    sip_address: str
    sip_username: str
    sip_password: str
    sip_transport: str
    sip_media_encryption: str
    sip_codecs: list[str]
    caller_number: str
    pbx_base_url: str
    pbx_api_key: str
    pbx_extension: str
    pbx_clid: str
    pbx_use_proxy: bool
    pbx_dial_format: str
    relay_ssh: str
    navigator_url: str
    navigator_token: str
    relay_container: str
    ringing_timeout_secs: int
    call_timeout_secs: int
    calls_dir: Path

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        codecs = os.getenv("SIP_CODECS", "PCMA/8000,PCMU/8000")
        return cls(
            api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
            agent_id=os.getenv("ELEVENLABS_AGENT_ID", "").strip(),
            phone_number_id=os.getenv("ELEVENLABS_PHONE_NUMBER_ID", "").strip(),
            sip_address=os.getenv("SIP_ADDRESS", "").strip(),
            sip_username=os.getenv("SIP_USERNAME", "").strip(),
            sip_password=os.getenv("SIP_PASSWORD", "").strip(),
            sip_transport=os.getenv("SIP_TRANSPORT", "tls").strip(),
            sip_media_encryption=os.getenv("SIP_MEDIA_ENCRYPTION", "allowed").strip(),
            sip_codecs=[c.strip() for c in codecs.split(",") if c.strip()],
            caller_number=os.getenv("SIP_CALLER_NUMBER", "").strip(),
            pbx_base_url=os.getenv("PBX_BASE_URL", "").strip(),
            pbx_api_key=os.getenv("PBX_API_KEY", "").strip(),
            pbx_extension=os.getenv("PBX_EXTENSION", "").strip(),
            pbx_clid=os.getenv("PBX_CLID", "").strip(),
            pbx_use_proxy=os.getenv("PBX_USE_PROXY", "false").strip().lower()
            in ("1", "true", "yes"),
            pbx_dial_format=os.getenv("PBX_DIAL_FORMAT", "plain").strip().lower(),
            relay_ssh=os.getenv("RELAY_SSH", "").strip(),
            navigator_url=os.getenv("NAVIGATOR_URL", "").strip(),
            navigator_token=os.getenv("NAVIGATOR_AGENT_TOKEN", "").strip(),
            relay_container=os.getenv("RELAY_CONTAINER", "zvonilka-relay").strip(),
            ringing_timeout_secs=int(os.getenv("RINGING_TIMEOUT_SECS", "45")),
            call_timeout_secs=int(os.getenv("CALL_TIMEOUT_SECS", "900")),
            calls_dir=Path(os.getenv("CALLS_DIR", "calls")),
        )

    def pbx_client(self) -> MegaPBX:
        return MegaPBX(self.pbx_base_url, self.pbx_api_key, use_proxy=self.pbx_use_proxy)

    def require(self, *fields: str) -> None:
        missing = [f for f in fields if not getattr(self, f)]
        if missing:
            names = ", ".join(f.upper() for f in missing)
            raise SystemExit(f"Не заданы обязательные параметры в .env: {names}")


# --- вывод -------------------------------------------------------------------


def say(message: str = "") -> None:
    print(message, flush=True)


def normalize_number(raw: str) -> str:
    """Приводит номер к E.164. Российские 8XXX и 7XXX превращаются в +7XXX."""
    digits = re.sub(r"[^\d+]", "", raw.strip())
    if digits.startswith("+"):
        result = digits
    elif digits.startswith("8") and len(digits) == 11:
        result = "+7" + digits[1:]
    elif digits.startswith("7") and len(digits) == 11:
        result = "+" + digits
    else:
        result = "+" + digits
    if not E164.match(result):
        raise ValueError(f"Не похоже на номер в формате E.164: {raw!r} -> {result!r}")
    return result


def format_for_pbx(number: str, dial_format: str) -> str:
    """Переводит E.164 в формат набора, который ждёт ВАТС.

    Документация требует набирать «с кодом города», но какой именно вид принимает
    makecall — плюс, семёрка или восьмёрка — нигде не зафиксировано. Поэтому формат
    вынесен в настройку: если набор не проходит, попробуйте соседний вариант.
    """
    if dial_format == "e164":
        return number
    digits = number.lstrip("+")
    if dial_format == "plain":
        return digits
    if dial_format == "national":
        return "8" + digits[1:] if digits.startswith("7") else digits
    raise SystemExit(
        f"Неизвестный PBX_DIAL_FORMAT={dial_format!r}. Допустимо: e164, plain, national."
    )


# --- артефакты звонка --------------------------------------------------------


def call_dir(cfg: Config, number: str, started: datetime) -> Path:
    safe = number.replace("+", "")
    path = cfg.calls_dir / f"{started:%Y%m%d-%H%M%S}_{safe}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_transcript(conversation: dict[str, Any]) -> str:
    lines = []
    for turn in conversation.get("transcript") or []:
        message = turn.get("message")
        if not message:
            continue  # реплики с одними tool-вызовами текста не несут
        who = "Агент " if turn.get("role") == "agent" else "Клиент"
        lines.append(f"[{turn.get('time_in_call_secs', 0):>4}s] {who}: {message}")
    return "\n".join(lines)


def save_artifacts(
    api: ElevenLabsAgents, conversation: dict[str, Any], directory: Path
) -> None:
    """Сохраняет транскрипт и, если запись есть, mp3."""
    conversation_id = conversation.get("conversation_id")

    (directory / "conversation.json").write_text(
        json.dumps(conversation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (directory / "transcript.txt").write_text(
        render_transcript(conversation) + "\n", encoding="utf-8"
    )

    if not conversation.get("has_audio"):
        say("  Запись недоступна (has_audio=false).")
        return
    try:
        audio = api.get_conversation_audio(conversation_id)
    except ElevenLabsError as exc:
        say(f"  Запись скачать не удалось: {exc}")
        return
    (directory / "recording.mp3").write_bytes(audio)
    say(f"  Запись: {directory / 'recording.mp3'} ({len(audio) // 1024} КБ)")


def watch_call(
    api: ElevenLabsAgents, conversation_id: str, timeout_secs: int
) -> dict[str, Any]:
    """Опрашивает разговор и печатает новые реплики по мере появления."""
    printed = 0
    last_status = None
    conversation: dict[str, Any] = {}

    for conversation in api.poll_conversation(conversation_id, timeout_secs=timeout_secs):
        status = conversation.get("status")
        if status != last_status:
            say(f"  · статус: {status}")
            last_status = status

        turns = conversation.get("transcript") or []
        for turn in turns[printed:]:
            message = turn.get("message")
            if message:
                who = "Агент " if turn.get("role") == "agent" else "Клиент"
                say(f"  [{turn.get('time_in_call_secs', 0):>4}s] {who}: {message}")
        printed = len(turns)

    return conversation


def collected_fields(conversation: dict[str, Any]) -> dict[str, Any]:
    """Что агент собрал в разговоре: плоский словарь поле-значение.

    Имена полей не перечисляем: их состав задаётся в панели ElevenLabs и
    меняется без нашего участия. Берём всё, что пришло, — иначе каждое новое
    поле пришлось бы добавлять ещё и сюда, а забытое поле выглядит как
    неотвеченный вопрос.
    """
    results = (conversation.get("analysis") or {}).get("data_collection_results") or {}
    return {name: (item or {}).get("value") for name, item in results.items()}


def summarize(conversation: dict[str, Any]) -> dict[str, Any]:
    metadata = conversation.get("metadata") or {}
    analysis = conversation.get("analysis") or {}
    return {
        "conversation_id": conversation.get("conversation_id"),
        "status": conversation.get("status"),
        "call_successful": analysis.get("call_successful"),
        "duration_secs": metadata.get("call_duration_secs"),
        "termination_reason": metadata.get("termination_reason"),
        "turns": len(conversation.get("transcript") or []),
        "summary": analysis.get("transcript_summary"),
        # Отдельным вложенным словарём, а не вперемешку: среди собираемых
        # полей есть своё «summary», и оно про другое.
        "collected": collected_fields(conversation),
    }


def report_call(cfg: Config, number: str, conversation: dict[str, Any]) -> None:
    """Отдаёт итог разговора навигатору: карточка кандидата и очередь сообщений.

    Номер передаём отсюда, потому что больше его знать неоткуда: в режиме релея
    ElevenLabs видит только добавочный 710 (metadata.phone_call.external_number),
    а агенту номер не сообщают вовсе — в тестовых звонках он подставлял в его
    место выдуманный. Достоверен ровно тот номер, который набрали.

    Разговор уходит целиком: состав собираемых агентом полей меняется, и разбор
    на стороне сервера избавляет от выкатки сюда при каждом новом поле.

    Сбой навигатора звонок не роняет и на код возврата не влияет. Артефакты уже
    на диске, обзвон продолжается; недоехавшая карточка — повод посмотреть логи,
    а не считать разговор неудавшимся.
    """
    if not cfg.navigator_url or not cfg.navigator_token:
        return
    import httpx

    try:
        response = httpx.post(
            cfg.navigator_url.rstrip("/") + "/api/agent/candidates/call",
            json={"phone": number, "conversation": conversation},
            headers={"X-Agent-Token": cfg.navigator_token},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 — причин отказа много, реакция одна
        say(f"  [!] Навигатор не принял итог звонка: {type(exc).__name__}: {exc}")
        return

    if body.get("duplicate"):
        say("  Навигатор: разговор уже был сохранён раньше.")
    elif body.get("message_id"):
        say(f"  Навигатор: карточка кандидата обновлена, сообщение №{body['message_id']} в очереди.")
    else:
        say("  Навигатор: карточка кандидата обновлена, отправлять нечего.")


# --- команды -----------------------------------------------------------------


def cmd_check(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    problems = 0

    try:
        agents = api.list_agents()
        say(f"[ок] Ключ API рабочий, агентов в воркспейсе: {len(agents)}")
    except ElevenLabsError as exc:
        say(f"[!!] Ключ API не принят: {exc}")
        return 1

    if not cfg.agent_id:
        say("[!!] ELEVENLABS_AGENT_ID не задан — возьмите его из дашборда (команда agents)")
        problems += 1
    else:
        try:
            agent = api.get_agent(cfg.agent_id)
            say(f"[ок] Агент: {agent.get('name')} ({cfg.agent_id})")
            privacy = ((agent.get("platform_settings") or {}).get("privacy")) or {}
            if privacy.get("record_voice") is False:
                say("[!] У агента выключено сохранение записи (privacy.record_voice=false)")
                problems += 1
        except ElevenLabsError as exc:
            say(f"[!!] Агент недоступен: {exc}")
            problems += 1

    if not cfg.phone_number_id:
        say("[!!] ELEVENLABS_PHONE_NUMBER_ID не задан — выполните setup-trunk")
        problems += 1
    else:
        try:
            number = api.get_phone_number(cfg.phone_number_id)
            say(f"[ок] Номер: {number.get('phone_number')} ({number.get('provider')})")
            outbound = number.get("outbound_trunk") or {}
            if not outbound:
                say("[--] Транк только входящий — команда call недоступна, используйте call-relay")
            else:
                say(
                    f"     транк: {outbound.get('address')} "
                    f"transport={outbound.get('transport')} "
                    f"auth={'да' if outbound.get('has_auth_credentials') else 'НЕТ (ACL по IP)'}"
                )
                if not outbound.get("has_auth_credentials"):
                    say("[!] Без digest-авторизации ElevenLabs полагается на ACL по IP,")
                    say("    а его SIP-адреса плавают. Почти наверняка АТС отклонит INVITE.")
                    problems += 1
            assigned = number.get("assigned_agent") or {}
            if not assigned:
                # Для call-pbx это блокирующая проблема: вызов приходит на транк
                # как входящий, и без привязки его некому обработать.
                say("[!!] К номеру не привязан агент — входящий вызов обрабатывать некому")
                say("     Исправить: python zvonilka.py assign-agent")
                problems += 1
            elif assigned.get("agent_id") != cfg.agent_id:
                say(f"[!] К номеру привязан другой агент: {assigned.get('agent_name')}")
        except ElevenLabsError as exc:
            say(f"[!!] Номер недоступен: {exc}")
            problems += 1

    # Сторона ВАТС нужна только для режима call-pbx, поэтому не считаем её ошибкой.
    if not cfg.pbx_api_key:
        say("[--] PBX_API_KEY не задан — режим call-pbx недоступен")
    else:
        try:
            with cfg.pbx_client() as pbx:
                users = pbx.users()
            say(f"[ок] CRM API ВАТС отвечает, сотрудников: {len(users)}")
            shorts = {
                str(u.get(key))
                for u in users
                for key in ("ext", "extension", "number", "shortnumber", "login")
                if u.get(key)
            }
            if cfg.pbx_extension and shorts and cfg.pbx_extension not in shorts:
                say(f"[!] Добавочного {cfg.pbx_extension} нет среди сотрудников ВАТС.")
                say("    Посмотрите список: python zvonilka.py pbx-users")
        except (PBXError, ValueError) as exc:
            say(f"[!!] ВАТС недоступна: {exc}")
            problems += 1

    say()
    say("Проблем не найдено." if not problems else f"Проблем: {problems}")
    return 0 if not problems else 1


def cmd_setup_trunk(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    cfg.require("sip_address", "sip_username", "sip_password", "caller_number")
    number = normalize_number(cfg.caller_number)

    say(f"Регистрирую SIP-транк {cfg.sip_address} (transport={cfg.sip_transport})")
    say(f"Номер, который увидит абонент: {number}")

    phone_number_id = api.create_sip_trunk_number(
        phone_number=number,
        label=args.label,
        address=cfg.sip_address,
        username=cfg.sip_username,
        password=cfg.sip_password,
        transport=cfg.sip_transport,
        media_encryption=cfg.sip_media_encryption,
        enabled_codecs=cfg.sip_codecs,
        agent_id=cfg.agent_id or None,
    )

    say()
    say(f"Готово. phone_number_id = {phone_number_id}")
    say("Впишите его в .env:")
    say(f"    ELEVENLABS_PHONE_NUMBER_ID={phone_number_id}")
    return 0


def cmd_numbers(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    numbers = api.list_phone_numbers()
    if not numbers:
        say("Номеров нет. Заведите транк: python zvonilka.py setup-trunk")
        return 0
    for item in numbers:
        agent = (item.get("assigned_agent") or {}).get("agent_name") or "—"
        say(
            f"{item.get('phone_number_id')}  {item.get('phone_number'):<16} "
            f"{item.get('provider'):<10} агент: {agent}"
        )
    return 0


def cmd_agents(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    agents = api.list_agents()
    if not agents:
        say("Агентов нет. Создайте агента в дашборде ElevenLabs.")
        return 0
    for item in agents:
        say(f"{item.get('agent_id')}  {item.get('name')}")
    return 0


def place_call(
    cfg: Config,
    api: ElevenLabsAgents,
    number: str,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Один звонок целиком: набор, ожидание, сохранение артефактов.

    Возвращает сводку. Ключ `error` присутствует, если звонок не состоялся.
    """
    started = datetime.now()
    say(f"Звоню на {number}...")

    try:
        response = api.outbound_call(
            agent_id=cfg.agent_id,
            agent_phone_number_id=cfg.phone_number_id,
            to_number=number,
            dynamic_variables=variables or None,
            ringing_timeout_secs=cfg.ringing_timeout_secs,
        )
    except ElevenLabsError as exc:
        say(f"  Отказ при инициации: {exc}")
        return {"phone_number": number, "status": "rejected", "error": str(exc)}

    conversation_id = response.get("conversation_id")
    say(f"  sip_call_id={response.get('sip_call_id')} conversation_id={conversation_id}")

    if not conversation_id:
        # success=true без conversation_id означает, что разговор не создался:
        # чаще всего это отказ SIP-авторизации или недоступность АТС.
        message = response.get("message") or "разговор не создан"
        say(f"  Разговор не начался: {message}")
        say("  Подсказка: python zvonilka.py sip-log — покажет сырой SIP-обмен.")
        return {"phone_number": number, "status": "not_started", "error": message}

    conversation = watch_call(api, conversation_id, cfg.call_timeout_secs)

    directory = call_dir(cfg, number, started)
    save_artifacts(api, conversation, directory)
    report_call(cfg, number, conversation)

    result = {"phone_number": number, "artifacts": str(directory), **summarize(conversation)}
    (directory / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    say(f"  Транскрипт: {directory / 'transcript.txt'}")
    return result


def conversation_number(api: ElevenLabsAgents, conversation_id: str) -> str:
    """Номер абонента, с которым шёл разговор. Пусто — если ещё не проставлен.

    Релей кладёт номер в INVITE тремя путями сразу, и здесь мы их перебираем:
    external_number появляется из заголовка X-Caller-ID, sip_candidate_phone —
    из X-Candidate-Phone. Дублирование намеренное: если ElevenLabs изменит
    обработку одного из них, сопоставление не развалится молча.
    """
    try:
        conversation = api.get_conversation(conversation_id)
    except ElevenLabsError:
        return ""

    phone_call = (conversation.get("metadata") or {}).get("phone_call") or {}
    candidates = [
        phone_call.get("external_number"),
        (phone_call.get("sip_header_dynamic_variables") or {}).get("sip_candidate_phone"),
    ]
    for value in candidates:
        digits = re.sub(r"\D", "", str(value or ""))
        # 710 — добавочный самого релея. Он означает, что номер не доехал.
        if len(digits) >= 10:
            return digits
    return ""


def find_new_conversation(
    api: ElevenLabsAgents, agent_id: str, since_unix: int,
    number: str = "", timeout_secs: int = 120
) -> str | None:
    """Ищет разговор, начавшийся после since_unix.

    ElevenLabs принимает наш вызов как входящий и conversation_id в ответ не
    возвращает — искать приходится самим. Когда номер известен, сверяем по нему:
    это единственное, что делает возможным параллельный обзвон. Сопоставление по
    одному лишь времени старта при двух звонках сразу перепутает разговоры между
    людьми, а вместе с ними — судимости и паспорта в карточках.

    Без номера остаётся прежнее поведение «первый разговор после T». Это
    honest fallback для одиночного звонка, но не для обзвона.
    """
    target = re.sub(r"\D", "", number or "")
    resolved: dict[str, str] = {}
    deadline = time.monotonic() + timeout_secs

    while time.monotonic() < deadline:
        try:
            page = api.list_conversations(agent_id=agent_id, page_size=30)
        except ElevenLabsError:
            page = {}

        for item in page.get("conversations") or []:
            if (item.get("start_time_unix_secs") or 0) < since_unix:
                continue
            conversation_id = item.get("conversation_id")
            if not conversation_id:
                continue
            if not target:
                return conversation_id
            if conversation_id in resolved:
                if resolved[conversation_id] == target:
                    return conversation_id
                continue
            # Номер проставляется не мгновенно. Пока его нет, разговор не
            # отбрасываем — проверим на следующем круге, иначе гонка между
            # нашим опросом и записью метаданных стоила бы потерянного звонка.
            found = conversation_number(api, conversation_id)
            if not found:
                continue
            resolved[conversation_id] = found
            if found == target:
                return conversation_id

        time.sleep(3)
    return None


def place_call_via_pbx(
    cfg: Config, api: ElevenLabsAgents, pbx: MegaPBX, number: str
) -> dict[str, Any]:
    """Звонок, инициированный АТС: makecall поднимает добавочный и соединяет с абонентом.

    Динамические переменные в этом режиме передать нельзя — ElevenLabs видит
    обычный входящий вызов, а makecall не умеет проставлять SIP-заголовки.
    """
    started = datetime.now()
    since_unix = int(started.timestamp()) - 5  # запас на расхождение часов

    dialed = format_for_pbx(number, cfg.pbx_dial_format)
    say(f"Звоню на {dialed} через ВАТС (добавочный {cfg.pbx_extension} поднимут первым)")
    try:
        response = pbx.make_call(
            phone=dialed, user=cfg.pbx_extension, clid=cfg.pbx_clid or None
        )
    except PBXError as exc:
        say(f"  АТС отклонила запрос: {exc}")
        return {"phone_number": number, "status": "rejected", "error": str(exc)}

    callid = response.get("callid")
    say(f"  callid={callid}")

    conversation_id = find_new_conversation(api, cfg.agent_id, since_unix)
    if not conversation_id:
        say("  Разговор в ElevenLabs не появился.")
        say("  Значит АТС не довела вызов до добавочного — проверьте маршрут на 710")
        say("  и что агент назначен номеру: python zvonilka.py assign-agent")
        result = {"phone_number": number, "status": "no_conversation", "callid": callid}
    else:
        say(f"  conversation_id={conversation_id}")
        conversation = watch_call(api, conversation_id, cfg.call_timeout_secs)
        directory = call_dir(cfg, number, started)
        save_artifacts(api, conversation, directory)
        report_call(cfg, number, conversation)
        result = {
            "phone_number": number,
            "callid": callid,
            "artifacts": str(directory),
            **summarize(conversation),
        }
        (directory / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        say(f"  Транскрипт: {directory / 'transcript.txt'}")

    # CDR АТС знает то, чего не знает ElevenLabs: взял ли абонент трубку вообще.
    if callid:
        record = pbx.wait_for_result(callid, timeout_secs=60)
        if record:
            say(f"  АТС: status={record.get('status')} duration={record.get('duration')}")
            result["pbx_status"] = record.get("status")
            result["pbx_duration"] = record.get("duration")
    return result


def originate_via_relay(cfg: Config, number: str) -> tuple[bool, str]:
    """Просит релей набрать абонента через ВАТС.

    Управляем по SSH, а не через AMI: канал уже аутентифицирован, и не нужно
    открывать на релее лишний порт. Для параллельного обзвона сюда логичнее
    подставить AMI, но на пилоте SSH проще и безопаснее.
    """
    target = format_for_pbx(number, cfg.pbx_dial_format)
    # Номер уходит расширением, а не глухим «start»: релей вставит его в
    # заголовок INVITE, и ElevenLabs вернёт его в метаданных разговора. Без
    # этого разговор опознаётся только по времени старта, и два звонка
    # одновременно означают перепутанные ответы, а не ускорение.
    remote = (
        f"docker exec {cfg.relay_container} asterisk -rx "
        f"'channel originate PJSIP/{target}@megapbx extension {target}@relay-outbound'"
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg.relay_ssh, remote],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "релей не ответил за 90 секунд"

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, output or f"ssh вернул код {result.returncode}"
    return True, output


def place_call_via_relay(
    cfg: Config, api: ElevenLabsAgents, number: str
) -> dict[str, Any]:
    """Звонок, инициированный релеем: агент подключается по факту ответа абонента.

    В отличие от makecall здесь нет служебного объявления ВАТС и нет догадок по
    аудио: контекст relay-outbound запускается только после SIP 200 OK от абонента.
    """
    started = datetime.now()
    since_unix = int(started.timestamp()) - 5

    say(f"Звоню на {number} через релей (агент подключится после ответа)")
    ok, output = originate_via_relay(cfg, number)
    if not ok:
        say(f"  Релей не смог набрать: {output}")
        return {"phone_number": number, "status": "originate_failed", "error": output}
    if output:
        say(f"  {output}")

    conversation_id = find_new_conversation(api, cfg.agent_id, since_unix, number)
    if not conversation_id:
        # Разговора нет — значит абонент не ответил либо канал не дошёл до агента.
        say("  Разговор не начался: абонент не взял трубку или сбросил.")
        return {"phone_number": number, "status": "no_answer"}

    say(f"  conversation_id={conversation_id}")
    conversation = watch_call(api, conversation_id, cfg.call_timeout_secs)

    directory = call_dir(cfg, number, started)
    save_artifacts(api, conversation, directory)
    report_call(cfg, number, conversation)
    result = {"phone_number": number, "artifacts": str(directory), **summarize(conversation)}
    (directory / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    say(f"  Транскрипт: {directory / 'transcript.txt'}")
    return result


def resolve_agent(api: ElevenLabsAgents, wanted: str) -> tuple[str, str]:
    """Находит агента по идентификатору или куску названия.

    По куску названия — потому что идентификаторы у ElevenLabs неразличимы на
    глаз, и перепутать в них два символа ровно тогда, когда звонишь живому
    человеку, слишком легко.
    """
    if wanted.startswith("agent_"):
        return wanted, str(api.get_agent(wanted).get("name") or wanted)

    needle = wanted.casefold()
    hits = [a for a in api.list_agents() if needle in str(a.get("name") or "").casefold()]
    if not hits:
        raise SystemExit(f"Агент «{wanted}» не найден. Список: python zvonilka.py agents")
    if len(hits) > 1:
        names = ", ".join(str(a.get("name") or "") for a in hits)
        raise SystemExit(f"Под «{wanted}» подходит несколько агентов: {names}")
    return str(hits[0].get("agent_id")), str(hits[0].get("name") or "")


@contextlib.contextmanager
def agent_on_number(cfg: Config, api: ElevenLabsAgents, agent_id: str):
    """На время разговора ставит на номер другого агента и возвращает прежнего.

    Агента ElevenLabs выбирает не звонок, а номер, на который звонок пришёл.
    Поэтому «позвонить другим агентом» — это переставить привязку у общего
    номера. Общего: пока она переставлена, тестовому агенту достанется и
    входящий звонок, и чужой обзвон, если тот идёт параллельно.

    Возврат стоит в finally не для красоты. Прогон, оборванный по Ctrl+C или
    таймауту, иначе оставил бы боевой обзвон говорить тестовым промптом, и
    заметили бы это по жалобам, а не по логу.
    """
    cfg.require("phone_number_id")
    number = api.get_phone_number(cfg.phone_number_id)
    before = str((number.get("assigned_agent") or {}).get("agent_id") or "")
    if agent_id == before:
        yield before
        return

    api.update_phone_number(cfg.phone_number_id, agent_id=agent_id)
    say(f"  на номере {number.get('phone_number')} временно агент {agent_id}")
    try:
        yield before
    finally:
        if before:
            api.update_phone_number(cfg.phone_number_id, agent_id=before)
            say(f"  агент на номере возвращён: {before}")
        else:
            say("  [!] Прежнего агента на номере не было — привязка осталась на тестовом.")


def cmd_call_relay(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    cfg.require("agent_id", "relay_ssh", "relay_container")
    agent_id = cfg.agent_id
    if getattr(args, "agent", ""):
        agent_id, name = resolve_agent(api, args.agent)
        say(f"Агент: {name} ({agent_id})")
    # Подменяем и в конфиге: по нему разговор потом ищется среди новых, и с
    # чужим идентификатором поиск не найдёт ничего.
    cfg.agent_id = agent_id

    with agent_on_number(cfg, api, agent_id):
        result = place_call_via_relay(cfg, api, normalize_number(args.number))
    say()
    say(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "done" else 1


# Описания уходят в LLM и определяют, позовёт ли агент инструмент вовремя и с
# какими аргументами. Поэтому это не документация для человека, а часть промпта:
# сказано, когда звать и в каком виде передавать значения.
#
# Слова «передай null» здесь быть не должно ни в одном описании. Инструмент
# ходит методом GET, и ElevenLabs не умеет собрать строку запроса, когда
# параметр равен null: вызов падает мгновенно с «Unable to execute function»,
# не дойдя до навигатора. На живом звонке это выглядит так, что агент говорит
# «у меня не грузится список вакансий» и прощается. Нужное поведение — не
# передавать параметр вовсе.
VACANCY_TOOL_PARAMS = {
    # Примеров значений здесь быть не должно. С формулировкой «...которую назвал
    # кандидат: «грузчик», «сварщик», «комплектовщик»» модель дважды за два
    # тестовых звонка подставила первый пример из списка, хотя кандидат никакой
    # профессии не называл, и звала ручку вхолостую.
    "profession": (
        "Профессия или направление в тех словах, которыми их назвал сам кандидат. "
        "Подставлять сюда значение, которого кандидат не произносил, запрещено — "
        "это не подсказка и не значение по умолчанию. Сказал «не знаю», «без "
        "разницы», «что есть» или не назвал вовсе — не вызывай инструмент, "
        "а задай вопрос."
    ),
    "city": (
        "Город, который назвал кандидат. Не назвал — пропусти параметр целиком, "
        "не подставляя ни пустую строку, ни null. Выдумывать город или "
        "подставлять правдоподобный запрещено."
    ),
    "region": (
        "Область или регион, если кандидат назвал его вместо города. Не назвал — "
        "пропусти параметр целиком, не подставляя ни пустую строку, ни null."
    ),
    "citizenship": (
        "Гражданство кандидата одним из кодов: РФ, РБ, КЗ, КРГ, УЗБ, ТДЖ, АРМ. "
        "Не сказал — пропусти параметр целиком, не подставляя ни пустую строку, "
        "ни null. Выдумывать гражданство запрещено."
    ),
    "age": (
        "Полных лет кандидату, число. Не назвал — пропусти параметр целиком, "
        "не подставляя ни ноль, ни null."
    ),
    "gender": (
        "Пол кандидата: «мужчина» или «женщина». Не называл и по имени не "
        "очевидно — пропусти параметр целиком, не подставляя null."
    ),
    "min_rate": (
        "Сколько кандидат хочет получать за смену в рублях, число. Не называл "
        "сумму — пропусти параметр целиком, не подставляя ни ноль, ни null."
    ),
}


def cmd_setup_tool(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Регистрирует инструмент подбора вакансий и привязывает его к агенту."""
    cfg.require("agent_id", "navigator_url", "navigator_token")

    if cfg.navigator_url.startswith("http://"):
        say("[!] Адрес навигатора без TLS — токен уйдёт открытым текстом.")
        if not args.allow_insecure:
            say("    Поднимите HTTPS либо запустите с --allow-insecure осознанно.")
            return 1

    say("Кладу токен в секреты ElevenLabs...")
    secret_id = api.create_secret(args.secret_name, cfg.navigator_token)
    say(f"  secret_id={secret_id}")

    tool_id = api.create_tool({
        "type": "webhook",
        "name": "search_vacancies",
        "description": (
            "Ищет вакансии в базе компании под конкретного кандидата. "
            "Вызывай ТОЛЬКО после того, как кандидат сам вслух назвал профессию "
            "или направление работы, и ты знаешь его регион. Если профессия не "
            "прозвучала — не вызывай, сначала спроси её. "
            "Возвращает до трёх вакансий с точными условиями. "
            "Называй кандидату только то, что пришло в ответе: "
            "если поле «неизвестно», так и скажи, не придумывай."
        ),
        "api_schema": {
            "url": cfg.navigator_url.rstrip("/") + "/api/agent/vacancies",
            "method": "GET",
            "request_headers": {"X-Agent-Token": {"secret_id": secret_id}},
            "query_params_schema": {
                "properties": {
                    name: {
                        "type": "integer" if name in ("age", "min_rate") else "string",
                        "description": description,
                    }
                    for name, description in VACANCY_TOOL_PARAMS.items()
                },
                "required": ["profession"],
            },
        },
        # Минимум по схеме — 5 секунд, по умолчанию 20. Двадцать секунд тишины
        # в телефонном разговоре — гарантированный сброс. Ручка отвечает за 60 мс.
        "response_timeout_secs": 8,
        # Агент скажет «секунду, посмотрю варианты» вместо паузы.
        "pre_tool_speech": "force",
        # По умолчанию для webhook-инструментов ошибка от агента СКРЫВАЕТСЯ, и он
        # продолжает говорить, будто ничего не спрашивал. Пусть лучше признается.
        "tool_error_handling_mode": "summarized",
    })
    say(f"  tool_id={tool_id}")

    api.attach_tools(cfg.agent_id, [tool_id])
    say(f"Инструмент привязан к агенту {cfg.agent_id}.")
    say()
    say("Проверить вызовы после звонка:")
    say(f"    python zvonilka.py tool-log {tool_id}")
    return 0


def cmd_tool_log(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Журнал вызовов инструмента: с чем агент его звал и что получил."""
    executions = api.get_tool_executions(args.tool_id)
    if not executions:
        say("Вызовов не было.")
        return 0
    for item in executions:
        say(json.dumps(item, ensure_ascii=False)[:600])
        say()
    return 0


def cmd_call_pbx(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    cfg.require("agent_id", "pbx_base_url", "pbx_api_key", "pbx_extension")
    number = normalize_number(args.number)
    with cfg.pbx_client() as pbx:
        result = place_call_via_pbx(cfg, api, pbx, number)
    say()
    say(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "done" else 1


def cmd_pbx_users(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    cfg.require("pbx_base_url", "pbx_api_key")
    with cfg.pbx_client() as pbx:
        users = pbx.users()
    if not users:
        say("Сотрудников не вернулось.")
        return 0
    for user in users:
        say(json.dumps(user, ensure_ascii=False))
    say()
    say(f"Всего: {len(users)}. Значение PBX_EXTENSION должно быть среди коротких номеров.")
    return 0


def cmd_assign_agent(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Привязывает агента к номеру — без этого входящий вызов некому обработать."""
    cfg.require("agent_id", "phone_number_id")
    number = api.get_phone_number(cfg.phone_number_id)
    assigned = (number.get("assigned_agent") or {}).get("agent_id")
    if assigned == cfg.agent_id:
        say(f"Агент уже привязан к {number.get('phone_number')}.")
        return 0
    say(f"Привязываю агента {cfg.agent_id} к номеру {number.get('phone_number')}")
    api.update_phone_number(cfg.phone_number_id, agent_id=cfg.agent_id)
    say("Готово.")
    return 0


def cmd_set_sip_password(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Задаёт пароль digest-авторизации входящего транка.

    Прочитать текущий пароль неоткуда: API отдаёт только has_auth_credentials
    и username. Поэтому если он неизвестен — задаём новый.

    Ответ GET нельзя слать обратно в PATCH как есть: там inbound_trunk, а PATCH
    ждёт inbound_trunk_config, и вместо has_auth_credentials/username нужен
    объект credentials. Поэтому переносим поля вручную.
    """
    cfg.require("phone_number_id")

    password = args.password or "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(24)
    )

    number = api.get_phone_number(cfg.phone_number_id)
    inbound = number.get("inbound_trunk") or {}
    username = args.username or inbound.get("username")
    if not username:
        raise SystemExit("У транка нет логина — задайте его через --username")

    say(f"Номер: {number.get('phone_number')} ({cfg.phone_number_id})")
    say(f"Логин: {username}")

    api.update_phone_number(
        cfg.phone_number_id,
        inbound_trunk_config={
            "credentials": {"username": username, "password": password},
            # Переносим то, что было, иначе PATCH затрёт вложенный объект целиком.
            "allowed_addresses": inbound.get("allowed_addresses"),
            "allowed_numbers": inbound.get("allowed_numbers"),
            "media_encryption": inbound.get("media_encryption"),
            "remote_domains": inbound.get("remote_domains"),
        },
    )

    say()
    say("Пароль обновлён. Впишите в relay/.env на VPS:")
    say(f"    ELEVENLABS_SIP_USERNAME={username}")
    say(f"    ELEVENLABS_SIP_PASSWORD={password}")
    say()
    say("Больше его нигде не посмотреть — сохраните сейчас.")
    return 0


def cmd_agent_config(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Выгружает конфигурацию агента и сверяет её с ожиданиями навигатора.

    Промпт и состав полей сбора правятся в дашборде ElevenLabs, а разбирает их
    навигатор. Две стороны расходятся молча: агент собирает поле, которого
    сервер не ждёт, — данные тихо оседают в сыром ответе и никто их не видит;
    сервер ждёт поле, которого у агента нет, — колонка месяцами стоит пустой.
    Ровно так потерялись судимость, статья и просьба перезвонить.

    Здесь эта сверка делается за одну команду, а заодно снимается копия
    конфигурации: восстановить агента после неудачной правки иначе неоткуда.
    """
    cfg.require("agent_id")
    agent = api.get_agent(cfg.agent_id)

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")

    prompt_block = (agent.get("conversation_config") or {}).get("agent", {}).get("prompt") or {}
    collection = (agent.get("platform_settings") or {}).get("data_collection") or {}
    tools = [t.get("name") for t in (prompt_block.get("tools") or []) if isinstance(t, dict)]

    say(f"Агент: {agent.get('name')} ({cfg.agent_id})")
    say(f"  модель: {prompt_block.get('llm')}, промпт {len(prompt_block.get('prompt') or '')} знаков")
    say(f"  инструменты: {', '.join(tools) or 'нет'}")
    # Потолок тарифа — 25 полей. Упереться в него посреди правки неприятно,
    # поэтому остаток мест показываем всегда.
    say(f"  полей сбора: {len(collection)} из 25")
    say(f"  копия: {destination}")

    if not cfg.navigator_url or not cfg.navigator_token:
        say()
        say("[!] Навигатор не настроен, сверку полей пропускаю.")
        return 0

    import httpx

    try:
        response = httpx.get(
            cfg.navigator_url.rstrip("/") + "/api/agent/expected_fields",
            headers={"X-Agent-Token": cfg.navigator_token},
            timeout=20,
        )
        response.raise_for_status()
        expected_raw = response.json()
    except Exception as exc:  # noqa: BLE001 — навигатор может лежать
        say()
        say(f"[!] Навигатор не ответил, сверку полей пропускаю: {type(exc).__name__}: {exc}")
        return 0

    expected = set(expected_raw.get("text") or []) | set(expected_raw.get("boolean") or [])
    # Поля, которые сервер разбирает отдельно, а не таблицей отображений. Список
    # приходит оттуда же: держать его копию здесь значило бы завести вторую
    # правду, которая разойдётся с первой при первой же правке навигатора.
    derived = set(expected_raw.get("parsed") or [])
    actual = set(collection)

    lost = sorted(actual - expected - derived)
    missing = sorted(expected - actual)

    say()
    if not lost and not missing:
        say("Сверка с навигатором: сходится, все поля разбираются.")
        return 0

    if lost:
        say("Агент собирает, а навигатор не разбирает (данные оседают в сыром ответе):")
        for name in lost:
            say(f"    {name}")
    if missing:
        say("Навигатор ждёт, а агент не собирает (колонки останутся пустыми):")
        for name in missing:
            say(f"    {name}")
    return 1


def cmd_push_prompt(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    """Заливает промпт из файла в агента, не трогая остальную конфигурацию.

    Читаем агента прямо перед записью и сохраняем весь блок prompt как есть,
    подменяя один текст. Иначе PATCH затирает то, чего в теле нет: так однажды
    уже отвязался инструмент подбора вакансий, потому что tool_ids не были
    отправлены обратно.

    Ключ tools из ответа выкидываем: ElevenLabs отдаёт и tools, и tool_ids, а
    принимает строго одно из двух.
    """
    cfg.require("agent_id")
    source = Path(args.prompt_file)
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    text = source.read_text(encoding="utf-8")

    current = api.get_agent(cfg.agent_id)
    block = {
        k: v
        for k, v in ((current.get("conversation_config") or {}).get("agent", {}).get("prompt") or {}).items()
        if k != "tools"
    }
    before = len(block.get("prompt") or "")
    block["prompt"] = text

    result = api.update_agent(cfg.agent_id, conversation_config={"agent": {"prompt": block}})
    updated = (result.get("conversation_config") or {}).get("agent", {}).get("prompt") or {}
    tools = [t.get("name") for t in (updated.get("tools") or []) if isinstance(t, dict)]

    say(f"Промпт залит: было {before} знаков, стало {len(updated.get('prompt') or '')}")
    say(f"  инструменты на месте: {', '.join(tools) or 'нет'}")
    return 0


def cmd_call(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    cfg.require("agent_id", "phone_number_id")
    number = normalize_number(args.number)

    variables = {}
    for pair in args.var or []:
        if "=" not in pair:
            raise SystemExit(f"Переменная должна быть вида имя=значение: {pair!r}")
        key, value = pair.split("=", 1)
        variables[key.strip()] = value.strip()

    result = place_call(cfg, api, number, variables)
    say()
    say(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "done" else 1


def cmd_call_list(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    if args.via_pbx:
        cfg.require("agent_id", "pbx_base_url", "pbx_api_key", "pbx_extension")
    else:
        cfg.require("agent_id", "phone_number_id")

    source = Path(args.csv_path)
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")

    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("В файле нет строк")
    if "phone_number" not in rows[0]:
        raise SystemExit("В CSV нужна колонка phone_number")

    mode = "через ВАТС" if args.via_pbx else "напрямую через ElevenLabs"
    say(f"Загружено записей: {len(rows)}. Режим: {mode}. Пауза: {args.pause} с.")
    if args.via_pbx:
        say("Внимание: в режиме ВАТС колонки-переменные в агента не передаются.")
    say()

    pbx = cfg.pbx_client() if args.via_pbx else None
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        raw_number = (row.get("phone_number") or "").strip()
        if not raw_number:
            continue
        try:
            number = normalize_number(raw_number)
        except ValueError as exc:
            say(f"[{index}/{len(rows)}] пропуск: {exc}")
            results.append({"phone_number": raw_number, "status": "invalid", "error": str(exc)})
            continue

        # Остальные колонки уезжают в промпт агента как {{переменные}}.
        # phone_number — зарезервированное имя, его не передаём.
        variables = {
            k: v for k, v in row.items() if k and k != "phone_number" and v and v.strip()
        }

        say(f"[{index}/{len(rows)}] {number}")
        result: dict[str, Any] = {}
        for attempt in range(1, args.retries + 1):
            if attempt > 1:
                say(f"  Попытка {attempt} из {args.retries} через {args.retry_delay} с.")
                time.sleep(args.retry_delay)
            if pbx is not None:
                result = place_call_via_pbx(cfg, api, pbx, number)
            else:
                result = place_call(cfg, api, number, variables)
            result["attempts"] = attempt
            if result.get("status") == "done":
                break

        results.append(result)
        say()
        if index < len(rows):
            time.sleep(args.pause)

    if pbx is not None:
        pbx.close()

    cfg.calls_dir.mkdir(parents=True, exist_ok=True)
    report = cfg.calls_dir / f"report_{datetime.now():%Y%m%d-%H%M%S}.csv"
    columns = [
        "phone_number", "status", "call_successful", "duration_secs",
        "turns", "attempts", "conversation_id", "callid", "pbx_status",
        "pbx_duration", "artifacts", "error",
    ]
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    done = sum(1 for r in results if r.get("status") == "done")
    say(f"Дозвонились: {done} из {len(results)}. Отчёт: {report}")
    return 0


def cmd_fetch(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    conversation = api.get_conversation(args.conversation_id)
    directory = call_dir(cfg, args.conversation_id, datetime.now())
    say(render_transcript(conversation))
    say()
    save_artifacts(api, conversation, directory)
    say(json.dumps(summarize(conversation), ensure_ascii=False, indent=2))

    # Разговор переживает процесс, который его начал: оборванный по таймауту
    # или Ctrl+C прогон оставляет готовый разговор в ElevenLabs и ничего не
    # отдаёт навигатору. Тогда fetch — единственный способ его подобрать, и
    # молча возвращать один текст на экран здесь мало.
    if getattr(args, "no_report", False):
        return 0

    status = str((conversation.get("status") or "")).lower()
    if status not in ("done", "completed", "processed"):
        say(f"  Разговор ещё не завершён (status={status or '?'}) — навигатору не отдаю:")
        say("  запись пометилась бы обработанной, и настоящий итог потом сочли бы повтором.")
        return 0

    number = normalize_number(args.phone) if args.phone else ""
    if not number:
        digits = conversation_number(api, args.conversation_id)
        number = normalize_number(digits) if digits else ""
    if not number:
        say("  Номер в разговоре не проставлен — навигатору отдавать не под кем.")
        say("  Укажите вручную: python zvonilka.py fetch <id> --phone +7XXXXXXXXXX")
        return 0

    say(f"  Отдаю навигатору как {number}")
    report_call(cfg, number, conversation)
    return 0


def cmd_sip_log(cfg: Config, api: ElevenLabsAgents, args: argparse.Namespace) -> int:
    if args.conversation_id:
        messages = api.get_conversation_sip_messages(args.conversation_id)
    else:
        cfg.require("phone_number_id")
        messages = api.get_phone_number_sip_messages(cfg.phone_number_id)

    if not messages:
        say("SIP-сообщений нет.")
        say("Если звонков не было — это норма. Если были, проверьте store_sip_messages у номера.")
        return 0

    for message in messages:
        stamp = datetime.fromtimestamp(message.get("created_at_unix_micro", 0) / 1_000_000)
        arrow = "<--" if message.get("direction") == "in" else "-->"
        say(f"{stamp:%H:%M:%S} {arrow} {message.get('remote_address')} ({message.get('transport')})")
        if message.get("error_message"):
            say(f"    ОШИБКА: {message['error_message']}")
        first_line = (message.get("raw_message") or "").splitlines()
        if first_line:
            say(f"    {first_line[0]}")
        say()
    return 0


# --- точка входа -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zvonilka", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="проверить конфигурацию и доступность транка")

    setup = sub.add_parser("setup-trunk", help="завести SIP-транк в ElevenLabs")
    setup.add_argument("--label", default="MegaPBX", help="название номера в дашборде")

    sub.add_parser("numbers", help="список телефонных номеров")
    sub.add_parser("agents", help="список агентов")

    call = sub.add_parser("call", help="позвонить на один номер")
    call.add_argument("number", help="номер в формате +7XXXXXXXXXX")
    call.add_argument(
        "--var", action="append", metavar="ИМЯ=ЗНАЧЕНИЕ",
        help="динамическая переменная для промпта агента, можно повторять",
    )

    call_relay = sub.add_parser(
        "call-relay", help="позвонить через релей (агент подключается после ответа)"
    )
    call_relay.add_argument("number", help="номер в формате +7XXXXXXXXXX")
    call_relay.add_argument(
        "--agent", default="",
        help="позвонить другим агентом: идентификатор или кусок названия ('ТЕСТ')",
    )

    call_pbx = sub.add_parser(
        "call-pbx", help="позвонить через ВАТС (обход блокировки зарубежных IP)"
    )
    call_pbx.add_argument("number", help="номер в формате +7XXXXXXXXXX")

    config_cmd = sub.add_parser(
        "agent-config", help="выгрузить конфигурацию агента и сверить поля с навигатором"
    )
    config_cmd.add_argument(
        "--out", default="prompts/agent_config_backup.json", help="куда сохранить копию"
    )

    push_cmd = sub.add_parser("push-prompt", help="залить промпт из файла в агента")
    push_cmd.add_argument("prompt_file", help="путь к файлу промпта")

    sub.add_parser("pbx-users", help="сотрудники и короткие номера ВАТС")

    setup_tool = sub.add_parser(
        "setup-tool", help="зарегистрировать инструмент подбора вакансий у агента"
    )
    setup_tool.add_argument("--secret-name", default="navigator_agent_token")
    setup_tool.add_argument(
        "--allow-insecure", action="store_true",
        help="разрешить http:// — токен уйдёт открытым текстом",
    )

    tool_log = sub.add_parser("tool-log", help="журнал вызовов инструмента")
    tool_log.add_argument("tool_id")

    sip_pw = sub.add_parser(
        "set-sip-password", help="задать пароль digest-авторизации входящего транка"
    )
    sip_pw.add_argument("--password", help="свой пароль; по умолчанию сгенерируется")
    sip_pw.add_argument("--username", help="логин, если нужно сменить и его")
    sub.add_parser("assign-agent", help="привязать агента к номеру (нужно для входящих)")

    batch = sub.add_parser("call-list", help="последовательный обзвон списка из CSV")
    batch.add_argument("csv_path", help="CSV с колонкой phone_number")
    batch.add_argument("--pause", type=float, default=5.0, help="пауза между номерами, с")
    batch.add_argument("--retries", type=int, default=1, help="попыток на номер")
    batch.add_argument("--retry-delay", type=float, default=60.0, help="пауза перед повтором, с")
    batch.add_argument(
        "--via-pbx", action="store_true", help="инициировать звонки через ВАТС, а не через ElevenLabs"
    )

    fetch = sub.add_parser("fetch", help="забрать транскрипт и запись по conversation_id")
    fetch.add_argument("conversation_id")
    fetch.add_argument("--phone", default="", help="чей это разговор, если номер в нём не проставлен")
    fetch.add_argument("--no-report", action="store_true", help="не отдавать итог навигатору")

    siplog = sub.add_parser("sip-log", help="сырые SIP-сообщения для отладки транка")
    siplog.add_argument("--conversation-id", help="по конкретному разговору")

    return parser


COMMANDS = {
    "check": cmd_check,
    "setup-trunk": cmd_setup_trunk,
    "numbers": cmd_numbers,
    "agents": cmd_agents,
    "agent-config": cmd_agent_config,
    "push-prompt": cmd_push_prompt,
    "call": cmd_call,
    "call-relay": cmd_call_relay,
    "call-pbx": cmd_call_pbx,
    "pbx-users": cmd_pbx_users,
    "setup-tool": cmd_setup_tool,
    "tool-log": cmd_tool_log,
    "assign-agent": cmd_assign_agent,
    "set-sip-password": cmd_set_sip_password,
    "call-list": cmd_call_list,
    "fetch": cmd_fetch,
    "sip-log": cmd_sip_log,
}


def main() -> int:
    # Консоль Windows по умолчанию не в UTF-8, а транскрипты у нас русские.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args()
    cfg = Config.load()
    cfg.require("api_key")

    try:
        with ElevenLabsAgents(cfg.api_key) as api:
            return COMMANDS[args.command](cfg, api, args)
    except KeyboardInterrupt:
        say("\nПрервано.")
        return 130
    except ElevenLabsError as exc:
        say(f"Ошибка ElevenLabs: {exc}")
        return 1
    except PBXError as exc:
        say(f"Ошибка ВАТС: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
