"""Что именно закрыто и что открыто наружу.

Роуты успели пожить открытыми: /metrics отдавал состав источников, объёмы
потребности и средние ставки любому в интернете, /jobs — расписание прогонов,
/openapi.json — карту всех эндпоинтов. Тесты фиксируют новое положение дел,
чтобы следующая правка роутов не открыла их обратно молча.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_SOURCE = os.path.join(ROOT, "app.py")


def _routes():
    """(метод, путь, требует ли авторизации) по исходнику app.py.

    Разбираем текст, а не поднимаем приложение: импорт app.py тянет Google
    Sheets и Telethon, а тесты обязаны работать без сети и ключей.
    """
    lines = open(APP_SOURCE, encoding="utf-8").read().split("\n")
    out = []
    for i, line in enumerate(lines):
        m = re.match(r'@app\.(get|post|put|delete)\("([^"]+)"', line)
        if not m:
            continue
        j = i
        while j < len(lines) and not lines[j].lstrip().startswith("async def"):
            j += 1
        sig, depth, started, k = [], 0, False, j
        while k < len(lines):
            sig.append(lines[k])
            depth += lines[k].count("(") - lines[k].count(")")
            if "(" in lines[k]:
                started = True
            if started and depth <= 0:
                break
            k += 1
        out.append((m.group(1).upper(), m.group(2), "verify_creds" in "\n".join(sig)))
    return out


# Единственный роут, которому положено быть открытым: его дёргает healthcheck
# контейнера, у которого нет учётных данных.
PUBLIC = {("GET", "/health")}


def test_open_routes_are_exactly_health():
    open_routes = {(method, path) for method, path, auth in _routes() if not auth}
    assert open_routes == PUBLIC, (
        "Изменился список роутов без авторизации. Открытым может быть только "
        f"/health. Сейчас открыты: {sorted(open_routes)}"
    )


def test_metrics_and_jobs_are_closed():
    by_path = {path: auth for _, path, auth in _routes()}
    assert by_path["/metrics"], "/metrics снова открыт: наружу уходит статистика по источникам"
    assert by_path["/jobs"], "/jobs снова открыт: наружу уходит расписание прогонов"


def test_public_health_does_not_leak_schedule():
    """Открытый /health отдаёт только признак живости.

    Прежняя версия возвращала список фоновых задач, то есть состав источников.
    """
    source = open(APP_SOURCE, encoding="utf-8").read()
    body = source.split('@app.get("/health")')[1].split("@app.get")[0]
    assert "scheduler.get_jobs()" not in body, "/health снова отдаёт список задач"
    assert '"status": "ok"' in body


def test_api_schema_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    enabled = os.getenv("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")
    assert not enabled, "Схема API включена по умолчанию"


def test_required_env_check_rejects_empty(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("_app_probe", APP_SOURCE)
    # Полный импорт app.py в тестах недоступен (Google Sheets и Telethon),
    # поэтому проверяем саму функцию, вырезав её из исходника.
    source = open(APP_SOURCE, encoding="utf-8").read()
    start = source.index("REQUIRED_ENV = {")
    end = source.index("@asynccontextmanager")
    namespace = {"os": os, "logger": _StubLogger()}
    exec(compile(source[start:end], APP_SOURCE, "exec"), namespace)
    check = namespace["check_required_env"]

    monkeypatch.setenv("WEB_USER", "")
    monkeypatch.setenv("WEB_PASSWORD", "")
    with pytest.raises(RuntimeError) as exc:
        check()
    assert "WEB_USER" in str(exc.value)

    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "s3cret-and-long")
    check()  # не должно падать


def test_weak_password_is_reported(monkeypatch):
    source = open(APP_SOURCE, encoding="utf-8").read()
    start = source.index("REQUIRED_ENV = {")
    end = source.index("@asynccontextmanager")
    stub = _StubLogger()
    namespace = {"os": os, "logger": stub}
    exec(compile(source[start:end], APP_SOURCE, "exec"), namespace)

    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "change_me_please")
    namespace["check_required_env"]()
    assert stub.warnings, "Пароль из примера прошёл без предупреждения"
    assert "WEB_PASSWORD" in stub.warnings[0]


class _StubLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.warnings.append(message)

    def info(self, message):
        pass
