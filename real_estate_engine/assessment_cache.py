"""Bounded local cache; callers decide which verified assessments are reusable."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class AssessmentCache:
    def __init__(self, path, max_entries=512):
        self.path = Path(path)
        self.max_entries = max_entries

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=1)
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS assessments '
                         '(key TEXT PRIMARY KEY, expires REAL NOT NULL, payload TEXT NOT NULL)')
        except BaseException:
            conn.close()
            raise
        return conn

    def get(self, key):
        if not self.path.is_file():
            return None
        try:
            with closing(self._connect()) as conn:
                row = conn.execute('SELECT expires,payload FROM assessments WHERE key=?', (key,)).fetchone()
            if row and row[0] > time.time():
                return row[0], json.loads(row[1])
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass  # A broken optional cache cannot prevent a fresh assessment.
        return None

    def put(self, key, value, ttl):
        if ttl <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
            now = time.time()
            with closing(self._connect()) as conn, conn:
                conn.execute('INSERT OR REPLACE INTO assessments VALUES (?,?,?)', (key, now + ttl, payload))
                conn.execute('DELETE FROM assessments WHERE expires<=?', (now,))
                conn.execute('DELETE FROM assessments WHERE key IN '
                             '(SELECT key FROM assessments ORDER BY expires DESC,key LIMIT -1 OFFSET ?)',
                             (self.max_entries,))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
