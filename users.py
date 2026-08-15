"""Сотрудники, роли и журнал входов.

Заменяет единственную учётку из переменных окружения. Решения заказчика,
на которых построен модуль (docs/OPEN-QUESTIONS.md, C1 и C1a):

  * учётные записи заводит сам Навигатор, без оглядки на другие продукты;
  * роли на старте две — рекрутер и администратор;
  * пароль выдаёт администратор ВРЕМЕННЫМ: он живёт 48 часов и работает ровно
    один раз, при первом входе человек задаёт свой. Так администратор
    перестаёт знать чужой пароль, и запись «сделал такой-то» в истории
    означает именно того, чьё имя стоит;
  * при увольнении учётка отключается, а не удаляется: иначе история правок
    осталась бы с автором, которого нет ни в одной таблице.

Проверка пароля и хеширование живут в auth.py — здесь только работа с базой.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from loguru import logger

import auth

ROLE_RECRUITER = "recruiter"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_RECRUITER, ROLE_ADMIN)

# Сколько живёт временный пароль. 48 часов — решение заказчика: за это время
# человек успевает войти, а забытая в переписке строка протухает сама.
TEMP_PASSWORD_TTL_HOURS = int(os.getenv("TEMP_PASSWORD_TTL_HOURS", "48") or 48)


# Хеш пароля, которого не существует. Нужен, чтобы проверка несуществующей
# почты занимала столько же времени, сколько проверка настоящей: иначе по
# времени ответа видно, заведён ли такой сотрудник. Формат корректный —
# иначе verify_password пишет в лог ложную ошибку о повреждённом хеше.
_DUMMY_HASH = (
    "pbkdf2_sha256$600000$+vv1G2lx6JQFPzaX3dX4oA==$bky0oETMAzRMyy+/7bTooVF5i5UIIuwWpKkNzU256Xc="
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class User:
    user_id: str
    email: str
    name: str
    password_hash: str
    must_change_password: bool
    password_expires_at: Optional[str]
    is_active: bool
    roles: Tuple[str, ...] = ()
    last_login_at: Optional[str] = None
    created_at: str = ""
    created_by: str = ""
    disabled_at: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    @property
    def title(self) -> str:
        return self.name or self.email

    def password_expired(self, *, now: Optional[datetime] = None) -> bool:
        if not self.password_expires_at:
            return False
        now = now or datetime.now()
        try:
            return datetime.fromisoformat(self.password_expires_at) <= now
        except ValueError:
            return False


# ------------------------------------------------------------ чтение

def _row_to_user(conn: sqlite3.Connection, row: sqlite3.Row) -> User:
    roles = tuple(
        r["role"] for r in conn.execute(
            "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (row["user_id"],)
        )
    )
    return User(
        user_id=row["user_id"],
        email=row["email"],
        name=row["name"],
        password_hash=row["password_hash"],
        must_change_password=bool(row["must_change_password"]),
        password_expires_at=row["password_expires_at"],
        is_active=bool(row["is_active"]),
        roles=roles,
        last_login_at=row["last_login_at"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        disabled_at=row["disabled_at"],
    )


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_by_email(conn: sqlite3.Connection, email: str) -> Optional[User]:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
    ).fetchone()
    return _row_to_user(conn, row) if row else None


def get(conn: sqlite3.Connection, user_id: str) -> Optional[User]:
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return _row_to_user(conn, row) if row else None


def list_users(conn: sqlite3.Connection, *, include_disabled: bool = True) -> List[User]:
    where = "" if include_disabled else " WHERE is_active = 1"
    rows = conn.execute(
        f"SELECT * FROM users{where} ORDER BY is_active DESC, name, email"
    ).fetchall()
    return [_row_to_user(conn, row) for row in rows]


def count_admins(conn: sqlite3.Connection) -> int:
    """Сколько действующих администраторов. Последнего отключать нельзя."""
    return conn.execute(
        "SELECT COUNT(*) FROM users u JOIN user_roles r ON r.user_id = u.user_id "
        "WHERE r.role = ? AND u.is_active = 1", (ROLE_ADMIN,),
    ).fetchone()[0]


# ------------------------------------------------------------ запись

def next_user_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT user_id FROM users ORDER BY user_id DESC LIMIT 1").fetchone()
    last = int(row["user_id"].split("-")[-1]) if row else 0
    return f"USR-{last + 1:04d}"


def create_user(
        conn: sqlite3.Connection,
        *,
        email: str,
        name: str,
        role: str,
        temp_password: str,
        created_by: str,
        ttl_hours: int = TEMP_PASSWORD_TTL_HOURS,
) -> User:
    """Заводит сотрудника с временным паролем."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("Нужна рабочая почта")
    if role not in ALL_ROLES:
        raise ValueError(f"Неизвестная роль: {role}")
    if get_by_email(conn, email):
        raise ValueError(f"Сотрудник с почтой {email} уже заведён")

    user_id = next_user_id(conn)
    expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users (user_id, email, name, password_hash, must_change_password, "
        "password_expires_at, is_active, created_at, created_by) "
        "VALUES (?, ?, ?, ?, 1, ?, 1, ?, ?)",
        (user_id, email, name.strip(), auth.hash_password(temp_password), expires,
         _now(), created_by),
    )
    conn.execute(
        "INSERT INTO user_roles (user_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?)",
        (user_id, role, _now(), created_by),
    )
    logger.info(f"[users] заведён {email} ({role}), автор {created_by}")
    return get(conn, user_id)


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    """Человек задаёт свой пароль: временный статус и срок годности снимаются."""
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, "
        "password_expires_at = NULL WHERE user_id = ?",
        (auth.hash_password(password), user_id),
    )
    logger.info(f"[users] {user_id} задал новый пароль")


def issue_temp_password(
        conn: sqlite3.Connection,
        user_id: str,
        *,
        temp_password: str,
        by: str,
        ttl_hours: int = TEMP_PASSWORD_TTL_HOURS,
) -> None:
    """Сброс пароля администратором: снова временный, снова со сроком."""
    expires = (datetime.now() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1, "
        "password_expires_at = ? WHERE user_id = ?",
        (auth.hash_password(temp_password), expires, user_id),
    )
    logger.info(f"[users] сброшен пароль {user_id}, автор {by}")


def set_active(conn: sqlite3.Connection, user_id: str, active: bool, *, by: str) -> None:
    """Отключение при увольнении. Запись остаётся — история правок ссылается на неё."""
    if active:
        conn.execute(
            "UPDATE users SET is_active = 1, disabled_at = NULL, disabled_by = '' "
            "WHERE user_id = ?", (user_id,),
        )
    else:
        conn.execute(
            "UPDATE users SET is_active = 0, disabled_at = ?, disabled_by = ? "
            "WHERE user_id = ?", (_now(), by, user_id),
        )
    logger.info(f"[users] {user_id} {'включён' if active else 'отключён'}, автор {by}")


def set_role(conn: sqlite3.Connection, user_id: str, role: str, *, by: str) -> None:
    if role not in ALL_ROLES:
        raise ValueError(f"Неизвестная роль: {role}")
    conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO user_roles (user_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?)",
        (user_id, role, _now(), by),
    )
    logger.info(f"[users] {user_id} → роль {role}, автор {by}")


def touch_login(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute("UPDATE users SET last_login_at = ? WHERE user_id = ?", (_now(), user_id))


# ------------------------------------------------------------ вход

def authenticate(conn: sqlite3.Connection, email: str, password: str) -> Tuple[Optional[User], str]:
    """Возвращает (пользователь, причина отказа).

    Пароль проверяется всегда, даже когда почта неизвестна: иначе по времени
    ответа видно, заведён ли такой сотрудник.
    """
    user = get_by_email(conn, email)
    if user is None:
        auth.verify_password(password, _DUMMY_HASH)
        return None, "неизвестная почта"
    if not user.is_active:
        auth.verify_password(password, user.password_hash)
        return None, "учётная запись отключена"
    if not auth.verify_password(password, user.password_hash):
        return None, "неверный пароль"
    if user.password_expired():
        return None, "срок временного пароля истёк"
    return user, ""


def bootstrap_from_env(conn: sqlite3.Connection) -> Optional[User]:
    """Первый администратор из окружения — чтобы не оказаться без входа.

    Пока в базе нет ни одного пользователя, но заданы AUTH_EMAIL и
    AUTH_PASSWORD_HASH, заводим по ним администратора. Пароль уже хеширован,
    менять его при первом входе не требуем: он не временный.
    """
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return None
    email = normalize_email(os.getenv("AUTH_EMAIL", ""))
    password_hash = os.getenv("AUTH_PASSWORD_HASH", "").strip()
    if not email or not password_hash:
        return None

    user_id = next_user_id(conn)
    conn.execute(
        "INSERT INTO users (user_id, email, name, password_hash, must_change_password, "
        "is_active, created_at, created_by) VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
        (user_id, email, "Администратор", password_hash, _now(), "окружение"),
    )
    conn.execute(
        "INSERT INTO user_roles (user_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?)",
        (user_id, ROLE_ADMIN, _now(), "окружение"),
    )
    logger.info(f"[users] первый администратор заведён из окружения: {email}")
    return get(conn, user_id)


# ---------------------------------------------------------- журнал

def record_login(
        conn: sqlite3.Connection,
        *,
        ok: bool,
        email: str,
        ip: str,
        reason: str = "",
        user_agent: str = "",
) -> None:
    conn.execute(
        "INSERT INTO login_audit (at, email, ip, ok, reason, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), normalize_email(email), ip, 1 if ok else 0, reason, user_agent[:200]),
    )


def recent_logins(conn: sqlite3.Connection, limit: int = 100) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM login_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,)
    ))
