#!/usr/bin/env python
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

import sys
import os
import logging
import signal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../'))
from shadowsocks import shell, daemon, eventloop, tcprelay, udprelay, \
    asyncdns, manager


def main():
    shell.check_python()

    # 读取
    config = shell.get_config(False)

    # TODO 临时打印配置信息
    print(config)

    # 根据配置对守护进程进行操作，不开启 daemon 时此处可忽略
    daemon.daemon_exec(config)

    """
    port_password 为一个字典，每个端口有不同的密码，用于多用户登录
    "port_password": {
        "8381": "foobar1",
        "8382": "foobar2",
        }
    """
    if config['port_password']:
        # 如果既配置了端口密码又配置了全局密码，则全局密码会被忽略
        if config['password']:
            logging.warn('warning: port_password should not be used with '
                         'server_port and password. server_port and password '
                         'will be ignored')
    else:
        config['port_password'] = {}
        server_port = config.get('server_port', None)
        if server_port:
            if type(server_port) == list:
                for a_server_port in server_port:
                    config['port_password'][a_server_port] = config['password']
            else:
                config['port_password'][str(server_port)] = config['password']

    # TODO 如果获取到 manager_address，则启动管理进程，先跳过不看
    if config.get('manager_address', 0):
        logging.info('entering manager mode')
        manager.run(config)
        return

    tcp_servers = []
    udp_servers = []

    # 如果设置了 dns 服务器
    if 'dns_server' in config:  # allow override settings in resolv.conf
        dns_resolver = asyncdns.DNSResolver(config['dns_server'])
    else:
        dns_resolver = asyncdns.DNSResolver()

    # 取出服务端口和其密码，当设置中不开启 port_password 时，也会退化到使用 port_password，此时 port_password 为单元素列表
    port_password = config['port_password']
    # TODO 为什么要删除
    del config['port_password']
    for port, password in port_password.items():
        a_config = config.copy()
        a_config['server_port'] = int(port)
        a_config['password'] = password
        logging.info("starting server at %s:%d" %
                     (a_config['server'], int(port)))
        tcp_servers.append(tcprelay.TCPRelay(a_config, dns_resolver, False))
        udp_servers.append(udprelay.UDPRelay(a_config, dns_resolver, False))

    def run_server():
        def child_handler(signum, _):
            logging.warn('received SIGQUIT, doing graceful shutting down..')
            list(map(lambda s: s.close(next_tick=True),
                     tcp_servers + udp_servers))
        signal.signal(getattr(signal, 'SIGQUIT', signal.SIGTERM),
                      child_handler)

        def int_handler(signum, _):
            sys.exit(1)
        signal.signal(signal.SIGINT, int_handler)

        try:
            loop = eventloop.EventLoop()
            dns_resolver.add_to_loop(loop)
            list(map(lambda s: s.add_to_loop(loop), tcp_servers + udp_servers))

            daemon.set_user(config.get('user', None))
            loop.run()
        except Exception as e:
            shell.print_exception(e)
            sys.exit(1)

    # 如果 workers 为 1 则直接启动 server，否则进行 fork()
    if int(config['workers']) > 1:
        if os.name == 'posix':
            children = []
            is_child = False
            for i in range(0, int(config['workers'])):
                r = os.fork()
                if r == 0:
                    logging.info('worker started')
                    is_child = True
                    run_server()
                    break
                else:
                    children.append(r)
            if not is_child:
                def handler(signum, _):
                    for pid in children:
                        try:
                            os.kill(pid, signum)
                            os.waitpid(pid, 0)
                        except OSError:  # child may already exited
                            pass
                    sys.exit()
                signal.signal(signal.SIGTERM, handler)
                signal.signal(signal.SIGQUIT, handler)
                signal.signal(signal.SIGINT, handler)

                # master
                for a_tcp_server in tcp_servers:
                    a_tcp_server.close()
                for a_udp_server in udp_servers:
                    a_udp_server.close()
                dns_resolver.close()

                for child in children:
                    os.waitpid(child, 0)
        else:
            logging.warn('worker is only available on Unix/Linux')
            run_server()
    else:
        run_server()


if __name__ == '__main__':
    main()
