"""Condition transport exposure on mapped paths, never on centroid proximity.

Passenger numbers and exposure shares remain explicit scenario proxies, not
measured customers. Missing routes retain unknown bounds and no lower reward.
"""
import copy
import math
import re
from .accessibility import (
    PedestrianNetwork, Route, best_arrival_to_entry, combine_candidate_entry_routes, resolve_access_route,
)
from .storage import haversine_distance


def _name(value):return re.sub(r'[^а-яa-z0-9]','',str(value).casefold().replace('ё','е'))


def _origins(evidence):
    values=list(evidence.get('accessibility_diagnostics',{}).get('arrivals',[]))
    origin=evidence.get('access_routes',{}).get('walk',{}).get('origin')
    if isinstance(origin,dict):values.append(origin)
    return list({v['node']:v for v in values if isinstance(v,dict) and v.get('verified') is True
                 and v.get('source') and isinstance(v.get('node'),str)}.values())


def _matches(source,origins):
    exact=[v for v in origins if v.get('transport_source_id')==source['source_id']]
    if exact:return exact,'explicit_source_identity'
    if source['kind']=='metro_exit':
        if source.get('entrance_nodes'):
            identifiers=set(source['entrance_nodes'])
            return [v for v in origins if v.get('kind')=='metro_exit' and v['node'] in identifiers],'exact_mapped_entrance_node'
        name=_name(source['name'])
        return [v for v in origins if v.get('kind')=='metro_exit' and name and
                name in {_name(n) for n in [v.get('name',''),*v.get('station_names',[])]}],'mapped_station_name_or_stop_area'
    # This identifies an existing stop node; it creates no geometric graph edge.
    # Ambiguous nearby stops, missing names and opposing stops stay unbound.
    found=[]
    for v in origins:
        if v.get('kind')!='public_transport_stop' or not _name(source['name']) or _name(source['name'])!=_name(v.get('name')):continue
        try:distance=haversine_distance(source['lat'],source['lng'],v['lat'],v['lng'])
        except (KeyError,TypeError,ValueError):continue
        if distance<=15:found.append(v)
    return (found if len(found)==1 else []),'unique_stop_name_and_coordinate_match_within_15m'


def condition_transport_on_routes(traffic,evidence):
    if traffic is None or not getattr(traffic,'transport_routing_required',False):return traffic
    traffic=copy.deepcopy(traffic)
    sources=getattr(traffic,'transport_sources',[]) or []
    registry=_origins(evidence)
    walk_request=evidence.get('access_routes',{}).get('walk',{})
    diag=evidence.get('accessibility_diagnostics') or {}
    ambiguous_mode=(walk_request.get('entry') is None and diag.get('missing_reason')=='ambiguous_building_entrance'
                    and diag.get('building_matches',0)>=1 and bool(diag.get('entrance_candidates')))
    candidate_entries=[walk_request['entry']] if walk_request.get('entry') is not None else (
        list(diag['entrance_candidates']) if ambiguous_mode else [])
    entry=walk_request.get('entry')
    resolved=resolve_access_route(evidence,'walk')
    selected_origin=walk_request.get('origin',{})
    network=None;failure=None
    try:
        if evidence.get('pedestrian_network') is not None:network=PedestrianNetwork(evidence['pedestrian_network'])
    except (TypeError,ValueError) as exc:failure=type(exc).__name__
    routes_by_entry=[{v['node']:r for v,r in zip(registry,network.travel_many_to(registry,cand))}
                     for cand in candidate_entries] if network is not None and candidate_entries else []
    records=[];total_low=0;total_high=0;known=0
    for source in sources:
        arrivals,binding=_matches(source,registry)
        entry_outcomes=[]
        if resolved['status']!='conflict' and routes_by_entry:
            for cand,graph_routes in zip(candidate_entries,routes_by_entry):
                routes=[]
                for origin in arrivals:
                    measured=(entry is not None and cand==entry and origin.get('node')==selected_origin.get('node')
                        and isinstance(entry,dict) and entry.get('verified') is True and entry.get('source')
                        and evidence.get('walk_time_minutes',{}).get('verified') is True
                        and resolved['status']=='reachable' and resolved.get('minutes') is not None)
                    if measured:
                        minute=resolved['minutes'];routes.append(Route('reachable',minute,resolved['source'],minute,minute))
                    else:
                        routes.append(graph_routes[origin['node']])
                entry_outcomes.append(best_arrival_to_entry(routes))
        state,low_time,high_time=combine_candidate_entry_routes(entry_outcomes)
        base_low,base_high=source['base_lower_daily'],source['base_upper_daily']
        if state=='unreachable':lo=hi=0;source_known=True
        elif state=='reachable':
            decay=lambda t:math.exp(-max(0,t-source['grace_minutes'])/source['decay_minutes'])
            lo=base_low*decay(high_time) if high_time is not None else 0
            hi=base_high*decay(low_time) if base_high is not None else None
            source_known=base_high is not None and high_time is not None
        else:lo=0;hi=base_high;source_known=False
        total_low+=lo
        total_high=None if total_high is None or hi is None else total_high+hi
        known+=source_known
        records.append(dict(source_id=source['source_id'],name=source['name'],kind=source['kind'],
            source=source['source'],as_of=source['as_of'],binding=binding,matched_origin_nodes=[v['node'] for v in arrivals],
            route_status=state,minutes_lower=low_time,minutes_upper=high_time,
            contribution_lower_daily=lo,contribution_upper_daily=hi,known=source_known,
            exposure_assumption={k:source[k] for k in ('decay_minutes','grace_minutes')},
            route_source=network.source if network else None,route_measurement_status=resolved['status']))
    # Each station has its own exposure envelope and path decay. Redistributing
    # shares by route rank can perversely reward making a small station farther
    # away, because a large station inherits its tier. No station-rank bonuses.
    metro=[(source,record) for source,record in zip(sources,records) if source['kind']=='metro_exit']
    for source,record in metro:
        low=record['minutes_lower'];high=record['minutes_upper']
        bounds=source.get('exposure_share_range')
        if bounds is None:
            # Previously saved source snapshots used rank tiers. Their min/max
            # remain explicit assumptions; the old rank ordering is not replayed.
            tiers=source['exposure_share_tiers'];bounds=(min(tiers),max(tiers))
        share_low,share_high=bounds
        if not 0<=share_low<=share_high<=1:raise ValueError('Invalid metro exposure bounds')
        record['exposure_assumption'].update(station_share_lower=share_low,station_share_upper=share_high,
            priority_basis='source_exposure_envelope_without_rank_redistribution')
        base_low=.85*source['station_passengers_lower']*share_low
        base_high=.85*source['station_passengers_upper']*share_high if source['station_passengers_upper'] is not None else None
        if record['route_status']=='unreachable':lo=hi=0
        elif record['route_status']=='reachable':
            decay=lambda t:math.exp(-max(0,t-source['grace_minutes'])/source['decay_minutes'])
            lo=base_low*decay(high) if high is not None else 0
            hi=base_high*decay(low) if base_high is not None else None
        else:lo=0;hi=base_high
        record['contribution_lower_daily']=lo;record['contribution_upper_daily']=hi
    total_low=sum(r['contribution_lower_daily'] for r in records)
    total_high=(sum(r['contribution_upper_daily'] for r in records)
                if all(r['contribution_upper_daily'] is not None for r in records) else None)
    diagnostics=dict(applied=True,method='mapped_walk_time_exposure_v2',sources=records,
        matched_entry=entry,matched_entries=candidate_entries if ambiguous_mode else ([entry] if entry is not None else []),
        entry_scope=walk_request.get('scope') or ('building_entrance_interval' if ambiguous_mode else None),
        graph_source=network.source if network else None,graph_as_of=network.as_of if network else None,
        graph_coverage_complete=network.complete if network else False,error_type=failure,
        status='bounded' if sources else 'unknown',
        warnings=['Транспортная аудитория — сценарный прокси, не измеренная проходимость или покупатели',
                  'При неизвестном маршруте нижний вклад равен нулю, верхний остаётся неопределённым',
                  'Один пассажиропоток станции не суммируется по нескольким выходам'])
    traffic.transport_route_diagnostics=diagnostics
    traffic.transport_lower_daily=total_low if sources else None
    traffic.transport_upper_daily=total_high if sources else None
    traffic.transport_source_coverage=known/len(sources) if sources else 0
    traffic.transport_data_status='route_complete' if sources and known==len(sources) else 'route_partial' if sources else 'unavailable'
    for field in ('estimated_daily_pedestrians','estimated_weekday_daily','estimated_weekend_daily',
                  'estimated_avg_hourly_daytime','estimated_peak_hourly','traffic_index'):
        setattr(traffic,field,None)
    traffic.data_status='route_conditioned_proxy'
    traffic.traffic_level='Маршрутный диапазон транспортной аудитории; замера у входа нет'
    for kind,field in (('metro_exit','metro_contribution_daily'),('public_transport_stop','stops_contribution_daily')):
        setattr(traffic,field,int(round(sum(r['contribution_lower_daily'] for r in records if r['kind']==kind))))
    traffic.data_warnings=list(dict.fromkeys([*traffic.data_warnings,*diagnostics['warnings']]))
    return traffic
