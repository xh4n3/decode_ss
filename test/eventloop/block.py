from _socket import AF_INET, SOCK_STREAM, SO_REUSEADDR, SOL_SOCKET
import select
from socket import socket


class EventLoop(object):

    KQ_FILTER_READ = select.KQ_FILTER_READ

    def __init__(self):
        self._fd_map = {}
        self._handler_map = {}
        self.kq = select.kqueue()
        self.klist = []
        self._stop = False

    def run(self):
        while not self._stop:
            events = self.poll()
            for e in events:
                self._fd_map[e.ident](self._handler_map[e.ident])

    def poll(self):
        events = self.kq.control(self.klist, 1, None)
        return events

    def add(self, f, mode, handler):
        fd = f.fileno()
        event = select.kevent(fd, filter=mode, flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR)
        self._handler_map[fd] = f
        self._fd_map[fd] = handler
        self.klist.append(event)

    def remove(self):
        pass

    def add_periodic(self):
        pass

    def remove_periodic(self):
        pass

    def modify(self):
        pass

    def stop(self):
        self._stop = True

    def __del__(self):
        pass


def test():
    loop = EventLoop()
    s = socket(AF_INET, SOCK_STREAM)
    s.bind(("127.0.0.1", 3009))
    s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    s.listen(5)

    # This blocks the whole callback
    def handler(fd):
        cl, _ = fd.accept()
        while True:
            data = cl.recv(1024)
            print repr(data)
            if not data:
                cl.close()
                break
        loop.stop()
        s.close()

    loop.add(s, EventLoop.KQ_FILTER_READ, handler)
    loop.run()

if __name__ == '__main__':
    test()