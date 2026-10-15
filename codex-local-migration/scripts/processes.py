"""Desktop process ownership and visible database blockers, standard library only."""
import os,re,signal,subprocess,time
from pathlib import Path
APP=re.compile(r'^(.+/(?:ChatGPT|Codex)\.app)/Contents/MacOS/[^ ]+(?: |$)')
def snapshot():
 result={}
 data=subprocess.run(['ps','-axo','uid=,pid=,ppid=,command='],capture_output=True,text=True,check=True).stdout
 for line in data.splitlines():
  fields=line.strip().split(None,3)
  if len(fields)==4 and fields[0].isdigit():
   uid,pid,ppid=map(int,fields[:3]);result[pid]={'uid':uid,'pid':pid,'ppid':ppid,'command':fields[3]}
 return result

def capture_helpers(procs):
 parents={pid for pid,row in procs.items() if row['uid']==os.getuid() and APP.match(row['command'])}
 return {pid:row for pid,row in procs.items() if row['ppid'] in parents and row['uid']==os.getuid() and '/Contents/Resources/' in row['command'] and ' app-server ' in ' '+row['command']+' '}

def eligible_orphans(captured,current):
 if any(row['uid']==os.getuid() and APP.match(row['command']) for row in current.values()):return []
 return [pid for pid,old in captured.items() if pid in current and current[pid]['uid']==os.getuid() and current[pid]['ppid']==1 and current[pid]['command']==old['command']]

def holders(codex,procs):
 files=[str(p) for p in Path(codex).rglob('*.sqlite') if 'migration-' not in str(p)]+[str(Path(codex)/'sqlite/codex-dev.db')]
 data=subprocess.run(['lsof','-t','--']+files,capture_output=True,text=True).stdout
 return [{'pid':pid,'ppid':procs.get(pid,{}).get('ppid'),'process':Path(procs.get(pid,{}).get('command','unknown').split(' ',1)[0]).name} for pid in sorted({int(v) for v in data.split() if v.isdigit()})]

def wait_until_closed(mod,home,codex,emit,request_quit=False,timeout=1800):
 captured=capture_helpers(snapshot());stopped=set();last=None;start=time.monotonic()
 if request_quit and Path(home).resolve()==Path.home().resolve():
  subprocess.run(['osascript','-e','tell application id "com.openai.codex" to quit'],capture_output=True,text=True)
 while True:
  try:mod.process_guard(home,codex);return
  except ValueError as exc:
   current=snapshot()
   for pid in eligible_orphans(captured,current):
    if pid in stopped:continue
    try:os.kill(pid,signal.SIGTERM);stopped.add(pid)
    except ProcessLookupError:pass
   detail={'reason':str(exc),'blockers':holders(codex,current),'stopped_captured_desktop_helpers':sorted(stopped)}
   signature=str(detail)
   if signature!=last:emit('waiting_for_app_exit',**detail);last=signature
   if time.monotonic()-start>=timeout:raise RuntimeError('等待应用退出超时，尚未写入数据。请关闭报告列出的占用进程后重新启动。')
   time.sleep(3)
