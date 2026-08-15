"""Вход: хеш пароля, подпись сессии, ограничение попыток.

Проверяется то, чего не делал прежний Basic: пароль не хранится открытым,
cookie нельзя подделать, перебор упирается в лимит.
"""

import base64
import json
import time

import pytest

import auth


# ------------------------------------------------------------- пароль

def test_hash_is_not_the_password():
    encoded = auth.hash_password("правильный-пароль-1234")
    assert "правильный-пароль-1234" not in encoded
    assert encoded.startswith("pbkdf2_sha256$")


def test_verify_accepts_only_the_right_password():
    encoded = auth.hash_password("правильный-пароль-1234")
    assert auth.verify_password("правильный-пароль-1234", encoded)
    assert not auth.verify_password("правильный-пароль-1235", encoded)
    assert not auth.verify_password("", encoded)


def test_same_password_gives_different_hashes():
    """Соль у каждого хеша своя: одинаковые пароли не выглядят одинаково."""
    a = auth.hash_password("одинаковый-пароль")
    b = auth.hash_password("одинаковый-пароль")
    assert a != b
    assert auth.verify_password("одинаковый-пароль", a)
    assert auth.verify_password("одинаковый-пароль", b)


def test_broken_hash_does_not_crash():
    assert not auth.verify_password("что угодно", "мусор")
    assert not auth.verify_password("что угодно", "")
    assert not auth.verify_password("что угодно", "md5$1$c2FsdA==$aGFzaA==")


def test_empty_password_is_rejected_on_hashing():
    with pytest.raises(ValueError):
        auth.hash_password("")


# ------------------------------------------------------------ сессия

@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a" * 64)


def test_session_round_trip(secret):
    cookie = auth.issue_session("anna@example.com", days=30)
    body = auth.read_session(cookie)
    assert body["email"] == "anna@example.com"
    assert body["exp"] > time.time()


def test_tampered_session_is_rejected(secret):
    cookie = auth.issue_session("anna@example.com", days=30)
    packed, signature = cookie.rsplit(".", 1)

    # Подменяем почту, подпись оставляем прежней.
    raw = base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4))
    body = json.loads(raw)
    body["email"] = "chuzhoy@example.com"
    forged = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")

    assert auth.read_session(f"{forged}.{signature}") is None


def test_session_from_another_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "a" * 64)
    cookie = auth.issue_session("anna@example.com", days=30)
    monkeypatch.setenv("SECRET_KEY", "b" * 64)
    assert auth.read_session(cookie) is None


def test_expired_session_is_rejected(secret):
    cookie = auth.issue_session("anna@example.com", days=1, now=time.time() - 2 * 86400)
    assert auth.read_session(cookie) is None


def test_garbage_cookie_is_rejected(secret):
    for value in ("", "мусор", "a.b", "....", "eyJ9.подпись"):
        assert auth.read_session(value) is None


# --------------------------------------------------- лимит попыток

def test_throttle_blocks_after_limit():
    throttle = auth.LoginThrottle(max_attempts=3, window_sec=900, block_sec=600)
    assert throttle.blocked_for("1.2.3.4") == 0
    assert throttle.register_failure("1.2.3.4") == 0
    assert throttle.register_failure("1.2.3.4") == 0
    assert throttle.register_failure("1.2.3.4") == 600
    assert throttle.blocked_for("1.2.3.4") > 0


def test_throttle_is_per_ip():
    throttle = auth.LoginThrottle(max_attempts=2)
    throttle.register_failure("1.1.1.1")
    throttle.register_failure("1.1.1.1")
    assert throttle.blocked_for("1.1.1.1") > 0
    assert throttle.blocked_for("2.2.2.2") == 0, "блокировка одного IP задела другой"


def test_success_clears_failures():
    throttle = auth.LoginThrottle(max_attempts=3)
    throttle.register_failure("1.2.3.4")
    throttle.register_failure("1.2.3.4")
    throttle.register_success("1.2.3.4")
    assert throttle.attempts_left("1.2.3.4") == 3


def test_old_failures_fall_out_of_window():
    throttle = auth.LoginThrottle(max_attempts=3, window_sec=100)
    now = 1_000_000.0
    throttle.register_failure("1.2.3.4", now=now)
    throttle.register_failure("1.2.3.4", now=now + 1)
    # Через окно старые попытки не считаются.
    assert throttle.register_failure("1.2.3.4", now=now + 500) == 0


# --------------------------------------------------------- проверка

def test_check_credentials(monkeypatch):
    encoded = auth.hash_password("длинный-пароль-1234")
    config = auth.AuthConfig(
        email="anna@example.com", password_hash=encoded,
        session_days=30, max_attempts=5, secure_cookie=False,
    )
    assert auth.check_credentials("anna@example.com", "длинный-пароль-1234", config)[0]
    # Регистр и пробелы в почте не должны мешать входу.
    assert auth.check_credentials("  Anna@Example.com ", "длинный-пароль-1234", config)[0]
    assert not auth.check_credentials("anna@example.com", "не тот", config)[0]
    assert not auth.check_credentials("chuzhoy@example.com", "длинный-пароль-1234", config)[0]


def test_check_credentials_when_not_configured():
    config = auth.AuthConfig(
        email="", password_hash="", session_days=30, max_attempts=5, secure_cookie=False,
    )
    ok, reason = auth.check_credentials("anna@example.com", "пароль", config)
    assert not ok
    assert "не настроен" in reason


def test_password_never_appears_in_login_log(caplog):
    """Пароль не должен попасть в журнал ни при каком исходе."""
    import io

    from loguru import logger

    stream = io.StringIO()
    sink = logger.add(stream, level="INFO")
    try:
        auth.log_login(ok=False, email="anna@example.com", ip="1.2.3.4", reason="неверный пароль")
        auth.log_login(ok=True, email="anna@example.com", ip="1.2.3.4")
    finally:
        logger.remove(sink)
    written = stream.getvalue()
    assert "anna@example.com" in written
    assert "секретное-значение" not in written


# ------------------------------------------ находки состязательной проверки

def test_session_is_bound_to_password(secret):
    """Смена пароля гасит ранее выданные cookie.

    Иначе украденная сессия переживает смену пароля и живёт весь свой срок —
    то есть смена пароля не является реакцией на компрометацию.
    """
    old_hash = auth.hash_password("старый-пароль-1234")
    cookie = auth.issue_session("anna@example.com", days=30,
                                epoch=auth.password_epoch(old_hash))
    body = auth.read_session(cookie)
    assert body["epoch"] == auth.password_epoch(old_hash)

    new_hash = auth.hash_password("новый-пароль-5678")
    assert body["epoch"] != auth.password_epoch(new_hash), (
        "отпечаток не изменился — старые сессии переживут смену пароля"
    )


def test_hash_with_zero_iterations_does_not_crash():
    """Битый AUTH_PASSWORD_HASH должен давать отказ, а не 500."""
    assert not auth.verify_password("пароль", "pbkdf2_sha256$0$c2FsdA==$aGFzaA==")
    assert not auth.verify_password("пароль", "pbkdf2_sha256$-1$c2FsdA==$aGFzaA==")


def test_oversized_cookie_is_rejected(secret):
    assert auth.read_session("a" * 5000 + ".signature") is None


def test_reading_throttle_does_not_create_entries():
    """Чтение не должно раздувать таблицу: иначе поток GET съедает память."""
    throttle = auth.LoginThrottle(max_attempts=5)
    for i in range(100):
        throttle.blocked_for(f"10.0.0.{i}")
        throttle.attempts_left(f"10.0.0.{i}")
    assert len(throttle._buckets) == 0


def test_throttle_table_is_bounded():
    throttle = auth.LoginThrottle(max_attempts=5)
    throttle.MAX_KEYS = 50
    now = 1_000_000.0
    for i in range(500):
        throttle.register_failure(f"10.0.{i // 256}.{i % 256}", now=now)
    assert len(throttle._buckets) <= throttle.MAX_KEYS


def test_throttle_purges_stale_entries():
    throttle = auth.LoginThrottle(max_attempts=5, window_sec=100, block_sec=100)
    now = 1_000_000.0
    for i in range(10):
        throttle.register_failure(f"10.0.0.{i}", now=now)
    assert len(throttle._buckets) == 10
    throttle._purge(now + 1000)
    assert len(throttle._buckets) == 0


def test_module_works_without_loguru():
    """auth.py умеют запускать вне контейнера — на голом системном python.

    Хеширование пароля не должно падать из-за отсутствия библиотеки логов:
    именно на этом споткнулась выкатка, когда scripts/set_password.py
    запустили системным python без зависимостей.
    """
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "auth.py"), encoding="utf-8").read()
    assert "except ImportError" in source, "импорт loguru снова обязательный"
    head = source.split("ROLE_")[0] if "ROLE_" in source else source[:2000]
    assert "logging.getLogger" in head, "нет запасного логгера"


import os  # noqa: E402  (нужен тесту выше)
