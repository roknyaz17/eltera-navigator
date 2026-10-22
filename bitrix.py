"""Отправка согласившихся кандидатов в Битрикс.

В воронку попадает не всякий, с кем поговорили, а тот, кто сказал «да»: итог
звонка next_stage или ready_to_start. На живом обзвоне это один разговор из
шестнадцати — воронка узкая, но в ней нет мусора, который менеджеру надо
разгребать. В чатах Eltera эта боль звучит прямо: «было 20 лидов, обработано
всего 6, 14 лежат без дела».

Доставка отложенная, через таблицу и повторные попытки. Постобработка звонка
не должна ни ждать Битрикс, ни падать из-за него: карточка кандидата важнее,
а недоехавший лид можно дослать. Обратный порядок означал бы, что недоступный
на минуту портал стоит нам потерянного звонка.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import httpx
from loguru import logger

# Кого считаем согласившимся. Только эти два исхода: в них человек прямо
# сказал, что готов оформляться. «Думает» и «интересно» сюда не входят —
# менеджеру потом объяснять, почему в воронке тот, кто ничего не обещал.
AGREED = ("next_stage", "ready_to_start")

# Сколько раз пробуем, прежде чем оставить лид человеку. Портал падает редко,
# но когда падает — на часы, и пять попыток с нарастающей паузой это покрывают.
MAX_ATTEMPTS = 5

# Стадия, в которую возвращаем ожившего кандидата: «Не обработан». Она кладёт
# лид обратно в общий пул, откуда его берут, — а именно этого мы и добиваемся,
# поднимая то, что месяцами лежало в чьей-то персональной очереди.
REVIVE_STATUS = "NEW"

# Стадии, куда нельзя лезть роботом.
#
# CONVERTED — человек уже дошёл до сделки. Вернуть его в «Не обработан» значит
# стереть чужую работу; из шестидесяти пяти наших первых лидов девять оказались
# именно такими.
#
# 12 — чёрный список. Туда попадают по причине, и робот эту причину не знает.
# Такого кандидата мы не заводим и не трогаем вовсе.
KEEP_AS_IS = {"CONVERTED"}
BLACKLIST = {"12"}

# Исходы, которых защита стадии «в сделке» не держит.
#
# Задумано было так: человек в сделке уже за пределами лидовой воронки, и
# возвращать его в «Не обработано» нечем. Для разговора, в котором он ни на
# что не согласился, это верно и сейчас.
#
# Сломалось это 19.10.2026. По выгрузке самой воронки сделок прошло двести
# звонков — а там каждый по определению «в сделке». Все три согласия легли в
# закрытую стадию со старым ответственным: карточка обновилась, расшифровка
# приехала, а в «Не обработано» не появилось ничего. Менеджер о согласии
# узнал бы только из чата.
#
# Поэтому согласие и просьбу перезвонить защита больше не держит. Сделку при
# этом не трогаем — это отдельная сущность, и стадию в ней робот не меняет;
# в логе видно, из какой стадии лид поднят, так что вернуть можно руками.
BREAK_THROUGH = AGREED + ("callback",)

# Лид, которого касались на этой неделе, считаем живым — но только там, где
# ошибка необратима, то есть при слиянии дублей. Слить чью-то свежую работу со
# старым отказом нельзя отменить кнопкой, а вот стадию менеджер поправит за
# секунду, поэтому подъём в «Не обработано» этой защитой больше не связан.
FRESH_DAYS = 7


class Blocked(RuntimeError):
    """Кандидат в чёрном списке: ни создавать, ни трогать нельзя."""


def webhook() -> str:
    """Адрес входящего вебхука портала. Пусто — интеграция выключена."""
    return os.getenv("BITRIX_WEBHOOK", "").strip().rstrip("/")


def enabled() -> bool:
    return bool(webhook())


def _title(lead: dict) -> str:
    who = lead.get("имя") or lead.get("телефон") or "кандидат"
    what = lead.get("вакансия") or "вахта"
    return f"{what} — {who}"


def _comments(lead: dict) -> str:
    """Подробности в карточку лида.

    Кладём то, что менеджер иначе будет выяснять повторным звонком: куда
    подобрали, на каких условиях и что человек сказал про сроки.
    """
    rows = [
        # Срок — первой строкой. Всё остальное можно прочитать и завтра.
        ("ПЕРЕЗВОНИТЬ", lead.get("перезвонить")),
        ("Откликался на", lead.get("отклик")),
        ("Вакансия", lead.get("вакансия")),
        ("Код вакансии", lead.get("код")),
        ("Объект", lead.get("объект")),
        ("Город", lead.get("город")),
        ("Ставка за смену", lead.get("ставка")),
        ("Гражданство", lead.get("гражданство")),
        ("Возраст", lead.get("возраст")),
        ("Откуда", lead.get("откуда")),
        # Ключ читаем оба: карточку лида собирают два разных места, и назвать
        # одно и то же поле по-разному они уже успели. Ловится это молча —
        # строка просто исчезает из карточки.
        ("Специальность", lead.get("профессия") or lead.get("специальность")),
        # Только утвердительная готовность. Агент проставляет эти поля и когда
        # не спрашивал — тогда выходит «нет», неотличимое от настоящего отказа.
        ("Готов", lead.get("готов")),
        ("Ждёт по деньгам", lead.get("хочет получать")),
        ("График", lead.get("график")),
        ("Готов выйти", lead.get("готов выйти") or lead.get("готов_выехать")),
        ("Медкнижка", lead.get("медкнижка")),
        ("Судимость", lead.get("судимость")),
        ("Канал связи", lead.get("канал")),
        ("Возражения", lead.get("возражения")),
        ("Спрашивал", lead.get("вопросы")),
        ("Договорились", lead.get("что дальше")),
        ("Звонков сделано", lead.get("звонков")),
    ]
    lines = [f"{name}: {value}" for name, value in rows if value not in (None, "", "неизвестно")]
    if lead.get("пересказ"):
        lines += ["", str(lead["пересказ"])]
    return "\n".join(lines)


def build_fields(lead: dict) -> dict:
    """Лид в терминах Битрикса.

    Только стандартные поля: пользовательские UF_* у каждого портала свои, и
    угадывать их — значит получить отказ на первом же лиде. Всё, чему нет
    стандартного места, уходит в комментарий.
    """
    phone = (lead.get("телефон") or "").strip()
    fields = {
        "TITLE": _title(lead),
        "NAME": lead.get("имя") or "",
        "OPENED": "Y",
        "COMMENTS": _comments(lead),
        # Стадия и источник вынесены в настройку: это справочники портала, и
        # менять их там будут без нас. Значения по умолчанию взяты из живого
        # справочника Eltera, а не выдуманы: NEW — «Не обработан»,
        # UC_YBANXJ — «Робообзвон».
        "STATUS_ID": os.getenv("BITRIX_LEAD_STATUS", "NEW").strip() or "NEW",
        "SOURCE_ID": os.getenv("BITRIX_LEAD_SOURCE", "UC_YBANXJ").strip() or "UC_YBANXJ",
        "SOURCE_DESCRIPTION": "Обзвон Eltera, голосовой агент",
    }
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}]
    return fields


# Предел на один комментарий в ленте. Пятиминутный разговор — это около
# четырёх тысяч знаков, так что запас тут не для нормы, а для аварии:
# зациклившийся агент однажды наговорил на мегабайт.
MAX_TRANSCRIPT = 20_000


def _duration(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    minutes, rest = divmod(total, 60)
    return f"{minutes} мин {rest} с" if minutes else f"{rest} с"


def transcript_comment(lead: dict) -> str:
    """Расшифровка с шапкой: когда говорили и сколько.

    Без шапки комментарий читается как переписка неизвестной давности, а для
    менеджера возраст разговора — половина смысла: обещание «выйду через
    неделю», данное три недели назад, значит совсем другое.
    """
    text = (lead.get("расшифровка") or "").strip()
    if not text:
        return ""
    if len(text) > MAX_TRANSCRIPT:
        text = text[:MAX_TRANSCRIPT] + "\n\n[…расшифровка обрезана]"

    head = " · ".join(part for part in (
        "Разговор с голосовым агентом",
        lead.get("когда") or "",
        _duration(lead.get("длительность")),
    ) if part)
    return f"{head}\n\n{text}"


def _call(method: str, params: dict, timeout: int = 20):
    """Один вызов портала с разбором его манеры отвечать 200 на отказ."""
    url = webhook()
    if not url:
        raise RuntimeError("BITRIX_WEBHOOK не задан")

    response = httpx.post(f"{url}/{method}.json", json=params, timeout=timeout)
    # Битрикс отвечает 200 и на отказ, с полем error в теле. Проверять только
    # код ответа значит считать успехом «неверный токен».
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"портал ответил не JSON: HTTP {response.status_code}")

    if body.get("error"):
        raise RuntimeError(f"{body['error']}: {body.get('error_description', '')}")
    return body.get("result")


def attach_transcript(lead_id: int, lead: dict, timeout: int = 20,
                      check_existing: bool = False) -> None:
    """Кладёт расшифровку в ленту лида отдельным комментарием.

    Отдельным, а не в COMMENTS: карточку менеджер пробегает глазами перед
    звонком, и разговор на четыре тысячи знаков хоронит в ней ставку, город и
    возражения. В ленте же расшифровка ровно там, где её ищут, — в истории
    общения с человеком.
    """
    comment = transcript_comment(lead)
    if not comment:
        return

    # При создании лида лента пуста по определению, и лишний запрос там не
    # нужен. А вот досыл к уже существующему лиду без этой проверки кладёт
    # вторую копию того же разговора — менеджер листает ленту и читает одно и
    # то же дважды, гадая, было ли два звонка.
    if check_existing:
        existing = _call("crm.timeline.comment.list",
                         {"filter": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead"}},
                         timeout) or []
        if any((item.get("COMMENT") or "").strip() == comment.strip() for item in existing):
            logger.info(f"[bitrix] лид {lead_id}: расшифровка уже в ленте, не дублирую")
            return

    _call("crm.timeline.comment.add",
          {"fields": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead", "COMMENT": comment}},
          timeout)


def find_twins(phone: str, timeout: int = 20) -> list[int]:
    """Лиды портала с этим же телефоном.

    Почти каждый, кому мы дозвонились, в воронке уже есть: мы и звоним по
    выгрузкам из неё. Без этой проверки на человека заводится второй лид —
    из первых шестидесяти пяти отправленных дублями оказались шестьдесят
    четыре.
    """
    if not phone:
        return []
    found = _call("crm.duplicate.findbycomm",
                  {"type": "PHONE", "values": [phone], "entity_type": "LEAD"},
                  timeout) or {}
    return list(found.get("LEAD") or [])


def _brief(lead_id: int, timeout: int = 20) -> dict:
    lead = _call("crm.lead.get", {"id": lead_id}, timeout) or {}
    return {"id": int(lead.get("ID") or lead_id),
            "status": str(lead.get("STATUS_ID") or ""),
            "modified": str(lead.get("DATE_MODIFY") or "")[:10],
            "title": str(lead.get("TITLE") or "")}


def _is_fresh(modified: str, days: int = FRESH_DAYS) -> bool:
    from datetime import datetime, timedelta
    try:
        when = datetime.strptime(modified, "%Y-%m-%d")
    except ValueError:
        return False
    return when >= datetime.now() - timedelta(days=days)


def all_twins(phone: str, timeout: int = 20) -> list[dict]:
    """Все лиды этого человека. Чёрный список останавливает разговор целиком."""
    twins = [_brief(lead_id, timeout) for lead_id in find_twins(phone, timeout)]
    for twin in twins:
        if twin["status"] in BLACKLIST:
            raise Blocked(f"кандидат в чёрном списке, лид {twin['id']}")
    return twins


# Тип сущности «лид» в терминах портала. Нужен объединению дублей.
ENTITY_LEAD = 1


def merge_twins(twins: list[dict], timeout: int = 20,
                keep: Optional[int] = None) -> tuple[dict, str]:
    """Сливает дубли одного человека в один лид. Возвращает выжившего и пометку.

    Объединение необратимо, поэтому трогаем только то, что заведомо мертво:
    ни одного лида в сделке, ни одного, к которому прикасались на этой неделе.
    Слить чью-то живую работу со старым отказом — потеря, которую не отменить
    кнопкой.

    Если хоть один дубль под защитой, не сливаем вовсе: разбирать такие случаи
    должен человек, у него есть контекст, которого у нас нет.
    """
    # По умолчанию выживает самый свежий: в нём последняя работа менеджера.
    # Явный keep нужен разовой сверке — там важнее сохранить стадию отказа,
    # а не свежесть: слить отказ со свежим откликом значит вернуть человека
    # в обзвон, хотя он уже сказал «нет».
    alive = next((t for t in twins if t["id"] == keep), None) if keep else None
    alive = alive or max(twins, key=lambda t: t["modified"])
    if len(twins) < 2:
        return alive, ""

    risky = [t for t in twins if t["status"] in KEEP_AS_IS or _is_fresh(t["modified"])]
    if risky:
        return alive, (f"дублей {len(twins)}, не сливаю: "
                       f"лид {risky[0]['id']} в работе или в сделке")

    # Портал отказывается сливать лиды, у которых расходятся стадия или имя:
    # отвечает HTTP 200, поля error в теле нет, а в result лежит
    # STATUS=CONFLICT и ничего не происходит. Мы этот ответ не читали и писали
    # в лог «слито» — дубли оставались, а по логу выглядело, что их больше
    # нет. Поэтому спорные поля сводим к значениям выжившего.
    #
    # Имя расходится сплошь и рядом: в одном лиде «Alexander Esipovich», в
    # другом «Есипович Александр Иванович». Выжившему мы его всё равно
    # сохраняем, так что затирание имени у дубля ничего не теряет — но если
    # слияние не пройдёт, дубль останется жить, поэтому поля возвращаем.
    others = [t for t in twins if t["id"] != alive["id"]]
    master = _call("crm.lead.get", {"id": alive["id"]}, timeout) or {}
    MATCH = ("STATUS_ID", "NAME", "LAST_NAME", "SECOND_NAME")
    same = {field: (master.get(field) or "") for field in MATCH}

    saved: dict[int, dict] = {}
    for twin in others:
        was = _call("crm.lead.get", {"id": twin["id"]}, timeout) or {}
        saved[twin["id"]] = {field: (was.get(field) or "") for field in MATCH}
        if saved[twin["id"]] != same:
            _call("crm.lead.update", {"id": twin["id"], "fields": same}, timeout)

    def restore() -> None:
        for twin_id, fields in saved.items():
            try:
                _call("crm.lead.update", {"id": twin_id, "fields": fields}, timeout)
            except Exception as exc:  # noqa: BLE001 — вернуть не вышло, но лид жив
                logger.warning(f"[bitrix] лид {twin_id}: поля не вернулись: {exc}")

    # Выживает первый в списке, а не самый свежий: порядок задаём сами, иначе
    # портал удалит тот лид, который мы следом собираемся поднимать.
    order = [alive["id"]] + [t["id"] for t in others]
    try:
        answer = _call("crm.entity.mergeBatch",
                       {"params": {"entityTypeId": ENTITY_LEAD,
                                   "entityIds": order}}, timeout) or {}
    except Exception as exc:  # noqa: BLE001 — портал может не дать слить
        restore()
        return alive, f"дублей {len(twins)}, объединить не вышло: {exc}"

    listed = ", ".join(str(t["id"]) for t in others)
    if answer.get("STATUS") != "SUCCESS":
        restore()
        return alive, (f"дублей {len(twins)}, портал отказал "
                       f"({answer.get('STATUS') or 'без ответа'}): остались {listed}")
    return alive, f"слито дублей {len(twins)} в лид {alive['id']} (были: {listed})"


def revive(twin: dict, lead: dict, timeout: int = 20) -> str:
    """Поднимает существующий лид вместо создания нового. Возвращает пометку.

    Свежесть правки стадию больше не защищает. Раньше лид, которого касались
    за последнюю неделю, считался «в работе у менеджера», и робот оставлял его
    на месте. На деле так терялись именно те звонки, ради которых всё и
    задумано: кандидат в «Думают» просил перезвонить, разговор ложился в ленту,
    а карточка оставалась в отстойнике, и найти её удавалось только поиском по
    номеру. Состоявшийся разговор — более свежий факт, чем чей-либо
    трёхдневный комментарий, поэтому теперь он поднимает лид всегда.

    Стадия сделки держится до последнего: там кандидат уже за пределами нашей
    воронки. Но не ценой согласия — если человек в этом разговоре сказал «да»
    или попросил перезвонить, лид поднимается и оттуда (см. BREAK_THROUGH).
    """
    outcome = str(lead.get("итог") or "")
    protected = twin["status"] in KEEP_AS_IS and outcome not in BREAK_THROUGH
    fields = build_fields(lead)
    for owned_by_portal in ("STATUS_ID", "SOURCE_ID", "OPENED"):
        fields.pop(owned_by_portal, None)

    if protected:
        # Разговор всё равно нужен — без него менеджер не узнает, что кандидат
        # за это время согласился.
        _call("crm.lead.update", {"id": twin["id"], "fields": {
            "COMMENTS": fields.get("COMMENTS", "")}}, timeout)
        return f"лид {twin['id']} в сделке, стадию не меняли"

    fields["STATUS_ID"] = REVIVE_STATUS
    _call("crm.lead.update", {"id": twin["id"], "fields": fields}, timeout)
    # Подъём из сделки помечаем отдельно: это единственный случай, когда робот
    # вынимает карточку из закрытой стадии, и в логе он должен быть заметен.
    mark = f" ({outcome}, достали из сделки)" if twin["status"] in KEEP_AS_IS else ""
    return (f"поднят лид {twin['id']} из «{twin['status']}» "
            f"в «{REVIVE_STATUS}»{mark}")


def update_fields(lead_id: int, lead: dict, timeout: int = 20) -> None:
    """Переписывает карточку лида тем, что мы знаем сейчас.

    Стадию и ответственного не трогаем: лид в работе живёт по правилам
    портала, и вернуть его из «Позвонить 2 раз» обратно в «Не обработан»
    значит стереть работу менеджера.
    """
    fields = build_fields(lead)
    for owned_by_portal in ("STATUS_ID", "SOURCE_ID", "OPENED"):
        fields.pop(owned_by_portal, None)
    _call("crm.lead.update", {"id": lead_id, "fields": fields}, timeout)


def send(lead: dict, timeout: int = 20) -> tuple[int, str]:
    """Доставляет кандидата в воронку. Возвращает номер лида и пометку.

    Сначала ищем человека по телефону. Он почти всегда там уже есть — мы и
    звоним по выгрузкам из этой же воронки, — и заводить ему второй лид значит
    засорять работу менеджеру. Нашли: поднимаем существующий обратно в
    «Не обработан». Не нашли: создаём, как раньше.

    Пометка возвращается вместо исключения там, где лид уже доставлен, а
    доделать не вышло: повторная попытка создала бы дубль, а дубль в воронке
    хуже, чем разговор, который придётся открыть в навигаторе.
    """
    phone = (lead.get("телефон") or "").strip()
    twins = all_twins(phone, timeout)

    if not twins:
        result = _call("crm.lead.add", {"fields": build_fields(lead)}, timeout)
        if not result:
            raise RuntimeError("портал не вернул номер лида")
        lead_id, note = int(result), ""
    else:
        # Сначала сливаем дубли, потом поднимаем то, что осталось. Обратный
        # порядок означал бы, что мы поднимаем один лид, а объединение потом
        # затирает его чужими полями.
        twin, merged = merge_twins(twins, timeout)
        lead_id = twin["id"]
        note = "; ".join(x for x in (merged, revive(twin, lead, timeout)) if x)
        logger.info(f"[bitrix] {phone}: {note}")

    try:
        # У поднятого лида лента уже не пуста, и та же расшифровка могла лечь
        # в неё в прошлый раз — проверяем, чтобы менеджер не читал один
        # разговор дважды.
        attach_transcript(lead_id, lead, timeout, check_existing=bool(twins))
    except Exception as exc:  # noqa: BLE001 — портал, сеть, размер комментария
        trouble = f"расшифровка не прикрепилась: {exc}"
        logger.warning(f"[bitrix] {phone}: {trouble}")
        return lead_id, "; ".join(x for x in (note, trouble) if x)
    return lead_id, note


def deliver_pending(conn, limit: int = 20) -> dict:
    """Досылает то, что ещё не ушло. Возвращает сводку для лога."""
    if not enabled():
        return {"skipped": "BITRIX_WEBHOOK не задан"}

    from datetime import datetime

    rows = conn.execute(
        """SELECT crm_id, phone, payload, attempts FROM crm_leads
           WHERE status = 'pending' AND attempts < ?
           ORDER BY created_at LIMIT ?""",
        (MAX_ATTEMPTS, max(1, min(limit, 100))),
    ).fetchall()

    sent, failed, skipped = 0, 0, 0
    for row in rows:
        now = datetime.now().isoformat(timespec="seconds")
        try:
            lead_id, note = send(json.loads(row["payload"] or "{}"))
        except Blocked as exc:
            # Чёрный список — не сбой, повторять нечего. Отдельный статус,
            # чтобы такой кандидат не висел в очереди и не считался потерей.
            conn.execute(
                """UPDATE crm_leads SET status = 'skipped', last_error = ?,
                       updated_at = ? WHERE crm_id = ?""",
                (str(exc)[:400], now, row["crm_id"]),
            )
            skipped += 1
            logger.info(f"[bitrix] {row['phone']}: пропускаем — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — портал, сеть, токен
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            conn.execute(
                """UPDATE crm_leads SET attempts = ?, status = ?, last_error = ?,
                       updated_at = ? WHERE crm_id = ?""",
                (attempts, status, str(exc)[:400], now, row["crm_id"]),
            )
            failed += 1
            logger.warning(f"[bitrix] {row['phone']}: попытка {attempts} не вышла — {exc}")
            continue

        # Замечание кладём в last_error при status='sent': иначе «лид ушёл, но
        # без расшифровки» выглядит снаружи точно так же, как полный успех.
        conn.execute(
            """UPDATE crm_leads SET status = 'sent', bitrix_id = ?, attempts = attempts + 1,
                   last_error = ?, updated_at = ? WHERE crm_id = ?""",
            (lead_id, note[:400], now, row["crm_id"]),
        )
        sent += 1
        # «Создан» говорим только про новый лид. У поднятого пометка уже
        # ушла в лог строкой выше, и второе «создан» подряд читается так,
        # будто мы всё-таки завели дубль.
        logger.info(f"[bitrix] {row['phone']}: лид {lead_id} "
                    + ("создан" if not note else "доставлен"))

    conn.commit()
    return {"отправлено": sent, "не вышло": failed,
            "пропущено": skipped, "взято": len(rows)}
