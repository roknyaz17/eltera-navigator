"""Короткий способ дёрнуть ручку навигатора изнутри контейнера.

Нужен потому, что curl в образ не кладём, а токен живёт в окружении процесса,
а не в командной строке: в истории и в `ps` ему делать нечего.

    docker compose exec -T app python nav_call.py GET  /api/agent/crm
    docker compose exec -T app python nav_call.py POST /api/agent/crm/deliver '{}'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


def main() -> int:
    method = sys.argv[1].upper()
    path = sys.argv[2]
    body = sys.argv[3] if len(sys.argv) > 3 else None

    token = os.getenv("AGENT_API_TOKEN", "").strip()
    if not token:
        print("AGENT_API_TOKEN пуст — ручки агента выключены")
        return 2

    headers = {"X-Agent-Token": token}
    data = None
    if body is not None:
        data = body.encode("utf-8")
        headers["Content-Type"] = "application/json"

    # Кодируем путь: в запросах встречаются русские значения с пробелами
    # («Тверская область»), а urllib отказывается их отправлять как есть.
    if "?" in path:
        route, _, query = path.partition("?")
        pairs = [p.partition("=") for p in query.split("&") if p]
        query = "&".join(f"{k}={urllib.parse.quote(v)}" for k, _, v in pairs)
        path = f"{route}?{query}"

    request = urllib.request.Request(f"{BASE}{path}", data=data,
                                     headers=headers, method=method)
    try:
        answer = urllib.request.urlopen(request, timeout=60).read().decode()
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: {error.read().decode()[:600]}")
        return 1

    try:
        print(json.dumps(json.loads(answer), ensure_ascii=False, indent=2))
    except ValueError:
        print(answer[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
