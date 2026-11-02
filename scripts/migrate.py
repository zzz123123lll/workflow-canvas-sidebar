"""Copy a stopped data home into an empty destination; never move unrelated directories."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid


def migrate(source, destination):
    source = source.absolute(); destination = destination.absolute()
    for path in [source, destination]:
        for part in [path, *path.parents]:
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('迁移路径不能经过链接')
    source = source.resolve(); destination = destination.resolve()
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('源和目标必须独立，不互相包含')
    if not (source/'kb/modules.json').is_file():raise ValueError('源不是插件数据目录')
    if destination == Path(destination.anchor) or destination == Path.home().resolve() or '.dsh' in destination.parts:
        raise ValueError('目标不能是系统、用户或宿主数据目录')
    if destination.exists() and any(destination.iterdir()):raise ValueError('请选择空目标目录；迁移不移动或覆盖已有目录')
    if (source/'.runs/.lock').exists():raise ValueError('源有执行锁，请先结束执行并关闭宿主后迁移')
    copies=[]
    excluded={'.runs/host-agent.json','.runs/api-instance.json','.runs/.lock','.runs/.watch.lock',
              '.runs/projects.sqlite3','.runs/projects.sqlite3-wal','.runs/projects.sqlite3-shm','kb/execution-tools.json'}
    for directory in ['kb','episodes','artifacts','.runs','skills']:
        root=source/directory
        if not root.exists():continue
        for path in root.rglob('*'):
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise ValueError('源资料中存在链接，请先处理再迁移')
            if path.is_file():
                rel=path.relative_to(source).as_posix()
                if rel not in excluded:copies.append((path,rel))
    database=source/'.runs/projects.sqlite3'
    if database.exists():
        with sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True) as db:
            if db.execute("SELECT name FROM sqlite_master WHERE name='execution_commands'").fetchone():
                if db.execute("SELECT 1 FROM execution_commands WHERE status IN ('queued','running','uncertain') LIMIT 1").fetchone():
                    raise ValueError('源还有未对账命令，请先恢复/核对再迁移')
    destination.mkdir(parents=True,exist_ok=True)
    for file,rel in copies:
        target=destination/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
    if database.exists():
        target=destination/'.runs/projects.sqlite3';target.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:src.backup(dst)
    marker={'product':'dsh-workflow-canvas-sidebar','schema':1,'data_home':str(destination),
            'instance_id':uuid.uuid4().hex,'exclusive':True}
    (destination/'.workflow-canvas-home.json').write_text(json.dumps(marker,ensure_ascii=False),encoding='utf-8')
    return {'copied_files':len(copies),'sqlite_consistent_backup':database.exists(),
            'source_preserved':True,'machine_tools_and_bridge_not_copied':True,'destination':str(destination)}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(migrate(args.source,args.destination),ensure_ascii=False))
