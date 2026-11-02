"""Continue a module conversation without declaring a new artifact revision."""
import hashlib
import json
from pathlib import Path

from wf_conversation import append_message, stage_context
from wf_external import workspace_root


def discuss(wf, run_id, node_id, answer, expected_generation, request_id, _lock_owned=False):
    if not request_id or not str(answer).strip() or len(answer) > 20000:
        raise ValueError('请指定命令及讨论内容（最多20000字）')
    if not _lock_owned and not wf.acquire_lock(run_id):
        raise ValueError('另一个执行器仍在工作，请等待结束后讨论')
    try:
        state_path = Path(wf.run_dir(run_id)) / 'state.json'
        state = wf.read_json(state_path, {})
        node = wf.node_state(state, node_id)
        fingerprint = hashlib.sha256(json.dumps([node_id, expected_generation, answer], ensure_ascii=False).encode()).hexdigest()
        commands = state.setdefault('discussion_requests', {})
        previous = commands.get(request_id)
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise ValueError('命令标识已用于其他讨论')
            if previous['status'] != 'completed':
                raise ValueError('此前讨论尚未确认完成，请先同步原生会话，避免重复执行')
            return previous
        if not node or int(node.get('execution_generation', 1)) != int(expected_generation):
            raise ValueError('模块版本已变化，请重新打开工作区')
        if state.get('status') in {'running', 'stopping'} or state.get('cleanup_pending'):
            raise ValueError('请等待执行结束后讨论')
        workspace = workspace_root(wf, state, node)
        before_evidence = json.dumps(node.get('artifact_evidence', []), sort_keys=True)
        previous_status = state.get('status')
        cancel_path = Path(wf.run_dir(run_id)) / '.cancel'
        cancel_path.unlink(missing_ok=True)
        append_message(run_id, node_id, expected_generation, 'user', answer,
                       kind='discussion', key='discussion:' + request_id, source='user', base=wf.BASE)
        context = stage_context(run_id, node_id, workspace, base=wf.BASE)
        task = {'task_id': state.get('task_id') or state.get('project_id') or run_id,
                'run_id': run_id, 'node_id': node_id, 'request_id': request_id,
                'objective': '本轮仅讨论、解释或确认要求。不得创建或修改成果文件；修改成果需另行创建修订。\n'
                             + '项目目标：' + str(state.get('project_goal') or '') + '\n用户问题：' + answer,
                'expected_artifacts': [], 'checklist': [], 'prior_messages': context['messages'],
                'conversation_history': {k: v for k, v in context.items() if k != 'messages'}}
        commands[request_id] = {'fingerprint': fingerprint, 'status': 'admitted', 'generation': expected_generation}
        node['discussion_status'] = 'running'
        state['status'] = 'running'
        wf._canvas_save(state)
        from wf_agent import run_agent
        try:
            report = run_agent(task, workspace=workspace, runtime_home=workspace.parent / 'discussion-runtime',
                               session_id=node.get('agent_session_id') or '', resume=True,
                               cancel_path=str(cancel_path), timeout_seconds=600)
        except Exception as error:
            report = {'status': 'uncertain', 'summary': '讨论执行中断，请同步原生会话并检查工作区后继续：' + str(error)[:800]}
        # A discussion never promotes files, changes checks or rewrites frozen evidence.
        assert json.dumps(node.get('artifact_evidence', []), sort_keys=True) == before_evidence
        if report.get('session_id'):
            node['agent_session_id'] = report['session_id']
            node['agent_has_session'] = True
        cancelled = cancel_path.exists() or report.get('status') == 'cancelled'
        node['discussion_status'] = 'cancelled' if cancelled else 'completed' if report.get('status') == 'completed' else 'uncertain'
        state['status'] = 'cancelled' if cancelled else previous_status
        commands[request_id].update(status=node['discussion_status'], summary=report.get('summary'), at=wf.now())
        if report.get('summary'):
            append_message(run_id, node_id, expected_generation, 'assistant', report['summary'],
                           kind='discussion', session_id=node.get('agent_session_id', ''),
                           source='host', key='discussion-result:' + request_id, base=wf.BASE)
        wf._canvas_save(state)
        wf.event(run_id, 'module_discussion', node=node_id, generation=expected_generation,
                 request_id=request_id, status=node['discussion_status'])
        return commands[request_id]
    finally:
        wf.release_lock()
