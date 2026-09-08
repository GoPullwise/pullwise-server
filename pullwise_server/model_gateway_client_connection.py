from __future__ import annotations

import select
import socket
import ssl
import threading
from contextlib import suppress


class ClientConnection:
    """Serialize socket probes with writes; never block waiting for a TLS record."""
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.lock = threading.RLock()

    def disconnected(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            if self.connection.fileno() < 0:
                return True
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            timeout = self.connection.gettimeout()
            try:
                self.connection.setblocking(False)
                return self.connection.recv(1) == b""
            except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                return False
            except OSError:
                return True
            finally:
                with suppress(OSError):
                    self.connection.settimeout(timeout)
        except (OSError, ValueError):
            return True
        finally:
            self.lock.release()
