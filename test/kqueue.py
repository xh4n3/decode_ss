# coding=utf-8
import select
from socket import socket
from socket import AF_INET, SOCK_STREAM, SOL_SOCKET, SO_REUSEADDR
from threading import Thread

fd = open('test')
s = socket(AF_INET, SOCK_STREAM)
s.bind(("127.0.0.1", 3000))
s.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
s.listen(1)
kq = select.kqueue()
flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
fflags = select.KQ_NOTE_DELETE | select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND \
         | select.KQ_NOTE_RENAME

# 监测文件事件，如果有新事件在这个 fd 上发生，则返回，监测事件类型由 fflags 规定
file_ev = select.kevent(fd.fileno(), filter=select.KQ_FILTER_VNODE, flags=flags, fflags=fflags)

# 监测 Socket 事件，如果有新数据可读则返回
socket_ev = select.kevent(s.fileno(), filter=select.KQ_FILTER_READ, flags=flags)

# 监测多个对象就只需把很多 kevent 对象塞进 events 列表中，然后传递给 control 函数
events = []
events.append(file_ev)
events.append(socket_ev)


# 处理这个 socket 请求
def socket_handler(cl):
    while True:
        data = cl.recv(100)
        print data
        if not data:
            cl.close()
            print 'socket closed'
            break

while True:
    revents = kq.control(events, 1, None)
    for e in revents:
        # 如果是 socket 触发的事件
        if e.ident == s.fileno():
            print 'Event from socket'
            if e.filter & select.KQ_FILTER_READ:
                cl, _ = s.accept()
                # 如果直接调用 socket_handler 函数，那么这个 eventloop 会被阻塞，所以此处使用线程
                Thread(None, socket_handler, args=(cl,)).start()
            else:
                print e
        # 如果是文件触发的事件
        if e.ident == fd.fileno():
            print 'Event from file'
            if e.fflags & select.KQ_NOTE_EXTEND:
                print 'extend'
            elif e.fflags & select.KQ_NOTE_WRITE:
                print 'write'
            elif e.fflags & select.KQ_NOTE_RENAME:
                print 'rename'
            elif e.fflags & select.KQ_NOTE_DELETE:
                print 'delete'
            else:
                print e


