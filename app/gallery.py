"""Persistent gallery for generated images and videos.

The /api/image/generate and /api/video/generate routes already return
base64 payloads to the client, but the client (and operator) lose them
as soon as the page reloads. This module persists every successful
generation to SQLite so the operator can browse, re-download, and
delete them from the Gallery tab.

We deliberately store the binary (not the base64) so the API can stream
the file back as image/png or video/mp4 on demand.
"""
from __future__ import annotations

import base64
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .settings import settings


MediaKind = Literal["image", "video"]
MediaSource = Literal["openai", "sora", "dalle", "user_upload", "external", "test"]


@dataclass
class GalleryItem:
    id: str
    owner: str
    kind: MediaKind
    source: MediaSource
    mime: str
    filename: str
    prompt: str
    model: str
    size: str | None
    width: int | None
    height: int | None
    seconds: int | None
    bytes_size: int
    external_id: str | None     # OpenAI file_id or video id
    external_url: str | None    # OpenAI url for downloading
    created_at: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "kind": self.kind,
            "source": self.source,
            "mime": self.mime,
            "filename": self.filename,
            "prompt": self.prompt,
            "model": self.model,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "seconds": self.seconds,
            "bytes_size": self.bytes_size,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


class GalleryStore:
    def __init__(self) -> None:
        path = Path(settings.notes_db_path).with_name("gallery.sqlite3")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS gallery (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    model TEXT NOT NULL,
                    size TEXT,
                    width INTEGER,
                    height INTEGER,
                    seconds INTEGER,
                    bytes_size INTEGER NOT NULL,
                    external_id TEXT,
                    external_url TEXT,
                    data BLOB,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_gallery_owner_created ON gallery(owner, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_gallery_owner_kind ON gallery(owner, kind, created_at DESC)")

    # ---- Public API ----

    def status(self) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS total, COALESCE(SUM(bytes_size),0) AS bytes_total FROM gallery").fetchone()
            images = db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(bytes_size),0) AS b FROM gallery WHERE kind='image'").fetchone()
            videos = db.execute("SELECT COUNT(*) AS c, COALESCE(SUM(bytes_size),0) AS b FROM gallery WHERE kind='video'").fetchone()
        return {
            "available": True,
            "persistent": False,
            "total": int(row["total"] or 0),
            "bytes_total": int(row["bytes_total"] or 0),
            "images_count": int(images["c"] or 0),
            "images_bytes": int(images["b"] or 0),
            "videos_count": int(videos["c"] or 0),
            "videos_bytes": int(videos["b"] or 0),
        }

    def add(
        self,
        *,
        owner: str,
        kind: MediaKind,
        source: MediaSource,
        mime: str,
        filename: str,
        prompt: str,
        model: str,
        size: str | None = None,
        width: int | None = None,
        height: int | None = None,
        seconds: int | None = None,
        data: bytes | None = None,
        b64: str | None = None,
        external_id: str | None = None,
        external_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GalleryItem:
        if data is None and b64 is not None:
            try:
                data = base64.b64decode(b64, validate=False)
            except Exception as exc:
                raise ValueError(f"invalid_base64: {exc}") from exc
        if not data:
            raise ValueError("data_required (bytes or b64)")
        if len(data) > 50_000_000:  # 50 MB cap per item
            raise ValueError("data_too_large (>50MB)")
        if not mime or len(mime) > 80: raise ValueError("invalid_mime")
        if not filename or len(filename) > 200: raise ValueError("invalid_filename")
        if not prompt: prompt = ""
        if len(prompt) > 4000: raise ValueError("prompt_too_long")
        if not model or len(model) > 80: raise ValueError("invalid_model")
        now = int(time.time())
        item_id = f"gal_{uuid.uuid4().hex[:16]}"
        with self._connect() as db:
            db.execute("""
                INSERT INTO gallery(id, owner, kind, source, mime, filename, prompt, model, size, width, height, seconds, bytes_size, external_id, external_url, data, metadata_json, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (item_id, owner, kind, source, mime, filename, prompt, model, size, width, height, seconds, len(data), external_id, external_url, data, _json(metadata or {}), now))
        return self.get(item_id)  # type: ignore[return-value]

    def get(self, item_id: str) -> GalleryItem | None:
        with self._connect() as db: row = db.execute("SELECT * FROM gallery WHERE id = ?", (item_id,)).fetchone()
        if not row: return None
        item = self._row(row)
        # Attach data on demand
        item._data = row["data"]  # type: ignore[attr-defined]
        return item

    def get_data(self, item_id: str) -> bytes | None:
        with self._connect() as db: row = db.execute("SELECT data FROM gallery WHERE id = ?", (item_id,)).fetchone()
        return row["data"] if row else None

    def list(self, owner: str, *, kind: MediaKind | None = None, limit: int = 100, offset: int = 0) -> list[GalleryItem]:
        limit = max(1, min(limit, 200)); offset = max(0, offset)
        sql = "SELECT * FROM gallery WHERE owner = ?"; params: list[Any] = [owner]
        if kind: sql += " AND kind = ?"; params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"; params.extend([limit, offset])
        with self._connect() as db: rows = db.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def delete(self, owner: str, item_id: str) -> bool:
        with self._connect() as db: cursor = db.execute("DELETE FROM gallery WHERE id = ? AND owner = ?", (item_id, owner))
        return cursor.rowcount > 0

    @staticmethod
    def _row(row: sqlite3.Row) -> GalleryItem:
        import json
        return GalleryItem(
            id=row["id"],
            owner=row["owner"],
            kind=row["kind"],
            source=row["source"],
            mime=row["mime"],
            filename=row["filename"],
            prompt=row["prompt"] or "",
            model=row["model"],
            size=row["size"],
            width=row["width"],
            height=row["height"],
            seconds=row["seconds"],
            bytes_size=row["bytes_size"],
            external_id=row["external_id"],
            external_url=row["external_url"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )


def _json(d: dict[str, Any]) -> str:
    import json
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


gallery = GalleryStore()
