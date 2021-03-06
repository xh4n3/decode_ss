#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2015 clowwindy
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

import time
import socket
import errno
import struct
import logging
import traceback
import random

from shadowsocks import encrypt, eventloop, shell, common
from shadowsocks.common import parse_header

# we clear at most TIMEOUTS_CLEAN_SIZE timeouts each time
TIMEOUTS_CLEAN_SIZE = 512

MSG_FASTOPEN = 0x20000000

# SOCKS command definition
"""
Socks5 协议
https://www.ietf.org/rfc/rfc1928.txt
"""
CMD_CONNECT = 1
CMD_BIND = 2
CMD_UDP_ASSOCIATE = 3

# for each opening port, we have a TCP Relay
# 每个服务端口对应一个 TCPRelay 实例

# for each connection, we have a TCP Relay Handler to handle the connection
# 每个链接对应一个 TCPRelayHandler 实例

# for each handler, we have 2 sockets:
#    local:   connected to the client
#    remote:  connected to remote server
# 每个 TCPRelayHandler 开启两个 socket，一个用于连接客户端，一个用于连接远程服务器

# for each handler, it could be at one of several stages:

# as sslocal:
# stage 0 SOCKS hello received from local, send hello to local
# stage 1 addr received from local, query DNS for remote
# stage 2 UDP assoc
# stage 3 DNS resolved, connect to remote
# stage 4 still connecting, more data from local received
# stage 5 remote connected, piping local and remote

# as ssserver:
# stage 0 just jump to stage 1
# stage 1 addr received from local, query DNS for remote
# stage 3 DNS resolved, connect to remote
# stage 4 still connecting, more data from local received
# stage 5 remote connected, piping local and remote

# 每个 TCPRelayHandler 有以下几种状态

# 对于 sslocal，即 ss 客户端
# TODO

# 对于 ssserver，即 ss 服务器
# 状态 0：跳过
# 状态 1：接收到客户端发来的 addr 信息，进行 DNS 解析
# 状态 3：DNS 解析完成，连接远程服务器
# 状态 4：处于连接状态，接收客户端数据
# 状态 5：连接到远程服务器，进行客户端和远程服务器的数据交换

STAGE_INIT = 0
STAGE_ADDR = 1
STAGE_UDP_ASSOC = 2
STAGE_DNS = 3
STAGE_CONNECTING = 4
STAGE_STREAM = 5
STAGE_DESTROYED = -1

# for each handler, we have 2 stream directions:
#    upstream:    from client to server direction
#                 read local and write to remote
#    downstream:  from server to client direction
#                 read remote and write to local

# 每个 Handler 有两个方向的数据流：
# 上行：客户端到服务端，从本地读数据，写数据到远程
# 下行：服务端到客户端，从远程读数据，写数据到本地

STREAM_UP = 0
STREAM_DOWN = 1

# for each stream, it's waiting for reading, or writing, or both
WAIT_STATUS_INIT = 0
WAIT_STATUS_READING = 1
WAIT_STATUS_WRITING = 2
WAIT_STATUS_READWRITING = WAIT_STATUS_READING | WAIT_STATUS_WRITING
# 每个数据流有三种等待状态，读等待，写等待，读写等待

BUF_SIZE = 32 * 1024


class TCPRelayHandler(object):
    def __init__(self, server, fd_to_handlers, loop, local_sock, config,
                 dns_resolver, is_local):
        # server 为 TCPHandler 实例
        self._server = server
        self._fd_to_handlers = fd_to_handlers
        self._loop = loop
        self._local_sock = local_sock
        self._remote_sock = None
        self._config = config
        self._dns_resolver = dns_resolver

        # TCP Relay works as either sslocal or ssserver
        # if is_local, this is sslocal
        self._is_local = is_local
        self._stage = STAGE_INIT
        self._encryptor = encrypt.Encryptor(config['password'],
                                            config['method'])
        self._fastopen_connected = False
        self._data_to_write_to_local = []
        self._data_to_write_to_remote = []
        self._upstream_status = WAIT_STATUS_READING
        self._downstream_status = WAIT_STATUS_INIT
        self._client_address = local_sock.getpeername()[:2]
        self._remote_address = None
        if 'forbidden_ip' in config:
            self._forbidden_iplist = config['forbidden_ip']
        else:
            self._forbidden_iplist = None
        #  如果是 sslocal 来运行的，就先随机选一台服务器
        if is_local:
            self._chosen_server = self._get_a_server()
        # 这里直接对 TCPRelay 实例中的 _fd_to_handlers 赋值
        fd_to_handlers[local_sock.fileno()] = self
        local_sock.setblocking(False)
        local_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        loop.add(local_sock, eventloop.POLL_IN | eventloop.POLL_ERR,
                 self._server)
        self.last_activity = 0
        # 调用 TCPHandler 的 update_activity() 方法
        self._update_activity()

    def __hash__(self):
        # default __hash__ is id / 16
        # we want to eliminate collisions
        """
        CPython 中默认的 __hash__ 是 id(entry)/16
        为了减少冲突，这里直接采用 id，而默认的 id 是采用了对象在内存中的地址
        """
        return id(self)

    @property
    def remote_address(self):
        return self._remote_address

    # 作为本地客户端运行时，随机选择一台服务器和服务端口
    def _get_a_server(self):
        server = self._config['server']
        server_port = self._config['server_port']
        if type(server_port) == list:
            server_port = random.choice(server_port)
        if type(server) == list:
            server = random.choice(server)
        logging.debug('chosen server: %s:%d', server, server_port)
        return server, server_port

    def _update_activity(self, data_len=0):
        # tell the TCP Relay we have activities recently
        # else it will think we are inactive and timed out
        """
        在 TCPRelayHandler 初始化时被调用一次，data_len = 0
        调用 TCPHandler 中的 update_activity() 方法
        用于通知 TCPHandler 这个 TCPRelayHandler 还有处于活跃状态，防止被 timeout
        """
        self._server.update_activity(self, data_len)

    def _update_stream(self, stream, status):
        # update a stream to a new waiting status

        # check if status is changed
        # only update if dirty
        dirty = False
        if stream == STREAM_DOWN:
            if self._downstream_status != status:
                self._downstream_status = status
                dirty = True
        elif stream == STREAM_UP:
            if self._upstream_status != status:
                self._upstream_status = status
                dirty = True
        if dirty:
            if self._local_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                if self._upstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                self._loop.modify(self._local_sock, event)
            if self._remote_sock:
                event = eventloop.POLL_ERR
                if self._downstream_status & WAIT_STATUS_READING:
                    event |= eventloop.POLL_IN
                if self._upstream_status & WAIT_STATUS_WRITING:
                    event |= eventloop.POLL_OUT
                self._loop.modify(self._remote_sock, event)

    def _write_to_sock(self, data, sock):
        # write data to sock
        # if only some of the data are written, put remaining in the buffer
        # and update the stream to wait for writing
        if not data or not sock:
            return False
        uncomplete = False
        try:
            l = len(data)
            s = sock.send(data)
            if s < l:
                data = data[s:]
                uncomplete = True
        except (OSError, IOError) as e:
            error_no = eventloop.errno_from_exception(e)
            if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                            errno.EWOULDBLOCK):
                uncomplete = True
            else:
                shell.print_exception(e)
                self.destroy()
                return False
        if uncomplete:
            if sock == self._local_sock:
                self._data_to_write_to_local.append(data)
                self._update_stream(STREAM_DOWN, WAIT_STATUS_WRITING)
            elif sock == self._remote_sock:
                self._data_to_write_to_remote.append(data)
                self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        else:
            if sock == self._local_sock:
                self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
            elif sock == self._remote_sock:
                self._update_stream(STREAM_UP, WAIT_STATUS_READING)
            else:
                logging.error('write_all_to_sock:unknown socket')
        return True

    def _handle_stage_connecting(self, data):
        if self._is_local:
            data = self._encryptor.encrypt(data)
        self._data_to_write_to_remote.append(data)
        if self._is_local and not self._fastopen_connected and \
                self._config['fast_open']:
            # for sslocal and fastopen, we basically wait for data and use
            # sendto to connect
            try:
                # only connect once
                self._fastopen_connected = True
                remote_sock = \
                    self._create_remote_socket(self._chosen_server[0],
                                               self._chosen_server[1])
                self._loop.add(remote_sock, eventloop.POLL_ERR, self._server)
                data = b''.join(self._data_to_write_to_remote)
                l = len(data)
                s = remote_sock.sendto(data, MSG_FASTOPEN, self._chosen_server)
                if s < l:
                    data = data[s:]
                    self._data_to_write_to_remote = [data]
                else:
                    self._data_to_write_to_remote = []
                self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
            except (OSError, IOError) as e:
                if eventloop.errno_from_exception(e) == errno.EINPROGRESS:
                    # in this case data is not sent at all
                    self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                elif eventloop.errno_from_exception(e) == errno.ENOTCONN:
                    logging.error('fast open not supported on this OS')
                    self._config['fast_open'] = False
                    self.destroy()
                else:
                    shell.print_exception(e)
                    if self._config['verbose']:
                        traceback.print_exc()
                    self.destroy()

    def _handle_stage_addr(self, data):
        try:
            if self._is_local:
                cmd = common.ord(data[1])
                if cmd == CMD_UDP_ASSOCIATE:
                    logging.debug('UDP associate')
                    if self._local_sock.family == socket.AF_INET6:
                        header = b'\x05\x00\x00\x04'
                    else:
                        header = b'\x05\x00\x00\x01'
                    addr, port = self._local_sock.getsockname()[:2]
                    addr_to_send = socket.inet_pton(self._local_sock.family,
                                                    addr)
                    port_to_send = struct.pack('>H', port)
                    self._write_to_sock(header + addr_to_send + port_to_send,
                                        self._local_sock)
                    self._stage = STAGE_UDP_ASSOC
                    # just wait for the client to disconnect
                    return
                elif cmd == CMD_CONNECT:
                    # just trim VER CMD RSV
                    data = data[3:]
                else:
                    logging.error('unknown command %d', cmd)
                    self.destroy()
                    return
            header_result = parse_header(data)
            if header_result is None:
                raise Exception('can not parse header')
            addrtype, remote_addr, remote_port, header_length = header_result
            logging.info('connecting %s:%d from %s:%d' %
                         (common.to_str(remote_addr), remote_port,
                          self._client_address[0], self._client_address[1]))
            self._remote_address = (common.to_str(remote_addr), remote_port)
            # pause reading
            self._update_stream(STREAM_UP, WAIT_STATUS_WRITING)
            self._stage = STAGE_DNS
            if self._is_local:
                # forward address to remote
                self._write_to_sock((b'\x05\x00\x00\x01'
                                     b'\x00\x00\x00\x00\x10\x10'),
                                    self._local_sock)
                data_to_send = self._encryptor.encrypt(data)
                self._data_to_write_to_remote.append(data_to_send)
                # notice here may go into _handle_dns_resolved directly
                self._dns_resolver.resolve(self._chosen_server[0],
                                           self._handle_dns_resolved)
            else:
                if len(data) > header_length:
                    self._data_to_write_to_remote.append(data[header_length:])
                # notice here may go into _handle_dns_resolved directly
                self._dns_resolver.resolve(remote_addr,
                                           self._handle_dns_resolved)
        except Exception as e:
            self._log_error(e)
            if self._config['verbose']:
                traceback.print_exc()
            self.destroy()

    def _create_remote_socket(self, ip, port):
        addrs = socket.getaddrinfo(ip, port, 0, socket.SOCK_STREAM,
                                   socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("getaddrinfo failed for %s:%d" % (ip, port))
        af, socktype, proto, canonname, sa = addrs[0]
        if self._forbidden_iplist:
            if common.to_str(sa[0]) in self._forbidden_iplist:
                raise Exception('IP %s is in forbidden list, reject' %
                                common.to_str(sa[0]))
        remote_sock = socket.socket(af, socktype, proto)
        self._remote_sock = remote_sock
        self._fd_to_handlers[remote_sock.fileno()] = self
        remote_sock.setblocking(False)
        remote_sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        return remote_sock

    def _handle_dns_resolved(self, result, error):
        if error:
            self._log_error(error)
            self.destroy()
            return
        if result:
            ip = result[1]
            if ip:

                try:
                    self._stage = STAGE_CONNECTING
                    remote_addr = ip
                    if self._is_local:
                        remote_port = self._chosen_server[1]
                    else:
                        remote_port = self._remote_address[1]

                    if self._is_local and self._config['fast_open']:
                        # for fastopen:
                        # wait for more data to arrive and send them in one SYN
                        self._stage = STAGE_CONNECTING
                        # we don't have to wait for remote since it's not
                        # created
                        self._update_stream(STREAM_UP, WAIT_STATUS_READING)
                        # TODO when there is already data in this packet
                    else:
                        # else do connect
                        remote_sock = self._create_remote_socket(remote_addr,
                                                                 remote_port)
                        try:
                            remote_sock.connect((remote_addr, remote_port))
                        except (OSError, IOError) as e:
                            if eventloop.errno_from_exception(e) == \
                                    errno.EINPROGRESS:
                                pass
                        self._loop.add(remote_sock,
                                       eventloop.POLL_ERR | eventloop.POLL_OUT,
                                       self._server)
                        self._stage = STAGE_CONNECTING
                        self._update_stream(STREAM_UP, WAIT_STATUS_READWRITING)
                        self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)
                    return
                except Exception as e:
                    shell.print_exception(e)
                    if self._config['verbose']:
                        traceback.print_exc()
        self.destroy()

    def _on_local_read(self):
        # handle all local read events and dispatch them to methods for
        # each stage
        if not self._local_sock:
            return
        is_local = self._is_local
        data = None
        try:
            data = self._local_sock.recv(BUF_SIZE)
        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        self._update_activity(len(data))
        if not is_local:
            data = self._encryptor.decrypt(data)
            if not data:
                return
        if self._stage == STAGE_STREAM:
            if self._is_local:
                data = self._encryptor.encrypt(data)
            self._write_to_sock(data, self._remote_sock)
            return
        elif is_local and self._stage == STAGE_INIT:
            # TODO check auth method
            self._write_to_sock(b'\x05\00', self._local_sock)
            self._stage = STAGE_ADDR
            return
        elif self._stage == STAGE_CONNECTING:
            self._handle_stage_connecting(data)
        elif (is_local and self._stage == STAGE_ADDR) or \
                (not is_local and self._stage == STAGE_INIT):
            self._handle_stage_addr(data)

    def _on_remote_read(self):
        # handle all remote read events
        data = None
        try:
            data = self._remote_sock.recv(BUF_SIZE)

        except (OSError, IOError) as e:
            if eventloop.errno_from_exception(e) in \
                    (errno.ETIMEDOUT, errno.EAGAIN, errno.EWOULDBLOCK):
                return
        if not data:
            self.destroy()
            return
        self._update_activity(len(data))
        if self._is_local:
            data = self._encryptor.decrypt(data)
        else:
            data = self._encryptor.encrypt(data)
        try:
            self._write_to_sock(data, self._local_sock)
        except Exception as e:
            shell.print_exception(e)
            if self._config['verbose']:
                traceback.print_exc()
            # TODO use logging when debug completed
            self.destroy()

    def _on_local_write(self):
        # handle local writable event
        if self._data_to_write_to_local:
            data = b''.join(self._data_to_write_to_local)
            self._data_to_write_to_local = []
            self._write_to_sock(data, self._local_sock)
        else:
            self._update_stream(STREAM_DOWN, WAIT_STATUS_READING)

    def _on_remote_write(self):
        # handle remote writable event
        self._stage = STAGE_STREAM
        if self._data_to_write_to_remote:
            data = b''.join(self._data_to_write_to_remote)
            self._data_to_write_to_remote = []
            self._write_to_sock(data, self._remote_sock)
        else:
            self._update_stream(STREAM_UP, WAIT_STATUS_READING)

    def _on_local_error(self):
        logging.debug('got local error')
        if self._local_sock:
            logging.error(eventloop.get_sock_error(self._local_sock))
        self.destroy()

    def _on_remote_error(self):
        logging.debug('got remote error')
        if self._remote_sock:
            logging.error(eventloop.get_sock_error(self._remote_sock))
        self.destroy()

    # 事件分发器
    def handle_event(self, sock, event):
        # handle all events in this handler and dispatch them to methods
        if self._stage == STAGE_DESTROYED:
            logging.debug('ignore handle_event: destroyed')
            return
        # order is important
        """
        SSLOCAL
            LOCAL_SOCK
                READ    _on_local_read()    从客户端发来的 SOCKS5 数据
                WRITE   _on_local_write()   通过 SOCKS5 协议往客户端发回数据
            REMOTE_SOCK
                READ    _on_remote_read()   从 SSSERVER 读加密后的数据
                WRITE   _on_remote_write()  往 SSSERVER 写加密后的数据
        SSSERVER:
            LOCAL_SOCK
                READ    _on_local_read()    从 SSLOCAL 读加密后的数据
                WRITE   _on_local_write()   往 SSSERVER 写加密后的数据
            REMOTE_SOCK
                READ    _on_remote_read()   从远程服务器读网页数据
                WRITE   _on_remote_write()  往远程服务器发送请求
        """
        if sock == self._remote_sock:
            if event & eventloop.POLL_ERR:
                self._on_remote_error()
                if self._stage == STAGE_DESTROYED:
                    return
            # POLL_HUP 已经断开，可能还有数据可读 POLL_IN 有数据可读
            if event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_remote_read()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & eventloop.POLL_OUT:
                self._on_remote_write()
        elif sock == self._local_sock:
            if event & eventloop.POLL_ERR:
                self._on_local_error()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & (eventloop.POLL_IN | eventloop.POLL_HUP):
                self._on_local_read()
                if self._stage == STAGE_DESTROYED:
                    return
            if event & eventloop.POLL_OUT:
                self._on_local_write()
        else:
            logging.warn('unknown socket')

    def _log_error(self, e):
        logging.error('%s when handling connection from %s:%d' %
                      (e, self._client_address[0], self._client_address[1]))

    def destroy(self):
        # destroy the handler and release any resources
        # promises:
        # 1. destroy won't make another destroy() call inside
        # 2. destroy releases resources so it prevents future call to destroy
        # 3. destroy won't raise any exceptions
        # if any of the promises are broken, it indicates a bug has been
        # introduced! mostly likely memory leaks, etc
        if self._stage == STAGE_DESTROYED:
            # this couldn't happen
            logging.debug('already destroyed')
            return
        self._stage = STAGE_DESTROYED
        if self._remote_address:
            logging.debug('destroy: %s:%d' %
                          self._remote_address)
        else:
            logging.debug('destroy')
        if self._remote_sock:
            logging.debug('destroying remote')
            self._loop.remove(self._remote_sock)
            del self._fd_to_handlers[self._remote_sock.fileno()]
            self._remote_sock.close()
            self._remote_sock = None
        if self._local_sock:
            logging.debug('destroying local')
            self._loop.remove(self._local_sock)
            del self._fd_to_handlers[self._local_sock.fileno()]
            self._local_sock.close()
            self._local_sock = None
        self._dns_resolver.remove_callback(self._handle_dns_resolved)
        self._server.remove_handler(self)


class TCPRelay(object):
    """
    stat_callback 用于监测该 TCPRelay 通信状态，在 Manager 类中被调用
    server.py 调用，默认没有 stat_callback
    tcprelay.TCPRelay(a_config, dns_resolver, False)
    """
    def __init__(self, config, dns_resolver, is_local, stat_callback=None):
        self._config = config
        self._is_local = is_local
        self._dns_resolver = dns_resolver
        self._closed = False
        self._eventloop = None
        self._fd_to_handlers = {}
        # 配置文件中设置的超时时间
        self._timeout = config['timeout']
        self._timeouts = []  # a list for all the handlers
        # we trim the timeouts once a while
        self._timeout_offset = 0   # last checked position for timeout
        # { handler: pos }
        self._handler_to_timeouts = {}  # key: handler value: index in timeouts

        # 用于客户端和服务端复用
        if is_local:
            listen_addr = config['local_address']
            listen_port = config['local_port']
        else:
            # 作为服务器使用时，监听端口为服务端口
            listen_addr = config['server']
            listen_port = config['server_port']
        self._listen_port = listen_port
        """
        socket.getaddrinfo(host, port[, family[, socktype[, proto[, flags]]]])
        family 可取 AF_UNIX（用于 UNIX 中进程间通信），AF_INET（IPv4 的 TCP，UDP 协议），AF_INET6（IPv6 协议）
        family 取 0 时，为兼容所有类型
        http://man7.org/linux/man-pages/man2/socket.2.html
        SOCK_STREAM TCP
        SOCK_DGRAM UDP
        """
        addrs = socket.getaddrinfo(listen_addr, listen_port, 0,
                                   socket.SOCK_STREAM, socket.SOL_TCP)
        if len(addrs) == 0:
            raise Exception("can't get addrinfo for %s:%d" %
                            (listen_addr, listen_port))
        # addrs 为 (family, socktype, proto, canonname, sockaddr) 的列表
        # 比如 [(2, 1, 6, '', ('93.184.216.34', 80))]
        af, socktype, proto, canonname, sa = addrs[0]
        # 创建一个新 socket
        server_socket = socket.socket(af, socktype, proto)
        # 告诉内核允许复用处于 TIME_WAIT 状态的本地 socket
        # http://www.gnu.org/software/libc/manual/html_node/Socket_002dLevel-Options.html
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(sa)
        server_socket.setblocking(False)
        if config['fast_open']:
            try:
                """
                TCP FAST OPEN
                http://conferences.sigcomm.org/co-next/2011/papers/1569470463.pdf
                http://edsiper.linuxchile.cl/blog/2013/02/21/linux-tcp-fastopen-in-your-sockets/
                此前在浏览器访问网页时，需要加载很多服务器上的文件，在与服务器的数据传输过程中，
                有 10% 到 30% 的时间是花在三次握手上的，而传统 TCP 协议只允许在建立握手以后进行数据交换
                FAST OPEN 提出了允许 TCP 在三次握手的时候就进行数据交互，即带着 HTTP 请求和返回内容来进行三次握手
                在 Google 发布的这篇论文中提到，如果简简单单的允许提早数据交互是有问题的
                在第一次握手时，浏览器发出了一个带有 GET Request 内容的 SYN 包去服务器，
                在这种情况下，服务器要返回的 SYN/ACK 包是带有 GET Reponse 内容的，
                如果这个包大小很大，则被利用成 DoS 攻击
                FAST OPEN 设置了一个最长16字节的 TFO Cookie，这个 Cookie 是由服务器根据请求者 ip 加密以后生成的，
                用于握手以及握手结束以后的 ip 来源验证，而且这个 cookie 也是被服务器定时清理的。
                当 cookie 验证失败以后，则协议会退化到普通 TCP 三次握手，即在握手过程中不进行数据交互。
                由于客户端在 SYN/ACK 包中可以获得一个有效的 cookie， 那么当请求的客户端越来越多的时候，
                服务器可能出现要维护的 cookie 太多的问题，此时 FAST OPEN 采用了防御 SYN 攻击的方法
                此外，FAST OPEN 可以防御反射的流量放大攻击 Amplified Reflection Attack，
                原因是只有获取到受害者的 cookie，才能伪造一个有效的 SYN 包
                TCP_FASTOPEN 23
                qlen 处于 TCP_SYN_RECV 状态的请求数，5 这个数字此处比较不明觉厉
                """
                server_socket.setsockopt(socket.SOL_TCP, 23, 5)
            except socket.error:
                logging.error('warning: fast open is not available')
                self._config['fast_open'] = False
        # 此处 1024 为 backlog，同时支持的连接数
        server_socket.listen(1024)
        self._server_socket = server_socket
        self._stat_callback = stat_callback

    def add_to_loop(self, loop):
        if self._eventloop:
            raise Exception('already add to loop')
        if self._closed:
            raise Exception('already closed')
        self._eventloop = loop
        # 加入到事件队列中
        self._eventloop.add(self._server_socket,
                            eventloop.POLL_IN | eventloop.POLL_ERR, self)
        self._eventloop.add_periodic(self.handle_periodic)

    def remove_handler(self, handler):
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            """
            Python 的时间复杂度
            对字典的删除操作的平均时间复杂度为 O(1)，这是在散列无冲突的情况下估算的
            https://wiki.python.org/moin/TimeComplexity
            http://www.orangecube.net/python-time-complexity
            此处用到了 Python 内置的 hash 函数，其实是调用了实例中的 __hash__ 方法
            此处由于 TCPRelayHandler 为新式类，本身就具有 __hash__ 方法，否则当对象为旧式类
            """
            self._timeouts[index] = None
            del self._handler_to_timeouts[hash(handler)]

    # 此处 handler 为 TCPRelayHandler 实例
    def update_activity(self, handler, data_len):
        # 如果有数据流，则通知 _stat_callback 记录数据流量
        if data_len and self._stat_callback:
            self._stat_callback(self._listen_port, data_len)

        # set handler to active
        now = int(time.time())
        # 每隔 TIMEOUT_PRECISION 秒，默认 10 秒，监测一次 timeout，此处距离上次更新太近，所以直接返回
        if now - handler.last_activity < eventloop.TIMEOUT_PRECISION:
            # thus we can lower timeout modification frequency
            return
        # 更新 last_activity 字段
        handler.last_activity = now
        index = self._handler_to_timeouts.get(hash(handler), -1)
        if index >= 0:
            # delete is O(n), so we just set it to None
            self._timeouts[index] = None
        length = len(self._timeouts)
        # 添加该 handler 到 _timeouts 列表中
        self._timeouts.append(handler)
        # 将 _timeouts 长度存入 hash(handler) 的键中
        self._handler_to_timeouts[hash(handler)] = length

    def _sweep_timeout(self):
        # tornado's timeout memory management is more flexible than we need
        # we just need a sorted last_activity queue and it's faster than heapq
        # in fact we can do O(1) insertion/remove so we invent our own
        # _timeouts 其实是一个列表，保存了很多 handler，指向 TCPRelayHandler 实例
        # 只有 _timeouts 非空的时候才进行操作
        if self._timeouts:
            logging.log(shell.VERBOSE_LEVEL, 'sweeping timeouts')
            now = time.time()
            length = len(self._timeouts)
            # _timeout_offset 初始值为 0，记录上次检测到的位置
            pos = self._timeout_offset
            # 从上次更新到的位置开始循环
            while pos < length:
                handler = self._timeouts[pos]
                # 取出 handler，如果空则跳过
                if handler:
                    # 如果没有超时，退出该循环，否则删除 TCPRelayHandler 实例，并在 _timeouts 列表中标记为空
                    if now - handler.last_activity < self._timeout:
                        break
                    else:
                        if handler.remote_address:
                            logging.warn('timed out: %s:%d' %
                                         handler.remote_address)
                        else:
                            logging.warn('timed out')
                        handler.destroy()
                        self._timeouts[pos] = None  # free memory
                        pos += 1
                else:
                    pos += 1
            # 每次最多删除 TIMEOUTS_CLEAN_SIZE 个超时的 TCPRelayHandler 实例
            # 或者 当前 pos 大于 _timeouts 的长度的一半
            # length >> 1 位操作符 等效于 length/2
            if pos > TIMEOUTS_CLEAN_SIZE and pos > length >> 1:
                # clean up the timeout queue when it gets larger than half
                # of the queue
                # 相当于删除已经检测过的 handlers
                self._timeouts = self._timeouts[pos:]
                # 将 _handler_to_timeouts 中每一项都减去 pos, 然后设置 pos 为 0
                for key in self._handler_to_timeouts:
                    self._handler_to_timeouts[key] -= pos
                pos = 0
            self._timeout_offset = pos

    def handle_event(self, sock, fd, event):
        # handle events and dispatch to handlers
        # 由 eventloop 触发，返回 socket， fd 和 event
        if sock:
            logging.log(shell.VERBOSE_LEVEL, 'fd %d %s', fd,
                        eventloop.EVENT_NAMES.get(event, event))
        if sock == self._server_socket:
            if event & eventloop.POLL_ERR:
                # TODO
                raise Exception('server_socket error')
            try:
                logging.debug('accept')
                # 建立新连接
                conn = self._server_socket.accept()
                # 交给 TCP 转发类
                TCPRelayHandler(self, self._fd_to_handlers,
                                self._eventloop, conn[0], self._config,
                                self._dns_resolver, self._is_local)
            except (OSError, IOError) as e:
                error_no = eventloop.errno_from_exception(e)
                if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                                errno.EWOULDBLOCK):
                    return
                else:
                    shell.print_exception(e)
                    if self._config['verbose']:
                        traceback.print_exc()
        else:
            if sock:
                handler = self._fd_to_handlers.get(fd, None)
                if handler:
                    handler.handle_event(sock, event)
            else:
                logging.warn('poll removed fd')

    def handle_periodic(self):
        if self._closed:
            # 关闭 socket，删除事件队列中对应事件
            if self._server_socket:
                self._eventloop.remove(self._server_socket)
                self._server_socket.close()
                self._server_socket = None
                logging.info('closed TCP port %d', self._listen_port)
            if not self._fd_to_handlers:
                logging.info('stopping')
                self._eventloop.stop()
        # 定时检测一下 timeouts
        self._sweep_timeout()

    def close(self, next_tick=False):
        logging.debug('TCP close')
        self._closed = True
        if not next_tick:
            if self._eventloop:
                self._eventloop.remove_periodic(self.handle_periodic)
                self._eventloop.remove(self._server_socket)
            self._server_socket.close()
            for handler in list(self._fd_to_handlers.values()):
                handler.destroy()
