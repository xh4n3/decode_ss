import socket
from threading import Thread
from time import sleep

HOST = '127.0.0.1'    # The remote host
PORT = 10000              # The same port as used by the server
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((HOST, PORT))
n = 0


def monitor():
    global n
    while 1:
        sleep(1)
        print '%s reqs/sec' % n
        n = 0

t = Thread(target=monitor)
t.daemon = True
t.start()

while 1:
    s.sendall(b'1')
    resp = s.recv(100)
    n += 1

s.close()
