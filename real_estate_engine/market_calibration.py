"""Explicit client/benchmark utility references, separate from AI assumptions."""
import copy
import math

REFERENCES={'residents','workers','site_audience','synergy','company_revenue'}


def validate_market_references(data):
    required={'source','kind','references'}
    optional={'competition_scale','outside_pressure','access_reference_minutes'}
    if (not isinstance(data,dict) or not required.issubset(data) or set(data)-required-optional or
            not isinstance(data['source'],str) or not data['source'].strip() or
            data['kind'] not in ('client_reference','validated_benchmark') or
            not isinstance(data['references'],dict) or set(data['references'])-REFERENCES):
        raise ValueError('Invalid market-context reference provenance')
    values=[*data['references'].values(),*(data[k] for k in optional if k in data)]
    if not values or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<v<=1e15 for v in values):
        raise ValueError('Market references must be finite positive utility levels')
    return data


def apply_market_references(config,data):
    validate_market_references(data)
    config=copy.deepcopy(config)
    reason=data['kind']+': '+data['source']
    for key,value in data['references'].items():config['references'][key]=dict(value=value,reason=reason)
    for key in ('competition_scale','outside_pressure'):
        if key in data:config[key]=dict(value=data[key],reason=reason)
    if 'access_reference_minutes' in data:config['access']['reference_minutes']=data['access_reference_minutes']
    paths=['references.'+key for key in data['references']]
    paths.extend(key for key in ('competition_scale','outside_pressure') if key in data)
    if 'access_reference_minutes' in data:paths.append('access.reference_minutes')
    return config,dict(kind=data['kind'],source=data['source'],parameter_paths=sorted(paths),
        note='Provided reference provenance; the program does not independently certify business outcomes')
