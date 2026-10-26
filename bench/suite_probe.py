#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded CPU-buffer TCP probes; JSON on clients, one connection per server.
No SSH data tunnel, no CUDA, no disk in the timed path. RTT includes both payload directions.
"""
import argparse
import json
import math
import socket
import struct
import time

SIZES = (1, 20480, 65536, 1048576)
COUNTS = (2000, 2000, 1000, 100)
WARMUP = 100
MAX_BYTES = 8 * 1024**3
BLOCK = 1024**2

def recv_into(sock, view):
    pos = 0
    while pos < len(view):
        n = sock.recv_into(view[pos:])
        if not n: raise RuntimeError('Unexpected EOF')
        pos += n

def exact(sock, n):
    data = bytearray(n)
    recv_into(sock, memoryview(data))
    return data

def distribution(values):
    values = sorted(values)
    if not values: raise ValueError('No observations')
    n = len(values)
    return {'n': n, 'min_us': values[0]/1000, 'p50_us': values[math.ceil(n*.5)-1]/1000,
            'p95_us': values[math.ceil(n*.95)-1]/1000, 'p99_us': values[math.ceil(n*.99)-1]/1000,
            'max_us': values[-1]/1000}

def latency(sock, loaded=False, quick=False):
    cases=[]
    sizes=(20480,) if loaded else SIZES
    counts=(2000,) if loaded else COUNTS
    if quick:counts=tuple(5 for _ in sizes)
    warmup=2 if quick else WARMUP
    for n,count in zip(sizes,counts):
        sock.sendall(struct.pack('!II',n,count+warmup))
        payload=bytes(i%251 for i in range(n)); received=bytearray(n);view=memoryview(received)
        elapsed=[];starts=[];ends=[]
        for i in range(count+warmup):
            start=time.monotonic_ns();sock.sendall(payload);recv_into(sock,view);end=time.monotonic_ns()
            if received!=payload:raise RuntimeError('Echo data mismatch')
            if i>=warmup:
                elapsed.append(end-start)
                if loaded:starts.append(start);ends.append(end)
        item={'bytes':n,'warmup':warmup,'rtt_ns':elapsed,**distribution(elapsed)}
        if loaded:item.update(start_ns=starts,end_ns=ends)
        cases.append(item)
    sock.sendall(struct.pack('!II',0,0))
    return {'kind':'latency','cases':cases,'all_payloads_verified':True}

def bulk(sock,total):
    sock.sendall(struct.pack('!Q',total));payload=bytes(BLOCK)
    start=time.monotonic_ns()
    for offset in range(0,total,BLOCK):sock.sendall(memoryview(payload)[:min(BLOCK,total-offset)])
    count=struct.unpack('!Q',exact(sock,8))[0];end=time.monotonic_ns()
    if count!=total:raise RuntimeError('Byte count mismatch')
    seconds=(end-start)/1e9
    return {'kind':'bulk','bytes':count,'seconds':seconds,'MiB_s':count/2**20/seconds,
            'start_ns':start,'end_ns':end}

def server(args):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind((args.ip,args.port));listener.listen(1);listener.settimeout(30)
        print('ready',flush=True);sock,_=listener.accept()
        with sock:
            sock.settimeout(10);sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
            deadline=time.monotonic()+120
            if args.kind=='echo':
                for _ in range(5):
                    n,count=struct.unpack('!II',exact(sock,8))
                    if (n,count)==(0,0):return
                    if n not in SIZES or not 1<=count<=5000:raise ValueError('Invalid echo request')
                    buf=bytearray(n);view=memoryview(buf)
                    for _ in range(count):
                        if time.monotonic()>deadline:raise TimeoutError('Server deadline')
                        recv_into(sock,view);sock.sendall(view)
                raise ValueError('Too many echo cases')
            else:
                total=struct.unpack('!Q',exact(sock,8))[0]
                if not 1<=total<=MAX_BYTES:raise ValueError('Invalid bulk byte count')
                buf=bytearray(BLOCK);view=memoryview(buf);received=0
                while received<total:
                    if time.monotonic()>deadline:raise TimeoutError('Server deadline')
                    n=sock.recv_into(view[:min(BLOCK,total-received)])
                    if not n:raise RuntimeError('Unexpected EOF')
                    received+=n
                sock.sendall(struct.pack('!Q',received))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('role',choices=('server','latency','bulk'))
    p.add_argument('ip');p.add_argument('--port',type=int,default=45682)
    p.add_argument('--kind',choices=('echo','sink'),default='echo')
    p.add_argument('--bytes',type=int,default=1342177280)
    p.add_argument('--loaded',action='store_true')
    p.add_argument('--quick',action='store_true',help='small smoke run, not the publication schedule')
    a=p.parse_args()
    if not 1<=a.port<=65535:p.error('port must be1..65535')
    if not 1<=a.bytes<=MAX_BYTES:p.error('bytes must be1..8GiB')
    if a.role=='server':server(a);return
    with socket.create_connection((a.ip,a.port),10) as sock:
        sock.settimeout(10);sock.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
        result=latency(sock,a.loaded,a.quick) if a.role=='latency' else bulk(sock,a.bytes)
        print(json.dumps(result),flush=True)
if __name__=='__main__':main()
