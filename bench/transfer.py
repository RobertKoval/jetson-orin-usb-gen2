# SPDX-License-Identifier: GPL-2.0-or-later
import socket,sys,time,json,struct
TOTAL=65536*4*2560*2
BLOCK=1024*1024
mode,ip=sys.argv[1:3]
def exact(s,n):
 b=b''
 while len(b)<n:
  x=s.recv(n-len(b))
  if not x:raise RuntimeError('early EOF')
  b+=x
 return b
if mode=='server':
 with socket.socket() as l:
  l.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);l.bind((ip,45680));l.listen(1);l.settimeout(30)
  print('ready',flush=True)
  s,_=l.accept()
  with s:
   s.settimeout(30);buf=bytearray(BLOCK);received=0
   total=struct.unpack('!Q',exact(s,8))[0]
   while received<total:
    n=s.recv_into(memoryview(buf)[:min(BLOCK,total-received)])
    if not n:raise RuntimeError('early EOF')
    received+=n
   s.sendall(struct.pack('!Q',received))
else:
 with socket.create_connection((ip,45680),10) as s:
  s.settimeout(30);s.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
  b=bytes(BLOCK);t=time.perf_counter();s.sendall(struct.pack('!Q',TOTAL))
  for offset in range(0,TOTAL,BLOCK):s.sendall(b[:min(BLOCK,TOTAL-offset)])
  count=struct.unpack('!Q',exact(s,8))[0];elapsed=time.perf_counter()-t
  assert count==TOTAL,(count,TOTAL)
  print(json.dumps(dict(bytes=count,seconds=elapsed,MiB_s=count/2**20/elapsed,Gbit_s=count*8/1e9/elapsed)),flush=True)
