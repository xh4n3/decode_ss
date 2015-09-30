#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2014-2015 clowwindy
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

from __future__ import absolute_import, division, print_function, \
    with_statement

import os
import sys
import logging
import signal
import time
from shadowsocks import common, shell

# this module is ported from ShadowVPN daemon.c


def daemon_exec(config):
    if 'daemon' in config:
        if os.name != 'posix':
            raise Exception('daemon mode is only supported on Unix')
        # 读取配置中 daemon 信息，如果不存在，则默认启动 daemon
        command = config['daemon']
        if not command:
            command = 'start'
        pid_file = config['pid-file']
        log_file = config['log-file']
        if command == 'start':
            # 启动 daemon
            daemon_start(pid_file, log_file)
        elif command == 'stop':
            daemon_stop(pid_file)
            # always exit after daemon_stop
            # 正常退出程序
            sys.exit(0)
        elif command == 'restart':
            daemon_stop(pid_file)
            daemon_start(pid_file, log_file)
        else:
            raise Exception('unsupported daemon command %s' % command)


def write_pid_file(pid_file, pid):
    """
    pidfile

    通常在 /var/run 目录中会看到很多进程的 pid 文件， 其实这些文件就是一个记录着进程的 PID 号的文本文件。
    它的作用是防止程序启动多个副本，只有获得 pid 文件写入权限的进程才能正常启动并把进程 PID 写入到该文件，
    而同一程序的其他进程则会检测到该文件无法写入退出。

    """
    # 文件描述符控制
    import fcntl
    # 获取文件信息
    import stat

    try:
        # O_RDWR | O_CREAT 如果文件存在，打开文件以读取写入，否则创建该文件，并使其拥有以下权限
        # S_IRUSR 文件所有者具可读取权限
        # S_IWUSR 文件所有者具可写入权限
        fd = os.open(pid_file, os.O_RDWR | os.O_CREAT,
                     stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        shell.print_exception(e)
        return -1
    # F_GETFD 获取文件描述符标记
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    assert flags != -1
    flags |= fcntl.FD_CLOEXEC
    r = fcntl.fcntl(fd, fcntl.F_SETFD, flags)
    assert r != -1
    # There is no platform independent way to implement fcntl(fd, F_SETLK, &fl)
    # via fcntl.fcntl. So use lockf instead
    try:
        """
        文件锁
        LOCK_EX exclusive 独占锁
        LOCK_NB non-blocking 非阻塞锁
        在独占锁的情况下，同一时间只有一个进程可以锁住这个文件。
        在有其他进程占有该锁时，
        如果是阻塞锁，lockf 函数会一直阻塞，直到获得锁，而非阻塞锁使 lockf 函数直接返回 IOError。
        fcntl.lockf(fd, operation[, length[, start[, whence]]])
        start 和 length 标记了要锁住的区域的起始位置和长度，而 whence 标记了整个锁区域的偏移量。
        SEEK_SET SEEK_CUR SEEK_END 分别表示文件开头，当前指针位置和文件结尾
        """
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_SET)
    except IOError:
        # pidfile 被其他进程锁住的情况，读取该 pidfile 内容
        r = os.read(fd, 32)
        if r:
            logging.error('already started at pid %s' % common.to_str(r))
        else:
            logging.error('already started')
        os.close(fd)
        return -1
    # 把 fd 对应文件修剪为长度为 0，即清空该文件
    os.ftruncate(fd, 0)
    # 将当前进程的 pid 文件写入到 fd 对应文件中
    os.write(fd, common.to_bytes(str(pid)))
    return 0


def freopen(f, mode, stream):
    """
    dup 和 dup2 用于复制一个现有的文件描述符，使两个描述符指向同一个 file，这个时候临时变量比如读写位置，
    都只会保存在一个 file 结构体中。dup2 比 dup 多了一个 newfd，便于指定新的描述符。
    而如果用 open 打开两次，会产生两个 file 结构体。
    int dup2(int oldfd, int newfd)
    如果 newfd 已经打开，会将其先关闭再 dup2
    http://www.cnblogs.com/sdphome/archive/2011/04/30/2033381.html
    """
    # freopen(log_file, 'a', sys.stdout)
    oldf = open(f, mode)
    oldfd = oldf.fileno()
    newfd = stream.fileno()
    os.close(newfd)
    # 先关闭 newfd，再将 oldfd 指向原来的 newfd 上
    # 所有本来要写入 newfd 即 stdout 的内容，现在由于 newfd 指向 log_file，被写入到 log_file
    os.dup2(oldfd, newfd)


def daemon_start(pid_file, log_file):
    # 启动一个 daemon
    def handle_exit(signum, _):
        # 如果信号为 SIGTERM，则 sys.exit(0)，其中 0 代表正常退出
        if signum == signal.SIGTERM:
            sys.exit(0)
        # 否则为异常退出
        sys.exit(1)

    # 设置当接收到 SIGINIT 或者 SIGTERM 信号时，调用 handle_exit 函数
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # fork only once because we are sure parent will exit
    pid = os.fork()
    # 断言 fork 函数返回正常
    assert pid != -1

    # 此处为父进程执行
    if pid > 0:
        # parent waits for its child
        # 睡眠 5 秒后正常退出
        time.sleep(5)
        sys.exit(0)

    # child signals its parent to exit
    # 获得父进程 pid
    ppid = os.getppid()
    # 获得子进程 pid
    pid = os.getpid()
    # 将子进程 PID 写入 pid 文件
    if write_pid_file(pid_file, pid) != 0:
        # 如果写入失败则杀死父进程，同时子进程退出自己
        # 写入失败原因可能是有另一进程已经启动，控制了 pid 文件
        os.kill(ppid, signal.SIGINT)
        sys.exit(1)

    # setsid() 以后，子进程就不会因为父进程的退出而终止
    os.setsid()
    # SIGHUP 挂起信号，SIG_IGN 为忽略该挂起信号
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    print('started')
    # 使用 SIGTERM 信号杀掉父进程，SIGTERM 给了程序一个处理任务的机会，SIGKILL 会直接杀死进程
    os.kill(ppid, signal.SIGTERM)
    # 关闭标准输入，相当于 os.close(sys.stdin.fileno())
    sys.stdin.close()
    try:
        # 以追加的方式将 stdout 和 stderr 重定向到 log_file
        freopen(log_file, 'a', sys.stdout)
        freopen(log_file, 'a', sys.stderr)
    except IOError as e:
        shell.print_exception(e)
        sys.exit(1)


def daemon_stop(pid_file):
    import errno
    try:
        with open(pid_file) as f:
            buf = f.read()
            pid = common.to_str(buf)
            if not buf:
                logging.error('not running')
    except IOError as e:
        shell.print_exception(e)
        # ENOENT No such file or directory
        if e.errno == errno.ENOENT:
            # always exit 0 if we are sure daemon is not running
            logging.error('not running')
            return
        sys.exit(1)
    pid = int(pid)
    if pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            # ESRCH No such process
            if e.errno == errno.ESRCH:
                logging.error('not running')
                # always exit 0 if we are sure daemon is not running
                return
            shell.print_exception(e)
            sys.exit(1)
    else:
        logging.error('pid is not positive: %d', pid)

    # sleep for maximum 10s
    for i in range(0, 200):
        # 一直杀到 daemon 进程死了为止
        try:
            # query for the pid
            os.kill(pid, 0)
        except OSError as e:
            if e.errno == errno.ESRCH:
                break
        time.sleep(0.05)
    else:
        logging.error('timed out when stopping pid %d', pid)
        sys.exit(1)
    print('stopped')
    # 删除 pidfile
    os.unlink(pid_file)


def set_user(username):
    if username is None:
        return

    import pwd
    import grp

    try:
        # 获取这个 username 的用户
        pwrec = pwd.getpwnam(username)
    except KeyError:
        logging.error('user not found: %s' % username)
        raise
    # 获得用户名
    user = pwrec[0]
    # 获得 uid
    uid = pwrec[2]
    # 获得 gid
    gid = pwrec[3]
    # 获取当前登录用户 uid
    cur_uid = os.getuid()
    # 如果已经是目标用户执行该进程，则直接返回
    if uid == cur_uid:
        return
    # 当前用户非 root，不能 set user
    if cur_uid != 0:
        logging.error('can not set user as nonroot user')
        # will raise later

    # inspired by supervisor
    if hasattr(os, 'setgroups'):
        # 先取出所有含有该用户的用户组，插入
        groups = [grprec[2] for grprec in grp.getgrall() if user in grprec[3]]
        groups.insert(0, gid)
        # 设置当前进程的用户组
        os.setgroups(groups)
    # 设置当前进程的组 id
    os.setgid(gid)
    # 设置当前进程的用户 id
    os.setuid(uid)
