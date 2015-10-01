#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2013-2015 clowwindy
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# from ssloop
# https://github.com/clowwindy/ssloop

from __future__ import absolute_import, division, print_function, \
    with_statement

import os
import time
import socket
import select
import errno
import logging
from collections import defaultdict

from shadowsocks import shell


__all__ = ['EventLoop', 'POLL_NULL', 'POLL_IN', 'POLL_OUT', 'POLL_ERR',
           'POLL_HUP', 'POLL_NVAL', 'EVENT_NAMES']

POLL_NULL = 0x00
POLL_IN = 0x01
POLL_OUT = 0x04
POLL_ERR = 0x08
POLL_HUP = 0x10
POLL_NVAL = 0x20


EVENT_NAMES = {
    POLL_NULL: 'POLL_NULL',
    POLL_IN: 'POLL_IN',
    POLL_OUT: 'POLL_OUT',
    POLL_ERR: 'POLL_ERR',
    POLL_HUP: 'POLL_HUP',
    POLL_NVAL: 'POLL_NVAL',
}

# we check timeouts every TIMEOUT_PRECISION seconds
TIMEOUT_PRECISION = 10

# Kqueue 适用于 BSD
# https://developer.apple.com/library/mac/documentation/Darwin/Reference/ManPages/man2/kqueue.2.html
class KqueueLoop(object):

    MAX_EVENTS = 1024

    def __init__(self):
        self._kqueue = select.kqueue()
        self._fds = {}

    # 用于修改 kqueue 中的事件
    # register self._control(fd, mode, select.KQ_EV_ADD)
    # unregister self._control(fd, self._fds[fd], select.KQ_EV_DELETE)
    def _control(self, fd, mode, flags):
        events = []
        # 用与运算判断动作，register 调用的时候 mode 为 POLL_IN
        if mode & POLL_IN:
            # KQ_FILTER_READ
            # Takes a descriptor and returns whenever there is data available to read
            # 当有关于此 fd 的新数据可读时返回
            events.append(select.kevent(fd, select.KQ_FILTER_READ, flags))
        if mode & POLL_OUT:
            # KQ_FILTER_WRITE
            # Takes a descriptor and returns whenever there is data available to write
            # 当该 fd 可写时返回
            events.append(select.kevent(fd, select.KQ_FILTER_WRITE, flags))
        for e in events:
            # 一个一个事件添加到 kqueue 中，0 为 maxevent
            self._kqueue.control([e], 0)

    def poll(self, timeout):
        if timeout < 0:
            timeout = None  # kqueue behaviour
        events = self._kqueue.control(None, KqueueLoop.MAX_EVENTS, timeout)
        results = defaultdict(lambda: POLL_NULL)
        for e in events:
            fd = e.ident
            if e.filter == select.KQ_FILTER_READ:
                results[fd] |= POLL_IN
            elif e.filter == select.KQ_FILTER_WRITE:
                results[fd] |= POLL_OUT
        return results.items()

    # 注册事件
    # 也是对添加事件的抽象
    def register(self, fd, mode):
        # _fds 字典以 fd 为键，mode 为值
        self._fds[fd] = mode
        # KQ_EV_ADD 为添加事件
        # 以 Manager 中添加为例，此处执行代码为:
        # _fds = {'socket': POLL_IN}
        # self._control(socket, POLL_IN, KQ_EV_ADD)
        self._control(fd, mode, select.KQ_EV_ADD)

    # 删除事件
    def unregister(self, fd):
        # 调用 _control 来删除事件，同时删除 _fds 中对应项
        # 以 Manager 中添加为例，此处执行代码为：
        # self._control(fd, POLL_IN, select.KQ_EV_DELETE)
        self._control(fd, self._fds[fd], select.KQ_EV_DELETE)
        del self._fds[fd]

    # 更改事件，先删除原有事件再注册新事件
    def modify(self, fd, mode):
        self.unregister(fd)
        self.register(fd, mode)

    def close(self):
        self._kqueue.close()

# 适用于大部分操作系统
class SelectLoop(object):

    def __init__(self):
        self._r_list = set()
        self._w_list = set()
        self._x_list = set()

    def poll(self, timeout):
        r, w, x = select.select(self._r_list, self._w_list, self._x_list,
                                timeout)
        results = defaultdict(lambda: POLL_NULL)
        for p in [(r, POLL_IN), (w, POLL_OUT), (x, POLL_ERR)]:
            for fd in p[0]:
                results[fd] |= p[1]
        return results.items()

    def register(self, fd, mode):
        if mode & POLL_IN:
            self._r_list.add(fd)
        if mode & POLL_OUT:
            self._w_list.add(fd)
        if mode & POLL_ERR:
            self._x_list.add(fd)

    def unregister(self, fd):
        if fd in self._r_list:
            self._r_list.remove(fd)
        if fd in self._w_list:
            self._w_list.remove(fd)
        if fd in self._x_list:
            self._x_list.remove(fd)

    def modify(self, fd, mode):
        self.unregister(fd)
        self.register(fd, mode)

    def close(self):
        pass


class EventLoop(object):
    """
    EventLoop 是对 SelectLoop，EpollLoop 和 KqueueLoop 的抽象
    在 linux 系统中会采用 epoll，unix/BSD 中采用 kqueue，退而选 select，否则退出

    Manager 中添加事件时调用：
    self._loop.add(self._control_socket,
                       eventloop.POLL_IN, self)
    """
    def __init__(self):
        if hasattr(select, 'epoll'):
            self._impl = select.epoll()
            model = 'epoll'
        elif hasattr(select, 'kqueue'):
            self._impl = KqueueLoop()
            model = 'kqueue'
        elif hasattr(select, 'select'):
            self._impl = SelectLoop()
            model = 'select'
        else:
            raise Exception('can not find any available functions in select '
                            'package')
        self._fdmap = {}  # (f, handler)
        self._last_time = time.time()
        self._periodic_callbacks = []
        self._stopping = False
        logging.debug('using event model: %s', model)

    # 等待事件
    def poll(self, timeout=None):
        events = self._impl.poll(timeout)
        return [(self._fdmap[fd][0], fd, event) for fd, event in events]

    # 注册事件
    def add(self, f, mode, handler):
        # f 可以为 socket
        # mode 可以为 POLL_IN
        # handler 可以为 self
        fd = f.fileno()
        # 在该实例中存储以文件描述符为键，文件对象和 handler 的元祖为值的字典
        self._fdmap[fd] = (f, handler)
        # 在 fdmap 中存储后注册该文件描述符和事件类型
        self._impl.register(fd, mode)

    # 删除事件
    def remove(self, f):
        fd = f.fileno()
        del self._fdmap[fd]
        self._impl.unregister(fd)

    # 注册周期事件
    def add_periodic(self, callback):
        self._periodic_callbacks.append(callback)

    # 删除周期事件
    def remove_periodic(self, callback):
        self._periodic_callbacks.remove(callback)

    # 修改已注册事件
    def modify(self, f, mode):
        fd = f.fileno()
        self._impl.modify(fd, mode)

    # 停止事件循环
    def stop(self):
        self._stopping = True

    # 启动事件循环
    def run(self):
        events = []
        while not self._stopping:
            asap = False
            try:
                events = self.poll(TIMEOUT_PRECISION)
            except (OSError, IOError) as e:
                if errno_from_exception(e) in (errno.EPIPE, errno.EINTR):
                    # EPIPE: Happens when the client closes the connection
                    # EINTR: Happens when received a signal
                    # handles them as soon as possible
                    asap = True
                    logging.debug('poll:%s', e)
                else:
                    logging.error('poll:%s', e)
                    import traceback
                    traceback.print_exc()
                    continue

            for sock, fd, event in events:
                handler = self._fdmap.get(fd, None)
                if handler is not None:
                    handler = handler[1]
                    try:
                        handler.handle_event(sock, fd, event)
                    except (OSError, IOError) as e:
                        shell.print_exception(e)
            now = time.time()
            if asap or now - self._last_time >= TIMEOUT_PRECISION:
                for callback in self._periodic_callbacks:
                    callback()
                self._last_time = now

    def __del__(self):
        self._impl.close()


# from tornado
# 从 Python 返回的 Exception 提取 errno
def errno_from_exception(e):
    """Provides the errno from an Exception object.

    There are cases that the errno attribute was not set so we pull
    the errno out of the args but if someone instatiates an Exception
    without any args you will get a tuple error. So this function
    abstracts all that behavior to give you a safe way to get the
    errno.
    """

    if hasattr(e, 'errno'):
        return e.errno
    elif e.args:
        return e.args[0]
    else:
        return None


# 获取 socket 错误
# from tornado
def get_sock_error(sock):
    error_number = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    return socket.error(error_number, os.strerror(error_number))
