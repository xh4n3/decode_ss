import socket
import threading


s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

s.bind(('127.0.0.1', 9800))

s.listen(2)


def handler(conn):
    data = conn.recv(100)
    print data
    print conn
    conn.close()

while 1:
    conn, addr = s.accept()
    threading.Thread(target=handler, args=(conn, )).start()



