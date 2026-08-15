"""Вход по почте и паролю: хеш, сессия, ограничение попыток, журнал.

Заменяет общую пару HTTP Basic на нормальный вход, как в макете
(`navigator/index.html`), но без того, что было в макете сделано «на показ»:
пароль там сравнивался в браузере и лежал в коде, сессия жила в localStorage.

Что здесь по-другому:
  * пароль хранится **хешем** (PBKDF2-HMAC-SHA256), в открытом виде его нет
    ни в окружении, ни в базе, ни в логах;
  * сессия — подписанная cookie, подпись проверяется на сервере, подделать её
    без SECRET_KEY нельзя;
  * попытки входа ограничены, каждая записывается в журнал.

Зависимостей не добавляет: всё на стандартной библиотеке. Отдельный модуль,
а не кусок app.py, чтобы это можно было тестировать без поднятия приложения
(app.py на импорте тянет Google Sheets).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

# --------------------------------------------------------------- пароль

# 600 000 итераций — рекомендация OWASP для PBKDF2-HMAC-SHA256 (2023).
# Число хранится в самой строке хеша, поэтому его можно поднять позже,
# не ломая уже выданные пароли.
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGO = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Пароль → строка вида `pbkdf2_sha256$600000$<salt>$<hash>`."""
    if not password:
        raise ValueError("Пустой пароль")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        PBKDF2_ALGO,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, encoded: str) -> bool:
    """Сверка пароля с хешем. Всегда за одно и то же время при верном формате."""
    try:
        algo, raw_iterations, raw_salt, raw_digest = encoded.split("$")
        if algo != PBKDF2_ALGO:
            return False
        iterations = int(raw_iterations)
        if iterations <= 0:
            raise ValueError("iterations")
        salt = base64.b64decode(raw_salt)
        expected = base64.b64decode(raw_digest)
    except (ValueError, AttributeError):
        logger.error("[auth] AUTH_PASSWORD_HASH повреждён или в неизвестном формате")
        return False
    actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------- сессия

COOKIE_NAME = "eltera_session"


def _secret() -> bytes:
    value = os.getenv("SECRET_KEY", "").strip()
    if not value:
        # До проверки на старте сюда не дойти; страховка от смены порядка старта.
        raise RuntimeError("SECRET_KEY не задан — подписывать сессию нечем")
    return value.encode("utf-8")


def _sign(payload: bytes) -> str:
    mac = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def password_epoch(password_hash: str) -> str:
    """Короткий отпечаток текущего пароля.

    Кладётся в сессию: сменили пароль — отпечаток другой, и все ранее выданные
    cookie перестают приниматься. Без этого украденная сессия жила бы весь срок
    и после смены пароля.
    """
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:12]


def issue_session(email: str, *, days: int, epoch: str = "", now: Optional[float] = None) -> str:
    """Выдаёт значение cookie: почта, срок, отпечаток пароля, разовый идентификатор."""
    now = time.time() if now is None else now
    body = {
        "email": email,
        "exp": int(now + days * 86400),
        "epoch": epoch,
        "sid": secrets.token_urlsafe(9),
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    packed = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{packed}.{_sign(raw)}"


def read_session(cookie: str, *, now: Optional[float] = None) -> Optional[dict]:
    """Проверяет подпись и срок. Возвращает содержимое или None."""
    # 4 КБ — потолок cookie по RFC 6265; всё, что длиннее, разбирать незачем.
    if not cookie or "." not in cookie or len(cookie) > 4096:
        return None
    packed, signature = cookie.rsplit(".", 1)
    # compare_digest на строках с не-ASCII бросает TypeError, а не возвращает
    # False. Подпись — всегда base64url, поэтому что угодно другое отсекаем
    # сразу: иначе подделанная cookie давала бы 500 вместо отказа.
    if not signature.isascii():
        return None
    try:
        raw = base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(_sign(raw), signature):
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    now = time.time() if now is None else now
    if int(body.get("exp", 0)) <= now:
        return None
    return body


# ------------------------------------------------- ограничение попыток

@dataclass
class _Bucket:
    failures: int = 0
    blocked_until: float = 0.0
    history: List[float] = field(default_factory=list)


class LoginThrottle:
    """Ограничитель попыток входа.

    Хранится в памяти процесса: пережить перезапуск он не должен, а
    распределённым приложение не является — планировщик и так требует ровно
    одного инстанса. Ключ — IP, а не почта: иначе перебор пароля к одной
    известной почте просто блокирует её владельца.
    """

    # Потолок числа отслеживаемых ключей. Без него поток запросов с разных
    # адресов раздувает словарь, пока процессу не станет плохо.
    MAX_KEYS = 10_000

    def __init__(self, max_attempts: int = 5, window_sec: int = 900, block_sec: int = 900):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.block_sec = block_sec
        self._buckets: Dict[str, _Bucket] = {}

    def _purge(self, now: float) -> None:
        """Выбрасывает записи, которые больше ни на что не влияют."""
        dead = [
            key for key, b in self._buckets.items()
            if b.blocked_until <= now and not [t for t in b.history if now - t < self.window_sec]
        ]
        for key in dead:
            self._buckets.pop(key, None)

    def _bucket(self, key: str, *, create: bool, now: float) -> Optional[_Bucket]:
        """Чтение не создаёт запись: иначе один GET раздувает словарь."""
        existing = self._buckets.get(key)
        if existing is not None or not create:
            return existing
        if len(self._buckets) >= self.MAX_KEYS:
            self._purge(now)
        if len(self._buckets) >= self.MAX_KEYS:
            # Словарь всё ещё полон — значит идёт распределённый перебор.
            # Ключи не добавляем, но и не притворяемся, что всё хорошо.
            logger.warning("[auth] таблица попыток входа переполнена, новые адреса не отслеживаются")
            return None
        bucket = _Bucket()
        self._buckets[key] = bucket
        return bucket

    def blocked_for(self, key: str, *, now: Optional[float] = None) -> int:
        """Сколько секунд осталось ждать. 0 — можно пробовать."""
        now = time.time() if now is None else now
        bucket = self._bucket(key, create=False, now=now)
        if bucket is None:
            return 0
        return max(0, int(bucket.blocked_until - now))

    def register_failure(self, key: str, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        bucket = self._bucket(key, create=True, now=now)
        if bucket is None:
            return 0
        bucket.history = [t for t in bucket.history if now - t < self.window_sec]
        bucket.history.append(now)
        bucket.failures = len(bucket.history)
        if bucket.failures >= self.max_attempts:
            bucket.blocked_until = now + self.block_sec
            bucket.history.clear()
            return self.block_sec
        return 0

    def register_success(self, key: str) -> None:
        self._buckets.pop(key, None)

    def attempts_left(self, key: str, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        bucket = self._bucket(key, create=False, now=now)
        if bucket is None:
            return self.max_attempts
        recent = [t for t in bucket.history if now - t < self.window_sec]
        return max(0, self.max_attempts - len(recent))


# ------------------------------------------------------------- журнал

def log_login(*, ok: bool, email: str, ip: str, reason: str = "") -> None:
    """Журнал входов. Пароль сюда не попадает ни при каких обстоятельствах."""
    safe_email = (email or "").strip().lower()
    if ok:
        logger.info(f"[auth] вход: {safe_email} c {ip}")
    else:
        logger.warning(f"[auth] отказ: {safe_email or '—'} c {ip} — {reason}")


# --------------------------------------------------------- конфигурация

@dataclass
class AuthConfig:
    email: str
    password_hash: str
    session_days: int
    max_attempts: int
    secure_cookie: bool

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password_hash)


def load_config() -> AuthConfig:
    return AuthConfig(
        email=os.getenv("AUTH_EMAIL", "").strip().lower(),
        password_hash=os.getenv("AUTH_PASSWORD_HASH", "").strip(),
        # 7 дней — решение заказчика: входить раз в неделю не обременительно,
        # а украденный ноутбук не даёт доступ на месяц.
        session_days=int(os.getenv("SESSION_DAYS", "7") or 7),
        max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "5") or 5),
        # За nginx с https ставить 1. По умолчанию 0: приложение сейчас
        # отдаётся по http, и cookie с Secure просто не долетит.
        secure_cookie=os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    )


def check_credentials(email: str, password: str, config: AuthConfig) -> Tuple[bool, str]:
    """Возвращает (успех, причина отказа для журнала)."""
    if not config.configured:
        return False, "вход не настроен"
    if (email or "").strip().lower() != config.email:
        # Пароль проверяем всё равно: иначе по времени ответа видно,
        # существует ли такая почта.
        verify_password(password, config.password_hash)
        return False, "неизвестная почта"
    if not verify_password(password, config.password_hash):
        return False, "неверный пароль"
    return True, ""
