"""Client-owned utility anchors, independent of the candidate collection.

No rent/demand magnitude or niche reference is invented here. AI-generated
0..100 placeholders are not ruble/customer thresholds. A missing anchor is
explicitly unscored until supplied; weights are never redistributed.
"""
import math


def validate_ranking_scales(data):
    if (not isinstance(data, dict) or not {'concept_sha256','metrics'}.issubset(data) or set(data)-{'concept_sha256', 'metrics','market_context'}
            or not isinstance(data['concept_sha256'], str) or len(data['concept_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in data['concept_sha256'])
            or not isinstance(data['metrics'], dict)):
        raise ValueError('ranking_scales: нужны concept_sha256 и metrics')
    if 'market_context' in data:
        from .market_calibration import validate_market_references
        validate_market_references(data['market_context'])
    # Local import avoids coupling the evidence adapter to ranking generation.
    from .ranking_policy import METRICS
    for name, anchors in data['metrics'].items():
        if name not in METRICS or not isinstance(anchors, dict) or set(anchors) != {'low', 'high', 'source'}:
            raise ValueError('ranking_scales: неизвестный показатель или неверные поля')
        low, high = anchors['low'], anchors['high']
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (low, high))
                or low >= high or (low < 0 and name != 'net_contribution_month')
                or (name == 'passing_traffic_index' and high > 100)
                or not isinstance(anchors['source'], str) or not anchors['source'].strip()):
            raise ValueError('ranking_scales: нужны конечные low < high и источник ориентира')
        if name=='market_fit_index' and (low!=0 or high!=100):
            raise ValueError('market_fit_index: определённый кодом домен 0..100')


def reference_for(criterion, profile, supplied=None):
    """Return explicit anchors or an unresolved record, never sample extrema."""
    if supplied is not None:
        validate_ranking_scales(supplied)
        from .business_model import concept_digest
        if supplied['concept_sha256'] != concept_digest(profile.concept):
            raise ValueError('Шкалы оценки относятся к другой концепции')
        item = supplied['metrics'].get(criterion.metric)
        if item is not None:
            return dict(item, usable=True, origin='client_reference')
    if criterion.metric in ('passing_traffic_index','market_fit_index'):
        return dict(low=0.0, high=100.0, source='defined_index_domain', usable=True, origin='metric_domain')
    # A Python caller can supply a fully specified profile. Inference is marked
    # explicitly so that model placeholders never acquire this trust.
    if profile.metadata.get('scale_origin') != 'ai_placeholders':
        return dict(low=criterion.low, high=criterion.high, source='caller_profile', usable=True, origin='caller_profile')
    if criterion.preference == 'range':
        return dict(low=criterion.low, high=criterion.high, source='concept_area_preference', usable=True, origin='concept_preference')
    constraints = profile.metadata.get('client_constraints', {})
    if criterion.metric == 'rent_month' and criterion.preference == 'lower':
        budget = constraints.get('max_price_month')
        if type(budget) in (int, float) and math.isfinite(budget) and budget > 0:
            return dict(low=0.0, high=budget, source='client_max_price_month', usable=True, origin='client_budget',
                        assumption='Линейная шкала относительно бюджета аренды; это предпочтение, не рыночная оценка')
    return dict(low=None, high=None, source='missing_client_reference', usable=False, origin='unresolved')
