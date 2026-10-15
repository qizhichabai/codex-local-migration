#!/usr/bin/env python3
"""Isolated behavioral tests. Never reads or modifies the user's Codex databases."""
import argparse,contextlib,io,json,os,sqlite3,tempfile,unittest,zipfile
from pathlib import Path
import migration_tool as mt
from export_local import Exporter
import migration as driver
import processes

SCHEMA='''
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',position INTEGER NOT NULL,created_at_ms INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL);
CREATE TABLE project_roots(project_id TEXT NOT NULL REFERENCES projects(id),position INTEGER NOT NULL,path TEXT NOT NULL,PRIMARY KEY(project_id,position));
CREATE TABLE thread_sections(id TEXT PRIMARY KEY);
CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,source TEXT NOT NULL,model_provider TEXT NOT NULL,cwd TEXT NOT NULL,title TEXT NOT NULL,archived INTEGER NOT NULL DEFAULT 0,archived_at INTEGER,is_pinned INTEGER DEFAULT 0,thread_section_id TEXT,project_id TEXT REFERENCES projects(id),name TEXT,thread_source TEXT);
CREATE TABLE thread_spawn_edges(parent_thread_id TEXT NOT NULL,child_thread_id TEXT PRIMARY KEY,status TEXT NOT NULL);
CREATE TABLE thread_dynamic_tools(thread_id TEXT,tool TEXT,PRIMARY KEY(thread_id,tool));
CREATE TABLE thread_attachments(id TEXT PRIMARY KEY,thread_id TEXT,payload TEXT);
'''
HISTORY='''CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_byte_offset INTEGER,rollout_end_byte_offset INTEGER,PRIMARY KEY(thread_id,turn_id));
CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,item_json TEXT,PRIMARY KEY(thread_id,turn_id,item_id));
CREATE TABLE thread_history_projection_state(thread_id TEXT PRIMARY KEY,next_rollout_byte_offset INTEGER);
CREATE TABLE thread_realtime_items(thread_id TEXT,item_id TEXT,PRIMARY KEY(thread_id,item_id));'''
CATALOG='''CREATE TABLE local_thread_catalog_hosts(host_id TEXT PRIMARY KEY,host_kind TEXT);
CREATE TABLE local_thread_catalog(host_id TEXT,thread_id TEXT,display_title TEXT,cwd TEXT,project_id TEXT,PRIMARY KEY(host_id,thread_id));'''

def init_home(home):
 codex=home/'.codex';codex.mkdir(parents=True)
 for name,sql in [(mt.STATE,SCHEMA),(mt.HISTORY,HISTORY),(mt.APPDB,CATALOG),('memories_1.sqlite','CREATE TABLE stage1_outputs(thread_id TEXT PRIMARY KEY,raw_memory TEXT); CREATE TABLE jobs(ownership_token TEXT);'),('goals_1.sqlite','CREATE TABLE thread_goals(thread_id TEXT PRIMARY KEY,goal TEXT);')]:
  p=codex/name;p.parent.mkdir(parents=True,exist_ok=True);c=sqlite3.connect(str(p));c.executescript(sql);c.commit();c.close()
 (codex/'config.toml').write_text('model = "target-model"\nAPI_KEY = "test-secret-not-real"\n');(codex/'.codex-global-state.json').write_text('{}\n')
 return codex

def add_thread(home,tid,project=None,archive=False,parent=None):
 codex=home/'.codex';cwd=home/'Documents'/'项目 A';cwd.mkdir(parents=True,exist_ok=True)
 p=codex/('archived_sessions' if archive else 'sessions')/(tid+'.jsonl');p.parent.mkdir(parents=True,exist_ok=True)
 meta={'id':tid,'session_id':tid,'cwd':str(cwd),'source':{'parent_thread_id':parent} if parent else 'cli'}
 data=[{'type':'session_meta','payload':meta},{'type':'turn_context','payload':{'cwd':str(cwd)}},{'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'literal '+str(home)+'/do-not-replace'}]}}]
 p.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in data))
 c=sqlite3.connect(str(codex/mt.STATE));c.execute('INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,archived,archived_at,project_id,thread_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(tid,str(p),1,2,json.dumps({'parent_thread_id':parent}) if parent else 'cli','openai',str(cwd),tid,int(archive),3 if archive else None,project,'subagent' if parent else 'cli'))
 if parent:c.execute('INSERT INTO thread_spawn_edges VALUES(?,?,?)',(parent,tid,'completed'))
 c.commit();c.close();c=sqlite3.connect(str(codex/mt.HISTORY));c.execute('INSERT INTO thread_history_projection_state VALUES(?,?)',(tid,p.stat().st_size));c.execute('INSERT INTO thread_turns VALUES(?,?,?,?)',(tid,'turn',0,p.stat().st_size));c.execute('INSERT INTO thread_items VALUES(?,?,?,?)',(tid,'turn','msg',json.dumps(data[-1])));c.commit();c.close()
 return p

class MigrationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='codex-migration-test-');self.base=Path(self.temp.name).resolve();self.source=self.base/'source';self.target=self.base/'target';self.sc=init_home(self.source);self.tc=init_home(self.target)
  c=sqlite3.connect(str(self.sc/mt.STATE));c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?)',('project-source','示例项目','{}',0,1,1));cwd=self.source/'Documents'/'项目 A';c.execute('INSERT INTO project_roots VALUES(?,?,?)',('project-source',0,str(cwd)));c.commit();c.close()
  add_thread(self.source,'source-parent','project-source');add_thread(self.source,'source-child','project-source',True,'source-parent');add_thread(self.target,'existing-target')
  (self.sc/'auth.json').write_text('{"access_token":"fake-do-not-copy"}');(self.sc/'skills/new-skill').mkdir(parents=True);(self.sc/'skills/new-skill/SKILL.md').write_text('---\nname: new-skill\ndescription: example\n---\n')
  (self.sc/'skills/new-skill/extra.py').write_text('source extra')
  (self.sc/'skills/link').symlink_to('new-skill/SKILL.md');(self.sc/'automations/task').mkdir(parents=True);(self.sc/'automations/task/automation.toml').write_text('status = "ACTIVE"\n')
  self.bundle=self.base/'bundle';self.m=Exporter(argparse.Namespace(home=self.source,codex_home=None,bundle=self.bundle)).run();self.workspace=self.target/'Documents'/'项目 A';self.workspace.mkdir(parents=True,exist_ok=True)
  self.dec=self.base/'decisions.json';mt.write_json(self.dec,{'path_map':{str(cwd):str(self.workspace)},'project_map':{'project-source':'new'},'conflicts':{'file:codex/config.toml':'keep_target'}})
  self.a=argparse.Namespace(bundle=self.bundle,home=self.target,codex_home=None,decisions=self.dec,report=self.base/'preflight.json',backup_dir=self.base/'backup')
 def tearDown(self):self.temp.cleanup()
 def prepare(self):
  with contextlib.redirect_stdout(io.StringIO()):ctx=mt.prepare(self.a)
  self.addCleanup(ctx['temp'].cleanup);return ctx
 def merge(self,ctx):
  with contextlib.redirect_stdout(io.StringIO()):mt.merge(ctx,self.a)
 def test_full_merge_preserves_target_and_raw_text(self):
  old=(self.tc/'config.toml').read_bytes();ctx=self.prepare();self.assertTrue(ctx['report']['ready'],ctx['report']);self.merge(ctx)
  result=driver.verification(self.bundle,self.tc);self.assertTrue(result['ok']);self.assertEqual(result['source_threads'],2);self.assertEqual(result['previous_target_threads'],1);self.assertEqual(result['total_threads'],3)
  self.assertEqual((self.tc/'config.toml').read_bytes(),old);self.assertFalse((self.tc/'auth.json').exists());self.assertFalse((self.tc/'automations').exists());self.assertTrue((self.tc/'skills/link').exists())
  ledger=mt.read_json(self.tc/'migration-imports'/(self.m['bundle_id']+'.json'));p=Path(ledger['rollout_paths']['source-parent']);text=p.read_text();self.assertIn(str(self.source)+'/do-not-replace',text);self.assertIn(str(self.workspace),text)
  self.assertNotIn('test-secret-not-real',(self.bundle/'payload/codex/config.toml').read_text())
 def test_repeat_no_duplicates_and_two_stage_rollback(self):
  before={name:mt.digest(self.tc/name) for name in [mt.STATE,mt.HISTORY,mt.APPDB]};self.merge(self.prepare());first=self.a.backup_dir;self.a.backup_dir=self.base/'backup2';ctx=self.prepare();self.assertTrue(ctx['report']['ready'],ctx['report']);self.merge(ctx)
  c=mt.ro_db(self.tc/mt.STATE);self.assertEqual(c.execute('SELECT count(*) FROM threads').fetchone()[0],3);c.close()
  mt.rollback(self.a.backup_dir/'rollback.json');mt.rollback(first/'rollback.json')
  self.assertEqual(before,{name:mt.digest(self.tc/name) for name in before})
 def test_divergent_conversation_needs_choice_then_clones_graph(self):
  self.merge(self.prepare());ledger=mt.read_json(self.tc/'migration-imports'/(self.m['bundle_id']+'.json'));p=Path(ledger['rollout_paths']['source-parent']);p.write_text(p.read_text().replace('literal ','different '))
  ctx=self.prepare();self.assertIn('divergent_thread',[x['kind'] for x in ctx['report']['pending']]);d=mt.read_json(self.dec);d['conflicts']['thread:source-parent']='keep_both';mt.write_json(self.dec,d);self.a.backup_dir=self.base/'branch-backup';ctx=self.prepare();self.assertTrue(ctx['report']['ready'],ctx['report']);self.merge(ctx)
  self.assertEqual(len(ctx['ids']),2);clone=Path(ctx['rollout_dest']['source-parent']);meta=json.loads(clone.read_text().splitlines()[0])['payload'];self.assertEqual(meta['id'],meta['session_id']);self.assertNotEqual(meta['id'],'source-parent');self.assertIn('different ',p.read_text())
 def test_incompatible_schema_blocks(self):
  c=sqlite3.connect(str(self.tc/mt.STATE));c.execute('ALTER TABLE threads RENAME COLUMN model_provider TO changed_provider');c.commit();c.close();ctx=self.prepare();self.assertFalse(ctx['report']['ready']);self.assertTrue(any('model_provider' in x for x in ctx['report']['errors']))
 def test_archive_roundtrip_and_path_escape(self):
  archive=self.base/'bundle.zip';mt.write_zip(self.bundle,archive);restored=driver.safe_unpack(archive,self.base/'extracted');mt.validate_bundle(restored)
  with zipfile.ZipFile(self.base/'bad.zip','w') as z:z.writestr('../outside','bad')
  with self.assertRaises(ValueError):driver.safe_unpack(self.base/'bad.zip',self.base/'reject')
  self.assertFalse((self.base/'outside').exists())
 def test_modified_target_refuses_rollback(self):
  self.merge(self.prepare());(self.tc/'skills/new-skill/SKILL.md').write_text('new user edit')
  with self.assertRaises(ValueError):mt.rollback(self.a.backup_dir/'rollback.json')
  self.assertEqual((self.tc/'skills/new-skill/SKILL.md').read_text(),'new user edit')
 def test_source_incomplete_tail_is_explicit_exception(self):
  p=self.sc/'sessions/source-parent.jsonl'
  with p.open('a') as f:f.write('{')
  other=self.base/'incomplete';m=Exporter(argparse.Namespace(home=self.source,codex_home=None,bundle=other)).run();self.assertIn('incomplete_tail',[x['kind'] for x in m['issues']]);self.assertNotEqual(m['status'],'verified')
 def test_symlink_dependency_outside_selected_tree_is_included(self):
  dependency=self.sc/'shared-resources/text.txt';dependency.parent.mkdir();dependency.write_text('dependency data')
  (self.sc/'skills/resource-link').symlink_to(dependency)
  m=Exporter(argparse.Namespace(home=self.source,codex_home=None,bundle=self.base/'dependency-bundle')).run();self.assertIn('payload/codex/shared-resources/text.txt',{x['path'] for x in m['files']})
 def test_physical_path_alias_is_not_duplicate_thread(self):
  alias=self.base/'source-alias';alias.symlink_to(self.source,target_is_directory=True)
  c=sqlite3.connect(str(self.sc/mt.STATE))
  for tid,path in c.execute('SELECT id,rollout_path FROM threads').fetchall():c.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(alias/Path(path).relative_to(self.source)),tid))
  c.commit();c.close()
  m=Exporter(argparse.Namespace(home=self.source,codex_home=None,bundle=self.base/'aliased-bundle')).run();self.assertEqual(m['statistics']['threads'],2)
 def test_guard_only_captured_orphan(self):
  app={'uid':os.getuid(),'pid':10,'ppid':1,'command':'/Applications/ChatGPT.app/Contents/MacOS/ChatGPT'};helper={'uid':os.getuid(),'pid':11,'ppid':10,'command':'/Applications/ChatGPT.app/Contents/Resources/codex app-server --analytics'};other={'uid':os.getuid(),'pid':12,'ppid':1,'command':'/usr/local/bin/codex app-server'}
  captured=processes.capture_helpers({10:app,11:helper,12:other});self.assertEqual(list(captured),[11]);orphan=dict(helper,ppid=1)
  self.assertEqual(processes.eligible_orphans(captured,{10:app,11:orphan}),[]);self.assertEqual(processes.eligible_orphans(captured,{11:orphan,12:other}),[11]);self.assertEqual(processes.eligible_orphans(captured,{11:dict(orphan,command='other')}),[])
 def test_same_name_skills_preserve_entire_target_version(self):
  (self.tc/'skills/new-skill').mkdir(parents=True);(self.tc/'skills/new-skill/SKILL.md').write_text('target version')
  d=mt.read_json(self.dec);d['conflicts']['skill:codex:new-skill']='keep_target';mt.write_json(self.dec,d);ctx=self.prepare();self.assertTrue(ctx['report']['ready'],ctx['report']);self.merge(ctx);self.assertEqual((self.tc/'skills/new-skill/SKILL.md').read_text(),'target version');self.assertFalse((self.tc/'skills/new-skill/extra.py').exists())

if __name__=='__main__':unittest.main(verbosity=2)
