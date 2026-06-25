from __future__ import annotations

import os
import shutil
import tempfile
import threading
from unittest.mock import patch

from pullwise_server import db


_LOCK = threading.Lock()
_TEMPLATE_DIR = tempfile.TemporaryDirectory()
_TEMPLATE_PATHS: dict[tuple[str, str], str] = {}


def start_fast_sqlite_connections(testcase) -> None:
    original_connect = db.connect

    def fast_connect():
        connection = original_connect()
        connection.execute("PRAGMA journal_mode=MEMORY")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    patcher = patch.object(db, "connect", fast_connect)
    patcher.start()
    testcase.addCleanup(patcher.stop)


def install_initialized_db_template(target_path: str, *, worker_token: str = "", worker_id: str = "") -> None:
    template_path = initialized_db_template(worker_token=worker_token, worker_id=worker_id)
    copy_sqlite_database(template_path, target_path)
    mark_database_initialized(target_path)


def initialized_db_template(*, worker_token: str = "", worker_id: str = "") -> str:
    key = (str(worker_token or ""), str(worker_id or ""))
    with _LOCK:
        existing = _TEMPLATE_PATHS.get(key)
        if existing and os.path.exists(existing):
            return existing
        template_path = os.path.join(_TEMPLATE_DIR.name, f"template_{len(_TEMPLATE_PATHS)}.sqlite3")
        env = {
            "PULLWISE_DB_PATH": template_path,
            "PULLWISE_WORKER_TOKEN": key[0],
            "PULLWISE_WORKER_ID": key[1],
        }
        with patch.dict(os.environ, env, clear=False):
            db.reset_initialization_cache()
            db.initialize()
            with db.connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.reset_initialization_cache()
        _TEMPLATE_PATHS[key] = template_path
        return template_path


def copy_sqlite_database(source_path: str, target_path: str) -> None:
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        target = target_path + suffix
        if os.path.exists(target):
            os.unlink(target)
    shutil.copyfile(source_path, target_path)
    for suffix in ("-wal", "-shm"):
        source = source_path + suffix
        if os.path.exists(source):
            shutil.copyfile(source, target_path + suffix)


def mark_database_initialized(path: str) -> None:
    absolute = os.path.abspath(path)
    with db._INITIALIZE_LOCK:
        db._INITIALIZED_DATABASES.add(absolute)