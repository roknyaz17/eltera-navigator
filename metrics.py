"""
Prometheus-метрики для Eltrea Bot.

Делятся на 4 группы:
1. UI-телеметрия — что менеджеры ищут на странице.
2. Snapshot текущего состояния (gauges) — обновляются перед scrape.
3. Pipeline — runs, durations, добавлено/обновлено/деактивировано.
4. LLM — расход токенов и стоимость.

Конвенция: все метрики начинаются с `eltrea_*`.
"""

from prometheus_client import Counter, Gauge, Histogram, Info

# ============================================================ Info
APP_INFO = Info("eltrea_app", "Базовая информация о приложении")

# ============================================================ UI
UI_PAGE_VIEWS = Counter(
    "eltrea_page_views_total",
    "Просмотры страницы /vacancies",
)

UI_FILTER_USED = Counter(
    "eltrea_filter_used_total",
    "Сколько раз использовался каждый фильтр на /vacancies",
    ["filter"],  # source, city, q, gender, age, geo, sb_policy, max_shifts, is_active
)

UI_FILTER_VALUE = Counter(
    "eltrea_filter_value_total",
    "Конкретные значения фильтров (для select-полей)",
    ["filter", "value"],
)

UI_SORT_USED = Counter(
    "eltrea_sort_used_total",
    "По какой колонке отсортировали",
    ["column", "order"],
)

# ============================================================ Snapshot
SNAP_VACANCIES = Gauge(
    "eltrea_vacancies_in_table",
    "Количество вакансий в целевой таблице",
    ["source", "is_active"],
)

SNAP_NEED_TOTAL = Gauge(
    "eltrea_vacancies_total_need",
    "Суммарная потребность по источнику (need_total)",
    ["source"],
)

SNAP_AVG_RATE = Gauge(
    "eltrea_vacancies_avg_shift_rate",
    "Средняя ставка по источнику (₽/смена)",
    ["source"],
)

SNAP_SHIFT_TYPE = Gauge(
    "eltrea_vacancies_by_shift_type",
    "Распределение по типу смены",
    ["shift_type"],  # day / night / mixed
)

SNAP_MIN_SHIFTS = Gauge(
    "eltrea_vacancies_by_min_shifts",
    "Распределение по min_shifts",
    ["min_shifts"],  # 15 / 20 / 30 / 45 / other
)

# ============================================================ Pipeline
PIPE_RUNS = Counter(
    "eltrea_pipeline_runs_total",
    "Запуски пайплайна",
    ["source", "status"],  # status: success / failed
)

PIPE_DURATION = Histogram(
    "eltrea_pipeline_duration_seconds",
    "Длительность прогона источника",
    ["source"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600),
)

PIPE_ADDED = Counter(
    "eltrea_pipeline_added_total",
    "Добавленных вакансий за прогон",
    ["source"],
)

PIPE_UPDATED = Counter(
    "eltrea_pipeline_updated_total",
    "Обновлённых вакансий за прогон",
    ["source"],
)

PIPE_SKIPPED = Counter(
    "eltrea_pipeline_skipped_total",
    "Пропущенных дубликатов за прогон",
    ["source"],
)

PIPE_DEACTIVATED = Counter(
    "eltrea_pipeline_deactivated_total",
    "Деактивированных вакансий за прогон",
    ["source"],
)

PIPE_NEEDS_REVIEW = Counter(
    "eltrea_pipeline_needs_review_total",
    "Сообщений в NEEDS_MANUAL_REVIEW",
    ["source"],
)

# ============================================================ LLM
LLM_REQUESTS = Counter(
    "eltrea_llm_requests_total",
    "LLM-запросов",
    ["source"],
)

LLM_TOKENS = Counter(
    "eltrea_llm_tokens_total",
    "Использованных токенов",
    ["source", "type"],  # type: input / output
)

LLM_COST = Counter(
    "eltrea_llm_cost_rub_total",
    "Стоимость LLM-вызовов, ₽",
    ["source"],
)

# ============================================================ Registry
REG_REQUESTS = Counter(
    "eltrea_registry_requests_total",
    "Входящие заявки реестра по итогу приёма",
    ["source", "kind"],  # kind: new / changed / unchanged / failed
)

REG_LLM_CALLS_SAVED = Counter(
    "eltrea_registry_llm_calls_saved_total",
    "Разборов, которых удалось избежать: содержимое заявки не изменилось",
    ["source"],
)

REG_POSITIONS = Gauge(
    "eltrea_registry_positions",
    "Позиций в реестре",
    ["source", "is_active"],
)

REG_EMPTY_FIELDS = Gauge(
    "eltrea_registry_empty_field_ratio",
    "Доля активных позиций, где поле не заполнено",
    ["field"],
)

REG_DICT_PENDING = Gauge(
    "eltrea_registry_dictionary_pending",
    "Размер очереди на подтверждение в справочниках",
    ["kind"],
)

# ============================================================ Cache
CACHE_HITS = Counter(
    "eltrea_cache_hits_total",
    "Hits в кеше вакансий (стрица отдалась мгновенно)",
)

CACHE_REFRESHES = Counter(
    "eltrea_cache_refreshes_total",
    "Сколько раз кеш перечитывал Sheets",
    ["mode"],  # mode: foreground / background
)

CACHE_FETCH_FAILURES = Counter(
    "eltrea_cache_fetch_failures_total",
    "Ошибки чтения Sheets для кеша",
    ["reason"],  # timeout / exception
)
