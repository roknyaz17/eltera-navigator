"""Проверка доставки лидов без обращения к порталу.

Живьём такую проверку не сделать: каждый прогон оставил бы в чужой воронке
лид, который менеджер пойдёт отрабатывать. Поэтому вызов портала подменён, а
проверяется то, ради чего писалось: что расшифровка не роняет уже созданный
лид и что «ушло, но не полностью» видно снаружи.

Запуск: python test_bitrix.py
"""

from __future__ import annotations

import sqlite3

import bitrix

CHECKS = []


def check(title: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((title, ok, detail))


def queue(*payloads: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE crm_leads (
        crm_id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, position_id TEXT,
        payload TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
        bitrix_id INTEGER, last_error TEXT DEFAULT '', created_at TEXT, updated_at TEXT)""")
    for number, payload in enumerate(payloads, start=1):
        conn.execute(
            """INSERT INTO crm_leads (phone, position_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, '2026-09-14', '2026-09-14')""",
            (f"+7999000{number:04d}", "ELT-1", payload),
        )
    return conn


def row(conn: sqlite3.Connection, crm_id: int = 1) -> sqlite3.Row:
    return conn.execute("SELECT * FROM crm_leads WHERE crm_id = ?", (crm_id,)).fetchone()


def test_dedup() -> None:
    """Проверки на дубли: кого поднимаем, кого не трогаем, кого пропускаем."""
    from datetime import datetime, timedelta

    today = datetime.now().strftime("%Y-%m-%d")
    long_ago = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    def portal(twins, status="8", modified=None):
        """Портал, у которого на телефон находится один лид с такой стадией."""
        log = []

        def call(method, params, timeout=20):
            log.append((method, params))
            if method == "crm.duplicate.findbycomm":
                return {"LEAD": twins}
            if method == "crm.lead.get":
                return {"ID": params["id"], "STATUS_ID": status,
                        "DATE_MODIFY": (modified or long_ago) + "T10:00:00+03:00",
                        "TITLE": "старый лид"}
            if method == "crm.lead.add":
                return 999
            if method == "crm.entity.mergeBatch":
                # Портал отвечает объектом со STATUS, а не «да/нет». Заглушка
                # возвращала True, и слияние падало на answer.get — из-за
                # этого весь файл обрывался до вывода отчёта, и ни одна
                # проверка ниже не считалась.
                return {"STATUS": "SUCCESS"}
            if method == "crm.timeline.comment.list":
                # Список, а не True: с этим ответом проверка на повтор
                # расшифровки идёт по настоящему пути, а не падает на типе.
                return []
            return True
        return call, log

    lead = {"имя": "Никита", "телефон": "+79990000000", "расшифровка": "Агент: Алло."}

    # --- человека в воронке нет: создаём -------------------------------
    bitrix._call, log = portal([])
    lead_id, note = bitrix.send(lead)
    check("новому кандидату заводим лид", lead_id == 999 and not note, f"{lead_id} {note!r}")
    check("создание идёт через lead.add",
          any(m == "crm.lead.add" for m, _ in log), f"{[m for m, _ in log]}")

    # --- человек есть, лид застоялся: поднимаем ------------------------
    bitrix._call, log = portal([51251], status="8")
    lead_id, note = bitrix.send(lead)
    check("существующий лид поднимаем, а не дублируем",
          lead_id == 51251 and all(m != "crm.lead.add" for m, _ in log),
          f"{lead_id}, вызовы {[m for m, _ in log]}")
    moved = [p for m, p in log if m == "crm.lead.update"]
    check("поднимаем в «Не обработан»",
          moved and moved[0]["fields"].get("STATUS_ID") == "NEW",
          f"{moved[0]['fields'].get('STATUS_ID') if moved else '—'}")

    # --- человек уже в сделке: стадию не трогаем -----------------------
    bitrix._call, log = portal([51251], status="CONVERTED")
    lead_id, note = bitrix.send(lead)
    fields = [p["fields"] for m, p in log if m == "crm.lead.update"]
    check("лид в сделке не откатываем",
          fields and "STATUS_ID" not in fields[0], f"{fields}")
    check("про сделку сказано в пометке", "сделке" in note, note)

    # --- в сделке, но человек согласился: достаём ------------------------
    # Защита стадии «в сделке» не должна стоить нам согласия. По выгрузке
    # воронки сделок там каждый кандидат, и все согласия уезжали в закрытую
    # стадию, где их никто не видел.
    bitrix._call, log = portal([51251], status="CONVERTED")
    lead_id, note = bitrix.send(dict(lead, итог="ready_to_start"))
    fields = [p["fields"] for m, p in log if m == "crm.lead.update"]
    check("согласие достаём из сделки в «Не обработано»",
          fields and fields[0].get("STATUS_ID") == "NEW", f"{fields}")
    check("подъём из сделки виден в пометке", "достали из сделки" in note, note)

    # --- в сделке и просто думает: по-прежнему не трогаем ---------------
    bitrix._call, log = portal([51251], status="CONVERTED")
    lead_id, note = bitrix.send(dict(lead, итог="thinking"))
    fields = [p["fields"] for m, p in log if m == "crm.lead.update"]
    check("«думает» стадию сделки не двигает",
          fields and "STATUS_ID" not in fields[0], f"{fields}")

    # --- лид трогали на этой неделе: стадию всё равно поднимаем ---------
    # Свежесть правки защищала стадию, и так терялись те самые звонки, ради
    # которых всё задумано: кандидат просил перезвонить, а карточка
    # оставалась в чьей-то персональной очереди.
    bitrix._call, log = portal([51251], status="8", modified=today)
    lead_id, note = bitrix.send(lead)
    fields = [p["fields"] for m, p in log if m == "crm.lead.update"]
    check("свежую правку стадия больше не защищает",
          fields and fields[0].get("STATUS_ID") == "NEW", f"{fields}")

    # --- чёрный список: не трогаем вовсе --------------------------------
    bitrix._call, log = portal([51251], status="12")
    try:
        bitrix.send(lead)
        check("чёрный список останавливает отправку", False, "исключения не было")
    except bitrix.Blocked as exc:
        check("чёрный список останавливает отправку", True)
        check("ничего не создали и не меняли",
              all(m in ("crm.duplicate.findbycomm", "crm.lead.get") for m, _ in log),
              f"{[m for m, _ in log]}")

    # --- два дубля, оба мёртвые: сливаем --------------------------------
    bitrix._call, log = portal([51251, 51999], status="8")
    lead_id, note = bitrix.send(lead)
    merged = [p for m, p in log if m == "crm.entity.mergeBatch"]
    check("дубли объединяем", bool(merged), f"{[m for m, _ in log]}")
    check("сливаем именно лиды",
          merged and merged[0]["params"].get("entityTypeId") == 1,
          f"{merged[0]['params'] if merged else '—'}")
    check("в объединение идут оба",
          merged and sorted(merged[0]["params"]["entityIds"]) == [51251, 51999],
          f"{merged[0]['params'].get('entityIds') if merged else '—'}")
    check("про объединение сказано в пометке", "слито" in note, note)

    # --- один из дублей свежий: не сливаем -------------------------------
    calls = []

    def mixed(method, params, timeout=20):
        calls.append((method, params))
        if method == "crm.duplicate.findbycomm":
            return {"LEAD": [51251, 51999]}
        if method == "crm.lead.get":
            # 51999 трогали сегодня — значит им кто-то занят.
            when = today if params["id"] == 51999 else long_ago
            return {"ID": params["id"], "STATUS_ID": "8",
                    "DATE_MODIFY": when + "T10:00:00+03:00", "TITLE": "лид"}
        if method == "crm.timeline.comment.list":
            return []
        return True

    bitrix._call = mixed
    lead_id, note = bitrix.send(lead)
    check("живой дубль останавливает объединение",
          all(m != "crm.entity.mergeBatch" for m, _ in calls), f"{[m for m, _ in calls]}")
    check("причина отказа от слияния названа", "не сливаю" in note, note)

    # --- портал не дал слить: лид всё равно доставлен --------------------
    def merge_fails(method, params, timeout=20):
        if method == "crm.duplicate.findbycomm":
            return {"LEAD": [51251, 51999]}
        if method == "crm.lead.get":
            return {"ID": params["id"], "STATUS_ID": "8",
                    "DATE_MODIFY": long_ago + "T10:00:00+03:00", "TITLE": "лид"}
        if method == "crm.entity.mergeBatch":
            raise RuntimeError("портал отказал")
        if method == "crm.timeline.comment.list":
            return []
        return True

    bitrix._call = merge_fails
    lead_id, note = bitrix.send(lead)
    check("неудачное объединение не роняет доставку",
          lead_id in (51251, 51999) and "объединить не вышло" in note, f"{lead_id} {note}")

    # --- очередь помечает такого пропущенным ----------------------------
    bitrix._call, _ = portal([51251], status="12")
    conn = queue('{"имя": "Никита", "телефон": "+79990000000"}')
    summary = bitrix.deliver_pending(conn)
    blocked = row(conn)
    check("в очереди он становится «skipped», а не «failed»",
          blocked["status"] == "skipped", f"{dict(blocked)}")
    check("сводка считает пропущенных", summary.get("пропущено") == 1, f"{summary}")


def main() -> int:
    calls = []

    def fake_call(method, params, timeout=20):
        calls.append((method, params))
        if method == "crm.lead.add":
            return 555
        if method == "crm.timeline.comment.add" and params["fields"]["COMMENT"].count("падай"):
            raise RuntimeError("комментарий не принят")
        return True

    bitrix._call = fake_call
    bitrix.os.environ["BITRIX_WEBHOOK"] = "https://example.invalid/rest/1/x"

    # --- обычная доставка ------------------------------------------------
    conn = queue('{"имя": "Никита", "расшифровка": "Агент: Алло.", "длительность": 78}')
    summary = bitrix.deliver_pending(conn)
    sent = row(conn)
    check("лид создан и помечен", sent["status"] == "sent" and sent["bitrix_id"] == 555,
          f"{dict(sent)}")
    check("расшифровка ушла в ленту",
          any(m == "crm.timeline.comment.add" for m, _ in calls), f"{[m for m, _ in calls]}")
    check("замечаний нет", sent["last_error"] == "", repr(sent["last_error"]))
    check("сводка считает отправленное", summary.get("отправлено") == 1, f"{summary}")

    # --- расшифровка не прошла -------------------------------------------
    calls.clear()
    conn = queue('{"имя": "Роман", "расшифровка": "падай"}')
    bitrix.deliver_pending(conn)
    broken = row(conn)
    check("лид всё равно создан", broken["status"] == "sent" and broken["bitrix_id"] == 555,
          f"{dict(broken)}")
    check("о непришедшей расшифровке видно снаружи",
          "расшифровка не прикрепилась" in (broken["last_error"] or ""),
          repr(broken["last_error"]))
    check("повторной попытки не будет", broken["attempts"] == 1, f"{broken['attempts']}")

    # --- разговора нет ----------------------------------------------------
    calls.clear()
    conn = queue('{"имя": "Без звонка"}')
    bitrix.deliver_pending(conn)
    check("пустую расшифровку в ленту не шлём",
          all(m != "crm.timeline.comment.add" for m, _ in calls), f"{[m for m, _ in calls]}")

    # --- портал отказал ---------------------------------------------------
    def refuses(method, params, timeout=20):
        raise RuntimeError("expired_token: срок действия истёк")

    bitrix._call = refuses
    conn = queue('{"имя": "Никита"}')
    bitrix.deliver_pending(conn)
    failed = row(conn)
    check("отказ портала оставляет лид в очереди", failed["status"] == "pending",
          f"{dict(failed)}")
    check("причина отказа записана", "expired_token" in (failed["last_error"] or ""),
          repr(failed["last_error"]))

    # --- стадию в работе не переписываем ---------------------------------
    seen = {}
    bitrix._call = lambda method, params, timeout=20: seen.update(params) or True
    bitrix.update_fields(555, {"имя": "Никита", "расшифровка": "Агент: Алло."})
    check("обновление карточки не трогает стадию и ответственного",
          not {"STATUS_ID", "SOURCE_ID", "OPENED"} & set(seen.get("fields", {})),
          f"{sorted(seen.get('fields', {}))}")

    test_dedup()

    bad = [(title, detail) for title, ok, detail in CHECKS if not ok]
    print(f"Проверок {len(CHECKS)}, прошло {len(CHECKS) - len(bad)}")
    for title, detail in bad:
        print(f"  ✗ {title}\n      {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
