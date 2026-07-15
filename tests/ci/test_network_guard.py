import socket

import pytest
import pytest_socket
import requests

from scripts.ci.pytest_network_guard import (
    ExternalNetworkBlocked,
    guarded_getaddrinfo,
)


@pytest.fixture()
def loopback_only_socket(monkeypatch):
    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(
        ["localhost", "127.0.0.1", "::1"],
        allow_unix_socket=True,
    )
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
    pytest_socket.enable_socket()


def test_loopback_socket_is_allowed(loopback_only_socket):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=1):
            connection, _ = server.accept()
            connection.close()


def test_external_literal_socket_is_blocked(loopback_only_socket):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.01)
        with pytest.raises(pytest_socket.SocketConnectBlockedError):
            client.connect(("203.0.113.10", 443))


def test_external_dns_is_blocked_with_nodeid(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/ci/test_network_guard.py::dns_case (call)")

    with pytest.raises(ExternalNetworkBlocked, match="tests/ci/test_network_guard.py::dns_case"):
        guarded_getaddrinfo("external.invalid", 443)


def test_http_client_cannot_bypass_dns_guard(loopback_only_socket):
    with pytest.raises(ExternalNetworkBlocked):
        requests.get("https://external.invalid/fictional", timeout=0.01)
