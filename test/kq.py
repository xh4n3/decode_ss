import select
from collections import defaultdict
import time

POLL_NULL = 0x00
POLL_IN = 0x01
POLL_OUT = 0x04
POLL_ERR = 0x08
POLL_HUP = 0x10
POLL_NVAL = 0x20


class EventLoop(object):

    MAX_EVENTS = 1024

    def __init__(self):
        self._kqueue = select.kqueue()
        self._fd = {}
        self._fdmap = {}  # (f, handler)
        self._stopping = False

    def poll(self, timeout):
        if timeout < 0:
            timeout = None
        events = self._kqueue.control(None, EventLoop.MAX_EVENTS, timeout)
        results = defaultdict(lambda: POLL_NULL)
        for e in events:
            fd = e.ident
            if e.filter == select.KQ_FILTER_READ:
                results[fd] |= POLL_IN
            elif e.filter == select.KQ_FILTER_WRITE:
                results[fd] |= POLL_OUT
        return results.items()

    def _control(self, fd, mode, flags):
        events = []
        if mode & POLL_IN:
            events.append(select.kevent(fd, select.KQ_FILTER_READ, flags))
        # seems have problem here, cause mode = 2 and POLL_OUT = 0x04,
        # this condition will never be true.
        if mode & POLL_OUT:
            events.append(select.kevent(fd, select.KQ_FILTER_WRITE, flags))
        # why is here a for loop.
        for e in events:
            self._kqueue.control([e], 100)

    def register(self, f, mode, handler):
        fd = f.fileno()
        self._fdmap[fd] = (f, handler)
        self._fd[fd] = mode
        # KQ_EV_ADD = 1
        self._control(fd, mode, select.KQ_EV_ADD)

    def unregister(self, fd):
        del self._fdmap[fd]
        # KQ_EV_DELETE = 2
        self._control(fd, self._fd[fd], select.KQ_EV_DELETE)
        del self._fd[fd]

    def run(self):
        events = []
        while not self._stopping:
            try:
                events = self.poll(10)
            except (OSError, IOError) as e:
                pass
            for sock, fd, event in events:
                handler = self._fdmap.get(fd, None)
                if handler is not None:
                    handler = handler[1]
                    try:
                        handler.handle_event(sock, fd, event)
                    except (OSError, IOError) as e:
                        pass

    def close(self):
        self._kqueue.close()
