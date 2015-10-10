import time

__author__ = 'Sh4n3'

import os


def pp(pid):
    while True:
        time.sleep(1)
        print 'pid %s' % str(os.getpid())

for i in range(3):
    p = os.fork()

if p == 0:
    pp(1)

while True:
    time.sleep(10)
    print os.getpid()


