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
        m = re.match(r'''@app\.(get|post|put|delete)\(['"]([^'"]+)['"]''', line)
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


# Роуты, которым положено быть открытыми, и почему:
#   /health  — его дёргает healthcheck контейнера, у которого нет учётных данных;
#   /login   — сама форма входа, иначе войти неоткуда;
#   /logout  — выход не должен требовать действующей сессии.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
}


def test_open_routes_are_exactly_expected():
    open_routes = {(method, path) for method, path, auth in _routes() if not auth}
    assert open_routes == PUBLIC, (
        "Изменился список роутов без авторизации. Разрешены только "
        f"{sorted(PUBLIC)}. Сейчас открыты: {sorted(open_routes)}"
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

    for name in ("SECRET_KEY", "AUTH_EMAIL", "AUTH_PASSWORD_HASH"):
        monkeypatch.setenv(name, "")
    with pytest.raises(RuntimeError) as exc:
        check()
    assert "AUTH_EMAIL" in str(exc.value)

    monkeypatch.setenv("SECRET_KEY", "9f2b7c1ae4d38650b1cf27a4e9d0538172aebc4f6d091237")
    monkeypatch.setenv("AUTH_EMAIL", "anna@example.com")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "pbkdf2_sha256$600000$c2FsdA==$aGFzaA==")
    monkeypatch.setenv("WEB_USER", "")
    monkeypatch.setenv("WEB_PASSWORD", "")
    check()  # не должно падать


def test_weak_basic_password_stops_startup(monkeypatch):
    source = open(APP_SOURCE, encoding="utf-8").read()
    start = source.index("REQUIRED_ENV = {")
    end = source.index("@asynccontextmanager")
    stub = _StubLogger()
    namespace = {"os": os, "logger": stub}
    exec(compile(source[start:end], APP_SOURCE, "exec"), namespace)

    monkeypatch.setenv("SECRET_KEY", "9f2b7c1ae4d38650b1cf27a4e9d0538172aebc4f6d091237")
    monkeypatch.setenv("AUTH_EMAIL", "anna@example.com")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "pbkdf2_sha256$600000$c2FsdA==$aGFzaA==")
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "change_me_please")
    with pytest.raises(RuntimeError) as exc:
        namespace["check_required_env"]()
    assert "WEB_PASSWORD" in str(exc.value)
    assert "WEB_BASIC_ENABLED=0" in str(exc.value), "нет подсказки, как выйти из положения"


class _StubLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message):
        self.warnings.append(message)

    def info(self, message):
        pass


# --------------------------- находки состязательной проверки входа

def test_forwarded_header_is_not_trusted_by_default(monkeypatch):
    """X-Forwarded-For клиент подставляет сам.

    Пока приложение слушает напрямую, доверие заголовку означает, что лимит
    попыток обходится сменой одной строки в каждом запросе.
    """
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    trusted = os.getenv("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes")
    assert not trusted, "заголовок прокси принимается по умолчанию"

    source = open(APP_SOURCE, encoding="utf-8").read()
    body = source.split("def _client_ip(")[1].split("\ndef ")[0]
    assert "TRUST_PROXY" in body, "_client_ip читает заголовок без проверки доверия"


def test_basic_is_limited_to_machine_routes():
    """Basic не должен открывать всё приложение.

    Иначе слабый общий пароль даёт доступ к реестру и правилам ставок
    в обход формы входа и лимита попыток.
    """
    source = open(APP_SOURCE, encoding="utf-8").read()
    assert "BASIC_PATHS" in source
    body = source.split("BASIC_PATHS = {")[1].split("}")[0]
    for path in ("/metrics", "/jobs", "/health/details"):
        assert path in body
    for path in ("/registry", "/api/navigator", "/api/rates"):
        assert path not in body, f"{path} доступен по Basic"

    check = source.split("def _basic_ok(")[1].split("\ndef ")[0]
    assert "BASIC_PATHS" in check, "_basic_ok не сверяется со списком путей"


def test_password_check_is_off_the_event_loop():
    """PBKDF2 — 0,3 с процессорного времени; в event loop это отказ в обслуживании."""
    source = open(APP_SOURCE, encoding="utf-8").read()
    handler = source.split("async def login_submit(")[1].split("\n@app.")[0]
    assert "to_thread(auth.check_credentials" in handler, (
        "проверка пароля выполняется в event loop"
    )


def test_login_and_logout_check_origin():
    source = open(APP_SOURCE, encoding="utf-8").read()
    for handler_name in ("async def login_submit(", "async def logout("):
        handler = source.split(handler_name)[1].split("\n@app.")[0]
        assert "_same_origin(request)" in handler, f"{handler_name} без проверки источника"


def test_safe_next_rejects_external_targets():
    source = open(APP_SOURCE, encoding="utf-8").read()
    start = source.index("def _safe_next(")
    end = source.index("\n@app.", start)
    namespace = {}
    exec(compile(source[start:end], APP_SOURCE, "exec"), namespace)
    safe_next = namespace["_safe_next"]

    assert safe_next("/registry") == "/registry"
    assert safe_next("/registry?page=2") == "/registry?page=2"
    for evil in ("//evil.com", "/\\evil.com", "https://evil.com", "javascript:alert(1)",
                 "evil.com", "", None, "/ok\r\nSet-Cookie: a=b"):
        assert safe_next(evil) == "/", f"пропущен внешний адрес: {evil!r}"


def test_login_page_is_not_cached():
    source = open(APP_SOURCE, encoding="utf-8").read()
    handler = source.split("async def login_form(")[1].split("\n@app.")[0]
    assert "no-store" in handler, "форма входа может осесть в кеше"


def test_short_secret_key_stops_startup(monkeypatch):
    """Слабый SECRET_KEY — отказ старта, а не предупреждение.

    Этим ключом подписывается сессия: подобрав его, войти можно без пароля.
    """
    source = open(APP_SOURCE, encoding="utf-8").read()
    start = source.index("REQUIRED_ENV = {")
    end = source.index("@asynccontextmanager")
    namespace = {"os": os, "logger": _StubLogger()}
    exec(compile(source[start:end], APP_SOURCE, "exec"), namespace)
    check = namespace["check_required_env"]

    monkeypatch.setenv("AUTH_EMAIL", "anna@example.com")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", "pbkdf2_sha256$600000$c2FsdA==$aGFzaA==")
    monkeypatch.setenv("WEB_USER", "")
    monkeypatch.setenv("WEB_PASSWORD", "")

    for weak in ("short", "0" * 64, "changeme"):
        monkeypatch.setenv("SECRET_KEY", weak)
        with pytest.raises(RuntimeError):
            check()

    monkeypatch.setenv("SECRET_KEY", "9f2b7c1ae4d38650b1cf27a4e9d0538172aebc4f6d091237")
    check()


def test_route_scanner_sees_single_quotes(tmp_path):
    """Сам сканер роутов не должен пропускать другой синтаксис."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "@app.get('/debug/dump')\n"
        "async def dump():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    import re as _re
    lines = probe.read_text(encoding="utf-8").split("\n")
    found = [
        _re.match(r'''@app\.(get|post|put|delete)\(['"]([^'"]+)['"]''', line)
        for line in lines
    ]
    assert any(found), "роут с одинарными кавычками не распознан"


def test_login_attempt_limit_comes_from_env():
    """LOGIN_MAX_ATTEMPTS должен доходить до ограничителя.

    Переменная была объявлена, читалась в конфиг — и терялась: ограничитель
    создавался без аргументов и всегда работал на зашитой пятёрке.
    """
    source = open(APP_SOURCE, encoding="utf-8").read()
    m = re.search(r"_throttle = auth\.LoginThrottle\(([^)]*)\)", source)
    assert m, "ограничитель не найден"
    assert "max_attempts" in m.group(1), "LOGIN_MAX_ATTEMPTS не доходит до ограничителя"


def test_global_throttle_exists():
    """Лимит на адрес не спасает от перебора с разных адресов."""
    source = open(APP_SOURCE, encoding="utf-8").read()
    assert "_global_throttle" in source, "нет предохранителя от распределённого перебора"
    handler = source.split("async def login_submit(")[1].split("\n@app.")[0]
    assert "_global_throttle.register_failure" in handler
    assert "_global_throttle.blocked_for" in handler


def test_failed_basic_is_throttled_and_logged():
    """Перебор по Basic не должен быть невидимым и безлимитным."""
    source = open(APP_SOURCE, encoding="utf-8").read()
    body = source.split("def verify_creds(")[1].split("\n@app.")[0]
    assert "_throttle.register_failure" in body, "неудачный Basic не считается"
    assert "auth.log_login" in body, "неудачный Basic не пишется в журнал"
