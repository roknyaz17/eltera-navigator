"""Хеш пароля для входа в Навигатор.

    python scripts/set_password.py                    # спросит пароль скрыто
    python scripts/set_password.py --check            # проверить, что пароль подходит к текущему хешу
    python scripts/set_password.py --generate         # придумать стойкий пароль и сразу захешировать

Пароль не передаётся аргументом командной строки намеренно: аргументы видны
в `ps` другим пользователям машины и попадают в историю оболочки.

Вывод — строка для `AUTH_PASSWORD_HASH` в `.env`. Сам пароль нигде не
сохраняется: ни в файл, ни в лог.
"""

import argparse
import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import hash_password, verify_password

MIN_LENGTH = 12


def ask_password(confirm: bool = True) -> str:
    password = getpass.getpass("Пароль: ")
    if not password:
        raise SystemExit("Пустой пароль")
    if len(password) < MIN_LENGTH:
        raise SystemExit(f"Слишком короткий пароль: минимум {MIN_LENGTH} символов")
    if confirm:
        again = getpass.getpass("Ещё раз: ")
        if password != again:
            raise SystemExit("Пароли не совпали")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(description="Хеш пароля для AUTH_PASSWORD_HASH")
    parser.add_argument("--check", action="store_true",
                        help="проверить пароль против AUTH_PASSWORD_HASH из окружения")
    parser.add_argument("--generate", action="store_true",
                        help="сгенерировать стойкий пароль и показать его один раз")
    parser.add_argument("--write-env", metavar="FILE", nargs="?", const=".env",
                        help="записать AUTH_PASSWORD_HASH прямо в .env (по умолчанию ./.env)")
    parser.add_argument("--email", default="",
                        help="заодно записать AUTH_EMAIL")
    args = parser.parse_args()

    if args.check:
        encoded = os.getenv("AUTH_PASSWORD_HASH", "").strip()
        if not encoded:
            raise SystemExit("AUTH_PASSWORD_HASH не задан в окружении")
        password = getpass.getpass("Пароль: ")
        print("подходит" if verify_password(password, encoded) else "НЕ подходит")
        return

    if args.generate:
        password = secrets.token_urlsafe(18)
        print("\nПароль (показывается один раз, сохраните в менеджере паролей):")
        print(f"\n    {password}\n")
    else:
        password = ask_password()

    encoded = hash_password(password)

    if args.write_env:
        # Пишем сами: копирование длинной строки хеша руками — это лишний шаг,
        # на котором легко потерять символ и потом гадать, почему не пускает.
        updates = {"AUTH_PASSWORD_HASH": encoded}
        if args.email:
            updates["AUTH_EMAIL"] = args.email.strip().lower()
        written = _write_env(args.write_env, updates)
        for name in written:
            print(f"{name} записан в {args.write_env}")
        print("\nПерезапустите приложение.")
        return

    print("Строка для .env:\n")
    print(f"AUTH_PASSWORD_HASH={encoded}\n")
    print("Рядом должны быть заданы:")
    print("    AUTH_EMAIL=<рабочая почта>")
    print("    SECRET_KEY=<openssl rand -hex 32>")
    print("\nПосле правки .env перезапустите приложение.")


def _write_env(path: str, updates: dict) -> list:
    """Правит .env на месте: строку с ключом заменяет, отсутствующую дописывает.

    Остальные строки, комментарии и порядок сохраняются — .env на сервере
    писали люди, и перетасовывать его ради одной переменной незачем.
    """
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    done = set()
    for i, line in enumerate(lines):
        for name, value in updates.items():
            if line.startswith(f"{name}=") and name not in done:
                lines[i] = f"{name}={value}"
                done.add(name)
    for name, value in updates.items():
        if name not in done:
            lines.append(f"{name}={value}")
            done.add(name)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return sorted(done)


if __name__ == "__main__":
    main()
