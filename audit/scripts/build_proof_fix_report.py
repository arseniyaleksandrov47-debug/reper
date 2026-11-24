"""Report one completed, frozen proof-fix replay without mixing earlier attempts."""
import json
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'audit/proof_fix_2026-11-03'
BASELINE=ROOT/'audit/big_run_2026-11-03/run_193231_893406'

def main():
    out=Path((BASE/'LATEST_FULL.txt').read_text(encoding='utf-8'))
    def read(path):return json.loads(path.read_text(encoding='utf-8'))
    summary=read(out/'summary.json');units=read(out/'unit_summary.json');old=read(BASELINE/'report_metrics.json')
    plan=read(out/'execution_plan.json')
    assert (summary['controls'],summary['studio'],summary['real_evaluations'])==(108,24,360),'Incomplete replay'
    controls=[read(p) for p in sorted((out/'controlled').glob('*.json'))]
    studios=[read(p) for p in sorted((out/'studio').glob('*.json'))]
    real=[read(p) for p in sorted((out/'real').glob('*.json'))]
    traces=[read(p) for p in sorted((out/'trace').glob('*.json'))]
    errors=Counter('cancelled' if t.get('error_type')=='CancelledError' else t.get('error_category',t.get('error_type'))
                   for t in traces if t.get('error_type'))
    control_rows=[]
    for case in sorted({r['case'] for r in controls}):
        checks=[c for r in controls if r['case']==case for c in r['checks']]
        control_rows.append(f"| {case} | {sum(c['passed'] for c in checks)} / {len(checks)} |")
    studio_rows=[]
    for context in range(3):
        for batch in (1,4):
            checks=[c for r in studios if r['context']==context and r['batch']==batch for c in r['checks']]
            studio_rows.append(f"| {context} | {batch} | {sum(c['passed'] for c in checks)} / {len(checks)} |")
    real_rows=[];analysis_rows=[];analysis_totals=Counter();changes=[];valid_cached=[];repeated_counts=Counter()
    approved=[]
    for r in real:
        old_r=read(BASELINE/f"real/{r['case']}.json")
        counts=r['counts'];repeat=Counter(a['status'] for a in r['repeated_assessments'].values());repeated_counts.update(repeat)
        real_rows.append(f"| {r['case']} | {counts.get('eligible',0)} | {counts.get('rejected',0)} | {counts.get('needs_verification',0)} | {r['seconds']:.1f} | {old_r['seconds']:.1f} | {r['repeat_ms']:.1f} |")
        states=Counter(a['analysis_status'] for a in r['assessments'].values());analysis_totals.update(states)
        analysis_rows.append(f"| {r['case']} | {dict(states)} |")
        for key,a in r['assessments'].items():
            after=r['repeated_assessments'][key]
            if a['status']!=after['status']:changes.append(dict(case=r['case'],id=key,before=a['status'],after=after['status']))
            if a['analysis_status']=='valid':valid_cached.append(a==after)
        approved.extend(dict(case=r['case'],id=k) for k,a in r['repeated_assessments'].items() if a['status']=='eligible')
    manual=read(out/'manual_review.json') if (out/'manual_review.json').exists() else dict(scope='Не завершена',findings=[])
    assert manual.get('review_complete') is True,'Complete manual proof review before building the final report'
    mutations=read(out/'offline_contract/contract_mutations.json')
    assert not mutations['failures'] and mutations['valid_controls']==mutations['valid_controls_expected']
    offline=read(out/'historical_decisions_revalidated.json')
    blocked=[r for r in offline['records'] if r['after']!='eligible']
    metrics={**summary,'unit_tests':units['tests'],'inference_errors':dict(errors),'valid_cached':len(valid_cached),
        'real_analysis_statuses':dict(analysis_totals),
        'changed_valid_cached':sum(not x for x in valid_cached),'repeat_status_changes':changes,
        'repeated_counts':dict(repeated_counts),'offline_prior_decisions':len(offline['records']),'offline_blocked_prior_decisions':blocked,
        'contract_mutations':mutations['mutations'],'valid_contract_controls':mutations['valid_controls'],
        'manual_findings':len(manual['findings'])}
    (out/'report_metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
    failures='\n'.join(f"- {r['case']} / повтор {r['repeat']} / `{r['id']}`: {r['expected']} → {r['actual']} ({r['analysis_status']})." for r in summary['control_failures']) or '- Расхождений нет.'
    integrations='\n'.join('- `'+r['case']+'`: `'+json.dumps(r,ensure_ascii=False)+'`.' for r in summary['integrations'])
    manual_text='\n'.join(f"- {r.get('case')} / ID {r.get('id')}: {r['finding']}" for r in manual['findings']) or '- Отдельных замечаний к доказательствам в выполненной ручной сверке не записано. Это не подтверждение пригодности объекта собственником.'
    report=f'''# Исправление доказательств пригодности и повторный прогон

Исправления кода прошли локальные проверки. Живой прогон показывает оставшиеся потери подходящих вариантов и зависимость от доступности API. Число обработанных входов не означает, что по каждому получена завершённая оценка. Готовность к автономному выбору оптимального помещения этим тестом не подтверждается.

## Изменения

- Номер этажа больше не подтверждает явное условие о посетителях или маршруте. Исключение — однозначное условие только о самом этаже: его число сравнивается с фактическим этажом.
- Явно заданные физические величины требуют исходных данных с соответствующей размерностью. Грузовой лифт без указанной нагрузки не доказывает перевозку паллеты 1000 кг. Размерность — дополнительный барьер; смысл параметра, значение и маршрут проверяются отдельно моделью.
- Проверяющая модель оценивает связь каждого условия с конкретными предложенными доказательствами. Ссылки ограничены для каждого условия. Остальной текст объявления может выявить противоречие, но не подменить отсутствующее доказательство.
- Дополнительно каждый отличный набор доказательств проверяется отдельным запросом, в котором нет остальных фактов объявления. Одинаковые наборы объединяются в совместную проверку условий. Это действует для допуска и окончательного отказа: отсутствие факта не становится запретом.
- Схема генерации запрещает отменять положительные и смешанные условия клиента. `not_required` разрешён только при явном снятии требования самим клиентом.
- Убраны примеры из инструкции проверки, которые провоцировали перенос требований к грузу на обычных посетителей. Сохранён свободный сценарий клиента; справочников ниш и допустимых этажей нет.
- Каталог цитат сохраняет строки целиком: десятичные числа и сокращения не превращаются в бессмысленные обрывки.
- По умолчанию помещение оценивается отдельно, параллельность ограничена двумя партиями; объём ответа ограничен по числу объектов. Температура структурированных ответов — 0; это не гарантирует детерминированность.
- Один общий ограничитель на ключ внутри процесса считает все HTTP-попытки, включая повторы и другие модели. Потолок — 40 запросов в скользящем окне 60 секунд, равномерный интервал по умолчанию 1,6 секунды. После 429 учитывается `Retry-After`, интервал увеличивается, повтор ограничен. Ответ 429 получает отдельный статус `rate_limited` и не кешируется как нехватка данных объявления. Другие процессы с тем же ключом в эту локальную очередь не входят.
- Почти одновременные ответы 429 объединяются в один эпизод замедления; после восьми успешных HTTP-ответов темп постепенно восстанавливается до исходного интервала. Это не снимает потолок 40 RPM.
- Добавлены бюджеты этапов: этаж/доступ 90 с, окружение 60 с, финансовые данные 30 с. Значения настраиваются. Готовые результаты сохраняются, незавершённые не становятся допуском. Неполное сравнение явно отмечается. Отмена ожидания не прерывает уже начатый синхронный HTTP-запрос; у него остаётся собственный сетевой timeout.

## Локальная проверка

**{units['tests']} тестов**, ошибок {units['errors']}, падений {units['failures']}. Проверены явные условия посетителей, отсутствие характеристик груза, разные единицы измерения, запрет переноса доказательств между условиями, чисто числовое условие этажа, отмена условий и частичный результат по истечении времени.

Дополнительно выполнены **{mutations['mutations']}** мутаций ответов и **{mutations['valid_controls']}** валидных контрольных ответов: ошибок барьера допуска **{len(mutations['failures'])}**. Это локальная проверка формата и обязательных полей на фиксированных ответах, а не точность новых ответов AI.

Повторная локальная проверка 33 ранее сохранённых допусков через новый валидатор блокирует **{len(blocked)}** прежних решений: две паллеты без характеристик пути и два условия о посетителях с доказательством только этажом. Это переоценка старых ответов, а не новый вызов модели. Остальные 29 решений этой проверкой не подтверждаются по смыслу — для этого нужен живой пересчёт и сверка доказательств.

## Объём живого повторного прогона

Использованы **те же 60 сохранённых объявлений**, что в предыдущем тесте, и новые ответы NVIDIA. Нового сбора рынка в этом прогоне нет. Выборка относится к Москве; перенос результатов на Санкт-Петербург и Екатеринбург не проверялся.

- 36 контрольных вариантов × 3 независимых запуска = **{summary['controls']}** решений.
- Два подходящих варианта студии × 3 формулировки × 2 размера партии × 2 порядка = **{summary['studio']}** оценок.
- 60 объявлений × 6 сценариев = **{summary['real_evaluations']}** первичных оценок; затем повтор тех же входных данных для проверки кеша.
- Вызовов обёртки вывода: **{summary['inference_calls']}**, ошибок по категориям: `{dict(errors)}`. Внутренние повторы могут создавать дополнительные HTTP-запросы.
- Производственный код во время этого прогона не менялся: **{summary['source_code_unchanged']}**. Рабочая база не менялась: **{summary['working_database_unchanged']}**.

В первом контрольном проходе размер партии — 1; в следующих двух — **{plan['subsequent_control_batch_size']}**. Сценарии и разметка сохранены, но это также проверка чувствительности к размеру партии, а не три запуска с полностью одинаковыми параметрами. Реальные объявления пересчитывались с явно выбранной партией **{plan['real_batch_size']}**. По умолчанию приложение по-прежнему обрабатывает одно объявление в партии. Запуск возобновлялся с проверкой хешей и сохранением уже завершённых случаев. После возобновления введена служебная пауза 60 с.

В последнем процессе записано **{summary.get('http_attempts_current_process','не записано')}** HTTP-попыток и **{max(0,summary.get('throttle_responses_current_process',0)-int(plan['resumed']))}** фактических ответов 429. Сырой счётчик ограничителя включает один служебный вызов при возобновлении; он вычтен из этой цифры. Счётчики последнего процесса не включают запросы до возобновления, а сохранённые трассировки включают все этапы и внутренние HTTP-повторы не всегда видны как отдельные файлы.

Для оставшейся части прогона установлен бюджет **{plan.get('phase_timeout')} с** на вызов оценки/повторной оценки, как в штатном поиске. Ранее завершённые контрольные случаи были без этого внешнего бюджета и сохранены. Это проверка работы при доступном API и ограниченном времени; число 360 включает также результаты о незавершённом анализе. Само прохождение через все входные данные не доказывает 360 законченных семантических оценок.

Лимит 40 RPM указан пользователем для его аккаунта. NVIDIA отдельно предупреждает, что фактическая доступность зависит от модели и нагрузки: [официальный FAQ](https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317). Поэтому локальное соблюдение потолка не гарантирует отсутствие HTTP 429 со стороны сервиса.

Предварительные попытки с таймаутами, ошибками транспорта и прерванным процессом сохранены в соседних каталогах с отметками `ABORTED`/`INCOMPLETE`. Завершённый промежуточный запуск `full_222003_214630` обнаружил 383 транспортных отказа, включая массовые HTTP 429; после него изменены и проверка доказательств, и ограничитель. Его цифры не смешаны с этим прогоном. Доступность API остаётся внешней зависимостью; успешный завершённый запуск не отменяет предыдущие сбои.

## Контрольные варианты

Ожидания выполнены: **{summary['controls_passed']}/{summary['controls']}**, ранее **{old['control_passed']}/{old['control_evaluations']}**. Автоматических допусков неизвестных или запрещённых контрольных вариантов: **{len(summary['unsafe_controls'])}**.

`not_eligible` проверяет барьер автоматического допуска: и отказ, и уточнение проходят этот контроль. Для явно подходящих вариантов требуется именно `eligible`, для неизвестных — `needs_verification`. Поэтому общий счёт не равен точности окончательной классификации.

| Сценарий | Ожидания выполнены |
|---|---:|
{chr(10).join(control_rows)}

{failures}

## Повторяемость студии

Ожидаемые допуски: **{summary['studio_passed']}/{summary['studio']}**, ранее **{old['studio_passed']}/{old['studio_evaluations']}**.

| Формулировка | Размер партии | Ожидания выполнены |
|---|---:|---:|
{chr(10).join(studio_rows)}

## Реальные объявления

| Сценарий | Допуск | Отказ | Уточнение | Первый расчёт, с | Ранее, с | Повтор, мс |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(real_rows)}

Первый проход: `{summary['real_counts']}`. Повтор: `{dict(repeated_counts)}`. Статус изменился у **{len(changes)}** сочетаний. Из **{len(valid_cached)}** исходно валидных оценок изменились **{sum(not x for x in valid_cached)}**. При ошибках и неверных ссылках повтор может заново обратиться к AI.

| Сценарий | Состояния анализа в первом проходе |
|---|---|
{chr(10).join(analysis_rows)}

Всего состояний: `{dict(analysis_totals)}`. `budget_exceeded` означает истечение времени, `rate_limited` — ограничение API, `transport_error` — сбой связи, `unsupported_evidence` — неверные доказательства. Ни одно из этих состояний не устанавливает непригодность объявления. `insufficient_facts` и `source_conflict` выявляются по исходным данным до AI. Часть `valid` также означает обоснованное уточнение, а не автоматический допуск.

Расчёт включает очередь при общей параллельности 2 и двух одновременно выполняемых сценариях. Это не изолированная задержка одного пользовательского запроса. Меньшее время при неполном результате не доказывает ускорения полного анализа. Распределение допусков по объявлениям не является точностью: независимая разметка собственниками отсутствует.

### Ручная сверка

{manual['scope']}

{manual_text}

## Сквозные запросы

{integrations}

Сравнение окружения ограничено шестью объектами на сценарий. `complete=false` и предупреждения сохраняются: расчётный лидер не подтверждает оптимум среди всех объявлений или прибыльность бизнеса. Сроки после введения бюджетов следует читать вместе с числом завершённых анализов.

## Воспроизведение

```powershell
python -m unittest discover -s tests -v
python audit/scripts/retest_proof_fix.py --mode full
python audit/scripts/build_proof_fix_report.py
```

Полные входные данные, ответы и доказательства находятся в каталоге `{out.name}`. Ключ API в исходники и отчёт не включается. Финальная проверка файлов и хешей записана отдельно в `final_verification.json`.
'''
    (out/'REPORT.md').write_text(report,encoding='utf-8');(BASE/'REPORT.md').write_text(report,encoding='utf-8')
    print(json.dumps({k:v for k,v in metrics.items() if k not in ('control_failures','integrations','repeat_status_changes','offline_blocked_prior_decisions')},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
