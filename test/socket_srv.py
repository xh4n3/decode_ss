import socket
import threading
import signal


s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

s.bind(('127.0.0.1', 9890))

s.listen(2)


def handler(conn):
    data = conn.recv(100)
    print data
    print conn
    conn.close()


def close_handler(*args, **kwargs):
    print args
    print kwargs
    s.close()
    print 'Closing'

signal.signal(signal.SIGINT, close_handler)

while 1:
    conn, addr = s.accept()
    threading.Thread(target=handler, args=(conn, )).start()


