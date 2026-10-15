#!/usr/bin/env python3
"""Read-only source exporter. All mutations are confined to --bundle."""
import argparse
import collections
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
import uuid
import plistlib
from migration_tool import (STATE,HISTORY,APPDB,FORMAT,SECRET,EXCLUDED_NAMES,stamp,read_json,write_json,digest,backup_db,ro_db,rows,schema,tables,qi,under,sanitize,clean_config,git_identity,transform_ui)

TREES=['skills','agents','plugins/cache','plugins/data','plans','memories','pets','attachments','generated_images','visualizations','vendor_imports','ambient-suggestions','.tmp/bundled-marketplaces','.tmp/marketplaces','.tmp/plugins','code-review-plugin','automations']
DBS=[STATE,HISTORY,'memories_1.sqlite','goals_1.sqlite','queue_1.sqlite',APPDB]
GLOBAL_KEYS=['local-projects','project-order','project-appearances','sidebar-project-thread-orders','thread-project-assignments','thread-project-membership-host-ids','electron-thread-read-state-v1','projectless-thread-ids','thread-projectless-output-directories','thread-workspace-root-hints','electron-managed-worktree-archives']
ATOM_KEYS=['prompt-history','sidebar-custom-sections-v3','flat-project-sidebar-preferences-v1','thread-descriptions-v1','thread-user-activity-times-v1','composer-prompt-drafts-v2','thread-workspace-draft-tabs-v1','composer-retained-documents-v1','recent-workspace-files-v3']
ATOM_PREFIXES=['thread-tab-routes-v1:','thread-client-id-v1:','sidebar-project-expanded-v1-codex:','codex-writing-block-deleted-thread-v1:']
ASSET_EXT={'.png','.jpg','.jpeg','.webp','.gif','.svg','.pdf','.docx','.xlsx','.pptx','.mp4','.mov','.mp3','.wav','.m4a','.html','.csv','.zip','.txt','.md'}

def safe_ui(d,ids,pids):
    out={k:d[k] for k in GLOBAL_KEYS if k in d}
    out['local-projects']={k:v for k,v in out.get('local-projects',{}).items() if k in pids}
    for key in ['thread-project-assignments','thread-project-membership-host-ids','thread-projectless-output-directories','thread-workspace-root-hints']:
        if key in out:out[key]={k:v for k,v in out[key].items() if k in ids}
    out['projectless-thread-ids']=[x for x in out.get('projectless-thread-ids',[]) if x in ids]
    out['project-order']=[x for x in out.get('project-order',[]) if x in pids]
    atom=d.get('electron-persisted-atom-state',{});a={}
    for k,v in atom.items():
        if k in ATOM_KEYS or (any(k.startswith(p) for p in ATOM_PREFIXES) and any(t in k for t in ids)):
            a[k]=v
    for k in ['prompt-history','thread-descriptions-v1','composer-prompt-drafts-v2','thread-workspace-draft-tabs-v1']:
        if isinstance(a.get(k),dict):a[k]={t:v for t,v in a[k].items() if t in ids}
    for owner,val in a.get('sidebar-custom-sections-v3',{}).items():
        for sec in val.get('sections',[]):
            sec['itemKeys']=[x for x in sec.get('itemKeys',[]) if x.startswith('codex:') and any(i in x for i in ids|pids)]
            sec['hostSectionIds']={k:v for k,v in sec.get('hostSectionIds',{}).items() if k=='local'}
        val['appServerMigratedHostIds']=[x for x in val.get('appServerMigratedHostIds',[]) if x=='local']
    if 'electron-thread-read-state-v1' in out:
        val=out['electron-thread-read-state-v1']
        val['unreadByIdentity']={k:v for k,v in val.get('unreadByIdentity',{}).items() if any(i in k for i in ids)}
    out['electron-persisted-atom-state']=a
    return out

class Exporter:
    def __init__(self,args):
        self.home=args.home.resolve();self.root=(args.codex_home or self.home/'.codex').resolve();self.bundle=args.bundle.resolve()
        
        if self.bundle.exists() and any(self.bundle.iterdir()):raise ValueError('导出目录必须为空，避免混入旧快照。')
        if under(self.bundle,self.root) or under(self.bundle,self.home/'.agents'):raise ValueError('导出目录不能位于正在导出的数据树内。')
        self.bundle.mkdir(parents=True,exist_ok=True);self.files={};self.links=[];self.excluded=[];self.issues=[];self.referenced_assets=set()
        self.m={'format':FORMAT,'bundle_id':str(uuid.uuid4()),'started_at':stamp(),'source_home':str(self.home),'source_codex_home':str(self.root),
                'source_platform':'macOS','cutoff_policy':'逐文件稳定快照；包含完整记录边界；数据库为只读在线备份；导出完成后新增消息不在此次快照内。',
                'workspace_policy':'所有工作区由用户另行打包，本包不包含其文件','credential_policy':'凭据容器及配置凭据字段排除，聊天原文不删改',
                'resource_roots':{},'external_assets':[],'app_paths':['/Applications/ChatGPT.app'],'databases':{},'threads':[],'projects':[]}
    def add_bytes(self,data,scope,rel,source,mode=0o600,transform=None):
        dest=self.bundle/'payload'/scope/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data);os.chmod(dest,mode)
        name=str(dest.relative_to(self.bundle));self.files[name]={'path':name,'scope':scope,'relative':str(rel),'source':str(source),'bytes':len(data),'sha256':digest(dest),'mode':mode,'transform':transform}
        return dest
    def add_file(self,p,scope,rel):
        p=Path(p)
        if p.is_symlink():
            resolved=p.resolve()
            self.links.append({'scope':scope,'relative':str(rel),'source':str(p),'link_text':os.readlink(p),'resolved_target':str(resolved),'source_exists':resolved.exists()})
            if not resolved.exists():self.issues.append({'kind':'broken_symlink','path':str(p),'target':str(resolved)})
            return
        for attempt in range(4):
            before=p.stat();data=p.read_bytes();after=p.stat()
            if (before.st_mtime_ns,before.st_size)==(after.st_mtime_ns,after.st_size) and len(data)==after.st_size:break
            if attempt==3:raise RuntimeError('源文件持续变化，停止封包: '+str(p))
        # Only dedicated credential containers/configs are filtered; source code, docs and chat content stay intact.
        self.add_bytes(data,scope,rel,p,stat.S_IMODE(after.st_mode))
    def tree(self,p,scope,rel):
        p=Path(p)
        if not p.exists() and not p.is_symlink():return
        if p.is_symlink():self.add_file(p,scope,rel);return
        for directory,dirs,files in os.walk(p,followlinks=False):
            base=Path(directory)
            for n in list(dirs):
                q=base/n
                if n in EXCLUDED_NAMES:
                    dirs.remove(n);self.excluded.append({'path':str(q),'reason':'凭据或可重建的依赖/版本控制目录'});continue
                if q.is_symlink():self.add_file(q,scope,Path(rel)/q.relative_to(p));dirs.remove(n)
            for n in files:
                q=base/n
                if n in EXCLUDED_NAMES or n.endswith(('.lock','-wal','-shm')) or n.startswith('.env.') and not n.endswith(('.example','.sample')):
                    self.excluded.append({'path':str(q),'reason':'凭据、系统元数据或运行时锁文件'});continue
                if not q.is_file() and not q.is_symlink():self.excluded.append({'path':str(q),'reason':'特殊设备/套接字'});continue
                self.add_file(q,scope,Path(rel)/q.relative_to(p))
    def dbs(self):
        for name in DBS:
            p=self.root/name
            if not p.is_file():continue
            dest=self.bundle/'payload/codex'/name;backup_db(p,dest);c=sqlite3.connect(str(dest))
            try:
                ts=tables(c)
                if name==STATE:
                    for t in ['remote_control_enrollments','external_agent_config_imports']:
                        if t in ts:c.execute('DELETE FROM '+qi(t))
                elif name==APPDB:
                    for t in ts:
                        cols=[x[1] for x in c.execute('PRAGMA table_info('+qi(t)+')')]
                        if 'host_id' in cols:c.execute('DELETE FROM '+qi(t)+" WHERE host_id <> 'local'")
                        if t in ['live_visualization_suggestions']:c.execute('DELETE FROM '+qi(t))
                elif name=='memories_1.sqlite':
                    if 'jobs' in ts:c.execute('DELETE FROM jobs')
                c.commit();c.execute('VACUUM')
                if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('源备份数据库损坏: '+name)
                self.m['databases'][name]=schema(c)
            finally:c.close()
            for suffix in ('-shm','-wal'):
                side=Path(str(dest)+suffix)
                if side.exists():
                    if suffix=='-wal' and side.stat().st_size:raise RuntimeError('导出数据库关闭后仍有 WAL 数据: '+name)
                    side.unlink()
            self.files[str(dest.relative_to(self.bundle))]={'path':str(dest.relative_to(self.bundle)),'scope':'codex','relative':name,'source':str(p),'bytes':dest.stat().st_size,'sha256':digest(dest),'mode':0o600,'transform':'SQLite online backup; local-only filtering; VACUUM'}
    def capture_rollout(self,p):
        p=Path(p)
        for attempt in range(5):
            before=p.stat();data=p.read_bytes();after=p.stat()
            if (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns):break
            if attempt==4:raise RuntimeError('会话持续变化，未能取得稳定快照: '+str(p))
        end=data.rfind(b'\n')+1
        if end!=len(data):
            self.issues.append({'kind':'incomplete_tail','path':str(p),'bytes':len(data)-end,'instruction':'存在未完成的一行，不能标记完整；请停止写入后重导。'})
        data=data[:end];counts=collections.Counter();role_counts=collections.Counter();metadata=None;record_count=0
        for line in data.splitlines():
            obj=json.loads(line);record_count+=1;counts[obj.get('type','unknown')]+=1
            payload=obj.get('payload',{})
            if obj.get('type')=='session_meta':metadata=payload
            if isinstance(payload,dict):role_counts[payload.get('role',payload.get('type','other'))]+=1
            self.inspect_assets(obj)
        if not metadata or not metadata.get('id'):raise RuntimeError('会话缺少 session_meta: '+str(p))
        rel=p.relative_to(self.root) if under(p,self.root) else Path('sessions/imported-external')/p.name
        dest=self.add_bytes(data,'codex',rel,p,0o600)
        return {'id':metadata['id'],'original_path':str(p),'payload_path':str(dest.relative_to(self.bundle)),'cwd':metadata.get('cwd'),
                'archived':str(rel).startswith('archived_sessions/'),'records':record_count,'record_types':dict(counts),'payload_roles_or_types':dict(role_counts),
                'bytes':len(data),'sha256':digest(dest),'captured_at':stamp(),'source_mtime_ns':after.st_mtime_ns}
    def inspect_assets(self,obj):
        # Explicit image/file references, not every historical source-code path.
        if isinstance(obj,dict):
            if obj.get('type') in ('session_meta','turn_context') or obj.get('role') in ('system','developer'):return
            if obj.get('name','').endswith(('view_image','imagegen')):
                args=obj.get('arguments',{})
                if isinstance(args,str):
                    try:args=json.loads(args)
                    except ValueError:args={}
                if isinstance(args,dict):
                    for k in ['path','referenced_image_paths']:
                        v=args.get(k,[])
                        for s in v if isinstance(v,list) else [v]:
                            if isinstance(s,str) and s.startswith('/'):self.referenced_assets.add(s)
            for k,v in obj.items():
                if k in ('image_url','file_path','image_path','local_path') and isinstance(v,str) and v.startswith('/'):
                    self.referenced_assets.add(v)
                elif k=='image_url' and isinstance(v,dict) and str(v.get('url','')).startswith('file://'):
                    from urllib.parse import unquote,urlsplit
                    self.referenced_assets.add(unquote(urlsplit(v['url']).path))
                self.inspect_assets(v)
        elif isinstance(obj,list):
            for v in obj:self.inspect_assets(v)
        elif isinstance(obj,str):
            for s in re.findall(r'!\[[^\]]*\]\((/[^\n)]+)\)',obj):self.referenced_assets.add(s)
    def run(self):
        global_original=read_json(self.root/'.codex-global-state.json',{})
        legacy=global_original.get('app-server-project-id-by-legacy-project-id-by-host',{}).get('local:'+str(self.root),{})
        self.m['legacy_project_id_map']=legacy
        global_raw=transform_ui(global_original,{},legacy)
        self.dbs();state_copy=self.bundle/'payload/codex'/STATE;sc=sqlite3.connect(str(state_copy))
        projects=rows(sc,'projects');pids={p['id'] for p in projects};edges=rows(sc,'thread_spawn_edges')
        original_database_membership={r['id']:r.get('project_id') for r in rows(sc,'threads')}
        self.add_bytes((json.dumps({'database_project_ids':original_database_membership,'desktop_assignments':{k:v for k,v in global_original.get('thread-project-assignments',{}).items() if k in original_database_membership},'legacy_project_id_map':legacy},ensure_ascii=False,indent=2)+'\n').encode(),'portable_state','local-project-membership.original.json',self.root,transform='Original local membership metadata preserved before normalizing export copy')
        assignments={k:v for k,v in global_raw.get('thread-project-assignments',{}).items() if v in pids}
        for r in rows(sc,'threads'):
            if r.get('project_id') in pids:assignments[r['id']]=r['project_id']
        changed=True
        while changed:
            changed=False
            for edge in edges:
                if edge['parent_thread_id'] in assignments and edge['child_thread_id'] not in assignments:
                    assignments[edge['child_thread_id']]=assignments[edge['parent_thread_id']];changed=True
        # Desktop legacy project assignments can be authoritative while the app-server
        # thread table still has all project_id values NULL. Normalize only the export.
        for tid,pid in assignments.items():sc.execute('UPDATE threads SET project_id=? WHERE id=? AND project_id IS NULL',(pid,tid))
        sc.commit();db_threads=rows(sc,'threads');pr=rows(sc,'project_roots');sc.close()
        item=self.files['payload/codex/'+STATE];item['sha256']=digest(state_copy);item['bytes']=state_copy.stat().st_size;item['transform']+='; legacy desktop project assignments normalized in export copy'
        paths={p.resolve() for p in (set((self.root/'sessions').rglob('*.jsonl'))|set((self.root/'archived_sessions').rglob('*.jsonl'))|{Path(t['rollout_path']) for t in db_threads})}
        for p in sorted(paths):
            if not p.is_file():self.issues.append({'kind':'missing_rollout','path':str(p)});continue
            self.m['threads'].append(self.capture_rollout(p))
        tids={t['id'] for t in self.m['threads']};dtids={t['id'] for t in db_threads}
        if len(tids)!=len(self.m['threads']):raise RuntimeError('原始记录中出现重复会话 ID，需要保留两版后人工处理')
        for tid in tids-dtids:self.issues.append({'kind':'unindexed_rollout','thread_id':tid,'instruction':'原始记录已保留，但缺少数据库登记，需目标 GPT 根据 session_meta 补建，工具不会静默丢弃。'})
        for tid in dtids-tids:self.issues.append({'kind':'missing_rollout_for_db_thread','thread_id':tid})
        for p in projects:
            roots=[{'path':x['path'],'git_identity':git_identity(x['path'])} for x in pr if x['project_id']==p['id']]
            p['roots']=roots;p['legacy_ids']=[k for k,v in legacy.items() if v==p['id']];p['thread_ids']=[t['id'] for t in db_threads if t.get('project_id')==p['id'] or global_raw.get('thread-project-assignments',{}).get(t['id'])==p['id']]
            self.m['projects'].append(p)
        self.m['projectless_directories']=sorted({t['cwd'] for t in db_threads if not any(under(t['cwd'],r['path']) for p in self.m['projects'] for r in p['roots'])})
        self.workspace_roots=sorted({r['path'] for p in self.m['projects'] for r in p['roots']}|set(self.m['projectless_directories'])|{str(self.home/'Documents/Codex')})
        filtered=safe_ui(global_raw,tids,{p['id'] for p in projects})
        self.add_bytes((json.dumps(filtered,ensure_ascii=False,indent=2)+'\n').encode(),'codex','.codex-global-state.json',self.root/'.codex-global-state.json',transform='Only local UI state; cloud/auth/capability entries omitted')
        previous=self.root/'.codex-global-state.json.bak'
        if previous.is_file():
            old=safe_ui(transform_ui(read_json(previous,{}),{},legacy),tids,{p['id'] for p in projects})
            self.add_bytes((json.dumps(old,ensure_ascii=False,indent=2)+'\n').encode(),'portable_state','desktop-state.previous.json',previous,transform='Previous local UI state retained for reference; not activated automatically')
        browser_state=self.home/'Library/Application Support/Codex/browser-sidebar-page-states.json'
        if browser_state.is_file():
            state=read_json(browser_state,{});local_pages={k:v for k,v in state.get('pages',{}).items() if v.get('conversationId') in tids}
            # Keep local chat tab descriptors as a portable reference, not live browser credentials/session databases.
            def clean_urls(v):
                if isinstance(v,dict):return {k:clean_urls(x) for k,x in v.items() if not SECRET.search(k)}
                if isinstance(v,list):return [clean_urls(x) for x in v]
                if isinstance(v,str) and v.startswith(('https://','http://')):
                    from urllib.parse import urlsplit,urlunsplit
                    u=urlsplit(v);return urlunsplit((u.scheme,u.hostname or '',u.path,'',''))
                return v
            if local_pages:self.add_bytes((json.dumps({'version':state.get('version'),'pages':clean_urls(local_pages)},ensure_ascii=False,indent=2)+'\n').encode(),'portable_state','browser-local-pages.json',browser_state,transform='Local conversation tab descriptors only; URL credentials/query removed; not restored into live browser profile')
        config,removed=clean_config((self.root/'config.toml').read_text() if (self.root/'config.toml').is_file() else '')
        self.add_bytes(config.encode(),'codex','config.toml',self.root/'config.toml',transform='Credential fields removed: '+','.join(removed))
        for name in ['AGENTS.md','agents.md','history.jsonl','instructions.md']:
            if (self.root/name).is_file():self.add_file(self.root/name,'codex',name)
        original_index=[]
        if (self.root/'session_index.jsonl').exists():
            for line in (self.root/'session_index.jsonl').read_text().splitlines():
                if not line.strip():continue
                x=json.loads(line)
                if x.get('id') in tids:original_index.append(x)
            self.add_bytes((''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in original_index)).encode(),'codex','session_index.jsonl',self.root/'session_index.jsonl',transform='Local thread IDs only; original index retained, importer rebuilds union')
        self.m['crosscheck']={'database_threads':len(db_threads),'rollout_threads':len(tids),'original_index_entries':len(original_index),'index_missing_ids':sorted(tids-{x['id'] for x in original_index}),'spawn_edges':edges}
        for name in TREES:
            if name=='automations':self.tree(self.root/name,'portable_state','automations')
            else:self.tree(self.root/name,'codex',name)
        self.tree(self.home/'.agents','agents','.')
        runtime=self.home/'.cache/codex-runtimes/codex-primary-runtime/plugins/openai-primary-runtime'
        if runtime.is_dir():
            self.m['resource_roots'][str(runtime)]='runtime_plugins';self.tree(runtime,'runtime_plugins','.')
        # Resolve dependencies crossing chosen roots, retaining link metadata instead of following loops.
        known=lambda p:under(p,self.root) or under(p,self.home/'.agents') or under(p,runtime)
        for link in list(self.links):
            p=Path(link['resolved_target'])
            if p.exists() and under(p,self.root):
                rel=p.relative_to(self.root);payload='payload/codex/'+str(rel)
                selected=payload in self.files or p.is_dir() and any(n.startswith(payload+'/') for n in self.files)
                if not selected and not any(part in EXCLUDED_NAMES for part in rel.parts):
                    if p.is_dir():self.tree(p,'codex',rel)
                    else:self.add_file(p,'codex',rel)
            elif p.exists() and not known(p):
                self.referenced_assets.add(str(p))
        refs=[]
        for s in sorted(self.referenced_assets):
            p=Path(s);status=''
            if s.startswith(('/absolute/','/path/to/','/actual/')):status='documentation_placeholder'
            elif any(under(p,r) for r in self.workspace_roots):status='workspace_excluded'
            elif under(p,self.root):
                relative=p.relative_to(self.root)
                payload='payload/codex/'+str(relative)
                if p.is_file() and payload not in self.files and not any(x in p.parts for x in EXCLUDED_NAMES):self.add_file(p,'codex',relative)
                status='included' if payload in self.files or p.is_dir() and any(n.startswith(payload+'/') for n in self.files) else 'missing'
            elif p.exists() and not any(under(p,r) for r in ['/Applications','/System','/Library',self.home/'.cache']):
                rel=Path(__import__('hashlib').sha256(str(p).encode()).hexdigest()[:16])/p.name
                if p.is_dir():self.tree(p,'external_assets',rel)
                else:self.add_file(p,'external_assets',rel)
                self.m['external_assets'].append({'source':s,'relative':str(rel)});status='included_external'
            elif not p.exists():status='missing'
            else:status='runtime_component_not_bundled'
            refs.append({'path':s,'status':status})
            if status=='missing':self.issues.append({'kind':'missing_referenced_asset','path':s,'instruction':'历史消息引用的本地文件在源电脑上已不存在；保留引用和完整聊天，不能凭空恢复原文件。'})
        self.m['asset_references']=refs
        app_info=Path('/Applications/ChatGPT.app/Contents/Info.plist')
        if app_info.is_file():
            info=plistlib.loads(app_info.read_bytes());self.m['source_app']={k:info.get(k) for k in ['CFBundleIdentifier','CFBundleShortVersionString','CFBundleVersion']}
        for t in self.m['threads']:
            # Database projections may lag a live rollout, but must never point past its captured complete prefix.
            if not (self.bundle/'payload/codex'/HISTORY).is_file():continue
            hc=ro_db(self.bundle/'payload/codex'/HISTORY)
            r=hc.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?',(t['id'],)).fetchone() if 'thread_history_projection_state' in tables(hc) else None;hc.close()
            if r and r[0]>t['bytes']:self.issues.append({'kind':'history_beyond_rollout','thread_id':t['id'],'offset':r[0],'bytes':t['bytes']})
        after=ro_db(self.root/STATE);after_ids={r[0] for r in after.execute('SELECT id FROM threads')};after.close()
        disk_after={p.resolve() for p in set((self.root/'sessions').rglob('*.jsonl'))|set((self.root/'archived_sessions').rglob('*.jsonl'))}
        if after_ids!=dtids or not disk_after.issubset(paths):raise RuntimeError('捕获过程中会话集合发生变化，请重新导出')
        self.m['excluded']=self.excluded+[
            {'path':str(self.root/'auth.json'),'reason':'登录凭据'},
            {'path':str(self.home/'Library/Application Support/Codex/Default'),'reason':'浏览器登录状态、线上缓存及可重建 Chromium 数据，不原样复制'},
            {'path':str(self.root/'logs_2.sqlite'),'reason':'运行日志可能含凭据；会话工具输出已完整保存在原始记录和历史库'},
            {'path':str(self.root/'shell_snapshots'),'reason':'终端环境快照可能含密钥，目标重新生成'},
            {'path':str(self.root/'plugins/.plugin-appserver'),'reason':'可重建的机器专用可执行组件，目标重新安装'},
            {'path':str(self.root/'computer-use'),'reason':'机器专用应用和运行组件，目标重新安装'},
        ]+[{'path':p,'reason':'用户已单独打包的工作区'} for p in self.workspace_roots]
        # Every top-level source entry is accounted for, including ephemeral runtime data.
        selected={Path(x['relative']).parts[0] for x in self.files.values() if x['scope']=='codex'}|{Path(x['relative']).parts[0] for x in self.links if x['scope']=='codex'}|{'sessions','archived_sessions'}
        self.m['source_root_inventory']=[{'name':p.name,'kind':'directory' if p.is_dir() else 'file','policy':'selected_or_partially_selected_see_file_manifest' if p.name in selected else 'excluded_or_portable_reference','reason':None if p.name in selected else ('previous local UI stored under portable_state' if p.name=='.codex-global-state.json.bak' else 'credential/browser state, machine identity, runtime cache, logs, locks or recreatable executable data')} for p in sorted(self.root.iterdir())]
        self.m['issues']=self.issues;self.m['files']=sorted(self.files.values(),key=lambda x:x['path']);self.m['symlinks']=self.links
        self.m['finished_at']=stamp();self.m['status']='verified' if not self.issues else 'verified_with_source_exceptions'
        self.m['statistics']={'threads':len(tids),'archived':sum(t['archived'] for t in self.m['threads']),'records':sum(t['records'] for t in self.m['threads']),
                              'files':len(self.files),'symlinks':len(self.links),'payload_bytes':sum(x['bytes'] for x in self.files.values()),'projects':len(projects),'source_exceptions':len(self.issues),
                              'project_assigned_threads':sum(t.get('project_id') in pids for t in db_threads),'subagent_threads':sum(t.get('thread_source')=='subagent' for t in db_threads),
                              'skill_definition_files':sum(x['path'].endswith('/SKILL.md') for x in self.files.values())}
        write_json(self.bundle/'manifest.json',self.m)
        write_json(self.bundle/'验证报告.json',{'status':self.m['status'],'finished_at':self.m['finished_at'],'statistics':self.m['statistics'],'crosscheck':self.m['crosscheck'],'source_exceptions':self.issues})
        self.guide();return self.m

    def guide(self):
        m=self.m
        lines=['# Codex 本地迁移说明','','本文件描述迁移数据及操作方式，不替代新电脑使用者的当前授权。','','## 包内数据','',
               '- 截止时间：'+m['finished_at']+'。此后新增内容不在此次快照中。',
               '- 源数据根：`'+m['source_codex_home']+'`。',
               '- 本地会话 '+str(m['statistics']['threads'])+' 份；归档 '+str(m['statistics']['archived'])+' 份；记录 '+str(m['statistics']['records'])+' 条。',
               '- 包含原始聊天、历史投影、项目关系、技能、agent、本地附件和已选中的依赖资源。',
               '- 不迁移登录凭据、浏览器登录状态或云端聊天；工作区默认由用户单独恢复。原始聊天正文不删改。',
               '- 调度文件仅作为原始参考保存，不自动启用自动化或运行导入的插件。',
               '- 数据库通过 SQLite 在线备份捕获；会话按完整记录边界捕获，各文件的捕获时间和字节边界见 manifest.json。','','## 项目路径','',
               '| 项目 | 源项目 ID | 原工作区 | Git 身份 |','|---|---|---|---|']
        for p in m['projects']:
            lines.append('| '+p['name'].replace('|','\\|')+' | `'+p['id']+'` | '+'<br>'.join('`'+r['path']+'`' for r in p['roots'])+' | '+'<br>'.join(r.get('git_identity') or '未取得可跨电脑核验的远程身份' for r in p['roots'])+' |')
        lines+=['','## 独立聊天工作区','']+['- `'+p+'`' for p in m['projectless_directories']]
        lines+=['','## 在新 Mac 导入','','先安装并启动一次 Codex 桌面应用；本工具适用于其本地 .codex 数据，不迁移普通 ChatGPT 云端聊天。',
                '将压缩包与外置 .sha256 文件交给新电脑 GPT；已安装 skill 时调用 $codex-local-migration 导入。',
                '也可以在校验压缩包后解压，打开“导入迁移.command”。它会预检并询问必要的工作区对应关系，最后退出应用、备份、合并和核验。',
                '冲突默认保留本机现有版本。若需要逐项选择，用同目录的 migration.py prepare 命令与 decisions.json；同 ID 正文分歧需要明确选择 keep_both。',
                '项目只在真实路径或可跨电脑的 Git 身份唯一时自动匹配；名称相同只作为候选。目录缺失时必须提供路径，或明确接受创建空目录并记录原文件未恢复。',
                '退出应用后不要手动提前重开；程序成功并写入正式完成报告后会重开。等待阶段会显示占用者。',
                '如果目标数据库结构不兼容或出现无法决定的聊天分歧，停止写入并交给 GPT 审阅报告，不使用源整库替换目标库。',
                '重复导入依靠包 ID、会话 ID 和导入记录去重；回滚前也需要完全退出应用。',
                '', '## 状态与验收','',
                '状态文件区分 prepared、waiting_for_app_exit、merging、completed、failed。只有 completed 且目标验证通过才表示正式迁移完成。',
                '完成报告核对源会话全文、原有目标会话前缀、项目归属、归档、父子 agent 关系、数据库完整性和已导入链接。',
                '重新打开应用后的侧边栏显示是独立的使用验证；不要仅根据侧边栏数量判断是否完整，也不要在显示异常时重复导入。',
                '源原始档案与回滚记录应保留。源文件原先已缺失、工作区另存和机器专用组件未恢复的内容会明确列出。',
                '', '## 源端异常','', '源端状态：'+m['status']+'；异常数：'+str(len(self.issues))+'。']
        lines += ['- '+json.dumps(x,ensure_ascii=False) for x in self.issues[:30]]
        (self.bundle/'迁移说明.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        write_json(self.bundle/'decisions.example.json',{'path_map':{},'project_map':{},'conflicts':{},'file_overrides':{},'desktop_owner_map':{}})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,default=Path.home());p.add_argument('--codex-home',type=Path);p.add_argument('--bundle',type=Path,required=True)
    args=p.parse_args()
    try:
        m=Exporter(args).run();print(json.dumps({'ok':True,'status':m['status'],'statistics':m['statistics'],'cutoff':m['finished_at']},ensure_ascii=False))
    except Exception as e:
        print('导出停止，不能将此目录视为已完成迁移包: '+str(e),file=sys.stderr);sys.exit(2)
