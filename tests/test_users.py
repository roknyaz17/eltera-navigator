"""Сотрудники, роли и журнал входов.

Проверяется то, ради чего единственная учётка из окружения заменялась на
таблицу людей (C1 и C1a): пароль, который администратор выдал, живёт недолго
и работает один раз; уволенный не входит, но не исчезает из истории; роль у
человека ровно одна; каждая попытка входа остаётся в журнале — без пароля.

Сети и ключей здесь нет: база настоящая, но временная, а всё остальное —
чистая логика модуля.
"""

import io
import sqlite3
from datetime import datetime, timedelta

import pytest
from loguru import logger

import auth
import users


TEMP = "временный-пароль-1234"
OWN = "свой-пароль-5678"
ADMIN = "USR-0001"


def make_user(
        conn: sqlite3.Connection,
        email: str = "anna@example.com",
        *,
        name: str = "Анна Иванова",
        role: str = users.ROLE_RECRUITER,
        temp_password: str = TEMP,
) -> users.User:
    return users.create_user(
        conn, email=email, name=name, role=role,
        temp_password=temp_password, created_by=ADMIN,
    )


@pytest.fixture
def log_stream():
    """Ловит записи loguru: часть проверок — про то, чего в логе быть не должно."""
    stream = io.StringIO()
    sink = logger.add(stream, level="DEBUG")
    try:
        yield stream
    finally:
        logger.remove(sink)


# ------------------------------------------------------- заведение сотрудника

def test_new_user_starts_temporary(conn):
    """Свежая учётка обязана быть «одноразовой».

    Если бы пароль сразу оказался постоянным, администратор навсегда знал бы
    чужой пароль — и подпись «сделал такой-то» в истории ничего не значила бы.
    """
    user = make_user(conn)
    assert user.must_change_password is True
    assert user.password_expires_at, "срок годности не выставлен — пароль вечен"
    assert user.roles == (users.ROLE_RECRUITER,)
    assert user.is_active is True
    assert user.created_by == ADMIN

    left = datetime.fromisoformat(user.password_expires_at) - datetime.now()
    assert timedelta(hours=47) < left <= timedelta(hours=users.TEMP_PASSWORD_TTL_HOURS)


def test_email_is_stored_in_lower_case(conn):
    """Почта — ключ входа. Разный регистр не должен плодить двойников."""
    user = make_user(conn, "  Anna@Example.COM  ")
    assert user.email == "anna@example.com"
    assert users.get_by_email(conn, "ANNA@example.com").user_id == user.user_id


def test_duplicate_email_is_rejected(conn):
    """Две учётки на одну почту — это два разных «кто это сделал» у одного человека."""
    make_user(conn, "anna@example.com")
    with pytest.raises(ValueError):
        make_user(conn, "ANNA@Example.com", name="Другая Анна")
    assert len(users.list_users(conn)) == 1


@pytest.mark.parametrize("email", ["", "   ", "анна", "anna.example.com"])
def test_broken_email_is_rejected(conn, email):
    """Почта без собаки — это опечатка: человек потом просто не сможет войти."""
    with pytest.raises(ValueError):
        make_user(conn, email)


def test_unknown_role_is_rejected(conn):
    """Роль решает, что человеку закрыто. Опечатка в ней не должна тихо пройти."""
    with pytest.raises(ValueError):
        make_user(conn, role="director")
    with pytest.raises(ValueError):
        make_user(conn, role="Рекрутер")


# ------------------------------------------------------------------- вход

def test_temp_password_lets_in_and_asks_to_change(conn):
    """Временный пароль обязан пускать — но ровно до смены пароля."""
    make_user(conn)
    user, reason = users.authenticate(conn, "Anna@Example.com", TEMP)
    assert reason == ""
    assert user is not None
    assert user.must_change_password is True, "вошёл и остался с чужим паролем"


def test_wrong_password_does_not_let_in(conn):
    make_user(conn)
    user, reason = users.authenticate(conn, "anna@example.com", "не тот пароль")
    assert user is None
    assert reason == "неверный пароль"


def test_unknown_email_is_refused_without_error_in_log(conn, log_stream):
    """Отказ по неизвестной почте — это норма, а не поломка.

    Заглушечный хеш нужен, чтобы по времени ответа нельзя было перебрать
    список сотрудников. Но если он битый, verify_password сыплет в лог
    ошибку о повреждённом хеше — и дежурный ищет несуществующую аварию.
    """
    user, reason = users.authenticate(conn, "chuzhoy@example.com", "какой-то пароль")
    assert user is None
    assert reason == "неизвестная почта"

    written = log_stream.getvalue()
    assert "ERROR" not in written, f"отказ уронил ошибку в лог: {written}"
    assert "повреждён" not in written


def test_expired_temp_password_does_not_let_in(conn):
    """Пароль, забытый в переписке, должен протухать сам.

    48 часов — весь смысл временного пароля: иначе строка из чата остаётся
    рабочим входом бессрочно.
    """
    user = make_user(conn)
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE users SET password_expires_at = ? WHERE user_id = ?", (past, user.user_id)
    )

    got, reason = users.authenticate(conn, "anna@example.com", TEMP)
    assert got is None
    assert reason == "срок временного пароля истёк"


def test_set_password_makes_account_permanent(conn):
    """Человек задал свой пароль — просить сменить и протухать больше нечему."""
    user = make_user(conn)
    users.set_password(conn, user.user_id, OWN)

    fresh = users.get(conn, user.user_id)
    assert fresh.must_change_password is False
    assert fresh.password_expires_at is None
    assert fresh.password_expired() is False

    assert users.authenticate(conn, "anna@example.com", OWN)[0] is not None
    assert users.authenticate(conn, "anna@example.com", TEMP)[0] is None, (
        "старый временный пароль продолжает пускать"
    )


def test_issue_temp_password_returns_account_to_temporary(conn):
    """Сброс пароля — это выдача нового временного, а не постоянного.

    Иначе после каждого «забыл пароль» администратор снова знал бы чужой
    рабочий пароль.
    """
    user = make_user(conn)
    users.set_password(conn, user.user_id, OWN)
    users.issue_temp_password(conn, user.user_id, temp_password="новый-времен-9012", by=ADMIN)

    fresh = users.get(conn, user.user_id)
    assert fresh.must_change_password is True
    assert fresh.password_expires_at, "сброшенный пароль остался бессрочным"
    assert users.authenticate(conn, "anna@example.com", OWN)[0] is None
    assert users.authenticate(conn, "anna@example.com", "новый-времен-9012")[0] is not None


# --------------------------------------------------- отключение и роли

def test_disabled_user_cannot_log_in_but_stays(conn):
    """Увольнение закрывает вход, но запись остаётся: на неё ссылается история правок."""
    user = make_user(conn)
    users.set_active(conn, user.user_id, False, by=ADMIN)

    got, reason = users.authenticate(conn, "anna@example.com", TEMP)
    assert got is None
    assert reason == "учётная запись отключена"
    assert users.get(conn, user.user_id) is not None, "учётка удалена — история осиротела"

    # Вернулся на работу: вход открывается обратно, заводить заново не нужно.
    users.set_active(conn, user.user_id, True, by=ADMIN)
    assert users.authenticate(conn, "anna@example.com", TEMP)[0] is not None
    assert users.get(conn, user.user_id).disabled_at is None


def test_role_change_replaces_the_old_one(conn):
    """Роль должна быть одна.

    Накопись у человека вторая, права считались бы по объединению — и
    бывший администратор остался бы администратором после понижения.
    """
    user = make_user(conn, role=users.ROLE_ADMIN)
    users.set_role(conn, user.user_id, users.ROLE_RECRUITER, by=ADMIN)

    fresh = users.get(conn, user.user_id)
    assert fresh.roles == (users.ROLE_RECRUITER,)
    assert fresh.is_admin is False


def test_count_admins_counts_only_active(conn):
    """По этому счётчику решают, можно ли отключить очередного администратора.

    Посчитай он отключённых — последнего действующего разрешили бы убрать, и
    в Навигатор стало бы некому войти.
    """
    first = make_user(conn, "admin1@example.com", role=users.ROLE_ADMIN)
    make_user(conn, "admin2@example.com", role=users.ROLE_ADMIN)
    make_user(conn, "recruiter@example.com", role=users.ROLE_RECRUITER)
    assert users.count_admins(conn) == 2

    users.set_active(conn, first.user_id, False, by=ADMIN)
    assert users.count_admins(conn) == 1


# --------------------------------------------------- первый администратор

def test_bootstrap_creates_admin_only_on_empty_base(conn, monkeypatch):
    """Пустая база не должна оказаться без входа — но и переписывать людей нельзя.

    Второй вызов обязан промолчать: иначе почта из окружения раз за разом
    возвращала бы себе доступ поверх того, что настроили руками.
    """
    monkeypatch.setenv("AUTH_EMAIL", "  Boss@Example.COM ")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", auth.hash_password(OWN))

    boss = users.bootstrap_from_env(conn)
    assert boss is not None
    assert boss.email == "boss@example.com"
    assert boss.is_admin is True
    # Пароль из окружения уже постоянный: менять его при первом входе незачем.
    assert boss.must_change_password is False
    assert users.authenticate(conn, "boss@example.com", OWN)[0] is not None

    assert users.bootstrap_from_env(conn) is None
    assert len(users.list_users(conn)) == 1


def test_bootstrap_skips_base_with_people(conn, monkeypatch):
    """Есть хоть один сотрудник — окружение больше не имеет права заводить вход."""
    make_user(conn)
    monkeypatch.setenv("AUTH_EMAIL", "boss@example.com")
    monkeypatch.setenv("AUTH_PASSWORD_HASH", auth.hash_password(OWN))
    assert users.bootstrap_from_env(conn) is None


def test_bootstrap_without_env_does_nothing(conn, monkeypatch):
    """Без настроек в окружении лучше пустая база, чем учётка с пустым паролем."""
    monkeypatch.delenv("AUTH_EMAIL", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_HASH", raising=False)
    assert users.bootstrap_from_env(conn) is None
    assert users.list_users(conn) == []


# ------------------------------------------------------------------ журнал

def test_login_audit_keeps_both_outcomes(conn):
    """Журнал нужен ради неудачных попыток: по ним видно перебор."""
    users.record_login(conn, ok=True, email="Anna@Example.com", ip="10.0.0.1")
    users.record_login(conn, ok=False, email="chuzhoy@example.com", ip="10.0.0.2",
                       reason="неизвестная почта")

    rows = users.recent_logins(conn)
    assert len(rows) == 2
    assert {row["ok"] for row in rows} == {0, 1}
    # Почта в журнале приведена к тому же виду, что и в учётках, иначе поиск
    # по человеку теряет часть его попыток.
    assert "Anna@Example.com" not in [row["email"] for row in rows]
    assert "anna@example.com" in [row["email"] for row in rows]


def test_password_never_lands_in_the_audit(conn, log_stream):
    """Журнал читают люди и он уезжает в бэкапы: пароля там быть не должно."""
    make_user(conn, temp_password="секретное-значение-1234")
    users.authenticate(conn, "anna@example.com", "секретное-значение-1234")
    users.record_login(conn, ok=False, email="anna@example.com", ip="10.0.0.1",
                       reason="неверный пароль", user_agent="Mozilla/5.0")

    dumped = "\n".join(
        " ".join(str(value) for value in tuple(row)) for row in users.recent_logins(conn)
    )
    assert "секретное-значение-1234" not in dumped
    assert "секретное-значение-1234" not in log_stream.getvalue()


def test_recent_logins_are_newest_first(conn):
    """Разбор инцидента начинается с последних попыток, а не с прошлогодних."""
    for i in range(3):
        users.record_login(conn, ok=False, email=f"user{i}@example.com", ip="10.0.0.1",
                           reason="неверный пароль")

    rows = users.recent_logins(conn)
    assert [row["email"] for row in rows] == [
        "user2@example.com", "user1@example.com", "user0@example.com",
    ]


def test_recent_logins_respects_limit(conn):
    """Журнал растёт бесконечно — страница не должна тянуть его целиком."""
    for i in range(10):
        users.record_login(conn, ok=True, email=f"user{i}@example.com", ip="10.0.0.1")
    assert len(users.recent_logins(conn, limit=3)) == 3


# ----------------------------------------------------------- нумерация

def test_user_ids_are_sequential(conn):
    assert users.next_user_id(conn) == "USR-0001"
    assert make_user(conn, "a@example.com").user_id == "USR-0001"
    assert make_user(conn, "b@example.com").user_id == "USR-0002"


def test_disabling_does_not_free_the_number(conn):
    """Номер выдаётся один раз.

    Переиспользуй его после увольнения — и правки уволенного оказались бы
    подписаны именем новичка.
    """
    first = make_user(conn, "a@example.com")
    make_user(conn, "b@example.com")
    users.set_active(conn, first.user_id, False, by=ADMIN)

    assert users.next_user_id(conn) == "USR-0003"
    assert make_user(conn, "c@example.com").user_id == "USR-0003"
