"""Account-backed workflow proposals, explicitly reviewed before project edits."""
import copy
import hashlib
import json
from pathlib import Path


def suggest(store, wf, body):
    modules = [m for m in wf.load_modules() if not m.get('archived')]
    agent = next((m for m in modules if m.get('executor') == 'agent'), None)
    if not agent:
        raise ValueError('模块库没有可用 AI 模块，请先创建通用 AI 模块')
    catalog = [{k: m.get(k) for k in ('id', 'name', 'description', 'executor', 'requires', 'artifacts', 'skills')}
               for m in modules]
    output = 'episodes/{stem}-workflow-proposal.json'
    prompt = ('根据项目目标与交付要求建议一个可编辑的流程。只设计，不执行制作，不修改项目配置。'
              '只选择下列真实模块ID，不得发明能力。每一步使用不同node id；输入要有明确上游产物或项目资料，'
              '可按需求覆盖agent模块职责与产物，但不得将程序模块当作任意AI。'
              '缺少能力时放入capability_gaps，不冒充已具备。只用本项目资料，不联网。'
              '实际写入expected_artifacts的JSON：{explanation:中文说明, capability_gaps:字符串数组, '
              'graph:{nodes:[{id,module,name,x,y,requires,artifacts,agent_prompt,input_bindings,deliveries}],edges:[{from,to}]}}。'
              'input_bindings必须是{输入路径:上游node_id}对象；deliveries必须是{产物路径:{role:primary或attachment或internal,required:true,review_required:true或false}}对象。'
              '不要使用artifact_declarations或content_review等其他字段。模块默认职责不适合本节点时须显式写agent_prompt。'
              '至少一个步骤，路径使用episodes/{stem}-有意义后缀。真实模块目录：\n'
              + json.dumps(catalog, ensure_ascii=False))
    graph = {'nodes': [{'id': 'workflow_proposal', 'module': agent['id'], 'name': '起草流程建议',
             'x': 100, 'y': 100, 'agent_prompt': prompt, 'requires': [], 'artifacts': [output],
             'skills': [], 'knowledge_aliases': {}, 'timeout_seconds': 600, 'checklist': ['已按真实模块目录提出流程', '没有执行建议流程'],
             'validation': {'type': 'json', 'path': output, 'schema': {'type': 'object',
                'required': ['explanation', 'capability_gaps', 'graph'], 'properties': {
                    'explanation': {'type': 'string'}, 'capability_gaps': {'type': 'array', 'items': {'type': 'string'}},
                    'graph': {'type': 'object', 'required': ['nodes', 'edges']}}}}}], 'edges': []}
    return store.admit_run(dict(body, episode='流程建议'),
                           graph_override=graph, purpose='workflow_proposal')


def proposal(base, store, wf, project_id, run_id):
    if store.owner(run_id) != project_id:
        raise ValueError('流程建议不属于当前项目')
    state = wf.read_json(Path(wf.run_dir(run_id)) / 'state.json', {})
    if state.get('run_kind') != 'workflow_proposal':
        raise ValueError('这次运行不是流程建议')
    node = wf.node_state(state, 'workflow_proposal')
    if not node or node.get('status') != 'passed':
        raise ValueError('流程建议尚未完成，请先处理该模块的待办')
    evidence = (node.get('artifact_evidence') or [None])[0]
    if not evidence:
        raise ValueError('建议缺少文件证据')
    path = Path(wf.contained_path(base, evidence['path']))
    raw = path.read_bytes()
    if len(raw) > 256000 or hashlib.sha256(raw).hexdigest() != evidence['sha256']:
        raise ValueError('建议文件已变化或过大，请创建修订后再采用')
    value = json.loads(raw)
    graph = copy.deepcopy(value['graph'])
    modules = {m['id']: m for m in wf.load_modules() if not m.get('archived')}
    gaps = list(value.get('capability_gaps') or [])
    errors, normalizations = [], []
    registry = wf.read_json(Path(base) / 'kb/skill-registry.json', {'skills': []})
    skills = {item['id']: item for item in registry.get('skills', [])}
    if not isinstance(graph, dict) or not isinstance(graph.get('nodes'), list) or not isinstance(graph.get('edges'), list):
        raise ValueError('建议流程结构无效')
    if len(graph['nodes']) > 40:
        raise ValueError('建议步骤过多，请拆分后再采用')
    for n in graph['nodes']:
        if not isinstance(n, dict) or not isinstance(n.get('id'), str) or not isinstance(n.get('module'), str):
            raise ValueError('每个建议步骤必须有真实模块和独立节点编号')
        for field in ('requires', 'artifacts', 'skills', 'required_knowledge', 'checklist'):
            if field in n and (not isinstance(n[field], list) or any(not isinstance(text, str) for text in n[field])):
                raise ValueError('步骤的' + field + '须为字符串数组')
        allowed = {'id', 'module', 'name', 'x', 'y', 'requires', 'artifacts', 'agent_prompt', 'input_bindings',
                   'input_schemas', 'deliveries', 'required_knowledge', 'skills', 'skill_focus', 'checklist',
                   'timeout_seconds', 'condition', 'validation', 'knowledge_aliases', 'artifact_declarations', 'content_review'}
        if set(n) - allowed:
            errors.append('步骤含不支持的字段：' + n['id'] + ' / ' + '、'.join(sorted(set(n) - allowed)))
        if 'deliveries' in n and not isinstance(n['deliveries'], dict):
            errors.append('交付用途必须为路径到用途的映射：' + n['id'])
            n['deliveries'] = {}
    ids = {n['id'] for n in graph['nodes']}
    if any(not isinstance(e, dict) or e.get('from') not in ids or e.get('to') not in ids for e in graph['edges']):
        raise ValueError('建议连线包含不存在的节点')
    for n in graph['nodes']:
        m = modules.get(n.get('module'))
        if not m:
            errors.append('模块不存在或已归档：' + str(n.get('module')))
            continue
        bound = n.get('input_bindings', {})
        if isinstance(bound, list):
            converted = {}
            for row in bound:
                if not isinstance(row, dict) or not isinstance(row.get('artifact'), str) or not isinstance(row.get('source'), str) or row['artifact'] in converted:
                    errors.append('输入绑定列表无法转换：' + str(n['id']))
                    break
                converted[row['artifact']] = row['source']
            else:
                n['input_bindings'] = bound = converted
                normalizations.append(n['id'] + '：输入绑定列表已转换为路径到上游节点的映射')
        if not isinstance(bound, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in bound.items()):
            errors.append('输入绑定必须为路径到上游节点的映射：' + str(n['id']))
        if 'artifact_declarations' in n:
            declared = n.pop('artifact_declarations')
            if not isinstance(declared, list) or any(not isinstance(d, dict) or not isinstance(d.get('path'), str) for d in declared):
                errors.append('交付声明无效：' + str(n['id']))
            else:
                n['deliveries'] = {d['path']: {k: d[k] for k in ('role', 'required', 'review_required') if k in d} for d in declared}
                normalizations.append(n['id'] + '：产物声明已转换为交付用途配置')
        if 'content_review' in n:
            review = n.pop('content_review')
            if not isinstance(review, dict) or review.get('artifact') not in (n.get('artifacts') or m.get('artifacts') or []):
                errors.append('内容审阅没有对应的产物：' + str(n['id']))
            elif review.get('required'):
                n.setdefault('deliveries', {}).setdefault(review['artifact'], {'role': 'primary', 'required': True})['review_required'] = True
                normalizations.append(n['id'] + '：内容审阅要求已绑定到指定产物；完成后仍须确认审阅')
        deliveries = n.get('deliveries', {})
        if not isinstance(deliveries, dict):
            errors.append('交付用途必须为路径到用途的映射：' + str(n['id']))
        else:
            for output, policy in deliveries.items():
                if output not in (n.get('artifacts') or m.get('artifacts') or []) or not isinstance(policy, dict) or policy.get('role') not in {'primary', 'attachment', 'internal'}:
                    errors.append('交付声明不对应真实产物：' + str(output))
        for identity in n.get('skills', m.get('skills') or []):
            skill = skills.get(identity)
            if not skill or not (Path(base) / skill.get('root', '') / skill.get('entry', 'SKILL.md')).is_file():
                gaps.append('步骤「' + str(n.get('name') or n['id']) + '」缺少技能：' + str(identity))
        if m['id'] in {'video_voice', 'video_capture', 'video_visuals'}:
            gaps.append('步骤「' + str(n.get('name') or n['id']) + '」需要单独配置并验证外部素材、配音或采集能力，请先替换为已可用模块')
        if m['id'] == 'video_render' or (m.get('validation') or {}).get('type') == 'video_render':
            from wf_video_tools import doctor
            health = doctor()
            if not health['available']:
                gaps.extend('渲染能力：' + text for text in health['problems'])
        for p in (n.get('artifacts') if isinstance(n.get('artifacts'), list) else m.get('artifacts') or []):
            try:
                wf.artifact_relative(p, 'proposal-check', run_id, project_id)
            except ValueError as error:
                errors.append(str(error))
    try:
        wf._validate_canvas(graph)
    except ValueError as error:
        errors.append(str(error))
    by_id = {n['id']: n for n in graph['nodes']}
    for n in graph['nodes']:
        ancestors, todo = set(), [e['from'] for e in graph['edges'] if e['to'] == n['id']]
        while todo:
            parent = todo.pop()
            if parent in ancestors:
                continue
            ancestors.add(parent)
            todo.extend(e['from'] for e in graph['edges'] if e['to'] == parent)
        bound = n.get('input_bindings', {})
        for path, producer in (bound.items() if isinstance(bound, dict) else []):
            parent = by_id.get(producer) or {}
            outputs = parent.get('artifacts', modules.get(parent.get('module'), {}).get('artifacts', []))
            if producer not in ancestors or path not in n.get('requires', []) or path not in outputs:
                errors.append('输入绑定未指向正确上游产物：' + n['id'] + ' / ' + path)
    return {'ok': True, 'project_id': project_id, 'run_id': run_id, 'generation': int(node.get('execution_generation', 1)),
            'sha256': evidence['sha256'], 'graph': graph, 'explanation': value.get('explanation', ''),
            'capability_gaps': gaps, 'errors': errors, 'normalizations': normalizations, 'can_apply': not errors and not gaps,
            'notice': '采用只更新项目画布；仍须检查输入、工具和交付要求，再点击运行。'}


def apply(base, store, wf, body):
    with store.connect() as db:
        old = db.execute('SELECT operation,result FROM commands WHERE request_id=?', (body.get('request_id'),)).fetchone()
    if old:
        result = json.loads(old['result'])
        saved = result.get('project') or {}
        source = (saved.get('graph') or {}).get('proposal_source') or {}
        if old['operation'] != 'update' or saved.get('project_id') != body.get('project_id') or saved.get('revision') != body.get('expected_revision', 0) + 1 or source.get('run_id') != body.get('run_id') or source.get('sha256') != body.get('sha256'):
            raise ValueError('命令标识已用于其他流程采用操作')
        return result
    current = proposal(base, store, wf, body['project_id'], body['run_id'])
    if not current['can_apply'] or body.get('sha256') != current['sha256']:
        raise ValueError('建议有缺失能力、无效绑定或已变化，请修订后再采用')
    graph = dict(current['graph'], proposal_source={k: current[k] for k in ('run_id', 'generation', 'sha256', 'normalizations')})
    return store.mutate('update', {'project_id': body['project_id'], 'expected_revision': body['expected_revision'],
                       'graph': graph, 'request_id': body['request_id']})
