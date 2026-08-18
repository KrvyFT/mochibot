import socket
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
