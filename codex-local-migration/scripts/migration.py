#!/usr/bin/env python3
"""Portable Codex export/import coordinator. Python 3.9+, stdlib only."""
import argparse,collections,contextlib,datetime as dt,fcntl,hashlib,json,os,shlex,shutil,sqlite3,stat,subprocess,sys,tempfile,time,uuid,zipfile
from pathlib import Path
import migration_tool as mt
from export_local import Exporter
import processes
ROOT=Path(__file__).resolve().parent
BLOCKING={'missing_rollout','missing_rollout_for_db_thread','unindexed_rollout','incomplete_tail','history_beyond_rollout'}
PRESERVE={'file','symlink','desktop_state','project_metadata','thread_metadata','database_row'}

def target(args):
 home=Path(args.home).expanduser().resolve()
 codex=Path(args.codex_home or (os.environ.get('CODEX_HOME') if home==Path.home() else None) or home/'.codex').expanduser().resolve()
 return home,codex

def skill_zip(folder,dest):
 with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
  for p in sorted(Path(folder).rglob('*')):
   if p.is_file() and not any(x in p.parts for x in ['__pycache__','.DS_Store']):z.write(p,arcname=Path(folder).name+'/'+str(p.relative_to(folder)))

def safe_unpack(archive,dest):
 dest=Path(dest).resolve()
 if dest.exists() and any(dest.iterdir()):raise ValueError('解压目标必须为空。')
 with zipfile.ZipFile(archive) as z:
  names=set()
  for info in z.infolist():
   name=info.filename;q=Path(name)
   if name in names or q.is_absolute() or '..' in q.parts or '\\' in name or stat.S_ISLNK(info.external_attr>>16):raise ValueError('压缩包含不安全或重复条目: '+name)
   names.add(name)
  if z.testzip():raise ValueError('压缩包 CRC 校验失败。')
  dest.mkdir(parents=True,exist_ok=True);z.extractall(dest)
  for info in z.infolist():
   p=dest/info.filename;mode=(info.external_attr>>16)&0o777
   if mode:p.chmod(mode)
 bundles=[p.parent for p in dest.rglob('manifest.json') if p.parent.name=='Codex本地迁移' and p.parent.parent==dest]
 if len(bundles)!=1:raise ValueError('未找到唯一的 Codex本地迁移/manifest.json。')
 return bundles[0]

def self_test():
 subprocess.run([sys.executable,str(ROOT/'test_migration.py')],check=True)

def export(args):
 out=Path(args.output).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
 home,codex=target(args)
 if mt.under(out,codex) or mt.under(out,home/'.agents'):raise ValueError('输出目录不能位于源数据树内。')
 self_test()
 with tempfile.TemporaryDirectory(prefix='codex-export-') as scratch:
  bundle=Path(scratch)/'Codex本地迁移'
  m=Exporter(argparse.Namespace(home=home,codex_home=codex,bundle=bundle)).run()
  blocking=[x for x in m['issues'] if x['kind'] in BLOCKING]
  if blocking:
   mt.write_json(out/'导出异常报告.json',{'ok':False,'issues':m['issues']});raise ValueError('源端存在会话完整性阻断问题，详见导出异常报告.json。')
  for name in ['migration.py','migration_tool.py','export_local.py','processes.py','test_migration.py']:shutil.copy2(ROOT/name,bundle/name)
  launcher='#!/bin/zsh\ncd "${0:A:h}"\nif ! command -v python3 >/dev/null; then\n echo "需要 Python 3.9+；请将迁移说明交给 GPT 定位内置 Python。"\n read reply\n exit 1\nfi\npython3 migration.py import --bundle "$PWD" --interactive\nresult=$?\nif [ "$result" -ne 0 ]; then echo "导入停止，请查看导入报告；按回车关闭。"; read reply; fi\nexit "$result"\n'
  (bundle/'导入迁移.command').write_text(launcher);(bundle/'导入迁移.command').chmod(0o700)
  if (ROOT.parent/'SKILL.md').is_file():skill_zip(ROOT.parent,bundle/'codex-local-migration.skill')
  (bundle/'测试报告.txt').write_text('随附测试用例在本次封包前于隔离临时目录执行通过；不代表目标电脑已执行合并。\n')
  support=[p for p in bundle.iterdir() if p.is_file() and p.name!='manifest.json']
  m['support_files']=[{'path':p.name,'bytes':p.stat().st_size,'sha256':mt.digest(p)} for p in support]
  mt.write_json(bundle/'manifest.json',m);mt.validate_bundle(bundle)
  tag=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
  archive=out/('Codex本地迁移包-'+tag+'.zip');partial=out/(archive.name+'.partial')
  if archive.exists():raise ValueError('同名迁移包已存在，请稍后重试。')
  mt.write_zip(bundle,partial)
  roundtrip=safe_unpack(partial,Path(scratch)/'roundtrip');mt.validate_bundle(roundtrip)
  for t in m['threads']:
   p=roundtrip/t['payload_path']
   if mt.digest(p)!=t['sha256'] or sum(1 for _ in p.open('rb'))!=t['records']:raise ValueError('会话回读核验失败: '+t['id'])
  os.replace(partial,archive);archive.chmod(0o600)
  checksum=mt.digest(archive);sha=out/(archive.name+'.sha256');sha.write_text(checksum+'  '+archive.name+'\n');sha.chmod(0o600)
  shutil.copy2(bundle/'迁移说明.md',out/'迁移说明.md')
  report={'ok':True,'archive':str(archive),'sha256':checksum,'bundle_id':m['bundle_id'],'snapshot_finished_at':m['finished_at'],'statistics':m['statistics'],'source_status':m['status'],'source_exceptions':m['issues'],'zip_crc':'passed','roundtrip_file_hashes':'passed','all_rollout_records':'passed','self_tests':'passed','target_migration_performed':False}
  mt.write_json(out/'迁移核对报告.json',report);print(json.dumps(report,ensure_ascii=False,indent=2))

def locate_bundle(args):
 out=Path(args.output).expanduser().resolve();out.mkdir(parents=True,exist_ok=True)
 if args.bundle:return Path(args.bundle).expanduser().resolve(),out
 archive=Path(args.archive).expanduser().resolve();sha=Path(args.sha256_file).expanduser() if args.sha256_file else Path(str(archive)+'.sha256')
 if not sha.is_file():raise ValueError('请一并提供 .sha256 文件；或先通过可信渠道核对外置 SHA-256 再使用已解压的 --bundle。')
 expected=sha.read_text().split()[0]
 if len(expected)!=64 or mt.digest(archive)!=expected:raise ValueError('压缩包 SHA-256 不一致。')
 dest=out/('解压-'+uuid.uuid4().hex[:10]);return safe_unpack(archive,dest),out

def auto_paths(m,home,codex,dec):
 c=mt.ro_db(codex/mt.STATE);projects=mt.rows(c,'projects');roots=mt.rows(c,'project_roots');c.close()
 for project in m['projects']:
  for root in project['roots']:
   old=root['path']
   if old in dec.setdefault('path_map',{}):continue
   direct=mt.map_path(old,{m['source_home']:str(home)})
   if Path(direct).is_dir() and any(x['path']==direct for x in roots):dec['path_map'][old]=direct;continue
   identity=root.get('git_identity')
   if identity:
    hits={r['path'] for r in roots if mt.git_identity(r['path'])==identity}
    if len(hits)==1:dec['path_map'][old]=hits.pop()
 for old in m.get('projectless_directories',[]):
  direct=mt.map_path(old,{m['source_home']:str(home)})
  if old not in dec['path_map'] and Path(direct).is_dir():dec['path_map'][old]=direct
 # A sole destination sidebar owner is a unique UI-only identity; no login token is copied.
 ui=mt.read_json(codex/'.codex-global-state.json',{});owners=ui.get('electron-persisted-atom-state',{}).get('sidebar-custom-sections-v3',{})
 source=mt.read_json(Path(m['_bundle'])/'payload/codex/.codex-global-state.json',{}).get('electron-persisted-atom-state',{}).get('sidebar-custom-sections-v3',{})
 if len(owners)==1:
  for owner in source:
   if owner not in owners:dec.setdefault('desktop_owner_map',{}).setdefault(owner,next(iter(owners)))
 return projects,roots

def arguments(args,bundle,decisions,report):
 home,codex=target(args)
 return argparse.Namespace(bundle=bundle,home=home,codex_home=codex,decisions=decisions,report=report,backup_dir=None)

def prepare(args,bundle,out):
 home,codex=target(args);m=mt.validate_bundle(bundle)
 if Path(m['source_codex_home']).resolve()==codex:raise ValueError('源与目标数据根相同；导入必须在新目标目录进行。')
 m['_bundle']=str(bundle)
 dp=Path(args.decisions).expanduser().resolve() if args.decisions else out/'decisions.json'
 dec=mt.read_json(dp,{'path_map':{},'project_map':{},'conflicts':{},'file_overrides':{},'desktop_owner_map':{}})
 auto_paths(m,home,codex,dec);mt.write_json(dp,dec)
 a=arguments(args,bundle,dp,out/'目标预检报告.json');ctx=mt.prepare(a)
 if args.conflict_policy=='preserve-target':
  chosen=[x for x in ctx['report']['pending'] if x['kind'] in PRESERVE]
  if chosen:
   for x in chosen:dec.setdefault('conflicts',{})[x['key']]='keep_target'
   mt.write_json(dp,dec);ctx['temp'].cleanup();ctx=mt.prepare(a)
 # A review file is useful even with zero pending items, as it records the chosen policy.
 lines=['# 导入预检与决策','','当前策略：'+args.conflict_policy+'。正式目标尚未因预检而写入。','','| 编号 | 类型 | 项目或路径 / 冲突键 | 处理 |','|---|---|---|---|']
 for i,item in enumerate(ctx['report']['pending'],1):lines.append('| '+str(i)+' | '+item['kind']+' | `'+item['key'].replace('|','\\|')+'` | 待决定 |')
 lines+=['','## 已确定路径','']+['- `'+old+'` → `'+new+'`' for old,new in dec.get('path_map',{}).items()]
 lines+=['','## 源异常','']+[json.dumps(x,ensure_ascii=False) for x in m.get('issues',[])]
 (out/'导入预检与决策.md').write_text('\n'.join(lines)+'\n')
 return ctx,a,dp

def interactive_paths(args,bundle,out,ctx,a,dp):
 if not sys.stdin.isatty():raise ValueError('存在待决策项；请在独立终端交互导入，或由 GPT 填写 decisions.json。')
 dec=mt.read_json(dp,{});m=ctx['m'];home,codex=target(args)
 c=mt.ro_db(codex/mt.STATE);projects=mt.rows(c,'projects');roots=mt.rows(c,'project_roots');c.close()
 byid={p['id']:p for p in projects}
 required={r['path'] for p in m['projects'] for r in p['roots']}|set(m.get('projectless_directories',[]))
 notes=dec.setdefault('workspace_notes',[])
 for old in sorted(required):
  if old in dec.get('path_map',{}) and Path(dec['path_map'][old]).is_dir():continue
  print('\n原工作区：'+old)
  suggestion=mt.map_path(old,{m['source_home']:str(home)})
  value=input('本机完整路径（回车使用 '+suggestion+'）：').strip() or suggestion
  p=Path(value).expanduser().absolute()
  if not p.is_dir():
   if input('目录不存在。创建空工作区并记录原文件未恢复？[y/N] ').strip().lower()!='y':raise ValueError('工作区未恢复，停止受影响的导入。')
   p.mkdir(parents=True,exist_ok=True);notes.append({'source':old,'target':str(p),'state':'empty_directory_created_source_workspace_files_not_restored'})
  dec.setdefault('path_map',{})[old]=str(p)
 for project in m['projects']:
  sid=project['id']
  if sid in dec.setdefault('project_map',{}):continue
  paths={dec['path_map'][r['path']] for r in project['roots']}
  hits=[p for p in projects if {r['path'] for r in roots if r['project_id']==p['id']}==paths]
  if len(hits)==1:dec['project_map'][sid]=hits[0]['id'];continue
  print('\n源项目：'+project['name'])
  candidates=hits or projects
  for i,p in enumerate(candidates,1):print(str(i)+'. '+p['name']+' ['+p['id']+']')
  pick=input('输入目标项目编号，或输入 new 使用上面的工作区登记新项目：').strip()
  if pick=='new':dec['project_map'][sid]='new'
  elif pick.isdigit() and 1<=int(pick)<=len(candidates):dec['project_map'][sid]=candidates[int(pick)-1]['id']
  else:raise ValueError('未取得明确的项目选择。')
 mt.write_json(dp,dec);ctx['temp'].cleanup();ctx,a,dp=prepare(args,bundle,out)
 if ctx['report']['pending']:
  dec=mt.read_json(dp,{})
  for item in ctx['report']['pending']:
   print('\n'+json.dumps(item,ensure_ascii=False,indent=2))
   choices=item.get('choices',[])
   if item['kind']=='sidebar_owner':
    value=input('目标侧边栏所有者 ID：').strip()
    if value not in item['target_candidates']:raise ValueError('无效的侧边栏所有者。')
    dec.setdefault('desktop_owner_map',{})[item['key'].split(':',1)[1]]=value
   elif choices:
    value=input('明确选择 '+ '/'.join(choices)+'：').strip()
    if value not in choices:raise ValueError('未确认此项差异。')
    dec.setdefault('conflicts',{})[item['key']]=value
   else:raise ValueError('该问题需要 GPT 检查报告后处理。')
  mt.write_json(dp,dec);ctx['temp'].cleanup();ctx,a,dp=prepare(args,bundle,out)
 return ctx,a,dp

def verification(bundle,codex):
 m=mt.validate_bundle(bundle);ledger=codex/'migration-imports'/(m['bundle_id']+'.json');record=mt.read_json(ledger,{})
 if not record:raise ValueError('目标没有该包的正式导入记录。')
 if record.get('manifest_sha256')!=mt.digest(bundle/'manifest.json'):raise ValueError('导入记录与迁移包清单不一致。')
 c=mt.ro_db(codex/mt.STATE);allrows={r['id']:r for r in mt.rows(c,'threads')};edges=mt.rows(c,'thread_spawn_edges');c.close()
 with tempfile.TemporaryDirectory(prefix='codex-verify-') as scratch:
  ctx={'codex':codex,'bundle':bundle,'m':m,'stage':Path(scratch),'target_threads':{tid:allrows[tid] for tid in record.get('target_ids_before',[]) if tid in allrows},'target_evidence':record.get('target_evidence_before',{})}
  if any(tid not in allrows for tid in record.get('target_ids_before',[])):raise ValueError('原有目标会话缺失。')
  mt.check_target_content(ctx,record)
 for link in record.get('imported_symlinks',[]):
  if not Path(link['dest']).is_symlink() or not Path(link['dest']).exists():raise ValueError('迁入链接目标缺失: '+link['dest'])
 ids=record.get('thread_id_map',{});actions=record.get('thread_actions',{});expected_edges={(ids.get(e['parent_thread_id'],e['parent_thread_id']),ids.get(e['child_thread_id'],e['child_thread_id'])) for e in m.get('crosscheck',{}).get('spawn_edges',[])}
 actual_edges={(e['parent_thread_id'],e['child_thread_id']) for e in edges}
 if not expected_edges.issubset(actual_edges):raise ValueError('父子 agent 关系缺失。')
 src=mt.ro_db(bundle/'payload/codex'/mt.STATE)
 source_rows=mt.rows(src,'threads');src.close()
 for row in source_rows:
  tid=row['id'];actual=allrows[ids.get(tid,tid)]
  if actions.get(tid)=='keep':continue
  expected=record['project_id_map'].get(row.get('project_id'),row.get('project_id'))
  if actual.get('project_id')!=expected or actual.get('archived')!=row.get('archived'):raise ValueError('项目归属或归档状态不符: '+tid)
 return {'ok':True,'bundle_id':m['bundle_id'],'verified_at':mt.stamp(),'source_threads':len(m['threads']),'previous_target_threads':len(record['target_ids_before']) if 'target_ids_before' in record else None,'target_prefix_baseline_available':'target_ids_before' in record,'total_threads':len(allrows),'source_archived_threads':sum(bool(t.get('archived')) for t in m['threads']),'source_status':m['status'],'source_exceptions':m.get('issues',[]),'journal':record.get('journal'),'original_archive':record.get('original_archive')}

def write_status(out,state,**detail):
 value={'state':state,'at':mt.stamp(),**detail};mt.write_json(out/'迁移运行状态.json',value);print(json.dumps(value,ensure_ascii=False),flush=True)

def offline(args,bundle,out,dp):
 home,codex=target(args);out.mkdir(parents=True,exist_ok=True)
 lock=(codex/'.migration-import.lock').open('a')
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 done=mt.read_json(out/'正式迁移完成报告.json',{})
 if done.get('ok') and done.get('bundle_id')==mt.read_json(bundle/'manifest.json')['bundle_id']:
  result=verification(bundle,codex);write_status(out,'completed',**result);return
 write_status(out,'prepared',message='预检准备完成，尚未写入正式目标。')
 request_quit=home==Path.home()
 if request_quit and not args.yes:
  if not sys.stdin.isatty():raise ValueError('请从生成的独立终端启动文件运行，保存工作后按回车。')
  input('保存工作后按回车；程序将请求退出桌面应用，核验完成后自动重开：')
 emit=lambda state,**detail:write_status(out,state,**detail)
 processes.wait_until_closed(mt,home,codex,emit,request_quit=request_quit,timeout=args.wait_seconds)
 ctx,a,dp=prepare(args,bundle,out)
 if not ctx['report']['ready']:raise ValueError('退出后最终预检未通过，正式目标未写入，详见目标预检报告.json。')
 protected={}
 for name in ['config.toml','attachments/pasted-text-attachments.json']:
  p=codex/name;key='file:codex/'+name
  if p.is_file() and mt.read_json(dp).get('conflicts',{}).get(key)!='use_source':protected[name]=mt.digest(p)
 a.backup_dir=out/'backups'/dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
 emit('merging',backup_dir=str(a.backup_dir),message='正在备份并合并，请保持应用关闭。')
 mt.merge(ctx,a)
 try:
  result=verification(bundle,codex)
  result['protected_files_unchanged']={name:mt.digest(codex/name)==value for name,value in protected.items()}
  if not all(result['protected_files_unchanged'].values()):raise ValueError('应保留的配置或附件索引发生变化。')
 except BaseException:
  mt.rollback(a.backup_dir/'rollback.json');raise
 result['journal']=str(a.backup_dir/'rollback.json')
 result['workspace_notes']=mt.read_json(dp).get('workspace_notes',[])
 mt.write_json(out/'正式迁移验证报告.json',result);mt.write_json(out/'正式迁移完成报告.json',result)
 text=['# 正式迁移完成报告','', '完成时间：'+result['verified_at'],'', '- 迁入源会话：'+str(result['source_threads'])+' 份。','- 保留迁移前目标会话：'+str(result['previous_target_threads'])+' 份。','- 合并后目标会话总数：'+str(result['total_threads'])+' 份。','- 全文、原有前缀、归属、归档、父子关系和数据库核验通过。','- 回滚记录：`'+str(a.backup_dir/'rollback.json')+'`。','','## 源异常及工作区缺口','']+[json.dumps(x,ensure_ascii=False) for x in result['source_exceptions']+result['workspace_notes']]
 (out/'正式迁移完成报告.md').write_text('\n'.join(text)+'\n');emit('completed',**result)
 if request_quit:
  app=next((str(p) for p in [Path('/Applications/Codex.app'),Path('/Applications/ChatGPT.app')] if p.is_dir()),None)
  if app:subprocess.run(['open','-a',app],check=False)

def import_bundle(args):
 bundle,out=locate_bundle(args);home,codex=target(args);ctx,a,dp=prepare(args,bundle,out)
 if args.interactive and not ctx['report']['ready']:ctx,a,dp=interactive_paths(args,bundle,out,ctx,a,dp)
 write_status(out,'prepared',ready=ctx['report']['ready'],pending=len(ctx['report']['pending']),errors=ctx['report']['errors'],source_status=ctx['report']['source_status'])
 if args.command=='prepare':return
 if not ctx['report']['ready']:raise ValueError('预检未通过；请填写 decisions.json 或在终端加 --interactive。')
 if args.launch:
  parts=[sys.executable,str(ROOT/'migration.py'),'run-offline','--bundle',str(bundle),'--output',str(out),'--decisions',str(dp),'--home',str(home),'--codex-home',str(codex),'--conflict-policy',args.conflict_policy,'--wait-seconds',str(args.wait_seconds)]
  command=out/'退出应用后导入.command';command.write_text('#!/bin/zsh\n'+shlex.join(parts)+'\nresult=$?\nif [ "$result" -ne 0 ]; then echo "导入停止，请查看迁移运行状态.json。按回车关闭。"; read reply; fi\nexit "$result"\n');command.chmod(0o700)
  print(json.dumps({'ready':True,'launcher':str(command),'message':'请在独立终端打开启动文件，保存工作后按回车。'},ensure_ascii=False));return
 ctx['temp'].cleanup();offline(args,bundle,out,dp)

def parser():
 p=argparse.ArgumentParser(description='Codex 本地迁移：导出、自包含导入、预检、验证、状态与回滚。');sub=p.add_subparsers(dest='command',required=True)
 def common(s):
  s.add_argument('--home',type=Path,default=Path.home());s.add_argument('--codex-home',type=Path)
 s=sub.add_parser('export');common(s);s.add_argument('--output',required=True)
 for name in ['prepare','import','run-offline']:
  s=sub.add_parser(name);common(s);g=s.add_mutually_exclusive_group(required=True);g.add_argument('--bundle');g.add_argument('--archive');s.add_argument('--sha256-file');s.add_argument('--output',default=str(Path.cwd()/'outputs/迁移导入'));s.add_argument('--decisions');s.add_argument('--conflict-policy',choices=['preserve-target','ask'],default='preserve-target');s.add_argument('--interactive',action='store_true');s.add_argument('--launch',action='store_true');s.add_argument('--yes',action='store_true');s.add_argument('--wait-seconds',type=int,default=1800)
 for name in ['status','verify']:
  s=sub.add_parser(name);common(s);s.add_argument('--bundle',required=True)
 s=sub.add_parser('rollback');s.add_argument('--journal',required=True,type=Path)
 sub.add_parser('self-test');return p

def main():
 args=parser().parse_args()
 try:
  if args.command=='export':export(args)
  elif args.command=='self-test':self_test()
  elif args.command=='rollback':mt.rollback(args.journal);print('已恢复本次迁移前的数据。')
  elif args.command=='run-offline':
   bundle,out=locate_bundle(args);dp=Path(args.decisions).resolve();offline(args,bundle,out,dp)
  elif args.command in ['prepare','import']:import_bundle(args)
  elif args.command=='verify':print(json.dumps(verification(Path(args.bundle).resolve(),target(args)[1]),ensure_ascii=False,indent=2))
  elif args.command=='status':
   bundle=Path(args.bundle).resolve();m=mt.validate_bundle(bundle);codex=target(args)[1];record=mt.read_json(codex/'migration-imports'/(m['bundle_id']+'.json'),{})
   c=mt.ro_db(codex/mt.STATE);rows={r['id']:r for r in mt.rows(c,'threads')};c.close();missing=[];files_missing=[]
   for t in m['threads']:
    tid=record.get('thread_id_map',{}).get(t['id'],t['id'])
    if tid not in rows:missing.append(tid)
    elif not Path(rows[tid]['rollout_path']).is_file():files_missing.append(tid)
   print(json.dumps({'bundle_id':m['bundle_id'],'import_ledger_present':bool(record),'source_threads':len(m['threads']),'found':len(m['threads'])-len(missing),'missing':missing,'missing_rollout_files':files_missing,'state':'imported_needs_full_verification' if record and not missing and not files_missing else 'not_completed'},ensure_ascii=False,indent=2))
 except Exception as exc:
  if getattr(args,'output',None) and args.command not in ['export']:
   out=Path(args.output).expanduser().resolve();out.mkdir(parents=True,exist_ok=True);write_status(out,'failed',message=str(exc))
  print('迁移停止：'+str(exc),file=sys.stderr);return 2
 return 0
if __name__=='__main__':sys.exit(main())
