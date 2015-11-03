from _socket import AF_INET, SOCK_STREAM, SO_REUSEADDR, SOL_SOCKET
import select
from socket import socket


class KqueueEventLoop(object):

    KQ_FILTER_READ = select.KQ_FILTER_READ

    def __init__(self):
        self._fd_map = {}
        self._handler_map = {}
        self._event_map = {}
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
        self._event_map[fd] = event
        self.klist.append(event)

    def remove(self, f):
        fd = f.fileno()
        del self._handler_map[fd]
        del self._fd_map[fd]
        self.klist.remove(self._event_map[fd])

    def add_periodic(self):
        pass

    def remove_periodic(self):
        pass

    def stop(self):
        self._stop = True


def test():
    loop = KqueueEventLoop()
    s = socket(AF_INET, SOCK_STREAM)
    s.bind(("127.0.0.1", 3004))
    s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    s.listen(5)

    def handler(f):
        print 'INFO: New connection established.'
        cl, _ = f.accept()
        cl.setblocking(False)
        loop.add(cl, KqueueEventLoop.KQ_FILTER_READ, read_data)

    def read_data(cl):
        try:
            data = cl.recv(1024)
            if not data:
                print 'INFO: Connection dropped.'
                loop.remove(cl)
                cl.close()
            else:
                print 'DATA: %s' % repr(data)
        except Exception, e:
            print 'ERROR: %s' % repr(e)
            loop.remove(cl)
            cl.close()

    loop.add(s, KqueueEventLoop.KQ_FILTER_READ, handler)
    loop.run()

if __name__ == '__main__':
    test()
