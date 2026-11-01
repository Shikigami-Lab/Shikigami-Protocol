"""Windows asyncio transport cleanup compatibility.

CPython's Proactor transport calls socket.shutdown() before releasing the socket.
A peer reset (WinError 10054) can interrupt that cleanup. Handle this error only
at the shutdown call so sockets are released and unrelated errors remain visible.
"""
import logging
import socket
import sys
from functools import wraps
from inspect import signature

logger = logging.getLogger(__name__)


def install_windows_transport_fix() -> bool:
    """Install the process-local Proactor cleanup fix once; leave other platforms alone."""
    if sys.platform != "win32":
        return False

    from asyncio.proactor_events import _ProactorBasePipeTransport
    from asyncio.base_events import Server

    original = _ProactorBasePipeTransport._call_connection_lost
    if getattr(original, "_shikigami_reset_safe", False):
        return True
    detach_with_transport = len(signature(Server._detach).parameters) > 1

    @wraps(original)
    def _call_connection_lost(transport, exc):
        if transport._called_connection_lost:
            return
        try:
            transport._protocol.connection_lost(exc)
        finally:
            sock = transport._sock
            try:
                if hasattr(sock, "shutdown") and sock.fileno() != -1:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except ConnectionResetError as error:
                        if getattr(error, "winerror", None) != 10054:
                            raise
                        logger.debug("Peer reset during Windows transport shutdown; releasing socket")
            finally:
                sock.close()
                transport._sock = None
                server = transport._server
                if server is not None:
                    if detach_with_transport:
                        server._detach(transport)
                    else:
                        server._detach()
                    transport._server = None
                transport._called_connection_lost = True

    _call_connection_lost._shikigami_reset_safe = True
    _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost
    return True
