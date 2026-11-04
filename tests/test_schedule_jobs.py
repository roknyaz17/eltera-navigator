"""Время прогона на экране «Источники» обязано совпадать с расписанием JOBS.

06.10.2026 Табия выпала из noon_tables: app.py залили целиком из копии,
снятой до её выкладки, а на экране у неё по-прежнему стояло «12:00». Месяц
данные не обновлялись, и никто этого не видел. Здесь проверяется ровно эта
связка: каждое время из navigator_api.SOURCE_SCHEDULE есть у задачи JOBS,
которая этот источник запускает.

app.py не импортируется — при импорте он поднимает клиента Google Sheets.
JOBS читается из исходника через ast.
"""

import ast
import re
from pathlib import Path

import navigator_api
from registry.sources import RETIRED_SOURCES

APP = Path(__file__).resolve().parent.parent / "app.py"
TIME = re.compile(r"^\d{2}:\d{2}$")


def _jobs():
    """{(час, минута): {источники}} из литерала JOBS в app.py."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    node = next(
        n.value for n in tree.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "JOBS" for t in n.targets)
    )
    slots = {}
    for cfg in node.values:
        fields = {k.value: v for k, v in zip(cfg.keys, cfg.values)}
        trigger = {kw.arg: ast.literal_eval(kw.value) for kw in fields["trigger"].keywords}
        key = (int(trigger["hour"]), int(trigger.get("minute", 0)))
        slots.setdefault(key, set()).update(ast.literal_eval(fields["sources"]))
    return slots


def test_каждое_время_на_экране_есть_в_jobs():
    slots = _jobs()
    missing = []
    for source, times in navigator_api.SOURCE_SCHEDULE.items():
        if source in RETIRED_SOURCES:
            continue
        for t in (x.strip() for x in times.split(",")):
            if not TIME.match(t):
                continue  # «вручную»
            h, m = map(int, t.split(":"))
            if source not in slots.get((h, m), set()):
                missing.append(f"{source} {t}")
    assert not missing, f"на экране есть время, а в JOBS прогона нет: {missing}"


def test_табия_в_прогоне_1200():
    assert "tabiya" in _jobs()[(12, 0)]
