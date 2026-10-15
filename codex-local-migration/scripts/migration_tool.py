#!/usr/bin/env python3
"""Codex local migration. Python 3.9+, stdlib only. Never execute imported content."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
import zipfile

FORMAT = 1
STATE = 'state_5.sqlite'
HISTORY = 'thread_history_1.sqlite'
APPDB = 'sqlite/codex-dev.db'
DB_TABLES = {
    STATE: ['thread_sections', 'projects', 'project_roots', 'threads', 'thread_dynamic_tools', 'thread_spawn_edges', 'thread_attachments'],
    HISTORY: ['thread_turns', 'thread_items', 'thread_history_projection_state', 'thread_realtime_items'],
    'memories_1.sqlite': ['stage1_outputs'],
    'goals_1.sqlite': ['thread_goals'],
    APPDB: ['local_thread_catalog_hosts', 'local_thread_catalog'],
}
SECRET = re.compile(r'(^|[_\-])(password|passwd|secret|access_token|refresh_token|id_token|api_key|apikey|authorization|cookie|private_key|client_secret|ownership_token|token|credential)([_\-]|$)', re.I)
EXCLUDED_NAMES = {'auth.json', 'credentials.json', 'credentials', '.env', 'Cookies', 'Cookies-journal', '.DS_Store', '.git', '__pycache__', 'node_modules'}

def stamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')

def read_json(p, default=None):
    return json.loads(Path(p).read_text(encoding='utf-8')) if Path(p).is_file() else copy.deepcopy(default)

def write_json(p, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def digest(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def fingerprint(p):
    p = Path(p)
    if p.is_symlink(): return {'kind': 'symlink', 'target': os.readlink(p)}
    if p.is_file(): return {'kind': 'file', 'sha256': digest(p), 'mode': stat.S_IMODE(p.stat().st_mode)}
    if p.exists(): raise ValueError('目标不是普通文件或链接: ' + str(p))
    return None

def under(p, root):
    try: Path(p).absolute().relative_to(Path(root).absolute()); return True
    except ValueError: return False

def safe_rel(s):
    p = Path(s)
    if p.is_absolute() or '..' in p.parts or not p.parts: raise ValueError('不安全的相对路径: ' + str(s))
    return p

def ro_db(p):
    p = Path(p)
    # Closed WAL-mode DBs without sidecars cannot always be opened read-only by
    # Apple's SQLite. No WAL means committed content is in the main file.
    # backup_db additionally checks that this quiescent state did not change.
    if not Path(str(p)+'-wal').exists() and not Path(str(p)+'-journal').exists():
        return sqlite3.connect(p.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=30)
    try: return sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=30)
    except sqlite3.OperationalError:
        header = p.open('rb').read(100)
        if header[18:20] == b'\x01\x01' and not Path(str(p)+'-journal').exists() and not Path(str(p)+'-wal').exists():
            return sqlite3.connect(p.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=30)
        raise

def backup_db(src, dest):
    src=Path(src);dest = Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        before=src.stat();inactive=not Path(str(src)+'-wal').exists() and not Path(str(src)+'-journal').exists()
        if dest.exists(): dest.unlink()
        a = ro_db(src); b = sqlite3.connect(str(dest))
        try: a.backup(b); b.execute('PRAGMA journal_mode=DELETE'); b.commit()
        finally: a.close(); b.close()
        after=src.stat()
        if not inactive or ((before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns) and not Path(str(src)+'-wal').exists() and not Path(str(src)+'-journal').exists()):return
    raise ValueError('关闭状态的数据库在备份时被重新写入，请重试: '+str(src))

def qi(s): return '"' + s.replace('"', '""') + '"'

def tables(c): return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

def rows(c, table):
    cur = c.execute('SELECT * FROM ' + qi(table)); keys = [x[0] for x in cur.description]
    return [dict(zip(keys, x)) for x in cur]

def schema(c):
    return {t: {'sql': c.execute('SELECT sql FROM sqlite_master WHERE name=?', (t,)).fetchone()[0],
                'columns': [list(x) for x in c.execute('PRAGMA table_info(' + qi(t) + ')')],
                'count': c.execute('SELECT count(*) FROM '+qi(t)).fetchone()[0]} for t in tables(c)}

def canonical_remote(s):
    if not s: return None
    s = s.strip()
    if s.startswith(('/', './', '../', 'file://')): return None
    if '://' in s:
        u = urllib.parse.urlsplit(s)
        s = (u.hostname or '') + u.path
    elif re.match(r'[^/]+@[^:]+:', s): s = s.split('@',1)[1].replace(':','/',1)
    return s.rstrip('/').removesuffix('.git') if hasattr(str,'removesuffix') else re.sub(r'\.git$', '', s.rstrip('/'))

def git_identity(p):
    try:
        r = subprocess.run(['git','-C',str(p),'config','--get','remote.origin.url'], capture_output=True, text=True, timeout=10)
        return canonical_remote(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired): return None

def sanitize(obj):
    if isinstance(obj, dict): return {k: sanitize(v) for k,v in obj.items() if not SECRET.search(k)}
    if isinstance(obj, list): return [sanitize(x) for x in obj]
    return obj

def clean_config(text):
    out=[]; removed=[]
    for line in text.splitlines(True):
        m=re.match(r'\s*([A-Za-z0-9_\-.\"\x27]+)\s*=',line)
        if m and SECRET.search(m.group(1).replace('.', '_').strip("\"'")):
            out.append('# migration: credential omitted: '+m.group(1)+'\n'); removed.append(m.group(1))
        else: out.append(line)
    return ''.join(out), removed

def path_replace(s, mapping):
    for old,new in sorted(mapping.items(),key=lambda x:len(x[0]),reverse=True):
        # Paths may appear in TOML, JSON keys, or colon-separated environment paths.
        s=re.sub(re.escape(old)+r'(?=$|[/\s\"\x27:\]\),])',lambda _:new,s)
    return s

def map_path(p, mapping):
    for old,new in sorted(mapping.items(),key=lambda x:len(x[0]),reverse=True):
        if p == old or p.startswith(old.rstrip('/')+'/'): return new.rstrip('/')+p[len(old.rstrip('/')):]
    return p

def map_obj(obj, mapping, ids=None):
    ids=ids or {}
    if isinstance(obj,dict): return {path_replace(ids.get(k,k),mapping):map_obj(v,mapping,ids) for k,v in obj.items()}
    if isinstance(obj,list): return [map_obj(x,mapping,ids) for x in obj]
    if isinstance(obj,str): return ids.get(obj,path_replace(obj,mapping))
    return obj

def structural_record(obj, mapping, ids):
    """Only change runtime metadata, never message text/tool output."""
    if obj.get('type') in ('session_meta','turn_context'):
        obj=copy.deepcopy(obj); p=obj.get('payload',{})
        if isinstance(p.get('cwd'),str): p['cwd']=map_path(p['cwd'],mapping)
        if obj['type']=='session_meta':
            if p.get('id') in ids: p['id']=ids[p['id']]
            if p.get('session_id') in ids:p['session_id']=ids[p['session_id']]
            def walk(v):
                if isinstance(v,dict):
                    for k,x in v.items():
                        if k in ('parent_thread_id','child_thread_id','thread_id') and isinstance(x,str): v[k]=ids.get(x,x)
                        else: walk(x)
                elif isinstance(v,list):
                    for x in v: walk(x)
            walk(p.get('source'))
    return obj

def render_rollout(src, dest, mapping, ids):
    old=new=0; offsets={0:0}; dest=Path(dest);dest.parent.mkdir(parents=True,exist_ok=True)
    with Path(src).open('rb') as a,dest.open('wb') as b:
        for line in a:
            obj=json.loads(line); changed=structural_record(obj,mapping,ids)
            data=(json.dumps(changed,ensure_ascii=False,separators=(',',':'))+'\n').encode() if changed!=obj else line
            b.write(data);old+=len(line);new+=len(data);offsets[old]=new
    return offsets

def compare_rollouts(a,b):
    """JSON equality avoids formatting differences while comparing every record."""
    with Path(a).open('rb') as fa,Path(b).open('rb') as fb:
        while True:
            x=fa.readline();y=fb.readline()
            if not x and not y:return 'same'
            if not x:return 'target_longer'
            if not y:return 'source_longer'
            if json.loads(x)!=json.loads(y):return 'diverged'

def record_evidence(p,limit=None):
    h=hashlib.sha256();count=0
    with Path(p).open('rb') as f:
        for line in f:
            if limit is not None and count>=limit:break
            h.update(json.dumps(json.loads(line),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()+b'\n');count+=1
    return {'records':count,'sha256':h.hexdigest()}

def validate_bundle(bundle):
    bundle=Path(bundle).resolve(); m=read_json(bundle/'manifest.json')
    if not m or m.get('format')!=FORMAT:raise ValueError('未知迁移包格式')
    expected={x['path'] for x in m['files']}
    actual=set()
    for p in (bundle/'payload').rglob('*'):
        if not p.is_file():continue
        name=str(p.relative_to(bundle))
        if p.name=='.DS_Store':continue
        if p.name.endswith('-shm') and name[:-4] in expected and not Path(str(p)[:-4]+'-wal').exists():continue
        actual.add(name)
    if actual!=expected: raise ValueError('载荷清单与实际文件不一致')
    for x in m['files']:
        p=bundle/safe_rel(x['path'])
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=x['bytes'] or digest(p)!=x['sha256']:raise ValueError('校验失败: '+x['path'])
    for x in m.get('support_files',[]):
        p=bundle/safe_rel(x['path'])
        if not p.is_file() or p.is_symlink() or digest(p)!=x['sha256']:raise ValueError('说明或工具文件校验失败: '+x['path'])
    for name in m['databases']:
        c=ro_db(bundle/'payload/codex'/name)
        try:
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('数据库完整性失败: '+name)
        finally:c.close()
    return m

def process_guard(home, codex):
    if Path(home).resolve()==Path.home().resolve():
        ps=subprocess.run(['ps','-axo','pid=,command='],capture_output=True,text=True,check=True).stdout
        for line in ps.splitlines():
            if re.search(r'/(?:ChatGPT|Codex)\.app/Contents/MacOS/[^ ]+',line):
                raise ValueError('请完全退出目标 Codex/ChatGPT 桌面应用后再执行。')
    dbs=[str(p) for p in Path(codex).rglob('*.sqlite') if 'migration-' not in str(p)]+[str(Path(codex)/APPDB)]
    if shutil.which('lsof') and dbs:
        r=subprocess.run(['lsof','-t','--']+dbs,capture_output=True,text=True)
        if r.stdout.strip():raise ValueError('目标数据库仍被进程占用；请退出应用及相关 CLI。')

def database_compatibility(src, target, table):
    if table not in tables(target):return '目标缺少数据表 '+table
    si={x[1]:x for x in src.execute('PRAGMA table_info('+qi(table)+')')}
    ti={x[1]:x for x in target.execute('PRAGMA table_info('+qi(table)+')')}
    if set(si)-set(ti):return '目标缺少源字段 '+str(sorted(set(si)-set(ti)))
    for k,x in ti.items():
        if k not in si and x[3] and x[4] is None and not x[5]:return '目标新增必填字段 '+k
        if k in si and (x[2].upper()!=si[k][2].upper() or x[5]!=si[k][5]):return '字段类型或主键不兼容 '+k
    return None

def insert_row(c,t,row,replace=False):
    cols=[x[1] for x in c.execute('PRAGMA table_info('+qi(t)+')')]
    r={k:v for k,v in row.items() if k in cols}
    sql='INSERT '+('OR REPLACE ' if replace else '')+'INTO '+qi(t)+' ('+','.join(qi(k) for k in r)+') VALUES ('+','.join('?' for _ in r)+')'
    c.execute(sql,list(r.values()))

def current_row(c,t,row):
    pk=[x[1] for x in sorted(c.execute('PRAGMA table_info('+qi(t)+')'),key=lambda x:x[5]) if x[5]]
    if not pk:raise ValueError('数据表无主键: '+t)
    cur=c.execute('SELECT * FROM '+qi(t)+' WHERE '+' AND '.join(qi(k)+'=?' for k in pk),[row[k] for k in pk])
    item=cur.fetchone()
    return dict(zip([x[0] for x in cur.description],item)) if item is not None else None

def conflict_key(db,table,row,c):
    pk=[x[1] for x in sorted(c.execute('PRAGMA table_info('+qi(table)+')'),key=lambda x:x[5]) if x[5]]
    return 'db:'+db+':'+table+':'+json.dumps([row[k] for k in pk],ensure_ascii=False,separators=(',',':'))

def write_zip(bundle, output):
    m=read_json(Path(bundle)/'manifest.json')
    include=list(dict.fromkeys([x['path'] for x in m['files']]+['manifest.json']+[x['path'] for x in m.get('support_files',[])]+['迁移说明.md','migration_tool.py','decisions.example.json','验证报告.json']))
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        for name in include:
            p=Path(bundle)/safe_rel(name)
            if p.is_file():z.write(p,arcname='Codex本地迁移/'+name)

def main():
    p=argparse.ArgumentParser(description='Codex 本地迁移工具：先预检，再在退出应用后合并。')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ['verify-bundle','preflight','merge','verify-target']:
        s=sub.add_parser(name);s.add_argument('--bundle',type=Path,default=Path(__file__).resolve().parent)
        if name!='verify-bundle':
            s.add_argument('--home',type=Path,default=Path.home());s.add_argument('--codex-home',type=Path)
            s.add_argument('--decisions',type=Path);s.add_argument('--report',type=Path)
        if name=='merge':s.add_argument('--backup-dir',type=Path)
    s=sub.add_parser('rollback');s.add_argument('--journal',type=Path,required=True);s.add_argument('--allow-changed',action='store_true')
    args=p.parse_args()
    try:
        if args.command=='verify-bundle':
            m=validate_bundle(args.bundle);print(json.dumps({'ok':True,'bundle_id':m['bundle_id'],'threads':len(m['threads']),'files':len(m['files']),'source_status':m['status']},ensure_ascii=False));return
        if args.command=='rollback':
            rollback(args.journal,allow_changed=args.allow_changed);print('已恢复目标迁移前的数据。');return
        ctx=prepare(args)
        if args.command=='preflight':
            print(json.dumps({'ready':ctx['report']['ready'],'report':str(ctx['report_path']),'pending':len(ctx['report']['pending'])},ensure_ascii=False));return
        if args.command=='merge':merge(ctx,args);return
        verify_target(ctx,args)
    except Exception as e:
        print('迁移工具停止: '+str(e),file=sys.stderr);sys.exit(2)

# Preparation, transactions and verification are defined below before main executes.

def desktop_merge(a,b,decisions,pending,prefix='state'):
    if isinstance(a,dict) and isinstance(b,dict):
        out=copy.deepcopy(a)
        for k,v in b.items():
            out[k]=desktop_merge(out[k],v,decisions,pending,prefix+'/'+k) if k in out else copy.deepcopy(v)
        return out
    if a==b:return copy.deepcopy(a)
    if isinstance(a,list) and isinstance(b,list):
        if not a:return copy.deepcopy(b)
        if not b:return copy.deepcopy(a)
        if all(isinstance(x,str) for x in a+b):return a+[x for x in b if x not in a]
    action=decisions.get('conflicts',{}).get(prefix)
    if action not in ('keep_target','use_source'):
        pending.append({'key':prefix,'kind':'desktop_state','choices':['keep_target','use_source']})
    return copy.deepcopy(b if action=='use_source' else a)

def transform_ui(obj,mapping,ids):
    if isinstance(obj,dict):
        result={}
        for k,v in obj.items():
            key=path_replace(k,mapping)
            for old,new in ids.items():key=key.replace(old,new)
            if k in ('prompt-history','composer-prompt-drafts-v2','chatgpt-conversation-prompt-drafts-v2'):
                result[key]={ids.get(t,t):x for t,x in v.items()} if isinstance(v,dict) else v
            else:result[key]=transform_ui(v,mapping,ids)
        return result
    if isinstance(obj,list):return [transform_ui(x,mapping,ids) for x in obj]
    if isinstance(obj,str):
        s=path_replace(obj,mapping)
        for old,new in ids.items():s=s.replace(old,new)
        return s
    return obj

def prepare(args):
    bundle=args.bundle.resolve();m=validate_bundle(bundle);home=args.home.expanduser().resolve()
    codex=(args.codex_home or home/'.codex').expanduser().resolve()
    dec=read_json(args.decisions,{}) if args.decisions else {}
    if not (codex/STATE).is_file():raise ValueError('目标缺少 '+STATE+'；请先安装并启动目标应用，或由目标 GPT 检查版本兼容性。')
    pending=[];errors=[];notes=[]
    for issue in m.get('issues',[]):
        if issue['kind'] in ('missing_rollout','missing_rollout_for_db_thread','unindexed_rollout','incomplete_tail','history_beyond_rollout'):
            errors.append('源数据需要先修复/补建登记，不能自动合并: '+json.dumps(issue,ensure_ascii=False))
        else:notes.append(issue)
    database_before={name:fingerprint(codex/name) for name in DB_TABLES if (codex/name).exists()}
    ui_before=fingerprint(codex/'.codex-global-state.json');index_before=fingerprint(codex/'session_index.jsonl')
    roots={'codex':codex,'agents':home/'.agents','runtime_plugins':codex/'migration-resources'/m['bundle_id']/'runtime_plugins','external_assets':codex/'migrated-assets'/m['bundle_id'],'portable_state':codex/'migration-preserved'/m['bundle_id']/'portable_state'}
    mapping={m['source_codex_home']:str(codex),m['source_home']+'/.agents':str(home/'.agents'),m['source_home']+'/Documents/Codex':str(home/'Documents/Codex')}
    for old,scope in m.get('resource_roots',{}).items():mapping[old]=str(roots[scope])
    for item in m.get('external_assets',[]):mapping[item['source']]=str(roots['external_assets']/safe_rel(item['relative']))
    mapping.update({str(k).rstrip('/'):str(Path(v).expanduser().absolute()).rstrip('/') for k,v in dec.get('path_map',{}).items()})
    for old in m.get('app_paths',[]):
        if old in dec.get('path_map',{}):continue
        candidates=[p for p in [Path(old),Path('/Applications/ChatGPT.app'),Path('/Applications/Codex.app')] if p.is_dir()]
        if candidates:mapping[old]=str(candidates[0])
    tc=ro_db(codex/STATE);target_projects=rows(tc,'projects');target_roots=rows(tc,'project_roots');tc.close()
    proots={p['id']:[r['path'] for r in target_roots if r['project_id']==p['id']] for p in target_projects}
    target_by_id={p['id']:p for p in target_projects};project_ids={};project_creates=set()
    previous_import=read_json(codex/'migration-imports'/(m['bundle_id']+'.json'),{})
    for p in m['projects']:
        explicit=dec.get('project_map',{}).get(p['id'])
        candidates=[];exact_candidates=[]
        for t in target_projects:
            tr=proots[t['id']];sr=[mapping.get(r['path'],r['path']) for r in p['roots']]
            exact=set(sr)==set(tr)
            sgit={r['git_identity'] for r in p['roots'] if r.get('git_identity')}
            tgit={git_identity(r) for r in tr};tgit.discard(None)
            if exact:exact_candidates.append(t['id'])
            if exact or (sgit and sgit==tgit):candidates.append(t['id'])
        if exact_candidates:candidates=exact_candidates
        if explicit=='new':
            dest=p['id']
            if dest in target_by_id:
                if previous_import.get('manifest_sha256')==digest(bundle/'manifest.json') and previous_import.get('project_id_map',{}).get(p['id'])==dest:pass
                else:pending.append({'key':'project:'+p['id'],'kind':'project_id_collision','choices':list(target_by_id)});dest=None
            else:project_creates.add(dest)
        elif explicit:
            dest=explicit
            if dest not in target_by_id:errors.append('project_map 引用了不存在的目标项目: '+dest)
        elif len(candidates)==1:dest=candidates[0]
        else:
            dest=None;pending.append({'key':'project:'+p['id'],'kind':'project_match','name':p['name'],'old_roots':[r['path'] for r in p['roots']],'candidates':candidates,'instruction':'询问用户项目对应的新路径，然后填写 path_map 和 project_map；确认要登记新项目时 project_map 填 new。'})
        project_ids[p['id']]=dest or p['id']
        for r in p['roots']:
            old=r['path']
            if old not in mapping:
                if dest in proots and old in proots[dest] and Path(old).is_dir():mapping[old]=old
                elif dest in proots and r.get('git_identity'):
                    hits=[x for x in proots[dest] if git_identity(x)==r['git_identity']]
                    if len(hits)==1:mapping[old]=hits[0]
            if old not in mapping:
                pending.append({'key':'path:'+old,'kind':'project_path','instruction':'请用户提供该根目录的新路径，写入 path_map。'})
            elif not Path(mapping[old]).is_dir():errors.append('项目目录尚不存在，请先恢复单独打包的工作区: '+mapping[old])
            elif dest in proots and mapping[old] not in proots[dest]:
                # Explicitly supplied roots may be added, but never silently infer a new root.
                if old not in dec.get('path_map',{}):pending.append({'key':'path:'+old,'kind':'project_root_confirmation'})
    for old in m.get('projectless_directories',[]):
        if old not in mapping:
            pending.append({'key':'path:'+old,'kind':'projectless_path','instruction':'独立聊天工作区已另行打包，请填写其恢复后的完整路径。'})
        elif not Path(mapping[old]).is_dir():errors.append('独立聊天目录尚不存在: '+mapping[old])
    temp=tempfile.TemporaryDirectory(prefix='codex-migration-preflight-');stage=Path(temp.name)
    ids={};thread_actions={};offset_maps={};rendered={};target_threads={}
    c=ro_db(codex/STATE)
    target_threads={x['id']:x for x in rows(c,'threads')};c.close()
    target_evidence={tid:record_evidence(t['rollout_path']) for tid,t in target_threads.items() if Path(t['rollout_path']).is_file()}
    for t in m['threads']:
        tid=t['id'];old=target_threads.get(tid);src=bundle/t['payload_path'];dst=stage/'rollouts'/tid
        offsets=render_rollout(src,dst,mapping,{})
        if old:
            tp=Path(old['rollout_path'])
            if not tp.is_file():errors.append('目标会话记录缺失: '+tid);relation='missing'
            else:relation=compare_rollouts(dst,tp)
            if relation=='diverged':
                action=dec.get('conflicts',{}).get('thread:'+tid)
                if action!='keep_both':pending.append({'key':'thread:'+tid,'kind':'divergent_thread','choices':['keep_both'],'instruction':'两边原始记录都保留；确认后为迁入分支分配新 ID。'})
                else:ids[tid]=str(uuid.uuid5(uuid.NAMESPACE_URL,m['bundle_id']+':'+tid))
                thread_actions[tid]='clone'
            else:thread_actions[tid]={'same':'keep','target_longer':'keep','source_longer':'replace'}.get(relation,'blocked')
        else:thread_actions[tid]='insert'
        rendered[tid]=dst;offset_maps[tid]=offsets
    # Reusing children after cloning a parent would damage one of the two graphs.
    changed=True
    while changed:
        changed=False
        for edge in m.get('crosscheck',{}).get('spawn_edges',[]):
            child=edge['child_thread_id']
            if edge['parent_thread_id'] in ids and child not in ids:
                ids[child]=str(uuid.uuid5(uuid.NAMESPACE_URL,m['bundle_id']+':'+child));thread_actions[child]='clone';changed=True
    for t in m['threads']:
        tid=t['id'];offset_maps[tid]=render_rollout(bundle/t['payload_path'],rendered[tid],mapping,ids)
        new_id=ids.get(tid,tid)
        if new_id!=tid and new_id in target_threads:
            if compare_rollouts(rendered[tid],Path(target_threads[new_id]['rollout_path'])) in ('same','target_longer'):thread_actions[tid]='keep'
            else:errors.append('已导入分支后来又发生分歧，请由目标 GPT 单独处理: '+tid)
    operations=[]
    ignore={STATE,HISTORY,'memories_1.sqlite','goals_1.sqlite',APPDB,'queue_1.sqlite','session_index.jsonl','.codex-global-state.json'}
    for item in m['files']:
        scope=item['scope'];rel=item['relative']
        if scope=='codex' and (rel in ignore or rel.startswith(('sessions/','archived_sessions/'))):continue
        src=bundle/item['path'];dest=roots[scope]/safe_rel(rel);data=None
        if scope=='codex' and rel.startswith('automations/'):
            dest=roots['portable_state']/safe_rel(rel)
        if scope=='codex' and rel=='config.toml':data=path_replace(src.read_text(encoding='utf-8'),mapping).encode()
        elif scope=='codex' and rel=='attachments/pasted-text-attachments.json':
            registry=read_json(src,{})
            for k in ('attachmentPaths','pendingRemovalPaths'):
                if k in registry:registry[k]=[map_path(x,mapping) for x in registry[k]]
            if 'textExcerptsByPath' in registry:registry['textExcerptsByPath']={map_path(k,mapping):v for k,v in registry['textExcerptsByPath'].items()}
            data=(json.dumps(registry,ensure_ascii=False,indent=2)+'\n').encode()
        candidate_sha=hashlib.sha256(data).hexdigest() if data is not None else item['sha256']
        before=fingerprint(dest)
        if before and before.get('sha256')==candidate_sha:continue
        key=('skill:'+scope+':'+rel.split('/')[1]) if rel.startswith('skills/') else 'file:'+scope+'/'+rel
        action=dec.get('conflicts',{}).get(key)
        override=dec.get('file_overrides',{}).get(scope+'/'+rel)
        if override:
            src=Path(override).expanduser().resolve()
            if not src.is_file():errors.append('替代文件不存在: '+str(src))
            else:data=src.read_bytes();candidate_sha=hashlib.sha256(data).hexdigest()
        if before and action not in ('keep_target','use_source'):
            pending.append({'key':key,'kind':'file','path':str(dest),'source_sha256':candidate_sha,'target_sha256':before.get('sha256'),'choices':['keep_target','use_source']})
        if before and action=='keep_target':continue
        operations.append({'dest':str(dest),'source':str(src),'data':data,'mode':item.get('mode',0o644),'before':before})
    for item in m.get('symlinks',[]):
        dest=roots[item['scope']]/safe_rel(item['relative']);target=map_path(item['resolved_target'],mapping)
        if target==item['resolved_target'] and not any(under(target,r) for r in roots.values()):
            errors.append('链接目标未映射: '+str(dest));continue
        link=os.path.relpath(target,str(dest.parent));before=fingerprint(dest)
        if before and before.get('kind')=='symlink' and (dest.parent/before['target']).resolve()==Path(target).resolve():continue
        key='skill:'+item['scope']+':'+item['relative'].split('/')[1] if item['relative'].startswith('skills/') else 'file:'+item['scope']+'/'+item['relative']
        action=dec.get('conflicts',{}).get(key)
        if before and action not in ('keep_target','use_source'):pending.append({'key':key,'kind':'symlink','path':str(dest),'choices':['keep_target','use_source']})
        if before and action=='keep_target':continue
        operations.append({'dest':str(dest),'link':link,'before':before})
    # An explicit keep_target decision applies to the entire skill, including missing files.
    for op in list(operations):
        for scope,base in roots.items():
            if under(op['dest'],base):
                rel=str(Path(op['dest']).relative_to(base))
                if rel.startswith('skills/') and dec.get('conflicts',{}).get('skill:'+scope+':'+rel.split('/')[1])=='keep_target':operations.remove(op)
                break
    source_ui=read_json(bundle/'payload/codex/.codex-global-state.json',{})
    current_ui=read_json(codex/'.codex-global-state.json',{})
    target_legacy=current_ui.get('app-server-project-id-by-legacy-project-id-by-host',{}).get('local:'+str(codex),{})
    target_ui_id={actual:old for old,actual in target_legacy.items() if old in current_ui.get('local-projects',{})}
    ui_ids={old:target_ui_id.get(new,new) for old,new in project_ids.items()};ui_ids.update(ids)
    ui=transform_ui(source_ui,mapping,ui_ids)
    # A matched existing project's own metadata remains authoritative; differences are handled in its DB row.
    for pid in list(ui.get('local-projects',{})):
        if pid in current_ui.get('local-projects',{}):ui['local-projects'][pid]=current_ui['local-projects'][pid]
    owner_map=dec.get('desktop_owner_map',{})
    atom=ui.get('electron-persisted-atom-state',{})
    if 'sidebar-custom-sections-v3' in atom:
        old=atom['sidebar-custom-sections-v3'];new=current_ui.get('electron-persisted-atom-state',{}).get('sidebar-custom-sections-v3',{})
        for owner in list(old):
            if owner in owner_map:old[owner_map[owner]]=old.pop(owner)
            elif new and owner not in new:
                pending.append({'key':'desktop_owner:'+owner,'kind':'sidebar_owner','target_candidates':list(new),'instruction':'在 desktop_owner_map 中填写新电脑当前账号对应的侧边栏所有者 ID。'})
    merged_ui=desktop_merge(current_ui,ui,dec,pending)
    ui_project_map=merged_ui.setdefault('app-server-project-id-by-legacy-project-id-by-host',{}).setdefault('local:'+str(codex),{})
    for source_id,actual in project_ids.items():ui_project_map[ui_ids[source_id]]=actual
    dbplans={};mapped_threads={};source_to_dest_rollout={}
    thread_manifest={t['id']:t for t in m['threads']}
    for t in m['threads']:
        tid=t['id'];new_id=ids.get(tid,tid);old=target_threads.get(new_id)
        if thread_actions[tid]=='keep' and old:path=old['rollout_path']
        elif thread_actions[tid]=='replace' and old:path=old['rollout_path']
        else:
            original=Path(t['original_path']);rel=Path('archived_sessions' if t.get('archived') else 'sessions')/('imported-'+m['bundle_id'])/original.name
            if new_id!=tid:rel=rel.with_name(rel.name.replace(tid,new_id))
            path=str(codex/rel)
        source_to_dest_rollout[tid]=path
        if thread_actions[tid] not in ('keep','blocked'):
            operations.append({'dest':path,'source':str(rendered[tid]),'mode':0o600,'before':fingerprint(path)})
    for name,ts in DB_TABLES.items():
        source=bundle/'payload/codex'/name;target=codex/name
        if not source.is_file():continue
        sc=ro_db(source)
        present=[t for t in ts if t in tables(sc)]
        nonempty=[t for t in present if sc.execute('SELECT count(*) FROM '+qi(t)).fetchone()[0]]
        if not target.is_file():
            if nonempty:errors.append('目标数据库不存在，不能直接覆盖创建: '+name)
            sc.close();continue
        tc=ro_db(target);plans=[]
        for table in present:
            err=database_compatibility(sc,tc,table)
            if err:errors.append(name+': '+err);continue
            for row in rows(sc,table):
                r=copy.deepcopy(row);tid=r.get('thread_id')
                if tid and tid not in thread_manifest:continue
                action=thread_actions.get(tid)
                if name==HISTORY and action=='keep':continue
                if table=='threads':
                    tid=r['id'];action=thread_actions[tid];r['id']=ids.get(tid,tid)
                    r['cwd']=map_path(r['cwd'],mapping);r['rollout_path']=source_to_dest_rollout[tid]
                    r['project_id']=project_ids.get(r.get('project_id'),r.get('project_id'))
                    if tid in ids:r['title']+='（迁入分支）'
                    if r.get('source','').startswith('{'):
                        r['source']=json.dumps(structural_record({'type':'session_meta','payload':{'source':json.loads(r['source'])}},mapping,ids)['payload']['source'],ensure_ascii=False)
                    mapped_threads[tid]=r
                    if action in ('keep','replace'):
                        existing=current_row(tc,table,r)
                        if existing:
                            meaningful=['title','archived','archived_at','is_pinned','thread_section_id','project_id','name']
                            diffs=[k for k in meaningful if r.get(k)!=existing.get(k)]
                            if diffs:
                                key=conflict_key(name,table,r,tc);choice=dec.get('conflicts',{}).get(key)
                                if choice not in ('keep_target','use_source'):pending.append({'key':key,'kind':'thread_metadata','different_fields':diffs,'choices':['keep_target','use_source']})
                                if choice=='use_source':
                                    for k in meaningful:existing[k]=r.get(k)
                                    if action=='keep':plans.append((table,existing,'update'))
                                elif action=='replace':
                                    for k in meaningful:r[k]=existing.get(k)
                        if action=='keep':continue
                elif table=='projects':
                    sid=r['id'];r['id']=project_ids[sid]
                    if sid not in project_creates:
                        old=current_row(tc,table,r)
                        if old:
                            diffs=[k for k in ['name','metadata'] if old.get(k)!=r.get(k)]
                            if diffs:
                                key=conflict_key(name,table,r,tc);choice=dec.get('conflicts',{}).get(key)
                                if choice not in ('keep_target','use_source'):pending.append({'key':key,'kind':'project_metadata','different_fields':diffs,'choices':['keep_target','use_source']})
                                if choice=='use_source':
                                    old.update({k:r[k] for k in diffs});plans.append((table,old,'update'))
                            continue
                elif table=='project_roots':
                    r['project_id']=project_ids[r['project_id']];r['path']=map_path(r['path'],mapping)
                    existing=[x for x in rows(tc,table) if x['project_id']==r['project_id']]
                    extra=[x[1] for x in plans if x[0]==table and x[1]['project_id']==r['project_id']]
                    if any(x['path']==r['path'] for x in existing+extra):continue
                    r['position']=max([x['position'] for x in existing+extra]+[-1])+1
                else:
                    if tid:r['thread_id']=ids.get(tid,tid)
                    for k in ('parent_thread_id','child_thread_id'): 
                        if k in r:r[k]=ids.get(r[k],r[k])
                    if 'project_id' in r:r['project_id']=project_ids.get(r['project_id'],r['project_id'])
                    if 'cwd' in r and r['cwd']:r['cwd']=map_path(r['cwd'],mapping)
                    if table in ('thread_history_projection_state','thread_turns'):
                        for k in ('next_rollout_byte_offset','rollout_byte_offset','rollout_end_byte_offset'):
                            if k in r and r[k] is not None:
                                if r[k] not in offset_maps[tid]:errors.append('历史数据库字节边界不在原始记录边界: '+tid+'/'+k)
                                else:r[k]=offset_maps[tid][r[k]]
                    if table=='thread_attachments':r['payload']=json.dumps(map_obj(json.loads(r['payload']),mapping,ui_ids),ensure_ascii=False)
                old=current_row(tc,table,r)
                if old==r:continue
                if old and not (name==HISTORY or (table=='threads' and action=='replace')):
                    # Catalog is a cache; source cache never overwrites existing target entries.
                    if name==APPDB:continue
                    key=conflict_key(name,table,r,tc);choice=dec.get('conflicts',{}).get(key)
                    if choice not in ('keep_target','use_source'):pending.append({'key':key,'kind':'database_row','different_fields':[k for k in r if r[k]!=old.get(k)],'choices':['keep_target','use_source']})
                    if choice!='use_source':continue
                plans.append((table,r,'update' if old else 'insert'))
        sc.close();tc.close();dbplans[name]=plans
    # Do not alter any target through a symlinked parent directory.
    allowed=[codex,home/'.agents']
    for op in operations:
        dest=Path(op['dest'])
        if not any(under(dest,x) for x in allowed):errors.append('目标文件不在允许的迁移数据目录: '+str(dest))
        for parent in dest.parents:
            if parent in allowed:break
            if parent.is_symlink():errors.append('目标父目录是符号链接，需人工处理: '+str(parent));break
    seen=set();unique=[]
    for x in pending:
        if x['key'] not in seen:unique.append(x);seen.add(x['key'])
    report={'created_at':stamp(),'bundle_id':m['bundle_id'],'ready':not unique and not errors,'pending':unique,'errors':errors,'notes':notes,
            'path_map':mapping,'project_map':project_ids,'thread_id_map':ids,'thread_actions':thread_actions,
            'file_operations':len(operations),'database_rows':{k:len(v) for k,v in dbplans.items()},'source_status':m['status']}
    report_path=(args.report or bundle/'目标预检报告.json').absolute();write_json(report_path,report)
    return dict(bundle=bundle,m=m,home=home,codex=codex,dec=dec,temp=temp,stage=stage,report=report,report_path=report_path,
                mapping=mapping,ids=ids,thread_actions=thread_actions,operations=operations,dbplans=dbplans,ui=merged_ui,
                target_threads=target_threads,target_evidence=target_evidence,rollout_dest=source_to_dest_rollout,
                database_before=database_before,ui_before=ui_before,index_before=index_before)

def update_row(c,table,row):
    pk=[x[1] for x in c.execute('PRAGMA table_info('+qi(table)+')') if x[5]]
    keys=[k for k in row if k not in pk]
    c.execute('UPDATE '+qi(table)+' SET '+','.join(qi(k)+'=?' for k in keys)+' WHERE '+' AND '.join(qi(k)+'=?' for k in pk),[row[k] for k in keys+pk])

def restore_journal(journal,check=True,allow_changed=False):
    jp=Path(journal);j=read_json(jp)
    progress=jp.with_suffix('.progress.jsonl')
    if progress.exists():
        for line in progress.read_text().splitlines():
            if not line.strip():continue
            event=json.loads(line);j['operations'][event.pop('index')].update(event)
    if check:
        process_guard(j['target_home'],j['target_codex'])
        if not allow_changed:
            changed=[x['dest'] for x in j['operations'] if x.get('attempted') and fingerprint(x['dest'])!=(x.get('after') if x.get('applied') else x['before'])]
            if changed:raise ValueError('迁移后文件已变化；默认不覆盖新数据。请先备份并确认回滚范围: '+str(changed[:5]))
    for op in reversed(j['operations']):
        if not op.get('attempted'):continue
        dest=Path(op['dest']);before=op['before']
        if dest.is_symlink() or dest.is_file():dest.unlink()
        elif dest.exists():raise ValueError('回滚遇到目录占用: '+str(dest))
        if before:
            dest.parent.mkdir(parents=True,exist_ok=True)
            if before['kind']=='symlink':dest.symlink_to(before['target'])
            else:shutil.copy2(jp.parent/op['backup'],dest);os.chmod(dest,before['mode'])
    j['status']='rolled_back';j['rolled_back_at']=stamp();write_json(jp,j)

def rollback(journal,allow_changed=False):restore_journal(journal,True,allow_changed)

def merge(ctx,args):
    if not ctx['report']['ready']:raise ValueError('存在待确认项或兼容性错误；详见 '+str(ctx['report_path']))
    process_guard(ctx['home'],ctx['codex'])
    codex=ctx['codex'];stage=ctx['stage'];ops=ctx['operations'];m=ctx['m']
    # Stage complete target databases. Never copy a source database over a target.
    for name,plans in ctx['dbplans'].items():
        if not plans:continue
        target=codex/name
        if fingerprint(target)!=ctx['database_before'].get(name):raise ValueError('预检后目标数据库变化，请重新预检: '+name)
        staged=stage/'db'/name;backup_db(target,staged);c=sqlite3.connect(str(staged))
        try:
            c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN')
            if name==HISTORY:
                for tid,action in ctx['thread_actions'].items():
                    if action=='replace':
                        for t in DB_TABLES[HISTORY]:
                            if t in tables(c):c.execute('DELETE FROM '+qi(t)+' WHERE thread_id=?',(tid,))
            for table,row,action in plans:
                if action=='insert':insert_row(c,table,row)
                else:update_row(c,table,row)
            if name==APPDB:
                if 'local_thread_catalog_sync_state' in tables(c):c.execute("UPDATE local_thread_catalog_sync_state SET watermark_updated_at=0,initial_build_complete=0 WHERE host_id='local'")
                if 'local_thread_catalog_metadata' in tables(c):c.execute('UPDATE local_thread_catalog_metadata SET catalog_revision=catalog_revision+1')
            c.commit()
            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('合并数据库完整性失败: '+name)
            if c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('合并数据库关联失败: '+name)
        finally:c.close()
        ops.append({'dest':str(target),'source':str(staged),'mode':0o600,'before':ctx['database_before'][name]})
        for suffix in ('-wal','-shm','-journal'):
            p=Path(str(target)+suffix)
            if p.exists():ops.insert(0,{'dest':str(p),'delete':True,'before':fingerprint(p)})
    ui_dest=codex/'.codex-global-state.json'
    ops.append({'dest':str(ui_dest),'data':(json.dumps(ctx['ui'],ensure_ascii=False,indent=2)+'\n').encode(),'mode':0o600,'before':ctx['ui_before']})
    # Rebuild the small index from the complete merged state, including archived/subagent sessions.
    state_path=stage/'db'/STATE if (stage/'db'/STATE).is_file() else codex/STATE
    c=ro_db(state_path);index_rows=rows(c,'threads');c.close()
    index=''.join(json.dumps({'id':r['id'],'thread_name':r['title'],'updated_at':dt.datetime.fromtimestamp(r['updated_at'],dt.timezone.utc).isoformat()},ensure_ascii=False)+'\n' for r in index_rows)
    ops.append({'dest':str(codex/'session_index.jsonl'),'data':index.encode(),'mode':0o600,'before':ctx['index_before']})
    original=codex/'migration-originals'/(m['bundle_id']+'.zip')
    if not original.exists():
        saved=stage/'original.zip';write_zip(ctx['bundle'],saved)
        ops.append({'dest':str(original),'source':str(saved),'mode':0o600,'before':None})
    ledger=codex/'migration-imports'/(m['bundle_id']+'.json')
    record={'bundle_id':m['bundle_id'],'imported_at':stamp(),'manifest_sha256':digest(ctx['bundle']/'manifest.json'),'thread_id_map':ctx['ids'],'project_id_map':ctx['report']['project_map'],
            'rollout_paths':ctx['rollout_dest'],'thread_actions':ctx['thread_actions'],'path_map':ctx['mapping'],'original_archive':str(original),
            'target_evidence_before':ctx['target_evidence'],'target_ids_before':list(ctx['target_threads']),
            'imported_symlinks':[{'dest':op['dest'],'link':op['link']} for op in ops if 'link' in op]}
    ops.append({'dest':str(ledger),'data':(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode(),'mode':0o600,'before':fingerprint(ledger)})
    # Coalesce duplicate destinations and reject concurrent target changes before any write.
    unique={}
    for op in ops:
        if op['dest'] in unique:raise ValueError('内部错误：重复写入目标 '+op['dest'])
        unique[op['dest']]=op
        if fingerprint(op['dest'])!=op['before']:raise ValueError('预检后目标已发生变化，请重新预检: '+op['dest'])
    backup=(args.backup_dir or ctx['bundle']/'target-backups'/dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')).absolute()
    if backup.exists() and any(backup.iterdir()):raise ValueError('备份目录必须为空')
    backup.mkdir(parents=True,exist_ok=True);os.chmod(backup,0o700)
    j={'format':1,'status':'prepared','created_at':stamp(),'bundle_id':m['bundle_id'],'target_home':str(ctx['home']),'target_codex':str(codex),'operations':[]}
    for i,op in enumerate(ops):
        entry={k:v for k,v in op.items() if k in ('dest','before')};entry['attempted']=False;entry['applied']=False
        if op['before'] and op['before']['kind']=='file':
            rel='files/'+str(i);p=backup/rel;p.parent.mkdir(exist_ok=True);shutil.copy2(op['dest'],p);entry['backup']=rel
        j['operations'].append(entry)
    jp=backup/'rollback.json';write_json(jp,j)
    def progress(index,values):
        with jp.with_suffix('.progress.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(dict(index=index,**values),ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
    try:
        process_guard(ctx['home'],codex)
        for i,op in enumerate(ops):
            dest=Path(op['dest']);entry=j['operations'][i]
            if fingerprint(dest)!=op['before']:raise ValueError('目标文件在执行期间变化: '+str(dest))
            entry['attempted']=True;progress(i,{'attempted':True})
            dest.parent.mkdir(parents=True,exist_ok=True)
            if op.get('delete'):
                if dest.exists() or dest.is_symlink():dest.unlink()
            elif 'link' in op:
                temp=dest.with_name(dest.name+'.migration-'+uuid.uuid4().hex);temp.symlink_to(op['link']);os.replace(temp,dest)
            else:
                fd,tmp=tempfile.mkstemp(prefix='.migration-',dir=str(dest.parent));os.close(fd)
                try:
                    if op.get('data') is not None:Path(tmp).write_bytes(op['data'])
                    else:shutil.copyfile(op['source'],tmp)
                    os.chmod(tmp,op.get('mode',0o600));os.replace(tmp,dest)
                finally:
                    if Path(tmp).exists():Path(tmp).unlink()
            entry['after']=fingerprint(dest);entry['applied']=True;progress(i,{'after':entry['after'],'applied':True})
        j['status']='committed';j['committed_at']=stamp();write_json(jp,j)
        check_target_content(ctx,record)
    except BaseException:
        restore_journal(jp,False,True);raise
    print(json.dumps({'ok':True,'journal':str(jp),'threads':len(m['threads']),'originals':str(original)},ensure_ascii=False))

def check_target_content(ctx,record):
    codex=ctx['codex'];c=ro_db(codex/STATE);target={r['id']:r for r in rows(c,'threads')}
    try:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('目标数据库检查失败')
        if c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('目标数据库关联检查失败')
        for t in ctx['m']['threads']:
            tid=t['id'];new_id=record['thread_id_map'].get(tid,tid)
            if new_id not in target:raise ValueError('迁入会话未登记: '+tid)
            dest=Path(target[new_id]['rollout_path'])
            rendered=ctx['stage']/'verify'/tid;render_rollout(ctx['bundle']/t['payload_path'],rendered,record['path_map'],record['thread_id_map'])
            if not dest.is_file() or compare_rollouts(rendered,dest) not in ('same','target_longer'):raise ValueError('迁入会话未完整保留: '+tid)
        # All preexisting target sessions must still be present and have at least their previous record prefix.
        for tid,old in ctx.get('target_threads',{}).items():
            if tid not in target:raise ValueError('原有目标会话丢失: '+tid)
            evidence=ctx.get('target_evidence',{}).get(tid)
            if evidence and record_evidence(target[tid]['rollout_path'],evidence['records'])!=evidence:raise ValueError('原有目标会话内容前缀被改变: '+tid)
    finally:c.close()
    for op in ctx.get('operations',[]):
        if 'link' in op and not Path(op['dest']).exists():raise ValueError('迁入链接目标缺失: '+op['dest'])

def verify_target(ctx,args):
    record=read_json(ctx['codex']/'migration-imports'/(ctx['m']['bundle_id']+'.json'))
    if not record:raise ValueError('目标不存在该包的导入记录')
    check_target_content(ctx,record)
    result={'ok':True,'bundle_id':ctx['m']['bundle_id'],'threads':len(ctx['m']['threads']),'verified_at':stamp()}
    out=args.report or ctx['bundle']/'目标验证报告.json';write_json(out,result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
