"""Durable, query-populated item knowledge; independent of game experience.

    The immutable upstream package remains the authority for cache repair.
    Cached source rows are rendered through the current usage policy on reads.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock


class ItemStoreError(RuntimeError): pass
class InvalidCachedItem(ValueError): pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class QueriedItemStore:
    def __init__(self, path):
        # Lazy initialization lets the SDK reject a path in the source package
        # before any SQLite write can alter its immutable files.
        self.path=Path(path).resolve()
        self._ready=False
        self._lock=Lock()

    def _ensure(self):
        with self._lock:
            if self._ready:return
            try:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                with closing(sqlite3.connect(self.path,timeout=3)) as db, db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS item_knowledge_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS item_knowledge(
                            provider_id TEXT NOT NULL,dataset_id TEXT NOT NULL,kind TEXT NOT NULL,item_id INTEGER NOT NULL,
                            requested_language TEXT NOT NULL,source_language TEXT NOT NULL,
                            source_sha256 TEXT NOT NULL,record_json TEXT NOT NULL,record_sha256 TEXT NOT NULL,
                            first_queried_at TEXT NOT NULL,last_fetched_at TEXT NOT NULL,
                            PRIMARY KEY(provider_id,dataset_id,kind,item_id,requested_language));
                        INSERT OR IGNORE INTO item_knowledge_meta VALUES('schema_version','1');
                    """)
                    if db.execute("SELECT value FROM item_knowledge_meta WHERE key='schema_version'").fetchone()[0]!="1":
                        raise ItemStoreError("ITEM_KNOWLEDGE_SCHEMA_MISMATCH")
                self._ready=True
            except (sqlite3.Error,OSError) as exc:raise ItemStoreError("ITEM_KNOWLEDGE_UNAVAILABLE") from exc

    @contextmanager
    def connect(self):
        self._ensure()
        db=None
        try:
            db=sqlite3.connect(self.path,timeout=3)
            yield db
            db.commit()
        except (sqlite3.Error,OSError) as exc:
            if db:db.rollback()
            raise ItemStoreError("ITEM_KNOWLEDGE_UNAVAILABLE") from exc
        finally:
            if db:db.close()

    def get(self,key):
        with self.connect() as db:
            row=db.execute("SELECT record_json,record_sha256,source_sha256 FROM item_knowledge WHERE provider_id=? AND dataset_id=? AND kind=? AND item_id=? AND requested_language=?",key).fetchone()
        if row is None:return None
        raw,digest,source_sha=row
        if not all(isinstance(value,str) for value in row):raise InvalidCachedItem("ITEM_KNOWLEDGE_INVALID_RECORD")
        if hashlib.sha256(raw.encode("utf-8")).hexdigest()!=digest:raise InvalidCachedItem("ITEM_KNOWLEDGE_HASH_MISMATCH")
        try:item=json.loads(raw)
        except (ValueError,TypeError) as exc:raise InvalidCachedItem("ITEM_KNOWLEDGE_INVALID_RECORD") from exc
        return item,source_sha

    def save(self,key,item,source_sha):
        raw=encoded(item);digest=hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now=datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("""INSERT INTO item_knowledge VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider_id,dataset_id,kind,item_id,requested_language) DO UPDATE SET
                  source_language=excluded.source_language,source_sha256=excluded.source_sha256,
                  record_json=excluded.record_json,record_sha256=excluded.record_sha256,last_fetched_at=excluded.last_fetched_at
                """,(*key,item["language"],source_sha,raw,digest,now,now))

    def list_items(self,*,provider_id,dataset_id,limit=100):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError("Invalid item list limit")
        with self.connect() as db:
            rows=db.execute("SELECT kind,item_id,requested_language,source_language,record_json,first_queried_at,last_fetched_at,record_sha256 FROM item_knowledge WHERE provider_id=? AND dataset_id=? ORDER BY kind,item_id,requested_language LIMIT ?",(provider_id,dataset_id,limit)).fetchall()
        items=[]
        for r in rows:
            try:
                if not isinstance(r[4],str) or hashlib.sha256(r[4].encode("utf-8")).hexdigest()!=r[7]:raise ValueError("hash mismatch")
                label=json.loads(r[4])["name"]
                if not isinstance(label,str):raise ValueError("invalid name")
            except (ValueError,TypeError,KeyError) as exc:
                raise ItemStoreError(f"ITEM_KNOWLEDGE_INVALID_RECORD: requery {r[0]}:{r[1]} to repair") from exc
            items.append({"kind":r[0],"id":r[1],"requested_language":r[2],"source_language":r[3],
                          "label":label,"first_queried_at":r[5],"last_fetched_at":r[6]})
        return items
