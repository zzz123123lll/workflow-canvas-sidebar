"""Project work and delivery are explicit; history is retained without becoming a current blocker."""

LABELS = {'preparing': '准备中', 'running': '执行中', 'needs_attention': '需要处理',
          'awaiting_review': '待审阅', 'deliverable': '可交付', 'revising': '修订中', 'archived': '已归档'}


def overview(store, project_id, results_for):
    project = store.get(project_id)
    runs = store.runs(project_id)
    business = [r for r in runs if r.get('run_kind') != 'workflow_proposal']
    current = next((r for r in business if r['run_id'] == project.get('work_run_id')), None)
    selection = 'explicit'
    if current is None:
        current = business[0] if business else None
        selection = 'legacy-display-default' if current else 'none'
    delivery = next((r for r in business if r['run_id'] == project.get('delivery_run_id')), None)
    dispositions = store.dispositions(project_id)
    pending, history = [], []
    result_map = {}
    for run in runs:
        rid = run['run_id']
        result = results_for(run)
        result_map[rid] = result
        actions = []
        for node in run.get('nodes', []):
            if node.get('status') in {'awaiting_input', 'awaiting_approval', 'failed', 'blocked', 'stale'}:
                actions.append({'run_id': rid, 'node_id': node['id'], 'name': node.get('name') or node['id'],
                                'status': node['status'], 'question': node.get('question') or node.get('detail')})
        for nid in result.get('pending_reviews', []):
            if not any(a['node_id'] == nid and a['status'] == 'awaiting_approval' for a in actions):
                actions.append({'run_id': rid, 'node_id': nid, 'name': next((n.get('name') or nid for n in run['nodes'] if n['id'] == nid), nid),
                                'status': 'awaiting_approval', 'question': '请阅读当前成果并确认内容审阅'})
        for change in result.get('external_changes', []):
            actions.append({'run_id': rid, 'node_id': change['node_id'], 'name': change['node_id'],
                            'status': 'stale', 'question': '发现工作区新修改，请同步并登记版本'})
        for action in actions:
            if current and rid == current['run_id'] and rid not in dispositions:
                pending.append(action)
            else:
                action['disposition'] = 'superseded' if rid in dispositions else 'unresolved-history'
                action['replacement'] = dispositions.get(rid)
                history.append(action)
    current_result = result_map.get(current['run_id'], {}) if current else {}
    if project['archived']:
        status = 'archived'
    elif current and current.get('status') in {'running', 'pausing', 'stopping'}:
        status = 'revising' if delivery and delivery['run_id'] != current['run_id'] else 'running'
    elif pending:
        status = 'awaiting_review' if all(a['status'] == 'awaiting_approval' for a in pending) else 'needs_attention'
    elif current_result.get('deliverable'):
        status = 'deliverable'
    elif current and current.get('status') in {'failed', 'cancelled', 'awaiting_input', 'awaiting_agent', 'paused'}:
        status = 'needs_attention'
    else:
        status = 'preparing'
    return {'ok': True, 'project': project, 'runs': runs, 'current_run': current,
            'current_selection': selection, 'delivery_run': delivery,
            'delivery_valid': bool(delivery and result_map.get(delivery['run_id'], {}).get('deliverable')),
            'project_status': status, 'project_status_label': LABELS[status],
            'pending_actions': pending, 'historical_pending_actions': history,
            'unresolved_history_count': sum(a['disposition'] == 'unresolved-history' for a in history),
            'dispositions': dispositions}
