#!/usr/bin/env python3
"""Install the adjacent, checksummed migration skill without replacing an existing version."""
from pathlib import Path
import hashlib,os,shutil,stat,tempfile,zipfile
BASE=Path(__file__).resolve().parent
archive=BASE/'codex-local-migration.skill';checksum=BASE/'codex-local-migration.skill.sha256'
expected=checksum.read_text().split()[0]
if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise SystemExit('skill 安装包校验失败，未安装。')
root=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))).expanduser().resolve()/'skills';root.mkdir(parents=True,exist_ok=True);dest=root/'codex-local-migration'
with tempfile.TemporaryDirectory(prefix='.install-migration-',dir=root) as temporary:
 stage=Path(temporary)
 with zipfile.ZipFile(archive) as z:
  seen=set()
  for info in z.infolist():
   p=Path(info.filename)
   if p.is_absolute() or '..' in p.parts or '\\' in info.filename or p.parts[0]!='codex-local-migration' or info.filename in seen or stat.S_ISLNK(info.external_attr>>16):raise SystemExit('安装包内容不符合预期，未安装。')
   seen.add(info.filename)
  z.extractall(stage)
 source=stage/'codex-local-migration'
 if dest.exists():
  a={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
  b={str(p.relative_to(dest)):hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
  if a==b:print('相同版本已安装：'+str(dest))
  else:raise SystemExit('本机已有不同版本，未覆盖：'+str(dest)+'。请交给 GPT 审阅升级。')
 else:shutil.move(str(source),str(dest));print('已安装：'+str(dest))
print('在新聊天中调用 $codex-local-migration；新电脑导入迁移包不要求预先安装此 skill。')
