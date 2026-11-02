"""Add missing built-in starters during upgrade; never replace user definitions."""
import argparse
import json
from pathlib import Path
import os


def read(path, default):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:return default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.canvas-tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def install(home, seed):
    modules = read(home/'kb/modules.json', {'version':3,'modules':[]})
    existing = {m['id'] for m in modules['modules']}
    added = [m for m in read(seed/'kb/modules.json', {})['modules'] if m['id'] not in existing]
    if added:
        modules['modules'].extend(added); write(home/'kb/modules.json', modules)
    template_index = read(seed/'kb/workflows/index.json', {})
    index = read(home/'kb/workflows/index.json', {'active':template_index['active'],'workflows':[]})
    known = {t['id'] for t in index['workflows']}
    templates = []
    for template in template_index['workflows']:
        if template['id'] in known:continue
        source = seed/'kb/workflows'/(template['id']+'.json')
        target = home/'kb/workflows'/source.name
        if target.exists():continue
        write(target, read(source, {})); index['workflows'].append(template); templates.append(template['id'])
    if templates:write(home/'kb/workflows/index.json', index)
    return {'added_modules':[m['id'] for m in added],'added_templates':templates,'existing_definitions_preserved':True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser();parser.add_argument('--home',type=Path,required=True);parser.add_argument('--seed',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(install(args.home,args.seed),ensure_ascii=False))
