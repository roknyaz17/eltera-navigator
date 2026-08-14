import hashlib
import json
import os
import re
from datetime import date
from typing import List, Dict, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from loguru import logger

load_dotenv()

# Цены DeepSeek V4 Flash (₽/млн токенов). При смене модели — переопределить через env.
PRICE_INPUT_RUB_PER_MTOK = float(os.getenv("LLM_PRICE_INPUT_RUB_PER_MTOK", "18.9"))
PRICE_OUTPUT_RUB_PER_MTOK = float(os.getenv("LLM_PRICE_OUTPUT_RUB_PER_MTOK", "37.8"))


class TokenUsage:
    """Аккумулятор расхода токенов в пределах одного прогона/инстанса."""

    def __init__(self):
        self.input = 0
        self.output = 0
        self.requests = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input += input_tokens
        self.output += output_tokens
        self.requests += 1

    @property
    def cost_rub(self) -> float:
        return (
            self.input / 1_000_000 * PRICE_INPUT_RUB_PER_MTOK
            + self.output / 1_000_000 * PRICE_OUTPUT_RUB_PER_MTOK
        )

    def summary(self) -> str:
        return (
            f"requests={self.requests}, "
            f"in={self.input}, out={self.output}, "
            f"cost~{self.cost_rub:.2f} RUB"
        )


def _normalize_for_id(value) -> str:
    """Нормализация значения для построения детерминированного vacancy_id."""
    if value is None:
        return ""
    s = str(value).lower().strip()
    # Пунктуацию заменяем на пробел, чтобы "общ.Пушкино" == "общ. Пушкино"
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def make_vacancy_id(vac: Dict) -> str:
    """
    Детерминированный идентификатор вакансии.
    Хэш от нормализованного набора ключевых полей —
    у двух одинаковых по сути вакансий будет один и тот же id.

    shift_type подмешан, чтобы день и ночь у одного объекта
    (например, Миксит) считались разными вакансиями.
    """
    parts = [
        _normalize_for_id(vac.get("counterparty")),
        _normalize_for_id(vac.get("city")),
        _normalize_for_id(vac.get("vacancy_name")),
        _normalize_for_id(vac.get("object_name")),
        _normalize_for_id(vac.get("work_format")),
        _normalize_for_id(vac.get("shift_type")),
    ]
    key = "|".join(parts)
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


class VacancyParserService:
    """Сервис для парсинга вакансий из текста сообщений с помощью LLM."""

    # Административные поля, которые остаются пустыми для ручного заполнения.
    # vacancy_id из списка убран — теперь генерируем детерминированно (см. make_vacancy_id).
    ADMIN_FIELDS = [
        "status", "priority", "responsible_manager",
        "recruiter_comment", "sales_script", "objections",
        "market_rate", "market_deviation"
    ]

    SYSTEM_PROMPT = """Ты — парсер заявок на персонал. Если текст не содержит ни одной вакансии, верни [].

    ГЛАВНОЕ ПРАВИЛО: ты ИЗВЛЕКАЕШЬ то, что написано, и НИЧЕГО не додумываешь.
    Нет данных в тексте — поля просто нет в ответе. Не выводи значение из
    здравого смысла, из своих знаний о рынке или из типичных практик: пустое
    поле в реестре честнее, чем правдоподобная выдумка. Приведением значений к
    единому виду занимается отдельный модуль после тебя — тебе унифицировать
    формулировки НЕ нужно.

    ВАЖНО О ФОРМАТЕ ОТВЕТА:
    - Ответ должен быть СТРОГО валидным JSON-массивом, без markdown-обёрток ```json ... ```.
    - Включай в JSON ТОЛЬКО те поля, для которых значение прямо следует из текста. Все поля, которые ты оставил бы как null или false из-за отсутствия данных, ПРОПУСКАЙ совсем. Это критично и для длины ответа, и для достоверности реестра.

    Если вакансии есть, извлеки все отдельные позиции. Для каждой верни JSON-объект с подходящими полями из набора (если поле не указано в тексте – ПРОСТО НЕ ВКЛЮЧАЙ его в JSON):

    Поля для извлечения:
    counterparty, vacancy_name, vacancy_category, city, region, object_name, object_address,
    work_format, shift_type, schedule, min_shifts, shift_hours, shift_rate,
    duties, requirements, requires_tsd,
    gender, age_from, age_to, citizenship_requirements,
    need_men, need_women, need_couples,
    housing_available, housing_free, housing_deduction, housing_conditions,
    meals_available, meals_free, meals_deduction, meals_times_per_day,
    medical_book_required, medical_book_payer, can_start_without_medical_book,
    uniform_available, uniform_free, transport_paid, transport_terms,
    advantages, risks, sb_policy

    ПОЯСНЕНИЯ К ПОЛЯМ:
    - shift_type: одно из значений "day" / "night" / "mixed". "day" — только дневные смены, "night" — только ночные, "mixed" — обе. Если про смены в тексте ничего не сказано — ПРОПУСТИ поле, не ставь "day" по умолчанию.
    - min_shifts: число — минимальный срок вахты в сменах. Извлекай из фраз "вахта от 30 смен" (→ 30), "20/30/45 смен" (→ 20), "от 15 смен" (→ 15).
    - schedule: ОРИГИНАЛЬНАЯ строка про график как в тексте — "вахта от 30 смен", "6/1 по 12 ч", "2/2", и т.п. Можешь склеить несколько фраз через пробел.
    - shift_rate: ОРИГИНАЛЬНАЯ строка про ставку как в тексте, вместе с единицей измерения — "3498 р/смена", "320 р/час - 3 520 р/смена". Не пересчитывай час в смену и наоборот.
    - city / region: только если написаны в тексте. Регион по городу НЕ определяй — это делается по справочнику после тебя.
    - requires_tsd: true, если в тексте есть "ТСД", "опыт ТСД", "работа с ТСД". Если про ТСД не сказано — пропусти поле (false означало бы, что в заявке явно написано «ТСД не нужен»).
    - Все остальные поля-признаки (питание, проживание, медкнижка, спецодежда, транспорт) — по тому же принципу: true/false только когда об этом прямо сказано, иначе поля нет.
    - sb_policy: строка одного из вариантов:
        * "нет" — если "БЕЗ СБ", "Проверки СБ нет", "СБ нет";
        * "без судимостей" — если "строго без судимостей", "БЕЗ судимостей";
        * "без тяж.статей" — если "Без тяж.статей", "без тяж.суд.";
        * "лёгкие статьи допускаются" — если "лёгкие статьи допускаются", "Допускаются лёгкие статьи";
        * "проверка СБ" — если "Проверка СБ", "ПРОВЕРКА СБ" без уточнений;
        * "выборочная" — если "выборочная ПРОВЕРКА СБ";
        Если ничего не сказано — пропусти поле.

    ПРАВИЛА РАЗДЕЛЕНИЯ И ПОДСЧЁТА (они про то, КАК читать текст, а не про то, что в нём дописать):

    Правило 1 (Моно-потребность):
    Если указана потребность только в мужчинах (например "10 м"), то need_women и need_couples равны 0 — текст прямо говорит, что женщин и семейных не требуется. Аналогично для "10 ж" (need_men = 0, need_couples = 0) и "10 сем" (need_men = 0, need_women = 0).

    Правило 2 (Потребность без разбивки по полу):
    Если потребность указана как "N человек" / "N чел" / "нужно N" БЕЗ разбивки по полу (например, "Клининг - 10 человек, Ж до 55 лет, М до 45 лет" — здесь 10 это потребность, а "Ж до 55, М до 45" — требования к возрасту по полам), ставь need_total=N и НЕ выставляй need_men/need_women/need_couples.

    Правило 3 (День и ночь — это РАЗНЫЕ вакансии):
    Если в одном блоке есть и дневные, и ночные смены с РАЗНОЙ потребностью или ставкой (например, Миксит: "ночь: 6 ж 2 м / день: 8 ж 3 м"), верни ДВА отдельных JSON-объекта: один с shift_type="day", второй с shift_type="night", в каждом свои need_men/need_women. Правило работает по тексту самой заявки, даже если справка по проекту описывает смены иначе.

    Правило 4 (Должность не указана):
    Если в тексте есть только название объекта/компании/проекта (например, "Стеллар гласс - 5 М до 50 лет, ставка 3000") без должности, положи название в object_name, а vacancy_name НЕ выдумывай — пропусти поле.

    СПРАВКА ПО ПРОЕКТУ (приходит не всегда — только если проект нашёлся в базе знаний контрагента; тогда она идёт после текста заявки, отдельным блоком):
    - Справка — это постоянное описание проекта (адрес, что за производство, часы смены, проживание, питание, форма, медкнижка). Заявкой она НЕ является и позиций не создаёт.
    - Позиции заводятся ТОЛЬКО по тексту заявки. Если в справке перечислены должности, которых в заявке нет, — не добавляй их. Если заявка просит одну должность из пяти описанных — вернёшь одну.
    - Заявка ГЛАВНЕЕ справки. Потребность (сколько человек), ставка, возраст, гражданство, сроки вахты, даты заселения — только из заявки. Расходятся цифры — верна заявка: справка пишется один раз, а пост присылают сегодня.
    - Справка заполняет ровно те поля, о которых заявка молчит: object_address, schedule, shift_hours, shift_type, duties, requirements, housing_*, meals_*, uniform_*, medical_book_*, requires_tsd, transport_*, advantages, risks.
    - Правило «не додумывать» действует и для справки: нет данных ни там, ни там — поля в JSON нет.

    Примеры:
    Пример 1 (не вакансия):
    Сообщение: "Ищу работу водителем категории С."
    Ответ: []

    Пример 2 (одна вакансия):
    Сообщение: "🚀 Молком, Пушкино\\nРФ до 45 лет, с опытом работы с тсд\\n👉 Комплектовщик - 10 ж 5 м 2 сем - 3498 р/смена (Хостел Березка)\\n✅вахта от 30 смен\\n✅питание бесплатное\\n❗️ Проверка СБ"
    Ответ: [{{"counterparty": "Молком", "vacancy_name": "Комплектовщик", "vacancy_category": "склад", "city": "Пушкино", "work_format": "вахта", "schedule": "вахта от 30 смен", "min_shifts": 30, "shift_rate": "3498 р/смена", "requirements": "РФ до 45 лет, опыт с ТСД", "requires_tsd": true, "age_to": 45, "citizenship_requirements": "РФ", "need_men": 5, "need_women": 10, "need_couples": 2, "housing_available": true, "housing_conditions": "Хостел Березка", "meals_available": true, "meals_free": true, "advantages": "Питание бесплатное", "sb_policy": "проверка СБ"}}]
    Обрати внимание: region отсутствует (в тексте его нет), shift_type отсутствует (про смены не сказано), gender отсутствует (нужны и мужчины, и женщины — но прямо про требования к полу не написано).

    Пример 3 (несколько должностей в одном блоке):
    Сообщение: "🚀 Все инструменты Домодедово \\nРФ/РБ - с полным комплектом документов до 45 лет \\n👉СПК -  10 м \\nСтавка: 320 р/час - 3 520 р/смена (первые 3 смены, далее %)\\n👉Грузчик / 3498 р/смена - 10 м\\n✅вахта от 15 смен\\n✅комплексный обед - 200р (вычет из расчета)\\n❗️ БЕЗ СБ"
    Ответ: [{{"counterparty": "Все инструменты", "vacancy_name": "СПК", "vacancy_category": "склад", "city": "Домодедово", "work_format": "вахта", "schedule": "вахта от 15 смен", "min_shifts": 15, "shift_rate": "320 р/час - 3 520 р/смена (первые 3 смены, далее %)", "requirements": "РФ/РБ, полный комплект документов, до 45 лет", "gender": "мужчины", "age_to": 45, "citizenship_requirements": "РФ/РБ", "need_men": 10, "need_women": 0, "need_couples": 0, "meals_available": true, "meals_deduction": 200, "advantages": "Без СБ", "sb_policy": "нет"}}, {{"counterparty": "Все инструменты", "vacancy_name": "Грузчик", "vacancy_category": "склад", "city": "Домодедово", "work_format": "вахта", "schedule": "вахта от 15 смен", "min_shifts": 15, "shift_rate": "3498 р/смена", "requirements": "РФ/РБ, полный комплект документов, до 45 лет", "gender": "мужчины", "age_to": 45, "citizenship_requirements": "РФ/РБ", "need_men": 10, "need_women": 0, "need_couples": 0, "meals_available": true, "meals_deduction": 200, "advantages": "Без СБ", "sb_policy": "нет"}}]

    Пример 4 (день и ночь — две разные вакансии):
    Сообщение: "🚀Миксит склад (Московская обл.)\\n- сборщик/комплектовщик - ночь: 6 ж 2 м / день: 8 ж 3 м - 300₽/час (3300₽/смена 11 часов)\\n✅Вахта от 20 смен"
    Ответ: [{{"counterparty": "Миксит", "vacancy_name": "сборщик/комплектовщик", "vacancy_category": "склад", "region": "Московская область", "object_name": "Миксит склад", "work_format": "вахта", "shift_type": "day", "schedule": "Вахта от 20 смен", "min_shifts": 20, "shift_hours": 11, "shift_rate": "300₽/час (3300₽/смена 11 часов)", "need_men": 3, "need_women": 8}}, {{"counterparty": "Миксит", "vacancy_name": "сборщик/комплектовщик", "vacancy_category": "склад", "region": "Московская область", "object_name": "Миксит склад", "work_format": "вахта", "shift_type": "night", "schedule": "Вахта от 20 смен", "min_shifts": 20, "shift_hours": 11, "shift_rate": "300₽/час (3300₽/смена 11 часов)", "need_men": 2, "need_women": 6}}]
    Обрати внимание: city отсутствует — в тексте указана только область, конкретного города нет; питание не упомянуто, поэтому полей про питание нет.

    Теперь разбери текст заявки:
    {message}
    """

    # Шапка справки. Сама справка подставляется ПОСЛЕ текста заявки (см.
    # prompt_template): при подстановке в системный промпт, до заявки, модель
    # берёт из неё заметно меньше — проверено на 25 реальных постах, 474
    # заполненных поля против 566.
    CONTEXT_HEADER = (
        "СПРАВКА ПО ПРОЕКТУ из базы знаний контрагента (не заявка, позиций не создаёт, "
        "при расхождении цифр верна заявка):\n"
    )

    def __init__(
            self,
            model_name: str = "gpt-4.1",
            temperature: float = 0.0,
            max_tokens: int = 16000,
            base_url: Optional[str] = None,
            api_key: Optional[str] = None,
    ):
        """
        Инициализирует сервис.

        :param model_name: имя модели в Timeweb Cloud
        :param temperature: температура генерации (0.0 для точного парсинга)
        :param max_tokens: максимальное количество токенов ответа
        :param base_url: URL API-агента (если не указан, берётся из переменной окружения)
        :param api_key: ключ API (если не указан, берётся из переменной окружения)
        """
        if base_url is None:
            base_url = os.getenv("TIMEWEB_BASE_URL")
        if api_key is None:
            api_key = os.getenv("OPENAI_API_KEY")

        self.llm = ChatOpenAI(
            base_url=base_url,
            api_key=api_key,
            model=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.prompt_template = ChatPromptTemplate.from_messages([
            ("system", self.SYSTEM_PROMPT),
            ("human", "{message}{context_block}")
        ])
        # Сколько разборов прошло со справкой по проекту — видно в логе прогона
        # рядом с расходом токенов.
        self.context_hits = 0
        # Цепочка БЕЗ StrOutputParser — нам нужен сырой AIMessage,
        # чтобы достать usage_metadata.
        self.chain = self.prompt_template | self.llm
        # Аккумулятор расхода токенов за всё время жизни инстанса
        self.usage = TokenUsage()

    # Максимум одна попытка повтора при пустом ответе / битом JSON.
    MAX_RETRIES = 1

    def _record_usage(self, response) -> Tuple[str, int, int]:
        """Достаёт content и usage_metadata из AIMessage, копит счётчики.

        Возвращает (content, input_tokens, output_tokens): расход по каждому
        конкретному вызову нужен реестру, чтобы записать его в заявку. Из
        общего аккумулятора его не вычислить — вызовы идут параллельно.
        """
        usage = getattr(response, "usage_metadata", None) or {}
        # На некоторых провайдерах usage возвращается как dict {'input_tokens': ...}
        # или через response_metadata.token_usage
        if not usage:
            meta = getattr(response, "response_metadata", {}) or {}
            tu = meta.get("token_usage") or meta.get("usage") or {}
            usage = {
                "input_tokens": tu.get("prompt_tokens") or tu.get("input_tokens") or 0,
                "output_tokens": tu.get("completion_tokens") or tu.get("output_tokens") or 0,
            }
        tokens_in = int(usage.get("input_tokens", 0) or 0)
        tokens_out = int(usage.get("output_tokens", 0) or 0)
        self.usage.add(tokens_in, tokens_out)
        content = response.content
        if not isinstance(content, str):
            content = str(content)
        return content, tokens_in, tokens_out

    def _context_block(self, context: Optional[str]) -> str:
        """Справка по проекту в виде куска промпта. Нет справки — пустая строка."""
        if not context or not context.strip():
            return ""
        self.context_hits += 1
        return f"\n\n{self.CONTEXT_HEADER}{context.strip()}"

    def _invoke_llm(self, text: str, context: Optional[str] = None) -> str:
        payload = {"message": text, "context_block": self._context_block(context)}
        return self._record_usage(self.chain.invoke(payload))[0]

    async def _ainvoke_llm(self, text: str, context: Optional[str] = None) -> str:
        return (await self._ainvoke_llm_ex(text, context))[0]

    async def _ainvoke_llm_ex(
            self, text: str, context: Optional[str] = None,
    ) -> Tuple[str, int, int]:
        payload = {"message": text, "context_block": self._context_block(context)}
        return self._record_usage(await self.chain.ainvoke(payload))

    @staticmethod
    def _extract_json_array(raw: str) -> Optional[str]:
        """
        Извлекает JSON-массив из ответа модели.
        Снимает обёртки ```json ... ```, лишний текст вокруг массива.
        Возвращает None, если корректно закрытый массив не найден.
        """
        if not raw:
            return None
        s = raw.strip()
        # Снимаем markdown-fence: ```json ... ``` или ``` ... ```
        m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, flags=re.DOTALL | re.IGNORECASE)
        if m:
            s = m.group(1).strip()
        # Ищем первый [ и последний ] — берём кусок между ними
        start = s.find("[")
        end = s.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        return s[start:end + 1]

    def _try_parse_raw(self, raw_json: str, attempt: int) -> Optional[List[Dict]]:
        """Возвращает распарсенный список или None (с логом причины)."""
        if not raw_json or not raw_json.strip():
            logger.warning(f"LLM вернул пустой ответ (попытка {attempt + 1}/{self.MAX_RETRIES + 1})")
            return None
        candidate = self._extract_json_array(raw_json)
        if not candidate:
            logger.warning(f"Не нашёл JSON-массив в ответе (попытка {attempt + 1}): {raw_json[:200]}…")
            return None
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, list):
                logger.warning(f"Корневой объект не массив (попытка {attempt + 1})")
                return None
            return parsed
        except json.JSONDecodeError as exc:
            logger.warning(f"Ошибка парсинга JSON (попытка {attempt + 1}): {exc}")
            return None

    def _enrich(self, data: List[Dict], source: str, source_url: str) -> List[Dict]:
        """Дополняет вакансии служебными полями и vacancy_id. In-place."""
        today = date.today().isoformat()
        for v in data:
            v["created_at"] = today
            v["updated_at"] = today
            v["last_updated_at"] = today
            v["source"] = source
            v["source_url"] = source_url
            v["needs_review"] = True
            v["is_active"] = True

            # need_total: если LLM сам выставил (например, "10 человек" без разбивки
            # по полу) — уважаем его значение. Иначе суммируем то, что известно.
            # Если не известно вообще ничего — поле остаётся пустым: ноль здесь
            # читался бы как «людей не требуется», а это не то же самое, что
            # «в заявке потребность не указана».
            if v.get("need_total") is None:
                parts = [v.get("need_men"), v.get("need_women"), v.get("need_couples")]
                known = [p for p in parts if p is not None]
                v["need_total"] = sum(known) if known else None

            for field in self.ADMIN_FIELDS:
                v.setdefault(field, None)

            v["vacancy_id"] = make_vacancy_id(v)
        return data

    def parse(self, text: str, source: str = "Градус", source_url: str = "") -> List[Dict]:
        """
        Парсит текст сообщения и возвращает список вакансий.

        :param text: текст сообщения из Telegram-канала
        :param source: название источника (канала)
        :param source_url: ссылка на канал
        :return: список словарей с полями вакансий (пустой список, если вакансий нет)
        """
        data: Optional[List[Dict]] = None
        last_raw = ""
        for attempt in range(self.MAX_RETRIES + 1):
            raw_json = self._invoke_llm(text)
            last_raw = raw_json
            parsed = self._try_parse_raw(raw_json, attempt)
            if parsed is not None:
                data = parsed
                break

        if data is None:
            logger.error(f"Парсер не справился после {self.MAX_RETRIES + 1} попыток, raw[:300]={last_raw[:300]!r}")
            return []

        return self._enrich(data, source, source_url)

    async def aparse(
            self,
            text: str,
            source: str = "Градус",
            source_url: str = "",
            context: Optional[str] = None,
    ) -> List[Dict]:
        """Асинхронная версия parse() — для параллельной обработки сегментов."""
        data = await self.aparse_raw(text, context)
        if data is None:
            return []
        return self._enrich(data, source, source_url)

    async def aparse_raw(self, text: str, context: Optional[str] = None) -> Optional[List[Dict]]:
        """Разбор без служебных полей — путь реестра.

        Отличие от aparse(): не подмешивает created_at/source/is_active и
        прочую специфику Google Sheets (этим занимается registry.ingest) и
        различает «модель не справилась» (None) и «вакансий в тексте нет» ([]).
        Второе — нормальная ситуация для служебных сообщений в канале, и
        помечать её как ошибку разбора не нужно.

        :param context: справка по проекту из базы знаний (см. project_kb.py).
            Дополняет поля, о которых заявка молчит, и никогда не спорит с ней.
        """
        return (await self.aparse_raw_ex(text, context))[0]

    async def aparse_raw_ex(
            self, text: str, context: Optional[str] = None,
    ) -> Tuple[Optional[List[Dict]], int, int]:
        """aparse_raw() + расход токенов на этот конкретный разбор."""
        last_raw = ""
        tokens_in = tokens_out = 0
        for attempt in range(self.MAX_RETRIES + 1):
            raw_json, t_in, t_out = await self._ainvoke_llm_ex(text, context)
            tokens_in += t_in
            tokens_out += t_out
            last_raw = raw_json
            parsed = self._try_parse_raw(raw_json, attempt)
            if parsed is None:
                continue
            if context and not parsed:
                # Справка может только дополнять. Если со справкой модель
                # решила, что вакансий нет, — перепроверяем по голому посту:
                # на одной заявке из ста длинное описание проекта уводит
                # разбор в пустой ответ, и позиция теряется целиком.
                fallback, f_in, f_out = await self.aparse_raw_ex(text, None)
                tokens_in += f_in
                tokens_out += f_out
                if fallback:
                    logger.warning(
                        "Со справкой разбор вернул пусто, без неё — "
                        f"{len(fallback)} позиц.; беру разбор без справки"
                    )
                    return fallback, tokens_in, tokens_out
            return parsed, tokens_in, tokens_out

        logger.error(
            f"Парсер не справился после {self.MAX_RETRIES + 1} попыток, "
            f"raw[:300]={last_raw[:300]!r}"
        )
        return None, tokens_in, tokens_out
