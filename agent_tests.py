"""Проверки голосового агента без звонков живым людям.

Каждый дефект в этом наборе стоил реального разговора. Агент семь минут
переспрашивал чужого ИИ-секретаря; обещал кандидату, что с ним свяжется
рекрутёр, хотя рекрутёр — он сам; не предложил упаковщице упаковщика при
девятнадцати таких позициях в реестре. Ловить это звонками дорого и медленно:
дефект виден один раз на полсотни номеров, а платим мы за все пятьдесят.

ElevenLabs умеет прогонять агента по заданной истории диалога и судить ответ
моделью. Здесь собран набор таких проверок, а сам файл — источник правды:
тесты в панели создаются и обновляются из него, а не наоборот.

    python agent_tests.py sync     залить набор в ElevenLabs
    python agent_tests.py run      прогнать и показать результат
    python agent_tests.py clean    убрать из панели то, чего нет здесь
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from typing import Any

import httpx

API = "https://api.elevenlabs.io/v1/convai"

# Инструмент подбора вакансий. Его вызов — половина разговора: если агент его
# не позвал или позвал с пустой профессией, человек услышит «ничего нет» при
# полном реестре.
SEARCH_TOOL = {"id": "tool_0701m1xy3dq3frctsbzc538hbryj", "type": "webhook"}

# Встроенный детектор автоответчика. Отдельным инструментом, а не репликой:
# агент на автоответчик молчит и дёргает его, и поймать это можно только так.
VOICEMAIL_TOOL = {"id": "voicemail_detection", "type": "system"}

GREETING = ("Алло, здравствуйте! Меня зовут Жанна, группа компаний Эльтера. "
            "Вы оставляли отклик на работу вахтой. Скажите, пожалуйста, "
            "как могу к вам обращаться?")


def agent(message: str, at: int) -> dict:
    return {"role": "agent", "message": message, "time_in_call_secs": at}


def human(message: str, at: int) -> dict:
    return {"role": "user", "message": message, "time_in_call_secs": at}


# Проверки ответа: агенту дают историю разговора, он отвечает, ответ судит
# модель по условию. Условие пишем так, чтобы его нельзя было прочитать двояко:
# «не рассказывает вакансию» проверяемо, «ведёт себя разумно» — нет.
RESPONSE_TESTS: list[dict] = [
    {
        "name": "автоответчик представился — прощаемся",
        "chat_history": [
            agent(GREETING, 0),
            human("Здравствуйте, я МТС Секретарь, искусственный интеллект. "
                  "Помогаю абоненту отвечать на звонки, когда он не может.", 12),
        ],
        "success_condition":
            "Агент прощается и завершает разговор. Он НЕ задаёт следующий вопрос "
            "анкеты, НЕ выясняет имя и НЕ рассказывает про вакансию.",
        "success_examples": ["Поняла вас, всего доброго!",
                             "Поняла, спасибо. Всего доброго, до свидания."],
        "failure_examples": ["Так, а сколько вам полных лет?",
                             "Скажите, пожалуйста, как могу к вам обращаться?"],
    },
    {
        "name": "грубость вместо ответа не сбивает",
        "chat_history": [
            agent("Так, а какой у вас опыт работы?", 120),
            human("Ты меня заебала уже.", 127),
        ],
        "success_condition":
            "Агент отвечает спокойно и задаёт следующий вопрос — разговор "
            "продолжается. Он НЕ прощается, НЕ завершает звонок, НЕ повторяет "
            "грубость и НЕ извиняется многословно.",
        "success_examples": [
            "Понимаю. Скажите, вы раньше работали комплектовщиком, на складах?",
            "Поняла. Так, а на складах работать доводилось?"],
        "failure_examples": [
            "Простите, если я вас чем-то обидела, мне очень жаль.",
            "Понимаю. Извините за беспокойство, всего доброго."],
    },
    {
        "name": "не задаём один вопрос третий раз",
        "chat_history": [
            agent(GREETING, 0),
            human("А что за работа?", 10),
            agent("Так, а сколько вам полных лет?", 14),
            human("Я ещё не освоила курс «Ответы на каверзные вопросы».", 20),
            agent("Так, а сколько вам полных лет?", 26),
            human("На такие сложные вопросы мой ИИ бессилен ответить.", 32),
        ],
        "success_condition":
            "Агент НЕ спрашивает про возраст третий раз. Он либо переходит к "
            "другому вопросу анкеты, либо прощается и завершает разговор.",
        "failure_examples": ["Так, а сколько вам полных лет?",
                             "Повторите, пожалуйста, сколько вам лет?"],
    },
    {
        "name": "в закрытии не обещаем рекрутёра",
        "chat_history": [
            agent("Тогда с вас первый разворот паспорта, прописка и номер СНИЛС. "
                  "Договорились?", 60),
            human("Да, давайте.", 70),
            agent("Давайте я скину вам вакансию и буду ждать от вас документы. "
                  "Вам куда удобнее, в Телеграм или в Ватсап?", 74),
            human("В Ватсап, на этот номер.", 82),
        ],
        "success_condition":
            "Агент говорит, что пришлёт вакансию, и ждёт документы У СЕБЯ, в том же "
            "чате. Он НЕ обещает, что с кандидатом свяжется рекрутёр, менеджер, "
            "коллега или кто-то ещё.",
        "success_examples": [
            "Всё, скину вам в Ватсап. Посмотрите условия, там же написано, какие "
            "документы нужны. Присылайте их мне туда, я на связи."],
        "failure_examples": [
            "Скину вам в Ватсап, а по документам с вами свяжется рекрутёр.",
            "Отправлю вакансию, дальше с вами свяжется наш менеджер."],
    },
    {
        "name": "не выдумываем суммы сверх ставки",
        "chat_history": [
            agent("Смотрите, ближайший к вам объект — город Чехов. Комплектовщики, "
                  "ставка три тысячи восемьсот пятьдесят за смену на руки. "
                  "Вахта от пятнадцати смен.", 40),
            human("А сколько в месяц выходит? Премии какие-нибудь есть?", 52),
        ],
        "success_condition":
            "Агент НЕ называет месячный доход и НЕ обещает премий, надбавок или "
            "выплат, которых не было в условиях. Он либо повторяет известную ставку "
            "за смену, либо говорит, что уточнит.",
        "failure_examples": ["В месяц выходит около ста тысяч.",
                             "Да, есть премия за переработку."],
    },
    {
        "name": "низкая ставка — ищем другое, а не прощаемся",
        "chat_history": [
            agent("Есть вариант в Электростали. Разнорабочий на склад шин, "
                  "три тысячи за смену.", 45),
            human("Три тысячи мало. Мне нужно хотя бы от четырёх.", 55),
        ],
        "success_condition":
            "Агент НЕ завершает разговор. Он ищет другой вариант, или уточняет, "
            "что подобрать по этой ставке, или прямо говорит, что посмотрит "
            "варианты дороже.",
        "failure_examples": ["Поняла. Подходящих вариантов нет, всего доброго.",
                             "К сожалению, больше ничего нет. До свидания."],
    },
    {
        "name": "про судимость спрашиваем со связкой",
        "chat_history": [
            agent("А какой у вас опыт работы?", 100),
            human("На складах работал, комплектовщиком.", 108),
        ],
        "success_condition":
            "Если агент заговаривает о судимости, он сначала подтверждает сказанное "
            "кандидатом и переводит разговор на документы, и только потом задаёт "
            "вопрос. Вопрос про судимость НЕ идёт встык после разговора об опыте.",
        "failure_examples": ["Поняла. У вас нет судимости?"],
    },
]

# Проверки вызова инструмента. Здесь ловится самое дорогое: агент дошёл до
# человека, но подбор не позвал или позвал впустую.
TOOL_TESTS: list[dict] = [
    {
        # Непредставившийся секретарь оператора: наш агент на него не отвечает
        # словами вовсе, а дёргает встроенный детектор автоответчика. Это
        # правильнее, чем прощаться вслух, но проверкой ответа такое не поймать
        # — судить там нечего, реплики нет.
        "name": "автоответчик не представился — узнаём его",
        "tool": VOICEMAIL_TOOL,
        "chat_history": [
            agent(GREETING, 0),
            human("Какой номер телефона для обратного звонка могу передать абоненту?", 11),
        ],
        "parameters": [],
    },
    {
        "name": "профессия и город уходят в подбор",
        "chat_history": [
            agent("Так, а вы из какого региона?", 20),
            human("Воронеж.", 25),
            agent("Воронежская область. А кем вы хотите работать?", 28),
            human("Упаковщицей.", 34),
        ],
        # Проверяем только сам факт вызова. Разбор параметров тут неприменим:
        # ни одно написание пути — profession, query_params.profession,
        # params_as_json.profession — до значений не достаёт, а агент к тому же
        # кладёт город то в city, то в region, и проверка была бы плавающей.
        "parameters": [],
    },
    {
        "name": "готов в другой регион — ищем без города",
        "chat_history": [
            agent("В вашем городе сейчас ничего нет. Но вы готовы приехать на вахту "
                  "в другой регион?", 60),
            human("Да, готов.", 68),
        ],
        "parameters": [],
    },
    {
        "name": "без профессии подбор не зовём",
        "chat_history": [
            agent(GREETING, 0),
            human("Роман.", 10),
        ],
        "verify_absence": True,
        "parameters": [],
    },
]


class Settings:
    """Ключ и агент из окружения.

    Свой, а не из zvonilka, чтобы набор можно было прогнать откуда угодно —
    в том числе с сервера в Амстердаме. Из России ElevenLabs отвечает
    редиректом на статью о страновых ограничениях, и тогда проверки не
    запускаются вовсе; сервер в это не упирается.
    """

    def __init__(self) -> None:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        import os
        self.api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        self.agent_id = os.getenv("ELEVENLABS_AGENT_ID", "").strip()

    def require(self) -> None:
        missing = [name for name, value in (("ELEVENLABS_API_KEY", self.api_key),
                                            ("ELEVENLABS_AGENT_ID", self.agent_id))
                   if not value]
        if missing:
            raise SystemExit("Не задано: " + ", ".join(missing))


def client(cfg: Settings) -> httpx.Client:
    # follow_redirects обязателен: список тестов иногда отвечает 302, и без
    # него разбор падает на HTML-странице редиректа вместо JSON.
    return httpx.Client(base_url=API, headers={"xi-api-key": cfg.api_key},
                        timeout=60, follow_redirects=True)


def examples(items: list[str], kind: str) -> list[dict]:
    """Примеры ответов для судьи. API ждёт объекты, а не строки.

    Примеры важнее условия: словами «не обещает рекрутёра» можно описать что
    угодно, а строка «с вами свяжется рекрутёр» в списке провалов не оставляет
    судье простора для толкования.
    """
    return [{"response": text, "type": kind} for text in items]


def body_of(test: dict, kind: str) -> dict:
    """Определение теста в том виде, в каком его ждёт API."""
    common = {"name": test["name"], "chat_history": test["chat_history"]}
    if kind == "llm":
        return {**common, "type": "llm",
                "success_condition": test["success_condition"],
                "success_examples": examples(test.get("success_examples", []), "success"),
                "failure_examples": examples(test.get("failure_examples", []), "failure")}
    # check_any_tool_matches обязателен: агент сначала произносит «секунду,
    # посмотрю варианты» и только следующим ходом зовёт инструмент. Без этого
    # флага проверка смотрит на первый ответ, где вызова ещё нет, и валится на
    # исправном агенте.
    return {**common, "type": "tool",
            "check_any_tool_matches": True,
            "tool_call_parameters": {
                "referenced_tool": test.get("tool", SEARCH_TOOL),
                "parameters": test.get("parameters", []),
                "verify_absence": test.get("verify_absence", False)}}


def existing(api: httpx.Client) -> dict[str, str]:
    """Что уже заведено в панели: имя → идентификатор."""
    found: dict[str, str] = {}
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = api.get("/agent-testing", params=params).json()
        for item in page.get("tests", []):
            found[item["name"]] = item["id"]
        if not page.get("has_more"):
            return found
        cursor = page.get("next_cursor")


def suite() -> list[tuple[dict, str]]:
    return ([(t, "llm") for t in RESPONSE_TESTS]
            + [(t, "tool") for t in TOOL_TESTS])


def cmd_sync(cfg: Settings) -> int:
    with client(cfg) as api:
        known = existing(api)
        for test, kind in suite():
            payload = body_of(test, kind)
            name = test["name"]
            if name in known:
                response = api.put(f"/agent-testing/{known[name]}", json=payload)
                verb = "обновлён"
            else:
                response = api.post("/agent-testing/create", json=payload)
                verb = "создан"
            if response.status_code >= 400:
                print(f"  [!] {name}: {response.status_code} {response.text[:200]}")
                continue
            print(f"  {verb}: {name}")
    print(f"\nВ наборе {len(suite())} проверок.")
    return 0


def cmd_clean(cfg: Settings) -> int:
    ours = {test["name"] for test, _ in suite()}
    with client(cfg) as api:
        for name, test_id in existing(api).items():
            if name in ours:
                continue
            api.delete(f"/agent-testing/{test_id}")
            print(f"  удалён лишний: {name}")
    return 0


def cmd_run(cfg: Settings, timeout_secs: int = 600) -> int:
    with client(cfg) as api:
        known = existing(api)
        wanted = [{"test_id": known[t["name"]]} for t, _ in suite() if t["name"] in known]
        if not wanted:
            print("Тестов в панели нет. Сначала: python agent_tests.py sync")
            return 2

        print(f"Прогоняю {len(wanted)} проверок на агенте {cfg.agent_id}")
        started = api.post(f"/agents/{cfg.agent_id}/run-tests",
                           json={"tests": wanted}).json()
        suite_id = started["id"]

        # Прогон идёт асинхронно: ответ приходит сразу, со статусом pending.
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            time.sleep(5)
            state = api.get(f"/test-invocations/{suite_id}").json()
            runs = state.get("test_runs") or []
            if runs and all(r.get("status") != "pending" for r in runs):
                return report(runs)
            done = sum(1 for r in runs if r.get("status") != "pending")
            print(f"  готово {done} из {len(runs)}...", end="\r")

        print("\nНе дождались результата.")
        return 1


def _why(rationale) -> list[str]:
    """Причина провала. Для ответов это строка, для инструментов — разбор по
    параметрам, и вытаскивать её надо по-разному."""
    if not rationale:
        return []
    if isinstance(rationale, str):
        return [rationale.strip()]
    lines = [str(m).strip() for m in (rationale.get("messages") or [])]
    if rationale.get("summary"):
        lines.append(str(rationale["summary"]).strip())
    return [line for line in lines if line]


def report(runs: list[dict]) -> int:
    passed = []
    failed = []
    for run in runs:
        result = run.get("condition_result") or {}
        ok = str(result.get("result") or "").lower() == "success"
        (passed if ok else failed).append((run, result))

    print(f"\nПроверок {len(runs)}, прошло {len(passed)}\n")
    for run, result in failed:
        print(f"  ✗ {run.get('test_name')}")
        for line in _why(result.get("rationale")):
            print(f"      {line[:280]}")
        # Показываем, что агент на самом деле сказал: без этого отчёт сообщает
        # о провале, но не даёт его починить.
        for response in (run.get("agent_responses") or []):
            said = (response.get("message") or "").strip()
            calls = [c.get("tool_name") for c in (response.get("tool_calls") or [])]
            if said:
                print(f"      агент: {said[:200]}")
            if calls:
                print(f"      вызвал: {', '.join(calls)}")
    for run, _ in passed:
        print(f"  ✓ {run.get('test_name')}")
    return 1 if failed else 0


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Проверки голосового агента")
    parser.add_argument("command", choices=("sync", "run", "clean"))
    args = parser.parse_args()

    cfg = Settings()
    cfg.require()
    return {"sync": cmd_sync, "run": cmd_run, "clean": cmd_clean}[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())
