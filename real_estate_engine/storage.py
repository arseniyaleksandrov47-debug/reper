import sqlite3
import math
import orjson
import logging
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
from typing import List, Optional, Dict, Any
from .models import CanonicalProperty
from .floor_policy import extract_floor_evidence
from .cities import runtime_root

logger = logging.getLogger("Storage")
WORKSPACE_DB_PATH = runtime_root() / "data" / "moscow_commercial.db"


class ClosingConnection(sqlite3.Connection):
    """Commit/roll back and release the file handle at the end of a storage operation."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates geodesic distance in meters between two lat/lng coordinates in C-runtime."""
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return math.inf
    r = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

class PropertyStorage:
    def __init__(self, db_path: str = "real_estate.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, factory=ClosingConnection)
        conn.create_function("HAVERSINE", 4, haversine_distance)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                dedup_hash TEXT NOT NULL,
                title TEXT,
                address TEXT,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                price_month REAL NOT NULL,
                price_sqm_year REAL NOT NULL,
                area_sqm REAL NOT NULL,
                floor INTEGER,
                has_separate_entrance BOOLEAN,
                power_kw REAL,
                url TEXT,
                photos_json TEXT,
                description TEXT,
                raw_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP,
                UNIQUE(source, external_id)
            );
            """)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(properties)")}
            if "last_seen_at" not in columns:
                # Legacy updated_at alone does not prove the advertisement was re-fetched.
                conn.execute("ALTER TABLE properties ADD COLUMN last_seen_at TIMESTAMP")
            conn.execute("""CREATE TABLE IF NOT EXISTS source_health (
                source TEXT PRIMARY KEY, last_attempt_at TIMESTAMP, last_success_at TIMESTAMP,
                status TEXT NOT NULL, details_json TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS quarantined_properties (
                source TEXT NOT NULL, external_id TEXT NOT NULL, reason TEXT NOT NULL,
                payload_json TEXT NOT NULL, last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source, external_id)
            )""")
            conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS properties_rtree USING rtree(
                id,
                min_lat, max_lat,
                min_lng, max_lng
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_properties_dedup ON properties (dedup_hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_properties_price ON properties (price_month);")
            # Repair missing/stale index entries left by earlier versions. R-Tree
            # bounds are rounded outwards, so containment avoids float noise.
            conn.execute("""
                INSERT OR REPLACE INTO properties_rtree
                    (id, min_lat, max_lat, min_lng, max_lng)
                SELECT p.id, p.lat, p.lat, p.lng, p.lng FROM properties p
                LEFT JOIN properties_rtree r ON r.id = p.id
                WHERE r.id IS NULL OR p.lat < r.min_lat OR p.lat > r.max_lat
                    OR p.lng < r.min_lng OR p.lng > r.max_lng;
            """)

    def upsert_property(self, prop: CanonicalProperty) -> bool:
        self.upsert_batch([prop])
        return True

    def upsert_batch(self, props: List[CanonicalProperty]) -> int:
        count = 0
        with self._get_connection() as conn:
            cur = conn.cursor()
            for prop in props:
                photos_str = orjson.dumps(prop.photos).decode('utf-8')
                raw_str = orjson.dumps(prop.raw_attributes).decode('utf-8')
                cur.execute("""
                INSERT INTO properties (
                    source, external_id, dedup_hash, title, address,
                    lat, lng, price_month, price_sqm_year, area_sqm,
                    floor, has_separate_entrance, power_kw, url,
                    photos_json, description, raw_json, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(source, external_id) DO UPDATE SET
                    dedup_hash=excluded.dedup_hash,
                    title=excluded.title,
                    address=excluded.address,
                    lat=excluded.lat,
                    lng=excluded.lng,
                    price_month=excluded.price_month,
                    price_sqm_year=excluded.price_sqm_year,
                    area_sqm=excluded.area_sqm,
                    floor=excluded.floor,
                    has_separate_entrance=excluded.has_separate_entrance,
                    power_kw=excluded.power_kw,
                    url=excluded.url,
                    photos_json=excluded.photos_json,
                    description=excluded.description,
                    raw_json=excluded.raw_json,
                    updated_at=CURRENT_TIMESTAMP,
                    last_seen_at=CURRENT_TIMESTAMP
                RETURNING id;
                """, (
                    prop.source.value, prop.external_id, prop.deduplication_hash, prop.title, prop.address,
                    prop.lat, prop.lng, prop.price_month, prop.price_sqm_year, prop.area_sqm,
                    prop.floor, 1 if prop.has_separate_entrance else 0, prop.power_kw, prop.url,
                    photos_str, prop.description, raw_str
                ))
                row = cur.fetchone()
                if row:
                    row_id = row[0]
                    cur.execute("""
                    INSERT OR REPLACE INTO properties_rtree (id, min_lat, max_lat, min_lng, max_lng)
                    VALUES (?, ?, ?, ?, ?);
                    """, (row_id, prop.lat, prop.lat, prop.lng, prop.lng))
                    cur.execute("DELETE FROM quarantined_properties WHERE source=? AND external_id=?",
                                (prop.source.value, prop.external_id))
                count += 1
        return count

    def quarantine_batch(self, records):
        """Retain source facts for review; never guess replacement coordinates."""
        with self._get_connection() as conn:
            conn.executemany("""INSERT INTO quarantined_properties(source,external_id,reason,payload_json)
                VALUES (?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET
                reason=excluded.reason,payload_json=excluded.payload_json,last_seen_at=CURRENT_TIMESTAMP""",
                [(p.source.value, p.external_id, reason, orjson.dumps(asdict(p)).decode("utf-8")) for p,reason in records])

    def record_source_health(self, source: str, details: Dict[str, Any]):
        status = details["status"]
        with self._get_connection() as conn:
            conn.execute("""INSERT INTO source_health
                (source, last_attempt_at, last_success_at, status, details_json)
                VALUES (?, CURRENT_TIMESTAMP, CASE WHEN ? IN ('ok', 'empty') THEN CURRENT_TIMESTAMP END, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    last_attempt_at=CURRENT_TIMESTAMP,
                    last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
                    status=excluded.status, details_json=excluded.details_json""",
                (source, status, status, orjson.dumps(details).decode("utf-8")))

    def find_within_radius_rtree(self, target_lat: float, target_lng: float, radius_meters: float = 500.0) -> List[sqlite3.Row]:
        """
        Microsecond spatial search using SQLite R-Tree virtual table index,
        followed by exact Haversine distance ordering.
        """
        if (not all(math.isfinite(v) for v in (target_lat,target_lng,radius_meters))
                or not -90 <= target_lat <= 90 or not -180 <= target_lng <= 180 or radius_meters <= 0):
            raise ValueError("Некорректные координаты или радиус геопоиска")
        # Exact bounding box of a spherical cap. The planar approximation loses
        # candidates in wide circles, around poles and across the date line.
        angular = min(math.pi, radius_meters / 6371000.0)
        phi = math.radians(target_lat)
        min_lat = math.degrees(max(-math.pi/2, phi-angular))
        max_lat = math.degrees(min(math.pi/2, phi+angular))
        wraps = False
        if phi-angular <= -math.pi/2 or phi+angular >= math.pi/2:
            min_lng, max_lng = -180.0, 180.0
        else:
            delta = math.degrees(math.asin(min(1.0, math.sin(angular)/math.cos(phi))))
            min_lng, max_lng = target_lng-delta, target_lng+delta
            if min_lng < -180 or max_lng > 180:
                min_lng, max_lng = (min_lng+180)%360-180, (max_lng+180)%360-180
                wraps = True
        longitude_filter = "(r.max_lng >= ? OR r.min_lng <= ?)" if wraps else "r.max_lng >= ? AND r.min_lng <= ?"

        query = f"""
        SELECT p.*, HAVERSINE(?, ?, p.lat, p.lng) as distance_m
        FROM properties_rtree r
        JOIN properties p ON r.id = p.id
        WHERE r.max_lat >= ? AND r.min_lat <= ?
          AND {longitude_filter}
          AND HAVERSINE(?, ?, p.lat, p.lng) <= ?
        ORDER BY distance_m ASC;
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(query, (
                target_lat, target_lng,
                min_lat, max_lat,
                min_lng, max_lng,
                target_lat, target_lng, radius_meters
            ))
            rows = cur.fetchall()
            # Read-only historical snapshots may predate the quarantine table.
            has_quarantine = conn.execute("SELECT 1 FROM sqlite_master WHERE name='quarantined_properties'").fetchone()
            if has_quarantine:
                excluded = {(r[0], r[1]) for r in conn.execute("SELECT source,external_id FROM quarantined_properties")}
                rows = [r for r in rows if (r["source"], r["external_id"]) not in excluded]
            return rows

    def find_candidates(
        self,
        target_lat: float,
        target_lng: float,
        radius_meters: float = 2500.0,
        address_keywords: Optional[List[str]] = None,
        min_area_sqm: Optional[float] = None,
        max_area_sqm: Optional[float] = None,
        max_price_month: Optional[float] = None,
        max_price_sqm_year: Optional[float] = None,
        max_listing_age_hours: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Отбирает уникальные помещения строго внутри радиуса и заданных фильтров.
        Ключевые слова адреса не расширяют территорию поиска.
        """
        cutoff = None
        if max_listing_age_hours is not None:
            if not math.isfinite(max_listing_age_hours) or max_listing_age_hours <= 0:
                raise ValueError("max_listing_age_hours должен быть положительным конечным числом")
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_listing_age_hours)
        rows = self.find_within_radius_rtree(target_lat, target_lng, radius_meters=radius_meters)
        results_by_id: Dict[int, Dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            results_by_id[int(d["id"])] = d

        # Фильтрация по параметрам помещения и дедупликация физических объектов
        filtered: List[Dict[str, Any]] = []
        seen_dedup: set = set()
        # Prefer the latest observed copy before deduplicating cross-platform advertisements.
        newest_first = sorted(results_by_id.values(), key=lambda x: (str(x.get("last_seen_at") or ""), -float(x.get("distance_m") or 0)), reverse=True)
        for item in newest_first:
            if cutoff is not None:
                try:
                    last_seen = datetime.fromisoformat(item.get("last_seen_at") or "").replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if last_seen < cutoff:
                    continue
            area = float(item.get("area_sqm") or 0.0)
            price_m = float(item.get("price_month") or 0.0)
            price_sqm = float(item.get("price_sqm_year") or 0.0)

            if min_area_sqm is not None and (not math.isfinite(area) or area <= 0 or area < min_area_sqm):
                continue
            if max_area_sqm is not None and (not math.isfinite(area) or area <= 0 or area > max_area_sqm):
                continue
            if max_price_month is not None and (not math.isfinite(price_m) or price_m <= 0 or price_m > max_price_month):
                continue
            if max_price_sqm_year is not None and (not math.isfinite(price_sqm) or price_sqm <= 0 or price_sqm > max_price_sqm_year):
                continue

            dedup = item.get("dedup_hash")
            # Identical area/rent/coordinates on different floors are different
            # candidates. Unknown floors must not hide a confirmed ground floor.
            floor_key = extract_floor_evidence(item).floors
            dedup_key = (dedup, floor_key)
            if dedup and dedup_key in seen_dedup:
                continue
            if dedup:
                seen_dedup.add(dedup_key)
            filtered.append(item)

        return sorted(filtered, key=lambda x: float(x.get("distance_m") or 0.0))


    def get_statistics(self) -> dict:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT source, COUNT(*), AVG(price_month), AVG(price_sqm_year) FROM properties GROUP BY source;")
            by_source = {row[0]: {"count": row[1], "avg_month": round(row[2] or 0), "avg_sqm_year": round(row[3] or 0)} for row in cur.fetchall()}
            cur.execute("SELECT COUNT(DISTINCT dedup_hash || '|floor=' || COALESCE(CAST(floor AS TEXT), '?')), COUNT(*) FROM properties;")
            unique_count, total_count = cur.fetchone()
            health = {r["source"]: {**orjson.loads(r["details_json"]),
                      "last_attempt_at": r["last_attempt_at"], "last_success_at": r["last_success_at"]}
                      for r in conn.execute("SELECT * FROM source_health")}
            quarantined_count = conn.execute("SELECT COUNT(*) FROM quarantined_properties").fetchone()[0]
            return {
                "total_offers": total_count,
                "unique_physical_spaces": unique_count,
                "by_source": by_source,
                "source_health": health,
                "quarantined_offers": quarantined_count,
            }
