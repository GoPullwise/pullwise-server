from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path


class JsonlGatewayAuditSink:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_metadata = self._path.parent.lstat()
        if not stat.S_ISDIR(parent_metadata.st_mode) or self._path.parent.is_symlink():
            raise ValueError("gateway audit parent must be a real directory")
        if self._path.exists():
            metadata = self._path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or self._path.is_symlink():
                raise ValueError("gateway audit path must be a regular file")
        self._lock = threading.Lock()

    def __call__(self, event: dict[str, object]) -> None:
        if not isinstance(event, dict) or event.get("schema_id") != "pullwise-model-gateway-audit/v1":
            raise ValueError("gateway audit event is invalid")
        encoded = (json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(encoded) > 16 * 1024:
            raise ValueError("gateway audit event is too large")
        with self._lock:
            descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

