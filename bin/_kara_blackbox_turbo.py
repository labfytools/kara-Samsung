"""Pass a bounded, immutable decoder input to the existing BlackBox writer."""
import fcntl
import os
import socket
from struct import Struct

HEADER = Struct("<QII")  # Window ID, call index, input offset in the window.
MAX_BYTES = 29 * 16000 * 2
CHUNK = 32768
SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL


def submit(channel, window, call, offset, pcm):
    # Diagnostic overload must neither stall inference nor create a second
    # persistent recording. The socket owns a sealed memory fd in transit.
    if not 0 < len(pcm) <= MAX_BYTES or len(pcm) % 2:
        return False
    fd = None
    try:
        fd = os.memfd_create("kara-turbo-input", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        if os.write(fd, pcm) != len(pcm):
            return False
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
        return socket.send_fds(channel, [HEADER.pack(window, call, offset)], [fd], socket.MSG_DONTWAIT) == HEADER.size
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def receive(channel, consume):
    # The parent drains this side channel within an existing worker response.
    # A slice never escapes the fd lifetime; the recorder copies admitted data.
    for _ in range(24):
        try:
            header, received, flags, _ = socket.recv_fds(channel, HEADER.size, 1, socket.MSG_CMSG_CLOEXEC)
        except BlockingIOError:
            break
        try:
            if len(received) != 1 or len(header) != HEADER.size or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
                continue
            fd = received[0]
            size = os.fstat(fd).st_size
            if not 0 < size <= MAX_BYTES or size % 2 or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS != SEALS:
                continue
            consume(HEADER.unpack(header), fd, size)
        except OSError:
            continue
        finally:
            for fd in received:
                os.close(fd)
