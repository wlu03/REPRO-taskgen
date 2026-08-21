from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .utils import path_within, utc_now


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.data_dir = self.root / "data"
        self.source_dir = self.root / "source"
        self.state_dir = self.root / "state"
        self.logs_dir = self.root / "logs"
        self.catalog_path = self.root / "catalog.json"
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.errors_path = self.logs_dir / "errors.jsonl"
        for directory in (self.root, self.data_dir, self.source_dir, self.state_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def record_dir(self, record_id: str) -> Path:
        path = self.data_dir / record_id
        if not path_within(path, self.data_dir):
            raise ValueError(f"Refusing to create a record directory outside data/: {record_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        if not path_within(resolved, self.root):
            raise ValueError(f"Path is outside the output root: {path}")
        return resolved.relative_to(self.root).as_posix()

    def resolve_relative(self, value: str) -> Path:
        """Resolve a catalog/checkpoint path while keeping it inside the output root."""
        candidate = (self.root / value).resolve()
        if not path_within(candidate, self.root):
            raise ValueError(f"Stored path escapes the output root: {value}")
        return candidate

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def read_bytes(self, path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def write_text(self, path: Path, value: str) -> None:
        self._atomic_write(path, value.encode("utf-8"))

    def write_bytes(self, path: Path, value: bytes) -> None:
        self._atomic_write(path, value)

    def write_json(self, path: Path, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        self._atomic_write(path, body.encode("utf-8"))

    def _atomic_write(self, path: Path, body: bytes) -> None:
        if not path_within(path, self.root):
            raise ValueError(f"Refusing to write outside output root: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def append_error(
        self,
        *,
        stage: str,
        message: str,
        record_id: str | None = None,
        url: str | None = None,
        error_type: str | None = None,
    ) -> None:
        payload = {
            "timestamp": utc_now(),
            "record_id": record_id,
            "stage": stage,
            "url": url,
            "error_type": error_type,
            "message": message,
        }
        self.errors_path.parent.mkdir(parents=True, exist_ok=True)
        with self.errors_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def load_checkpoint(self) -> dict[str, Any]:
        return self.read_json(self.checkpoint_path) or {
            "schema_version": "1.0",
            "updated_at": None,
            "source_url": None,
            "records": {},
        }

    def save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        checkpoint["updated_at"] = utc_now()
        self.write_json(self.checkpoint_path, checkpoint)
