# Prometheus

## Что изменилось

`/metrics` закрыт Basic-авторизацией. Раньше он отдавался кому угодно, и наружу
уходили состав источников, объёмы потребности, средние ставки и расход LLM.

Поэтому при развёртывании нужен **один дополнительный шаг**: положить пароль
в файл, который читает Prometheus.

## Развёртывание

```bash
mkdir -p secrets && printf '%s' "$WEB_PASSWORD" > secrets/web_password && chmod 600 secrets/web_password
```

Пароль должен совпадать с `WEB_PASSWORD` из `.env`. Логин задан в
`prometheus.yml` (`basic_auth.username`) — если `WEB_USER` не `admin`, поправьте
и там.

Каталог `secrets/` в `.gitignore`, в репозиторий не попадает.

## Если забыть про этот шаг

Контейнер `prometheus` не поднимется: он не найдёт `/etc/prometheus/web_password`.
Это намеренно — молча собирать метрики без авторизации больше нельзя. В логах
будет `error loading config ... password_file`.

Если приложение уже работает, а Prometheus падает — метрики просто не собираются,
на работу навигатора это не влияет.

## Проверить

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/metrics
```

Должно быть `401`. С паролем:

```bash
curl -s -u "$WEB_USER:$WEB_PASSWORD" http://localhost:8000/metrics | head -5
```

Цель в Prometheus — `http://localhost:9090/targets`, состояние `UP`.
