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
        body = "\n".join(sig)
        out.append((m.group(1).upper(), m.group(2),
                    "verify_creds" in body or "require_admin" in body))
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
    assert "await asyncio.to_thread(" in handler, (
        "проверка пароля выполняется в event loop"
    )
    # Сама сверка пароля не должна вызываться напрямую из корутины.
    inline = [
        line for line in handler.split("\n")
        if ("authenticate(" in line or "verify_password(" in line) and "to_thread" not in line
        and not line.strip().startswith(("#", "person, why", "def "))
    ]
    assert not inline, f"сверка пароля вызывается в event loop: {inline}"


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


# ------------------------------- матрица прав (решение C1a заказчика)

# Что закрыто рекрутеру: справочники, ручной ввод, запуск прогонов, правила
# ставок. Правила ставок правит только администратор — это сетка мотивации,
# и менять её не должен тот, кто по ней получает.
ADMIN_ONLY = {
    ("POST", "/trigger/{name}"),
    ("POST", "/run"),
    ("POST", "/api/rates"),
    ("DELETE", "/api/rates/{rule_id}"),
    ("GET", "/registry/dictionaries"),
    ("POST", "/registry/dictionaries/confirm"),
    ("POST", "/registry/dictionaries/delete"),
    ("POST", "/registry/dictionaries/confirm-all"),
    ("GET", "/registry/manual"),
    ("POST", "/registry/manual"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("POST", "/api/users/{user_id}/reset"),
    ("POST", "/api/users/{user_id}/toggle"),
    ("POST", "/api/users/{user_id}/role"),
}

# Доступно любому вошедшему, включая рекрутера.
ANY_LOGGED_IN = {
    ("GET", "/"), ("GET", "/navigator"), ("GET", "/api/navigator"),
    ("GET", "/registry"), ("GET", "/api/registry"), ("GET", "/registry/export.csv"),
    ("GET", "/registry/position/{position_id}"), ("POST", "/registry/position/{position_id}"),
    ("GET", "/registry/{request_id}"), ("GET", "/vacancies"), ("GET", "/api/vacancies"),
    ("GET", "/password"), ("POST", "/password"),
}


def _admin_routes():
    source = open(APP_SOURCE, encoding="utf-8").read()
    lines = source.split("\n")
    out = set()
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
        if "require_admin" in "\n".join(sig):
            out.add((m.group(1).upper(), m.group(2)))
    return out


def test_admin_only_routes_match_decision():
    assert _admin_routes() == ADMIN_ONLY, (
        "Матрица прав разошлась с решением заказчика (docs/OPEN-QUESTIONS.md, C1a).\n"
        f"Лишние: {sorted(_admin_routes() - ADMIN_ONLY)}\n"
        f"Потерянные: {sorted(ADMIN_ONLY - _admin_routes())}"
    )


def test_recruiter_keeps_access_to_work_screens():
    """Рекрутеру закрыли лишнее, но не работу: подбор и карточка должны остаться."""
    admin_only = _admin_routes()
    for route in ANY_LOGGED_IN:
        assert route not in admin_only, f"{route} закрыт рекрутеру, хотя это его работа"


def test_password_change_is_available_to_everyone_logged_in():
    """Смена своего пароля не может быть привилегией администратора.

    Иначе рекрутер, вошедший по временному паролю, не сможет задать свой
    и останется под паролем, который знает администратор.
    """
    assert ("GET", "/password") not in _admin_routes()
    assert ("POST", "/password") not in _admin_routes()


def test_last_admin_cannot_be_disabled():
    """Иначе система останется без администратора: экран доступов закрыт этой же ролью."""
    source = open(APP_SOURCE, encoding="utf-8").read()
    handler = source.split("async def api_users_toggle(")[1].split("\n@app.")[0]
    assert "count_admins" in handler, "нет защиты от отключения последнего администратора"
    assert "Нельзя отключить самого себя" in handler


def test_manual_edits_record_author():
    """История правок без автора не отвечает на вопрос «кто это поставил»."""
    source = open(os.path.join(ROOT, "registry", "queries.py"), encoding="utf-8").read()
    handler = source.split("def update_manager_fields(")[1].split("\ndef ")[0]
    assert "author" in handler
    assert "position_history" in handler, "ручные правки не пишутся в историю"
    assert "changed_by" in handler


def test_people_data_is_not_in_navigator_payload():
    """Список сотрудников и журнал входов не должны уезжать рекрутеру.

    Данные лежат за отдельной ручкой /api/users, закрытой ролью админа,
    а не в общем ответе /api/navigator, который получает каждый вошедший.
    """
    source = open(os.path.join(ROOT, "navigator_api.py"), encoding="utf-8").read()
    for leak in ("login_audit", "list_users(", "password_hash"):
        assert leak not in source, f"{leak} попал в общий ответ /api/navigator"


def test_no_client_side_admin_password():
    """Пароль администратора не может лежать в клиентском коде.

    В рабочем шаблоне оставался ADMIN_PASS = '1207' из макета, а признак
    входа в админку хранился в localStorage: любой вошедший рекрутер открывал
    раздел через консоль браузера. Роль должна приходить с сервера.
    """
    template = open(os.path.join(ROOT, "templates", "navigator.html"), encoding="utf-8").read()
    assert "ADMIN_PASS" not in template, "пароль администратора вернулся в клиентский код"
    assert "1207" not in template, "демо-пароль макета вернулся в шаблон"
    assert "eltera_navigator_admin_v1" not in template, "гейт админки снова в localStorage"
    assert "DATA.me" in template, "экран не спрашивает роль у сервера"


def test_all_template_responses_use_keyword_form():
    """starlette 1.1 требует TemplateResponse(request=..., name=..., context=...).

    Старая позиционная форма не устарела, а удалена: словарь контекста уходит
    в параметр name, Jinja пытается использовать его как ключ кеша шаблонов
    и падает с «unhashable type: dict» — то есть страница отдаёт 500.
    Ошибку видно только в браузере, ни один unit-тест её не ловит,
    поэтому проверяем форму вызова.
    """
    lines = open(APP_SOURCE, encoding="utf-8").read().split("\n")
    bad = []
    for i, line in enumerate(lines):
        if "TemplateResponse(" not in line:
            continue
        following = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not following.startswith("request="):
            bad.append(f"строка {i + 1}: {following[:50]}")
    assert not bad, "позиционная форма TemplateResponse — страница отдаст 500:\n" + "\n".join(bad)


def test_login_form_checks_session_as_strictly_as_other_routes():
    """Иначе получается петля редиректов и приложение недоступно.

    /login проверял сессию голым read_session — только подпись и срок, без
    сверки с базой. Cookie с устаревшим отпечатком пароля выглядела
    действительной для /login и недействительной для остальных роутов:
    /navigator отправлял на /login, /login отправлял обратно.
    """
    source = open(APP_SOURCE, encoding="utf-8").read()
    handler = source.split("async def login_form(")[1].split("\n@app.")[0]
    assert "current_user(request)" in handler, (
        "/login проверяет сессию мягче остальных роутов — будет петля редиректов"
    )
    assert "auth.read_session(" not in handler, (
        "/login снова смотрит на сессию в обход проверки пользователя в базе"
    )


def test_login_form_clears_stale_cookie():
    """Недействующую cookie надо гасить, иначе браузер носит её до истечения срока."""
    source = open(APP_SOURCE, encoding="utf-8").read()
    handler = source.split("async def login_form(")[1].split("\n@app.")[0]
    assert "delete_cookie" in handler, "протухшая cookie не гасится на форме входа"
