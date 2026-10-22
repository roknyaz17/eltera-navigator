"""Канал МЕЛИОРа: где кончается один объект и начинается следующий.

Разметка поста — единственное, что у этого источника нельзя проверить по
данным: заголовки каждый раз новые, эмодзи объекта своё, а ошибка нарезки не
падает, а тихо отдаёт модели кусок без должности или, наоборот, два объекта
одной строкой. Поэтому все случаи, которые встретились в канале, закреплены
тестами.
"""

import pytest

from melior_message_processor import MeliorMessageProcessor

SNAPSHOT = """АКТУАЛЬНАЯ ЗАЯВКА
(Москва/МО):

💊 СКЛАД МЕДИКАМЕНТОВ
МО, г. Пушкино
👥 Комплектовщик 15Ж, 15М
🪪 РФ от 20 до 45 лет
💵 Комплектовщик 3650 - 4000 руб/смена
🔹 Срок вахты 21/33 смены

🚘 ФОРУМ АВТО (склад авто аксессуаров и мелких запчастей)
МО, г. Троицк
👥 Комплектовщик 10М
💵 Комплектовщик 3520 руб/ смена
🔹 Срок вахты 35 смен

🍏 ЗОЛОТОЕ ЯБЛОКО (склад косметики и парфюмерии)
г. Москва, метро Южная
👥 Комплектовщик 15Ж 15М
💵 Комплектовщик фикс+сделка, до 4500 руб/ смена
"""

# Пост с поводом в шапке и датой заселения отдельной строкой: и то, и другое
# выглядит как заголовок объекта, но объектом не является.
AIRPORT = """ВОЗОБНОВЛЕНИЕ ПОДБОРА
🛫🛫🛫🛫🛫

✈️ АЭРОПОРТ
МО, Шереметьево
‼️ЗАСЕЛЕНИЕ С 09.07‼️
👥 Упаковщик 2 СЕМЕЙНЫЕ ПАРЫ
👥 Повар ГЦ 1М
🪪 РФ до 50 лет
💵 Упаковщик 3600 руб смена
💵 Повар ГЦ 4100 руб смена
🚨 СБ НЕТ
"""

SERVICE = """🆕🆕🆕
Партнеры, по многочисленным просьбам, мы в МАХ

https://max.ru/join/XXXX
"""

GREETING = """Дорогие партнёры!

От всей души поздравляем вас с наступающим Новым годом! Ваш труд — это смены,
которые мы закрываем вместе.
"""


@pytest.fixture()
def proc():
    # Справки по объектам тут не нужны: тестируется разметка постов.
    return MeliorMessageProcessor(sheets_service=None, llm_parser=None, use_kb=False)


def test_snapshot_splits_by_objects(proc):
    segments = proc._iter_segments(SNAPSHOT)
    assert len(segments) == 3
    assert "СКЛАД МЕДИКАМЕНТОВ" in segments[0]
    assert "ФОРУМ АВТО" in segments[1]
    assert "ЗОЛОТОЕ ЯБЛОКО" in segments[2]
    # Каждый кусок уносит свои ставки и никаких чужих.
    assert "3520" not in segments[0] and "3650" not in segments[1]
    # Шапка с регионом достаётся первому куску, а не теряется.
    assert "АКТУАЛЬНАЯ ЗАЯВКА" in segments[0]


def test_single_object_post_is_not_split(proc):
    post = SNAPSHOT.split("\n\n")[2]
    assert proc._iter_segments(post) == [post]


def test_event_lines_do_not_become_objects(proc):
    """«ЗАСЕЛЕНИЕ С 09.07» — повод, а не объект.

    Разрезав по нему, мы оставили бы аэропорт без единой вакансии, а три
    должности — под заголовком «Заселение».
    """
    segments = proc._iter_segments(AIRPORT)
    assert len(segments) == 1
    assert "Упаковщик" in segments[0] and "Повар ГЦ" in segments[0]


def test_object_query_takes_object_not_header(proc):
    """Объект ищем по его блоку: в шапке поста названия объекта нет."""
    query = proc.object_query(AIRPORT)
    assert query.startswith("✈️ АЭРОПОРТ")
    assert "Шереметьево" in query
    assert "ВОЗОБНОВЛЕНИЕ" not in query


def test_service_messages_are_skipped(proc):
    assert proc._has_vacancy_signals(SNAPSHOT)
    assert proc._has_vacancy_signals(AIRPORT)
    assert not proc._has_vacancy_signals(SERVICE)
    # «смены» в поздравлении — не заявка: нужна разметка потребности и ставки.
    assert not proc._has_vacancy_signals(GREETING)


def test_counterparty_is_forced(proc):
    """Контрагента в тексте нет — только объекты. Ставим его сами."""
    assert proc.counterparty_override == "МЕЛИОР"


def test_collect_requests_keeps_one_request_per_post(proc):
    messages = [
        {"id": 817, "date": "2026-09-16T06:16:00", "text": SNAPSHOT, "channel_id": -1003439917213},
        {"id": 661, "date": "2026-07-18T09:00:00", "text": SERVICE, "channel_id": -1003439917213},
    ]
    requests, had_snapshot = proc.collect_requests(messages, source="melior")
    assert [r.source_ref for r in requests] == ["msg:817"]
    # Снимок по заголовку не ищем — его назначает пайплайн, прочитав канал целиком.
    assert had_snapshot is False
    assert len(requests[0].parse_chunks) == 3
    assert requests[0].source_url.endswith("/817")
