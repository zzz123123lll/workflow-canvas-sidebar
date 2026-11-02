"""Adopt native-chat file changes as a checked, immutable new generation."""
import copy
import hashlib
import json
from pathlib import Path


def workspace_root(wf, state, node):
    generation = int(node.get('execution_generation', 1))
    run = Path(wf.run_dir(state['run_id']))
    candidates = [run / 'nodes' / wf.safe_component(node['id']) / ('generation-' + str(generation)) / 'workspace',
                  run / 'n' / hashlib.sha256(node['id'].encode()).hexdigest()[:8] / ('g' + str(generation)) / 'workspace']
    actual = Path(node.get('workspace') or '').resolve()
    if actual not in [p.resolve() for p in candidates]:
        raise ValueError('工作区不属于当前模块和版本')
    return actual


def adopt(wf, run_id, node_id, expected_generation, expected_hashes, request_id):
    if not request_id or not isinstance(expected_hashes, dict) or not expected_hashes:
        raise ValueError('必须指定命令和预览时的成果哈希')
    if not wf.acquire_lock(run_id):
        raise ValueError('执行器尚未停止，请稍后登记修改')
    try:
        state = wf.read_json(Path(wf.run_dir(run_id)) / 'state.json', {})
        previous = state.get('adoption_requests', {}).get(request_id)
        fingerprint = hashlib.sha256(json.dumps([node_id, expected_generation, expected_hashes], sort_keys=True).encode()).hexdigest()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise ValueError('命令 ID 已用于其他修改')
            return previous
        node = wf.node_state(state, node_id)
        if not node or int(node.get('execution_generation', 1)) != int(expected_generation):
            raise ValueError('模块版本已变化，请重新同步')
        if state.get('status') in {'running', 'stopping'} or state.get('cleanup_pending'):
            raise ValueError('运行仍在执行，不能登记修改')
        source_workspace = workspace_root(wf, state, node)
        graph = wf._canvas_graph_for(run_id)
        source_node = next(n for n in graph['nodes'] if n['id'] == node_id)
        module = wf._run_module_snapshot(state).get(source_node.get('module'), {})
        definition = dict(module, **source_node)
        artifacts = [wf.artifact_relative(p, state['episode'], run_id, state.get('task_id'))
                     for p in definition.get('artifacts', [])]
        if not artifacts or set(artifacts) != set(expected_hashes):
            raise ValueError('预览的文件集合与声明不一致')
        observed = []
        for logical in artifacts:
            current = wf._file_evidence(wf.contained_path(source_workspace, logical))
            if not current or current['sha256'] != expected_hashes[logical]:
                raise ValueError('原生文件在预览后又发生变化，请重新同步：' + logical)
            observed.append(dict(current, path=logical, produced_by=node_id, run_id=run_id))
        old = copy.deepcopy({k: v for k, v in node.items() if k != 'generation_history'})
        old['artifact_evidence'] = wf._freeze_evidence(state, node)
        old['generation'] = int(expected_generation)
        old['archived_at'] = wf.now()
        generation = int(expected_generation) + 1
        new_workspace = Path(wf.run_dir(run_id)) / 'nodes' / node_id / ('generation-' + str(generation)) / 'workspace'
        new_workspace.mkdir(parents=True, exist_ok=True)
        for item in observed:
            wf._copy_scoped_file(wf.contained_path(source_workspace, item['path']), wf.contained_path(new_workspace, item['path']))
        # Check the copied bytes, then apply the same independent rules as an AI turn.
        for item in observed:
            copied = wf._file_evidence(wf.contained_path(new_workspace, item['path']))
            if not copied or copied['sha256'] != item['sha256']:
                raise ValueError('复制期间文件又发生变化，请重试登记')
        upstream = wf._upstream_inputs(definition, state, str(new_workspace))
        from wf_validation import validate_node
        validation = dict(definition.get('validation') or {})
        for key in ('media', 'spec', 'report', 'bundle', 'source_media', 'path'):
            if validation.get(key):
                validation[key] = wf.artifact_relative(validation[key], state['episode'], run_id, state.get('task_id'))
        tools = wf.read_json(Path(wf.run_dir(run_id)) / 'execution-tools.json', {})
        validation_evidence = validate_node(dict(definition, validation=validation), {'upstream_inputs': upstream}, new_workspace, tools)
        node.setdefault('generation_history', []).append(old)
        node.update(execution_generation=generation, workspace=str(new_workspace), status='passed',
                    artifact_evidence=observed, validation_evidence=validation_evidence,
                    upstream_inputs=upstream, review_status='pending', checks=[False] * len(node.get('checks') or []),
                    detail='原生修改已通过文件与规则检查，等待内容审阅', finished_at=wf.now(),
                    agent_report={'summary': '从原生会话登记文件修改；尚未完成内容审阅'},
                    continuation_of=node.get('agent_session_id'), agent_has_session=False)
        node.pop('agent_session_id', None)
        node.pop('stale_due_to', None)
        # Freeze directly from the checked staging bytes. The prior frozen version
        # is never overwritten, and working aliases are promoted only afterwards.
        prefix = 'artifacts/' + run_id + '/nodes/' + node_id + '/generation-' + str(generation) + '/'
        frozen = []
        for item in observed:
            logical = item['path']
            destination = prefix + logical
            existing = wf._file_evidence(wf.contained_path(wf.BASE, destination))
            if existing and existing['sha256'] != item['sha256']:
                raise ValueError('目标历史版本已存在，拒绝覆盖')
            wf._copy_scoped_file(wf.contained_path(new_workspace, logical), wf.contained_path(wf.BASE, destination))
            wf._copy_scoped_file(wf.contained_path(new_workspace, logical), wf.contained_path(wf.BASE, logical))
            frozen.append(dict(item, path=destination, logical_path=logical, generation=generation))
        node['artifact_evidence'] = frozen
        adjacent = {n['id']: [] for n in graph['nodes']}
        for edge in graph.get('edges', []):
            adjacent[edge['from']].append(edge['to'])
        affected, stack = set(), list(adjacent[node_id])
        while stack:
            current = stack.pop()
            if current not in affected:
                affected.add(current)
                stack.extend(adjacent[current])
        for downstream in state['nodes']:
            if downstream['id'] in affected:
                downstream.update(status='stale', stale_due_to={'node_id': node_id, 'generation': generation},
                                  detail='原生修改产生了新版本，请更新受影响步骤')
        result = {'node_id': node_id, 'generation': generation, 'affected': sorted(affected), 'fingerprint': fingerprint}
        state.setdefault('adoption_requests', {})[request_id] = result
        state['status'] = 'creation_ready' if affected else 'completed'
        wf._canvas_save(state)
        wf.event(run_id, 'native_changes_adopted', node=node_id, generation=generation, affected=sorted(affected), request_id=request_id)
        return result
    finally:
        wf.release_lock()


def review(wf, run_id, node_id, generation, hashes, request_id):
    if not wf.acquire_lock(run_id):
        raise ValueError('请等待执行结束后再审阅')
    try:
        state = wf.read_json(Path(wf.run_dir(run_id)) / 'state.json', {})
        node = wf.node_state(state, node_id)
        previous = (node or {}).get('reviews', {}).get(request_id)
        if previous:
            if previous['generation'] != generation or previous['hashes'] != hashes:
                raise ValueError('审阅命令标识已用于其他版本')
            return {'generation': generation, 'review_status': 'approved'}
        if not node or int(node.get('execution_generation', 1)) != int(generation):
            raise ValueError('审阅版本已经变化，请重新打开成果')
        evidence = node.get('artifact_evidence') or []
        expected = {e.get('logical_path') or e['path']: e['sha256'] for e in evidence}
        if not evidence or hashes != expected:
            raise ValueError('审阅文件与当前版本不一致')
        for item in evidence:
            current = wf._file_evidence(wf.contained_path(wf.BASE, item['path']))
            if not current or current['sha256'] != item['sha256']:
                raise ValueError('文件已变化，不能沿用审阅结论')
        node['review_status'] = 'approved'
        node['checks'] = [True] * len(node.get('checks') or [])
        node.setdefault('reviews', {})[request_id] = {'generation': generation, 'hashes': hashes, 'reviewer': 'user', 'at': wf.now(), 'decision': 'approved'}
        wf._canvas_save(state)
        wf.event(run_id, 'content_review', node=node_id, generation=generation, decision='approved', reviewer='user', request_id=request_id)
        return {'generation': generation, 'review_status': 'approved'}
    finally:
        wf.release_lock()
