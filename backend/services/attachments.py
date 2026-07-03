from __future__ import annotations

import io
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from backend.core.config import DATA_DIR, RUNTIME_DIR


ATTACHMENTS_DIR = RUNTIME_DIR / "attachments"
ATTACHMENT_DB = DATA_DIR / "attachments.sqlite"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_EDGE = 2048
THUMB_EDGE = 360

ALLOWED_IMAGE_MIMES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class StoredAttachment:
    attachment_id: str
    session_id: str
    type: str
    mime_type: str
    size: int
    width: int
    height: int
    storage_path: str
    thumb_path: str
    created_at: str

    def to_meta(self, *, referenced: bool = False) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "session_id": self.session_id,
            "type": self.type,
            "mime_type": self.mime_type,
            "size": self.size,
            "width": self.width,
            "height": self.height,
            "thumb_url": f"/api/attachments/{self.attachment_id}/thumb",
            "view_url": f"/api/attachments/{self.attachment_id}",
            "created_at": self.created_at,
            "referenced": referenced,
        }


class AttachmentService:
    def __init__(self, db_path: Path = ATTACHMENT_DB, root: Path = ATTACHMENTS_DIR) -> None:
        self.db_path = db_path
        self.root = root
        self._lock = threading.RLock()
        self._init_db()

    def save_upload(self, *, session_id: str, filename: str, content_type: str, data: bytes) -> dict[str, Any]:
        clean_session = self._safe_session_id(session_id)
        mime_type = self._normalize_mime(content_type)
        if mime_type not in ALLOWED_IMAGE_MIMES:
            raise ValueError("仅支持 png/jpeg/webp/gif 图片")
        if not data:
            raise ValueError("图片为空")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 10MB 限制")

        image = self._load_image(data)
        width, height = image.size
        attachment_id = f"att_{uuid4().hex[:20]}"
        ext = ALLOWED_IMAGE_MIMES[mime_type]
        session_dir = self.root / clean_session
        session_dir.mkdir(parents=True, exist_ok=True)
        original_path = session_dir / f"{attachment_id}{ext}"
        thumb_path = session_dir / f"{attachment_id}.thumb.webp"

        normalized_data = self._normalize_original(data, image, mime_type)
        original_path.write_bytes(normalized_data)
        self._write_thumbnail(image, thumb_path)

        created_at = _now()
        storage_rel = self._relative_runtime_path(original_path)
        thumb_rel = self._relative_runtime_path(thumb_path)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                insert into attachments(
                    attachment_id, session_id, type, mime_type, size, width, height,
                    storage_path, thumb_path, original_filename, created_at
                )
                values (?, ?, 'image', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attachment_id,
                    clean_session,
                    mime_type,
                    len(normalized_data),
                    width,
                    height,
                    storage_rel,
                    thumb_rel,
                    filename[:240],
                    created_at,
                ),
            )
        return self.get(attachment_id).to_meta()

    def get(self, attachment_id: str) -> StoredAttachment:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                select attachment_id, session_id, type, mime_type, size, width, height,
                       storage_path, thumb_path, created_at
                from attachments
                where attachment_id=?
                limit 1
                """,
                (attachment_id,),
            ).fetchone()
        if row is None:
            raise KeyError(attachment_id)
        return StoredAttachment(
            attachment_id=str(row["attachment_id"]),
            session_id=str(row["session_id"]),
            type=str(row["type"] or "image"),
            mime_type=str(row["mime_type"]),
            size=int(row["size"] or 0),
            width=int(row["width"] or 0),
            height=int(row["height"] or 0),
            storage_path=str(row["storage_path"]),
            thumb_path=str(row["thumb_path"]),
            created_at=str(row["created_at"]),
        )

    def list_for_session(self, session_id: str, attachment_ids: list[str], *, referenced: bool = False) -> list[dict[str, Any]]:
        if not attachment_ids:
            return []
        result: list[dict[str, Any]] = []
        clean_session = self._safe_session_id(session_id)
        seen: set[str] = set()
        for attachment_id in attachment_ids:
            if not attachment_id or attachment_id in seen:
                continue
            seen.add(attachment_id)
            item = self.get(str(attachment_id))
            if item.session_id != clean_session:
                raise PermissionError(f"attachment {attachment_id} does not belong to this session")
            result.append(item.to_meta(referenced=referenced))
        return result

    def file_path(self, attachment_id: str, *, thumbnail: bool = False) -> tuple[Path, str]:
        item = self.get(attachment_id)
        rel_path = item.thumb_path if thumbnail else item.storage_path
        path = (RUNTIME_DIR / rel_path).resolve()
        runtime_root = RUNTIME_DIR.resolve()
        if runtime_root not in path.parents and path != runtime_root:
            raise PermissionError("attachment path escaped runtime directory")
        if not path.exists():
            raise FileNotFoundError(attachment_id)
        mime = "image/webp" if thumbnail else item.mime_type
        return path, mime

    def read_as_data_url(self, attachment_id: str) -> str:
        item = self.get(attachment_id)
        path, _ = self.file_path(attachment_id, thumbnail=False)
        import base64

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{item.mime_type};base64,{encoded}"

    def delete_session(self, session_id: str) -> None:
        clean_session = self._safe_session_id(session_id)
        session_dir = self.root / clean_session
        with self._lock, self._connect() as conn:
            conn.execute("delete from attachments where session_id=?", (clean_session,))
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)

    def _init_db(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists attachments (
                    attachment_id text primary key,
                    session_id text not null,
                    type text not null,
                    mime_type text not null,
                    size integer not null,
                    width integer not null,
                    height integer not null,
                    storage_path text not null,
                    thumb_path text not null,
                    original_filename text default '',
                    created_at text not null
                )
                """
            )
            conn.execute("create index if not exists idx_attachments_session on attachments(session_id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_image(self, data: bytes) -> Image.Image:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("无法识别图片文件") from exc
        if image.width <= 0 or image.height <= 0:
            raise ValueError("图片尺寸无效")
        return image

    def _normalize_original(self, data: bytes, image: Image.Image, mime_type: str) -> bytes:
        if mime_type == "image/gif":
            return data
        if max(image.size) <= MAX_IMAGE_EDGE:
            return data
        image = image.copy()
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
        output = io.BytesIO()
        if mime_type == "image/jpeg":
            image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
        elif mime_type == "image/png":
            image.save(output, format="PNG", optimize=True)
        elif mime_type == "image/webp":
            image.save(output, format="WEBP", quality=90)
        return output.getvalue()

    def _write_thumbnail(self, image: Image.Image, path: Path) -> None:
        thumb = image.copy()
        if getattr(thumb, "is_animated", False):
            thumb.seek(0)
        thumb.thumbnail((THUMB_EDGE, THUMB_EDGE))
        thumb.convert("RGB").save(path, format="WEBP", quality=82)

    def _relative_runtime_path(self, path: Path) -> str:
        return path.resolve().relative_to(RUNTIME_DIR.resolve()).as_posix()

    def _normalize_mime(self, content_type: str) -> str:
        return str(content_type or "").split(";", 1)[0].strip().lower()

    def _safe_session_id(self, session_id: str) -> str:
        value = str(session_id or "default").strip()
        if not value:
            return "default"
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:120]


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


attachment_service = AttachmentService()
