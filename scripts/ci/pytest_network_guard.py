"""CI-only pytest plugin that blocks external DNS before socket connection."""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any


_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ALLOWED_NAMES = {"localhost"}


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test attempts external name resolution."""


def _is_loopback_host(host: Any) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    value = str(host).strip().lower().rstrip(".")
    if value in _ALLOWED_NAMES:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
    if not _is_loopback_host(host):
        current_test = os.environ.get("PYTEST_CURRENT_TEST", "<collection>")
        nodeid = current_test.rsplit(" (", 1)[0]
        raise ExternalNetworkBlocked(
            f"CI network guard blocked external DNS/socket access in {nodeid}"
        )
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def pytest_sessionstart(session) -> None:
    socket.getaddrinfo = guarded_getaddrinfo


def pytest_sessionfinish(session, exitstatus) -> None:
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
