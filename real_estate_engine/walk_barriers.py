"""Conservative geometric checks for mapped pedestrian barrier crossings.

Geometry can veto a dubious edge. It never invents a connection or a crossing.
"""
import math


_RAIL={'rail','light_rail','narrow_gauge','tram','subway'}
_ROADS={'motorway','motorway_link','trunk','trunk_link','primary','primary_link',
        'secondary','secondary_link','tertiary','tertiary_link'}
_WALLS={'fence','wall','hedge','retaining_wall','city_wall'}


def _yes(value):return value not in (None,'','no','false','0')


def _level(tags):
    try:
        level=float(tags['layer'])
    except (KeyError,TypeError,ValueError):return 1 if _yes(tags.get('bridge')) else -1 if _yes(tags.get('tunnel')) else 0
    if not math.isfinite(level):raise ValueError('Invalid mapped layer')
    return level


def _separate(a,b):
    return _level(a)!=_level(b) and any(_yes(t.get(k)) for t in (a,b) for k in ('bridge','tunnel'))


def _cross(a,b,c,d):
    """Return one intersection point, overlap, or None in local metre units."""
    r=(b[0]-a[0],b[1]-a[1]);s=(d[0]-c[0],d[1]-c[1]);q=(c[0]-a[0],c[1]-a[1])
    det=r[0]*s[1]-r[1]*s[0]
    if abs(det)<1e-9:
        if abs(q[0]*r[1]-q[1]*r[0])>1e-6:return None
        size=r[0]*r[0]+r[1]*r[1]
        if size<=0:return None
        lo,hi=sorted(((q[0]*r[0]+q[1]*r[1])/size,
                      ((d[0]-a[0])*r[0]+(d[1]-a[1])*r[1])/size))
        lo,hi=max(0,lo),min(1,hi)
        if hi<lo-1e-9:return None
        if hi-lo>1e-7:return 'overlap'
        return (a[0]+lo*r[0],a[1]+lo*r[1])
    t=(q[0]*s[1]-q[1]*s[0])/det;u=(q[0]*r[1]-q[1]*r[0])/det
    if not (-1e-9<=t<=1+1e-9 and -1e-9<=u<=1+1e-9):return None
    return (a[0]+t*r[0],a[1]+t*r[1])


class BarrierIndex:
    def __init__(self,nodes,ways):
        self.nodes=nodes;latitude=next(iter(nodes.values()))['lat'] if nodes else 0
        self.scale=111320*math.cos(math.radians(latitude));self.grid={};self.segments=[]
        self.points={k:(v['lng']*self.scale,v['lat']*111320) for k,v in nodes.items()}
        for way in ways:
            tags=way['tags'];kind=None
            if tags.get('barrier') in _WALLS:kind='barrier'
            elif tags.get('railway') in _RAIL:kind='railway'
            elif tags.get('highway') in _ROADS or tags.get('motorroad')=='yes':kind='road'
            elif tags.get('waterway') in ('river','canal','stream','riverbank') or tags.get('natural')=='water':kind='water'
            if kind is None:continue
            for a,b in zip(way['nodes'],way['nodes'][1:]):
                if a not in self.points or b not in self.points or self.points[a]==self.points[b]:continue
                index=len(self.segments);self.segments.append((a,b,way,kind))
                for cell in self._cells(self.points[a],self.points[b]):self.grid.setdefault(cell,[]).append(index)

    @staticmethod
    def _cells(a,b):
        x1,x2=sorted((math.floor(a[0]/50),math.floor(b[0]/50)))
        y1,y2=sorted((math.floor(a[1]/50),math.floor(b[1]/50)))
        if (x2-x1+1)*(y2-y1+1)>10000:raise ValueError('Mapped segment exceeds bounded local geometry')
        return ((x,y) for x in range(x1,x2+1) for y in range(y1,y2+1))

    def conflict(self,a,b,way):
        indices={i for cell in self._cells(self.points[a],self.points[b]) for i in self.grid.get(cell,())}
        for i in sorted(indices):
            c,d,obstacle,kind=self.segments[i]
            if obstacle['id']==way['id'] or _separate(way['tags'],obstacle['tags']):continue
            point=_cross(self.points[a],self.points[b],self.points[c],self.points[d])
            if point is None:continue
            shared={a,b}&{c,d}
            if point!='overlap':
                shared={n for n in shared if math.hypot(self.points[n][0]-point[0],self.points[n][1]-point[1])<.1}
                for node in shared:
                    tags=self.nodes[node]['tags']
                    permitted=tags.get('foot') not in ('no','private') and tags.get('access') not in ('no','private')
                    if not permitted or tags.get('locked')=='yes':continue
                    if kind=='railway' and tags.get('railway') in ('crossing','level_crossing'):break
                    if kind=='road' and tags.get('highway')=='crossing' and tags.get('crossing')!='no':break
                    if kind=='barrier' and tags.get('barrier') in ('entrance','gate','lift_gate','swing_gate','kissing_gate','stile') and (
                            tags.get('foot') in ('yes','designated','permissive') or tags.get('access') in ('yes','permissive')):break
                    if kind=='water' and (tags.get('ford')=='yes' or way['tags'].get('ford')=='yes'):break
                else:return dict(kind=kind,way_id=obstacle['id'],reason='unverified_at_grade_crossing')
                continue
            return dict(kind=kind,way_id=obstacle['id'],reason='walk_edge_overlaps_barrier')
        return None
