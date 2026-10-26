# SPDX-License-Identifier: GPL-2.0-or-later
"""Separate end-to-end SHA256 check; timing includes hashing, not a bandwidth benchmark."""
import socket,sys,time,json,struct,hashlib,random
TOTAL=65536*4*2560*2;BLOCK=1024*1024
mode,ip=sys.argv[1:3]
def exact(s,n):
 b=bytearray()
 while len(b)<n:
  x=s.recv(n-len(b))
  if not x:raise RuntimeError('early EOF')
  b.extend(x)
 return b
if mode=='server':
 with socket.socket() as l:
  l.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);l.bind((ip,45681));l.listen(1);l.settimeout(30)
  print('ready',flush=True);s,_=l.accept()
  with s:
   s.settimeout(30);buf=bytearray(BLOCK);view=memoryview(buf);received=0;h=hashlib.sha256()
   total=struct.unpack('!Q',exact(s,8))[0]
   if total!=TOTAL:raise RuntimeError('unexpected transfer size')
   while received<total:
    n=s.recv_into(view[:min(BLOCK,total-received)])
    if not n:raise RuntimeError('early EOF')
    h.update(view[:n]);received+=n
   s.sendall(struct.pack('!Q',received)+h.digest())
else:
 b=bytearray(random.Random(73917).randbytes(BLOCK));h=hashlib.sha256()
 with socket.create_connection((ip,45681),10) as s:
  s.settimeout(30);s.setsockopt(socket.IPPROTO_TCP,socket.TCP_NODELAY,1)
  t=time.perf_counter();s.sendall(struct.pack('!Q',TOTAL))
  for offset in range(0,TOTAL,BLOCK):
   struct.pack_into('!Q',b,0,offset);v=memoryview(b)[:min(BLOCK,TOTAL-offset)]
   h.update(v);s.sendall(v)
  ack=exact(s,40);count=struct.unpack('!Q',ack[:8])[0]
  if count!=TOTAL or ack[8:]!=h.digest():raise RuntimeError('data integrity mismatch')
  print(json.dumps(dict(bytes=count,sha256=h.hexdigest(),verified=True,seconds_including_hash=time.perf_counter()-t)),flush=True)
