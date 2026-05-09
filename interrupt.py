import threading
import platform
import time


class Interrupted(Exception):
    pass


_interrupted = [False]
_stop_event = None
_listener_thread = None


def reset():
    _interrupted[0] = False


def is_interrupted():
    return _interrupted[0]


def _listen_windows():
    import msvcrt
    while not _stop_event.is_set():
        if msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch == b'\x1b':
                _interrupted[0] = True
        time.sleep(0.05)


def _listen_unix():
    import tty
    import termios
    import select
    try:
        fd = open('/dev/tty', 'rb', buffering=0)
    except OSError:
        return
    old = termios.tcgetattr(fd.fileno())
    try:
        tty.setraw(fd.fileno())
        while not _stop_event.is_set():
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                ch = fd.read(1)
                if ch == b'\x1b':
                    _interrupted[0] = True
    finally:
        termios.tcsetattr(fd.fileno(), termios.TCSADRAIN, old)
        fd.close()


def start_listener():
    global _listener_thread, _stop_event
    if _listener_thread and _listener_thread.is_alive():
        return
    _stop_event = threading.Event()
    target = _listen_windows if platform.system() == 'Windows' else _listen_unix
    _listener_thread = threading.Thread(target=target, daemon=True)
    _listener_thread.start()


def stop_listener():
    if _stop_event:
        _stop_event.set()
    if _listener_thread:
        _listener_thread.join(timeout=1)
