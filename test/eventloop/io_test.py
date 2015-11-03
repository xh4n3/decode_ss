from _socket import SOCK_STREAM, AF_INET
import socket
from threading import Thread
from time import sleep


def connect():
    s = socket.socket(AF_INET, SOCK_STREAM)
    s.connect(("127.0.0.1", 3002))
    while True:
        s.send(b'hello_world')
        sleep(0.1)

for i in range(500):
    t = Thread(None, connect, None)
    t.setDaemon(True)
    t.start()

while True:
    sleep(1)
