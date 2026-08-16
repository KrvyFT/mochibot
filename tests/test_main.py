import socket
import sys

import pytest

from mochi.main import _admin_port_available


def test_admin_port_check_rejects_active_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    try:
        assert not _admin_port_available("127.0.0.1", listener.getsockname()[1])
    finally:
        listener.close()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows socket reuse differs")
def test_admin_port_check_allows_time_wait_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    client = socket.create_connection(("127.0.0.1", port))
    connection, _ = listener.accept()
    connection.shutdown(socket.SHUT_WR)
    client.recv(1)
    client.close()
    connection.close()
    listener.close()

    assert _admin_port_available("127.0.0.1", port)
