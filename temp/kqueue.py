import select
from time import sleep


fd = open('test')

print fd
kq = select.kqueue()

flags = select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
fflags = select.KQ_NOTE_DELETE | select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND \
         | select.KQ_NOTE_RENAME
ev = select.kevent(fd, filter=select.KQ_FILTER_VNODE, flags=flags, fflags=fflags)
revents = kq.control([ev], 1, None)
print revents
sleep(5)
while 1:
    revents = kq.control([ev], 1, None)

    for e in revents:
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


