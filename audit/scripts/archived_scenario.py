"""Adapt recorded audience decisions for historical audit replay."""
from real_estate_engine.market_context import validate_audience_usage


def load_audience_usage(scenario):
    sources = scenario['sources']
    usage = {}
    for name, item in scenario['audience_usage'].items():
        quote = item.get('waiver_quote')
        if item['usage'] == 'waived' and not quote:
            quote = sources[item['source_id']]
        usage[name] = dict(usage=item['usage'], source_id=item['source_id'],
                           reason=item['reason'], waiver_quote=quote)
    return validate_audience_usage(usage, sources, scenario)
