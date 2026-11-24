# Changelog

## [Unreleased]

### Изменено

- **Доступ для непосетительских сценариев (`floor_policy.py`)**:
  - Уточнены инструкции первичной оценки, ревью (120B) и проверки доказательств для `audience_access`: для сценариев без клиентского визита (доставка, курьерская выдача, dark-kitchen) открытый/отдельный вход без ограничений для курьеров и персонала оценивается как `met`, а не `not_met`; версия кэша `floor_policy` увеличена до `revision=24`.
- **Ускорение батч-классификации (`competitor_classification.py`, `competitors.py`)**:
  - В сетевой wire-пакет `_wire_organization` вынесена компактная дедуплицированная карта `offer_evidence_catalog` без повторного дублирования текстов доказательств (`categories`/`features`/`feature_flags`) с сохранением `negative_features`, `conflicting_features` и `source_conflicts` (`revision=11` после уточнения инструкции классификатора: прямой запрет на додумывание непредставленного ассортимента в общих продуктовых/розничных категориях без явных признаков товара).
  - Дефолтный `classification_reasoning_budget` снижен с `768` до `256` токенов (переопределяется явно или через `NVIDIA_CLASSIFICATION_REASONING_BUDGET`).
- **Практичное обогащение и опциональный учёт ФНС (`fns.py`, `legal_entity.py`, `market_context.py`, `ranking_policy.py`)**:
  - В `market_context.py` скорректирован вклад неподтверждённого маршрута в покрытие (`access_coverage = 1 / (1 + access.weight)` вместо `1 - access.weight`): при известных жителях/работниках/конкурентах отсутствие одного маршрута в OSM или авто-маршрута доставки больше не обнуляет `coverage` и значение `market_fit_index`, сохраняя консервативный интервал и `missing_inputs`; в `ranking_policy.py` (`revision=23`) уточнена семантика `access.weight` для генератора профиля.
  - В `fns.py` сетевые точки (`is_chain=True`) пропускаются до сетевых запросов (`scope='chain_excluded'`, `finance_status='not_applicable_chain'`).
  - В `legal_entity.py` расширен поиск внутренних ссылок (`about`, `о нас`, `о компании`, `policy`, `info`, `doc`, `сведения`) и добавлен HTML-ограниченный fallback по типичным путям `/contacts`, `/rekvizity`, `/oferta`.
  - В `market_context.py` реализован режим опциональной ФНС (Вариант A): отчётность `company_*` больше не занижает `coverage` и не заполняет `missing_inputs` для сетей и организаций без доказанного самостоятельного ИНН ООО (сохраняя консервативный интервал `[0, 1]` и активный вес при наличии подтверждённых данных ФНС или конфликте ИНН).
- **CLI UX для ориентира затрат (`run_engine.py`)**:
  - Добавлен флаг `--full-cost-limit <₽/мес>` для прямого задания верхней границы `premises_cost_month` в `ranking_scales` (с сохранением через `client_search_command`), а также интерактивный запрос и CLI-команда повтора при статусе `provide_references`.
- **2-модельная схема ИИ и ускорение классификации** (`competitor_classification.py`, `competitors.py`, `docs/SCORING_CONCEPT.md`):
  - Настройки моделей по умолчанию переведены на две независимые модели NVIDIA: `nvidia/nemotron-3.5-lightning-30b-a3b` (основная) и `nvidia/nemotron-3.5-super-vl-120b-a12b` (верификатор).
  - Классификация окружения переведена на `batch_size=8` с предварительной фильтрацией SQLite-кэша до упаковки батчей и очисткой эфемерных признаков статуса/оценки в ключах кэша; для Nemotron в первичном запросе оценки объекта используется `response_format={'type': 'json_object'}` со схемой в промпте.
- **Детерминированные масштабы полезности без генерации чисел ИИ** (`market_context.py`, `ranking_policy.py`, `docs/BUSINESS_MODEL.md`):
  - Числовые пороги `references`, `competition_scale`, `outside_pressure` и `access.reference_minutes` удалены из `market_context_schema` и инструкций генератора профиля; их подставляет `apply_default_market_scales` из `DEFAULT_UTILITY_SCALES` с явным происхождением `system_default_scale`.
  - Явные клиентские ориентиры через `ranking_scales` (`apply_market_references`) по-прежнему переопределяют отдельные шкалы и фиксируют их в `model_assumption_sensitivity`.
- **Гигиена репозитория и документация** (`README.md`, `docs/ARCHITECTURE.md`, `tests/fixtures/audit/`):
  - Тяжёлые прогоны `audit/` вынесены из рабочего дерева; лёгкие регрессионные фикстуры перенесены в `tests/fixtures/audit/`; добавлена актуальная архитектурная сводка и эмпирическая фиксация покрытия ФНС БФО.
