from kq import EventLoop
from socket import *
import select
from threading import Thread
from concurrent.futures import ProcessPoolExecutor as Pool

pool = Pool(4)

def fib(num):
    if num == 0:
        return 0
    elif num == 1:
        return 1
    elif num > 1:
        return fib(num-1) + fib(num-2)
    else:
        return False


def handler(client):
    while 1:
        s = client.recv(100)
        try:
            s = int(s)
        except:
            break
        future = pool.submit(fib, s)
        result = future.result()
        resp = str(result)
        client.send(resp)
    print 'Closed'


def main():
    s = socket(AF_INET, SOCK_STREAM)
    s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 10000))
    s.listen(10)
    while 1:
        client, _ = s.accept()
        Thread(None, handler, args=(client,)).start()

    # kq = select.kqueue()
    # ke = select.kevent(s.fileno(), filter=select.KQ_FILTER_READ, flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE)
    # revents = kq.control([ke], 1, None)
    # print revents
    # while True:
    #     revents = kq.control([ke], 1, None)
    #     print revents
    #     for event in revents:
            # If the kernel notifies us saying there is a read event available
            # on the master fd(s.fileno()), we accept() the
            # connection so that we can recv()/send() on the the accept()ed
            # socket
            # if (event.filter == select.KQ_FILTER_READ):
            #     cl, _ = s.accept()
            #     handle_connection(cl)

main()
