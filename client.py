import socket

HOST = '127.0.0.1'    # The remote host
PORT = 10000              # The same port as used by the server
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((HOST, PORT))
while 1:
    data = raw_input('data:\n')
    s.sendall(data)
    indata = s.recv(1024)
    print 'Received', repr(indata)
s.close()
