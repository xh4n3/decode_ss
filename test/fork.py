import time
import os

__author__ = 'Sh4n3'


def pp(pid):
    for i in range(3):
        time.sleep(1)
        print 'pid %s' % str(os.getpid())

for i in range(3):
    p = os.fork()

children = []

if p == 0:
    pp(1)
else:
    # TODO WTF HOW DOES IT HAPPEN
    children.append(p)
    print children
    for child in children:
        os.waitpid(child, 0)
        print 'wait end'

