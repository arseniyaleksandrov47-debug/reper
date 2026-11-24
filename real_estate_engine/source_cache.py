"""Small persistent TTL cache for downloaded public source snapshots."""
import gzip
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class SourceCache:
    def __init__(self,path='.local-cache/public_sources.sqlite3',max_entries=96):
        self.path=Path(path);self.max_entries=max_entries

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(self.path,timeout=2)
        try:conn.execute('CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, fetched REAL, expires REAL, body BLOB)')
        except BaseException:conn.close();raise
        return conn

    def get(self,key):
        if not self.path.is_file():return None
        try:
            with closing(self._connect()) as conn:
                row=conn.execute('SELECT fetched,expires,body FROM sources WHERE key=?',(key,)).fetchone()
            if row and row[1]>time.time():return row[0],gzip.decompress(row[2])
        except (OSError,sqlite3.Error,ValueError,EOFError):pass
        return None

    def put(self,key,body,ttl):
        if len(body)>16000000:return
        try:
            now=time.time()
            with closing(self._connect()) as conn,conn:
                conn.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?,?)',(key,now,now+ttl,gzip.compress(body)))
                conn.execute('DELETE FROM sources WHERE expires<=?',(now,))
                conn.execute('DELETE FROM sources WHERE key IN (SELECT key FROM sources ORDER BY fetched DESC LIMIT -1 OFFSET ?)',(self.max_entries,))
        except (OSError,sqlite3.Error):pass
