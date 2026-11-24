"""Create the completed live-run report from persisted public observations."""
import hashlib,json
from collections import Counter
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
BASE=ROOT/'audit/big_run_2026-11-03'

def main():
    out=Path((BASE/'LATEST.txt').read_text(encoding='utf-8'))
    def read(name):return json.loads((out/name).read_text(encoding='utf-8'))
    summary=read('live_summary.json');collection=read('collection.json');sample=read('fresh_sample.json')
    control_records=[json.loads(p.read_text(encoding='utf-8')) for p in (out/'controlled').glob('*.json')]
    all_checks=[c for r in control_records for c in r['checks']]
    studio_records=[json.loads(p.read_text(encoding='utf-8')) for p in (out/'studio').glob('*.json')]
    trace=[json.loads(p.read_text(encoding='utf-8')) for p in (out/'trace').glob('*.json')]
    call_errors=Counter(d.get('error_type') for d in trace if d.get('error_type'))
    for name,value in [('controls',len(all_checks)),('studio',sum(len(r['checks']) for r in studio_records))]:
        if value != {'controls':108,'studio':24}[name]:raise ValueError('Incomplete '+name)
    source_rows='\n'.join(f"| {name} | {d['collected']} | {d.get('accepted',0)} | {d['status']} |" for name,d in collection['source_results'].items())
    real_rows='\n'.join(f"| {r['case']} | {r['counts'].get('eligible',0)} | {r['counts'].get('rejected',0)} | {r['counts'].get('needs_verification',0)} | {r['seconds']:.1f} | {r['cache_ms']:.1f} | {'да' if r['cache_preserves_results'] else 'нет'} |" for r in summary['real_summaries'])
    case_rows=[]
    for name in sorted({r['case'] for r in control_records}):
        checks=[c for r in control_records if r['case']==name for c in r['checks']]
        case_rows.append(f"| {name} | {sum(c['passed'] for c in checks)} / {len(checks)} | {sum(c['expected']!='eligible' and c['actual']=='eligible' for c in checks)} |")
    studio_rows=[]
    for context in (0,1,2):
        for batch in (1,4):
            records=[r for r in studio_records if r['context_index']==context and r['batch_size']==batch]
            checks=[c for r in records for c in r['checks']]
            studio_rows.append(f"| {context} | {batch} | {sum(c['passed'] for c in checks)} / {len(checks)} |")
    flips='\n'.join(f"- `{name}`: {' → '.join(statuses)}." for name,statuses in summary['control_status_variation'].items()) or '- Изменений не было.'
    failures='\n'.join(f"- `{x['case']}`, запуск {x['repeat']}, `{x['id']}`: {x['expected']} → {x['actual']} ({x['analysis_status']})." for x in summary['control_failures']) or '- Нет.'
    integration='\n'.join(f"- `{r['case']}`: {json.dumps(r,ensure_ascii=False)}" for r in summary['integrations']) or '- Нет автоматически допущенных объектов; сквозное ранжирование не запускалось.'
    manual=read('manual_review.json') if (out/'manual_review.json').exists() else {'findings':[],'scope':'not yet reviewed'}
    real_total={key:sum(r['counts'].get(key,0) for r in summary['real_summaries']) for key in ['eligible','rejected','needs_verification']}
    real_records=[json.loads(p.read_text(encoding='utf-8')) for p in (out/'real').glob('*.json')]
    if len(real_records)!=6 or sum(r['count'] for r in real_records)!=360:
        raise ValueError('Incomplete real-advertisement evaluation')
    repeated_counts=Counter(a['status'] for r in real_records for a in r['repeated_assessments'].values())
    valid_cached=[(r['case'],key,a==r['repeated_assessments'][key]) for r in real_records
                  for key,a in r['assessments'].items() if a['analysis_status']=='valid']
    decision_changes=sum(c['before']!=c['after'] for r in real_records for c in r['cache_deltas'])
    repeat_rows='\n'.join(f"| {r['case']} | {dict(Counter(a['status'] for a in r['repeated_assessments'].values()))} | {sum(c['before']!=c['after'] for c in r['cache_deltas'])} |" for r in real_records)
    metrics=dict(collected=collection['batch_collected'],accepted=collection['batch_saved_or_updated'],quarantined=collection['batch_quarantined'],
        control_evaluations=len(all_checks),control_passed=summary['control_passed'],unsafe_control_approvals=len(summary['control_unsafe_approvals']),
        control_status_variants=len(summary['control_status_variation']),studio_evaluations=summary['studio_evaluations'],studio_passed=summary['studio_passed'],
        real_evaluations=summary['real_evaluations'],real_counts=real_total,repeated_real_counts=dict(repeated_counts),
        valid_cached=len(valid_cached),valid_cached_changed=sum(not equal for _,_,equal in valid_cached),decision_changes=decision_changes,
        ai_call_errors=dict(call_errors),manual_findings=len(manual['findings']),
        source_code_unchanged=summary['source_code_unchanged'],working_database_unchanged=summary['working_database_unchanged'])
    (out/'report_metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
    manual_text='\n'.join(f"- `{f.get('case')}` / ID {f.get('id')}: {f.get('finding')}" for f in manual['findings']) or '- Отдельных ручных замечаний к допущенным объектам в этом файле не записано. Это не подтверждение их пригодности собственником.'
    report=f'''# Большой живой прогон — 3 ноября 2026

Живой прогон выполнен с новым подключённым ключом NVIDIA. Ключ сохранён локально в шифрованном хранилище Windows DPAPI, не включён в исходники и результаты. Ранее завершённая локальная проверка описана отдельно в `audit/big_run_2026-11-03/REPORT.md`; её цифры не выдаются за новые живые результаты.

**Вывод: готовность к автономному выбору оптимального помещения не подтверждена.** Формальные проверки проходят, но ручная сверка обнаружила смысловые ошибки в доказательствах доступа и перевозки груза, а независимые повторы — нестабильность подходящих вариантов. Сквозной расчёт кофейни превысил лимит 200 секунд; два других отчёта завершились с `comparison_complete=false`.

Во время живого прогона производственный код не менялся: **{summary['source_code_unchanged']}**. Рабочая база не менялась: **{summary['working_database_unchanged']}**. Исходные хеши сохранены в `manifest.json`.

## Масштаб

- Получено **{collection['batch_collected']}** объявлений, принято **{collection['batch_saved_or_updated']}**, в карантине **{collection['batch_quarantined']}**.
- **60 свежих объявлений × 6 сценариев = {summary['real_evaluations']}** проверок пригодности этажности и доступа. Неизвестные/противоречивые этажи отсеиваются до AI.
- **36 контрольных вариантов × 3 независимых запуска = {len(all_checks)}** решений.
- Дополнительно **{summary['studio_evaluations']}** оценок двух вариантов студии: три формулировки контекста × два размера пакета × два порядка кандидатов.
- Вызовов AI-обёртки: **{summary['inference_calls']}**. Вызов обёртки может включать повтор или исправление доказательств, поэтому это не обязательно число HTTP-запросов. Ошибки вызовов по типам: `{dict(call_errors)}`.

Контрольные примеры искусственные и размечены вручную для смысла пути посетителя. Реальные объявления не имеют подтверждённой собственниками разметки; их распределение решений не является точностью модели.

## Источники

| Источник | Получено | Принято | Статус |
|---|---:|---:|---|
{source_rows}

Ограниченный сбор занял **{collection['total_pipeline_time_ms']/1000:.2f} с**. Доступность Авито меняется между запусками и не зависит от ключа NVIDIA. Отказ источника отражён в статусе и не подменяется пустым рынком.

Выборка ограничена географией и числовыми параметрами: центр 55.7558 / 37.6173, радиус 40 км, площадь 20–350 м², аренда до 600 000 ₽/месяц. Из подходящего пула выбрано 60 объектов с балансировкой по доступным источникам, seed 2026110304. Распределение источников: `{dict(Counter(p['source'] for p in sample))}`. Это не репрезентативная случайная выборка всего рынка. Живые сборщики по-прежнему запрашивают Москву.

## Контрольные варианты: повторяемость

Получено **{summary['control_passed']} из {len(all_checks)}** ожидаемых результатов. Необоснованных автоматических допусков у неизвестных или явно запрещённых вариантов: **{len(summary['control_unsafe_approvals'])}**.

Важное правило подсчёта: `not_eligible` означает «не допускать автоматически». И окончательное исключение, и уточнение проходят этот барьер. Поэтому показатель не равен точности окончательной классификации. Для явно подходящего варианта требуется `eligible`, для неизвестного — `needs_verification`.

| Сценарий | Ожидания выполнены | Необоснованные допуски |
|---|---:|---:|
{chr(10).join(case_rows)}

Расхождения с контрольной разметкой:

{failures}

Изменили статус между запусками **{len(summary['control_status_variation'])}** контрольных вариантов:

{flips}

Неизменность ответа в кеше не равна независимой повторяемости: здесь каждый контрольный запуск создавал новый resolver, а основной код не менялся.

## Два проблемных варианта студии

Итог: **{summary['studio_passed']} из {summary['studio_evaluations']}** ожидаемых допусков. Проверялись верхний этаж с разрешённым доступом по записи и подвал с разрешённым отдельным входом; отсутствие требований к лестницам задано контекстом. Изменялись формулировка запроса, порядок объектов и размер партии.

| Вариант контекста | Размер партии | Подтверждено |
|---|---:|---:|
{chr(10).join(studio_rows)}

Даже если один запуск подтвердил вариант, это не доказывает стабильное решение: для этого приведены все повторения. Полные решения и доказательства — в `studio/`.

## Свежие реальные объявления

| Сценарий | Допуск | Отказ | Уточнение | Первый расчёт, с | Повтор, мс | Ответы совпали |
|---|---:|---:|---:|---:|---:|---|
{real_rows}

Суммарно: `{real_total}`. Первый расчёт включает очередь при ограничении параллельных AI-запросов. Это не изолированный тест задержки одного пользователя. Повтор использует кеш только для валидных результатов; при транспортной ошибке, повреждённом ответе или неверных доказательствах могут происходить новые вызовы и изменение результата. Списки изменений сохранены как `cache_deltas` в `real/*.json`.

При немедленном повторе все **{len(valid_cached)}** исходно валидных оценок сохранились без изменений; изменённых валидных кешированных оценок: **{sum(not equal for _,_,equal in valid_cached)}**. У ранее невалидных результатов выполнены новые вызовы, итоговый статус изменился в **{decision_changes}** случаях. Это не самопроизвольное изменение содержимого кеша.

| Сценарий | Распределение после повтора | Изменений итогового статуса |
|---|---|---:|
{repeat_rows}

Суммарно после повтора: `{dict(repeated_counts)}`. Это повтор тех же 360 сочетаний, а не дополнительная независимая выборка объявлений.

### Ручная сверка допущенных объектов

Область проверки: {manual['scope']}.

{manual_text}

Согласие моделей и наличие цитаты не заменяют проверку логического следования. Окончательная доступность помещения и правдивость объявления в этом аудите не подтверждены собственником.

## Сквозное ранжирование

{integration}

Сквозные запросы ограничены максимум шестью анализами окружения на сценарий и тремя сценариями. Поле `complete=false` означает, что победитель не подтверждён среди всей выборки. Наличие расчётного лидера не доказывает оптимум рынка или прибыльность.

Лимит 200 секунд установлен самим аудитом и относится ко всему сквозному запросу. Он показывает превышение этого бюджета в тесте, а не гарантированный таймаут приложения. Между ранними проверками и интеграционными вызовами прошло достаточно времени для истечения TTL отдельных оценок (900 секунд); сквозные запросы могут повторно обращаться к AI и не считаются проверкой немедленного кеша.

## Проблемы, требующие исправления

1. Проверяется наличие цитаты в объявлении, но её смысл иногда не доказывает требование. Охрана, метро, парковка и приглашение на просмотр подменяют разрешённый доступ будущих посетителей. Две модели способны согласиться с одной ошибкой.
2. Защита от доказательства только номером этажа применяется к `audience_access` и `route_to_unit`. Явное условие `request_0` о посетителях в двух сквозных результатах подтверждено только `floor`/`offered_floors`. Формальная проверка пропускает этот пробел.
3. Грузовой лифт без характеристик не подтверждает перевозку паллеты 1000 кг; первый этаж без пути разгрузки тоже не подтверждает её. Оба необоснованных допуска зафиксированы вручную.
4. Подходящие контрольные варианты теряются при изменении формулировки, порядка и размера партии. Требуется отдельная проверка полноты, а не только защита от ошибочного допуска.
5. Время первого анализа 60 объектов составляет минуты при текущем ограничении параллельности. Цель анализа за секунды этим прогоном не подтверждена.

Проблемы зафиксированы на одной неизменной версии. После этого прогона производственный код для исправления перечисленных пунктов ещё не менялся; новый контрольный прогон должен оценивать отдельную версию.

Пункт 2 дополнительно воспроизведён локально через тот же валидатор на двух сохранённых решениях: `explicit_floor_only_reproduction.json`. Это воспроизведение дефекта, а не успешно пройденный тест правильности. Команда без сетевых запросов:

```powershell
python audit/scripts/repro_explicit_floor_only_evidence.py --run {out.relative_to(ROOT).as_posix()}
```

## Что этот прогон подтверждает

Прогон проверяет текущую реализацию на существенно большем количестве повторений, а не гарантирует отсутствие будущих ошибок. Модель остаётся вероятностной; отдельный правильный ответ не означает устойчивости. Для оценки качества на реальном рынке нужны независимая разметка объявлений, подтверждение условий доступа и измеренные результаты бизнеса.

Ключи сервисов в отчёт и трассировки не включены. Настройка нового ключа проверена на обеих используемых моделях. Регрессионная проверка после настройки: **159 тестов**, ошибок 0; результаты `unit_summary.json` и `unit_tests.log` сохранены рядом с этим отчётом.

## Воспроизведение

```powershell
python audit/scripts/big_run_post_contract.py
python audit/scripts/build_big_live_report.py
```

Новый запуск создаёт отдельную базу и каталог. Путь последнего живого прогона записан в `audit/big_run_2026-11-03/LATEST.txt`. Скрипт отчёта требует завершённых контрольных повторений и не строит цифры из незавершённого плана.
'''
    (out/'REPORT.md').write_text(report,encoding='utf-8')
    (BASE/'LIVE_REPORT.md').write_text(report,encoding='utf-8')
    print(json.dumps(metrics,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
