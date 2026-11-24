"""Pedestrian travel on an explicitly supplied network, without centroid snapping.

Edges describe actual pedestrian connections (including crossings/entrances).
The caller owns their provenance. A missing path in a partial network is unknown,
not evidence of an inaccessible building. No address-word or straight-line bonus.
"""
import heapq
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    status: str
    minutes: float = None
    source: str = ''
    minutes_lower: float = None
    minutes_upper: float = None


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def best_arrival_to_entry(routes):
    """Aggregate arrival routes for a single entrance without inventing missing waits."""
    reachable = [r for r in routes if r.status == 'reachable']
    if reachable:
        low = min(r.minutes_lower for r in reachable)
        uppers = [r.minutes_upper for r in reachable if r.minutes_upper is not None]
        return 'reachable', low, (min(uppers) if uppers else None)
    if routes and all(r.status == 'unreachable' for r in routes):
        return 'unreachable', None, None
    return 'unknown', None, None


def combine_candidate_entry_routes(entry_outcomes):
    """Bound walk time across multiple mapped candidate entrances of one building."""
    if not entry_outcomes:
        return 'unknown', None, None
    if all(state == 'unreachable' for state, _, _ in entry_outcomes):
        return 'unreachable', None, None
    reachable = [(low, high) for state, low, high in entry_outcomes if state == 'reachable']
    if reachable:
        min_low = min(low for low, _ in reachable)
        all_bounded = len(reachable) == len(entry_outcomes) and all(high is not None for _, high in reachable)
        max_high = max(high for _, high in reachable) if all_bounded else None
        return 'reachable', min_low, max_high
    return 'unknown', None, None


def resolve_access_route(evidence,kind):
    """Resolve a supplied verified time or a path between verified graph nodes.

    Never snap an advertisement centroid to an entrance. A partial graph's
    missing path remains unknown; contradictory direct/derived times defer.
    """
    metric='walk_time_minutes' if kind=='walk' else 'delivery_time_minutes'
    direct=evidence.get(metric,{})
    number=direct.get('value') if isinstance(direct,dict) and direct.get('verified') is True and isinstance(direct.get('source'),str) and direct['source'].strip() else None
    if type(number) not in (int,float) or not math.isfinite(number) or number<0:number=None
    routes=evidence.get('access_routes',{})
    request=routes.get(kind) if isinstance(routes,dict) else None
    if request is not None:
        data=evidence.get('pedestrian_network')
        if kind!='walk':
            if number is not None:return dict(status='reachable',minutes=number,source=direct['source'],method='supplied_verified_time')
            return dict(status='unknown',minutes=None,source='',reason='Пешеходный граф не устанавливает время доставки')
        if data is None or not isinstance(request,dict):
            if number is None:return dict(status='unknown',minutes=None,source='',reason='Нет сети для указанного маршрута')
        else:
            try:
                network=PedestrianNetwork(data)
                result=network.travel(request.get('origin'),request.get('entry'))
            except (ValueError,TypeError):
                return dict(status='unknown',minutes=None,source='',reason='Неподтверждённые параметры сети')
            if number is not None and (result.status=='unreachable' or result.status=='reachable' and (
                    number<result.minutes_lower-1e-9 or result.minutes_upper is not None and number>result.minutes_upper+1e-9)):
                return dict(status='conflict',minutes=None,source=network.source,reason='Время и пешеходная сеть расходятся')
            if result.status!='unknown' or number is None:
                return dict(status=result.status,minutes=number if number is not None else result.minutes,
                            minutes_lower=number if number is not None else result.minutes_lower,
                            minutes_upper=number if number is not None else result.minutes_upper,
                            source=network.source,as_of=network.as_of,
                            method='pedestrian_network',coverage_complete=network.complete,
                            scope=request.get('scope','unit_entrance'),
                            time_kind=network_data_time_kind(data))
    diag=evidence.get('accessibility_diagnostics') or {}
    if (kind=='walk' and isinstance(evidence.get('pedestrian_network'),dict)
            and diag.get('missing_reason')=='ambiguous_building_entrance'
            and diag.get('building_matches',0)>=1 and diag.get('entrance_candidates') and diag.get('arrivals')):
        data=evidence['pedestrian_network']
        try:
            network=PedestrianNetwork(data)
            outcomes=[best_arrival_to_entry(network.travel_many_to(diag['arrivals'],cand))
                      for cand in diag['entrance_candidates']]
        except (ValueError,TypeError):
            outcomes=[]
        if outcomes:
            status,low,high=combine_candidate_entry_routes(outcomes)
            if number is not None and (status=='unreachable' or status=='reachable' and (
                    number<low-1e-9 or high is not None and number>high+1e-9)):
                return dict(status='conflict',minutes=None,source=network.source,reason='Время и пешеходная сеть расходятся')
            if status!='unknown' or number is None:
                exact=number if number is not None else (low if high is not None and math.isclose(low,high) else None)
                return dict(status=status,minutes=exact,
                            minutes_lower=number if number is not None else low,
                            minutes_upper=number if number is not None else high,
                            source=network.source,as_of=network.as_of,
                            method='pedestrian_network',coverage_complete=network.complete,
                            scope='building_entrance_interval',time_kind=network_data_time_kind(data))
    if number is not None:return dict(status='reachable',minutes=number,source=direct['source'],method='supplied_verified_time')
    return dict(status='unknown',minutes=None,source='',reason='Нет подтверждённого маршрута')


def network_data_time_kind(data):
    return data.get('time_kind','supplied_network_travel_time')


class PedestrianNetwork:
    def __init__(self, data):
        if not isinstance(data, dict) or not isinstance(data.get('source'), str) or not data['source'].strip():
            raise ValueError('Pedestrian network needs a source')
        if not isinstance(data.get('as_of'), str) or not data['as_of'].strip():
            raise ValueError('Pedestrian network needs an as_of date')
        if type(data.get('coverage_complete')) is not bool:
            raise ValueError('Declare pedestrian network coverage')
        nodes, edges = data.get('nodes'), data.get('edges')
        if not isinstance(nodes, list) or not nodes or len(nodes) > 100000:
            raise ValueError('Invalid pedestrian nodes')
        if not all(isinstance(n, str) and n for n in nodes) or len(set(nodes)) != len(nodes):
            raise ValueError('Pedestrian node IDs must be unique strings')
        if not isinstance(edges, list) or len(edges) > 400000:
            raise ValueError('Invalid pedestrian edges')
        self.source = data['source']
        self.as_of = data['as_of']
        self.complete = data['coverage_complete']
        self.graph = {n: {} for n in nodes}
        self.reverse = {n: {} for n in nodes}
        self.upper_graph={n:{} for n in nodes};self.upper_reverse={n:{} for n in nodes}
        for edge in edges:
            if not isinstance(edge, dict) or edge.get('from') not in self.graph or edge.get('to') not in self.graph:
                raise ValueError('Unknown pedestrian edge endpoint')
            if not _positive(edge.get('minutes')) or type(edge.get('walk_allowed')) is not bool or type(edge.get('bidirectional')) is not bool:
                raise ValueError('Edge needs positive minutes and explicit pedestrian permissions')
            if not edge['walk_allowed']:
                continue
            wait_low=edge.get('wait_minutes_lower',0);wait_high=edge.get('wait_minutes_upper',0)
            if (type(wait_low) not in (int,float) or not math.isfinite(wait_low) or wait_low<0 or
                    wait_high is not None and (type(wait_high) not in (int,float) or not math.isfinite(wait_high) or wait_high<wait_low)):
                raise ValueError('Invalid crossing delay bounds')
            a, b, minutes = edge['from'], edge['to'], float(edge['minutes'])
            self._edge(a, b, minutes+wait_low,None if wait_high is None else minutes+wait_high)
            if edge['bidirectional']:
                self._edge(b, a, minutes+wait_low,None if wait_high is None else minutes+wait_high)
        self._cache = {}

    def _edge(self, a, b, minutes, upper):
        self.graph[a][b] = min(minutes, self.graph[a].get(b, math.inf))
        self.reverse[b][a] = min(minutes, self.reverse[b].get(a, math.inf))
        if upper is not None:
            self.upper_graph[a][b]=min(upper,self.upper_graph[a].get(b,math.inf))
            self.upper_reverse[b][a]=min(upper,self.upper_reverse[b].get(a,math.inf))

    @staticmethod
    def _verified_node(endpoint):
        if (not isinstance(endpoint, dict) or endpoint.get('verified') is not True
                or not isinstance(endpoint.get('source'), str) or not endpoint['source'].strip()
                or not isinstance(endpoint.get('node'), str)):
            return None
        return endpoint['node']

    def _distances(self, node, reverse=False, upper=False):
        key = node, reverse, upper
        if key in self._cache:
            return self._cache[key]
        graph=(self.upper_reverse if reverse else self.upper_graph) if upper else (self.reverse if reverse else self.graph)
        distances = {node: 0.0}
        pending = [(0.0, node)]
        while pending:
            duration, current = heapq.heappop(pending)
            if duration != distances[current]:
                continue
            for target, minutes in graph[current].items():
                total = duration + minutes
                if not math.isfinite(total):
                    raise ValueError('Pedestrian path duration overflow')
                if total < distances.get(target, math.inf):
                    distances[target] = total
                    heapq.heappush(pending, (total, target))
        # Bound memory even when a large export contains many cohort origins.
        if len(self._cache) >= 32:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = distances
        return distances

    def travel(self, origin, destination):
        a, b = self._verified_node(origin), self._verified_node(destination)
        if a not in self.graph or b not in self.graph:
            return Route('unknown', source=self.source)
        minutes = self._distances(a).get(b)
        if minutes is not None:
            upper=self._distances(a,upper=True).get(b)
            return Route('reachable',minutes if upper is not None and math.isclose(minutes,upper) else None,
                         self.source,minutes,upper)
        return Route('unreachable' if self.complete else 'unknown', source=self.source)

    def travel_many_to(self,origins,destination):
        """Two reverse searches serve every origin for this one entrance."""
        target=self._verified_node(destination)
        if target not in self.graph:return [Route('unknown',source=self.source) for _ in origins]
        lower=self._distances(target,reverse=True);upper=self._distances(target,reverse=True,upper=True)
        result=[]
        for origin in origins:
            node=self._verified_node(origin)
            if node not in self.graph:result.append(Route('unknown',source=self.source));continue
            low,high=lower.get(node),upper.get(node)
            if low is None:
                result.append(Route('unreachable' if self.complete else 'unknown',source=self.source));continue
            result.append(Route('reachable',low if high is not None and math.isclose(low,high) else None,
                                self.source,low,high))
        return result

    def isochrone(self, endpoint, minutes, *, towards_entry=False):
        if not _positive(minutes):
            raise ValueError('Isochrone horizon must be positive')
        node = self._verified_node(endpoint)
        if node not in self.graph:
            return {'status': 'unknown', 'nodes': {}, 'coverage_complete': self.complete}
        distances = self._distances(node, reverse=towards_entry)
        upper=self._distances(node,reverse=towards_entry,upper=True)
        return {'status': 'ok', 'nodes': {n: t for n, t in distances.items() if t <= minutes},
                'guaranteed_nodes':{n:t for n,t in upper.items() if t<=minutes},
                'nodes_meaning':'potentially_within_horizon; guaranteed_nodes includes bounded waiting',
                'coverage_complete': self.complete, 'source': self.source, 'as_of': self.as_of}

    def detour(self, home, entry, metro_exit):
        routes = [self.travel(home, entry), self.travel(entry, metro_exit), self.travel(home, metro_exit)]
        if any(r.status != 'reachable' for r in routes):
            return {'status': 'unknown', 'extra_minutes': None, 'source': self.source}
        if any(r.minutes is None for r in routes):
            low=max(0,routes[0].minutes_lower+routes[1].minutes_lower-routes[2].minutes_upper) if routes[2].minutes_upper is not None else 0
            high=(max(0,routes[0].minutes_upper+routes[1].minutes_upper-routes[2].minutes_lower)
                  if all(r.minutes_upper is not None for r in routes[:2]) else None)
            return dict(status='bounded',extra_minutes=None,extra_minutes_lower=low,extra_minutes_upper=high,
                        source=self.source,meaning='route_plausibility_only')
        return {'status': 'ok', 'extra_minutes': max(0.0, routes[0].minutes + routes[1].minutes - routes[2].minutes),
                'source': self.source, 'meaning': 'route_plausibility_only'}
