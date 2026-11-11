import errno
import os
from contextlib import contextmanager


@contextmanager
def player_lock(path):
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise BlockingIOError("Player state is locked.") from exc
                raise
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
