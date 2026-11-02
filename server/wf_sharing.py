"""Portable project definitions and opt-in project materials. Execution is always explicit."""
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat

MODULE_FIELDS = {'id','name','description','category','executor','kind','agent_prompt','requires','artifacts',
                 'checklist','skills','skill_focus','capabilities','required_knowledge','input_bindings',
                 'input_schemas','deliveries','review_required','timeout_sec','timeout_seconds','retry','knowledge_aliases'}
MAX_BYTES = 20 * 1024 * 1024
NODE_FIELDS = MODULE_FIELDS | {'module','x','y'}


def relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('项目包资料路径不合法')
    path = PurePosixPath(value)
    if path.as_posix() != value or path.is_absolute() or not path.parts or any(p in {'', '.', '..'} or p.endswith((' ', '.')) for p in path.parts):
        raise ValueError('项目包资料不能越过项目目录')
    if any(re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I) for p in path.parts):
        raise ValueError('资料路径包含系统保留名称')
    if path.name.startswith('.') or path.suffix.lower() not in {'.md','.txt','.json','.csv','.tsv','.pdf','.png','.jpg','.jpeg','.webp'}:
        raise ValueError('不支持此资料类型')
    return path


def checked(root, name):
    for component in [root.absolute(), *root.absolute().parents]:
        if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
            raise ValueError('项目资料目录不能经过链接')
    root = root.resolve(); dest = root.joinpath(*relative(name).parts)
    current = dest
    while True:
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('项目包不读取或写入链接')
        if current == root:break
        current = current.parent
    if not dest.resolve().is_relative_to(root):raise ValueError('资料路径越界')
    return dest


def reject_secret(value):
    text = json.dumps(value, ensure_ascii=False)
    if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9]{32,}', text):
        raise ValueError('项目定义疑似包含访问凭据，请移除后再分享')


def export_project(base, store, wf, project_id, materials=False):
    project = store.get(project_id)
    def portable(definition):
        value = dict(definition)
        prefix = 'tasks/' + project_id + '/files/'
        for field in ['required_knowledge', 'requires']:
            if isinstance(value.get(field), list):
                value[field] = ['files/' + p[len(prefix):] if isinstance(p, str) and p.startswith(prefix) else p for p in value[field]]
        return value
    selected = {n['module'] for n in project['graph']['nodes']}
    modules = [portable({k:v for k,v in m.items() if k in MODULE_FIELDS}) for m in wf.load_modules() if m['id'] in selected]
    if selected != {m['id'] for m in modules}:raise ValueError('项目引用的模块定义缺失，请先修复流程')
    portable_project = {k:project[k] for k in ['name','goal','delivery']}
    portable_project['graph'] = {'nodes':[portable({k:v for k,v in n.items() if k in NODE_FIELDS}) for n in project['graph']['nodes']],
                         'edges':[{k:e[k] for k in ['from','to']} for e in project['graph']['edges']]}
    package = {'schema':'canvas.project/v1','project':portable_project,
               'modules':modules,'materials':[],
               'notice':'仅包含AI项目定义和主动选择的项目资料；不含账号、会话、成果历史、共享资料或机器工具/程序校验配置。导入不会运行；本机程序校验需重新配置。'}
    reject_secret(package)
    if materials:
        root = Path(base)/'kb/knowledge/tasks'/project_id/'files'
        if root.exists():
            total = 0
            for file in sorted(root.rglob('*')):
                if file.is_dir():continue
                name = file.relative_to(root).as_posix(); source = checked(root, name)
                raw = source.read_bytes();total += len(raw)
                if total > MAX_BYTES:raise ValueError('项目资料超过20MB，请分开分享原始文件')
                if source.suffix.lower() in {'.txt','.md','.json','.csv','.tsv'}:
                    reject_secret(raw.decode('utf-8', errors='replace'))
                package['materials'].append({'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'base64':base64.b64encode(raw).decode('ascii')})
    return package


def import_project(base, store, wf, body):
    package = body.get('package'); key = str(body.get('request_id') or '')
    if not key or len(key)>160 or not isinstance(package,dict) or package.get('schema')!='canvas.project/v1':
        raise ValueError('请选择有效的项目包')
    reject_secret(package)
    project = package.get('project') or {}; graph = json.loads(json.dumps(project.get('graph') or {}))
    incoming = package.get('modules') or []; materials = package.get('materials') or []
    if not isinstance(incoming,list) or len(incoming)>100 or not isinstance(materials,list) or len(materials)>200:
        raise ValueError('项目包过大')
    if not isinstance(graph.get('nodes'),list) or not isinstance(graph.get('edges'),list) or len(graph['nodes'])>200:
        raise ValueError('项目流程无效或过大')
    nodes = graph['nodes']; node_ids = [wf.safe_component(n.get('id'), 'node_id') for n in nodes]
    if len(node_ids)!=len({v.casefold() for v in node_ids}):raise ValueError('项目包含重复节点')
    try:wf.topo_order(graph)
    except SystemExit as error:raise ValueError('项目依赖图不合法') from error
    prefix = 'shared-' + hashlib.sha256(key.encode()).hexdigest()[:12] + '-'
    definitions, mapping = [], {}
    for module in incoming:
        if not isinstance(module,dict) or module.get('executor')!='agent':
            raise ValueError('项目分享目前只导入AI模块定义；程序执行器请按扩展文档安装')
        ident = wf.safe_component(module.get('id'), 'module_id')
        if ident in mapping:raise ValueError('项目包模块ID重复')
        target_id = prefix + ident
        mapping[ident] = target_id
        definition = {k:v for k,v in module.items() if k in MODULE_FIELDS}
        definition.update(id=target_id, knowledge_aliases={})
        definitions.append(definition)
    for node in nodes:
        if node.get('module') not in mapping:raise ValueError('节点引用了包外模块')
        node['module'] = mapping[node['module']]
        node['knowledge_aliases'] = {}
        for name in ['requires','artifacts']:
            for value in node.get(name,[]):wf.artifact_relative(value,'portable','run-portable','project-portable')
    decoded, total, seen = [], 0, set()
    for item in materials:
        name = relative(item.get('path')).as_posix()
        if name.casefold() in seen:raise ValueError('项目资料路径重复')
        seen.add(name.casefold())
        raw = base64.b64decode(item.get('base64',''),validate=True);total+=len(raw)
        if total>MAX_BYTES or hashlib.sha256(raw).hexdigest()!=item.get('sha256'):raise ValueError('资料大小或哈希不正确')
        decoded.append((name,raw))
    # Stable IDs make repeated imports idempotent. Existing definitions are never overwritten.
    existing = wf.read_json(wf.MODULES_PATH, {'version':3,'modules':[]})
    by_id = {m['id']:m for m in existing['modules']}
    for definition in definitions:
        old = by_id.get(definition['id'])
        if old is not None and old != definition:raise ValueError('同一导入标识已有不同模块定义')
    definition_map = {d['id']:d for d in definitions}
    for node in nodes:
        module = definition_map[node['module']]
        for field in ['artifacts','requires']:
            for value in node.get(field,module.get(field,[])):
                if not isinstance(value,str):raise ValueError('输入输出声明必须是路径列表')
                if not value.startswith(('knowledge/','shared/','files/','inputs/')):
                    wf.artifact_relative(value,'portable','run-portable','project-portable')
    fingerprint = hashlib.sha256(json.dumps(package,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    result = store.mutate('create', {'request_id':key,'name':project.get('name'), 'goal':project.get('goal'),
        'delivery':project.get('delivery',''),'graph':graph,'import_sha256':fingerprint})
    pid = result['project']['project_id']
    for definition in definitions:
        if definition['id'] not in by_id:existing['modules'].append(definition)
    wf.write_json(wf.MODULES_PATH,existing)
    root = Path(base)/'kb/knowledge/tasks'/pid/'files'
    root.mkdir(parents=True,exist_ok=True)
    for name,raw in decoded:
        target = checked(root,name);target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and target.read_bytes()!=raw:raise ValueError('导入重试未覆盖后续修改的项目资料')
        target.write_bytes(raw)
    return dict(result, imported_materials=len(decoded), notice='已创建独立项目，尚未执行。请检查职责、资料与能力后运行。')
