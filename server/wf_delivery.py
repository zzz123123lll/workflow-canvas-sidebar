"""Export verified bytes and a portable manifest, excluding host/private state."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import uuid
import zipfile


def configure(wf, run_id, body):
    request_id = str(body.get('request_id') or '')
    declarations = body.get('declarations')
    if not request_id or len(request_id) > 160 or not isinstance(declarations, list) or not declarations:
        raise ValueError('请选择交付文件并指定命令标识')
    fingerprint = hashlib.sha256(json.dumps(declarations, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not wf.acquire_lock(run_id):
        raise ValueError('请等待执行结束后确认交付范围')
    try:
        state = wf.read_json(Path(wf.run_dir(run_id)) / 'state.json', {})
        previous = state.get('delivery_commands', {}).get(request_id)
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise ValueError('命令标识已用于其他交付范围')
            return previous['result']
        if state.get('status') in {'running', 'stopping'} or state.get('cleanup_pending'):
            raise ValueError('执行尚未结束')
        if body.get('expected_state_revision') != int(state.get('state_revision', 0)):
            raise ValueError('运行状态已变化，请刷新后确认')
        allowed = set()
        for node in state.get('nodes', []):
            for item in node.get('artifact_evidence') or []:
                logical = item.get('logical_path') or item['path']
                if item.get('kind') == 'directory':
                    allowed.update((node['id'], logical.rstrip('/') + '/' + child['path']) for child in item.get('files', []))
                else:
                    allowed.add((node['id'], logical))
        normalized, seen = [], set()
        for row in declarations:
            key = (row.get('node_id'), row.get('path'))
            if key not in allowed or key in seen or row.get('role') not in {'primary', 'attachment', 'internal'}:
                raise ValueError('交付范围必须对应当前成果且不能重复')
            seen.add(key)
            normalized.append({'node_id': key[0], 'path': key[1], 'role': row['role'],
                'required': bool(row.get('required', True)), 'review_required': bool(row.get('review_required'))})
        if seen != allowed or not any(row['role'] == 'primary' for row in normalized):
            raise ValueError('请为每份成果声明用途，并至少选择一份主成果')
        state.setdefault('delivery_history', []).append({'declarations': state.get('delivery_declarations'), 'at': wf.now()})
        state['delivery_declarations'] = normalized
        for row in normalized:
            if row['role'] != 'internal' and row['review_required']:
                node = wf.node_state(state, row['node_id'])
                if node.get('review_status') != 'approved':
                    node['review_status'] = 'pending'
        result = {'ok': True, 'run_id': run_id, 'declarations': normalized}
        state.setdefault('delivery_commands', {})[request_id] = {'fingerprint': fingerprint, 'result': result}
        wf._canvas_save(state)
        wf.event(run_id, 'delivery_scope_confirmed', request_id=request_id)
        return result
    finally:
        wf.release_lock()


def export_bundle(base, state, results):
    if not results.get('deliverable'):
        raise ValueError('尚有缺失、过期、变更或待审阅成果，请处理后导出交付包')
    root = Path(base).resolve()
    directory = root / '.runs' / state['run_id'] / 'deliveries'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / ('delivery-' + uuid.uuid4().hex + '.zip')
    temporary = destination.with_suffix('.tmp')
    manifest = {'schema': 'canvas.delivery/v1', 'project_id': state.get('project_id'),
                'run_id': state['run_id'], 'goal': state.get('project_goal'),
                'delivery_requirements': state.get('delivery_requirements'), 'files': [], 'omitted_optional_files': [],
                'nodes': [{'id': n['id'], 'generation': n.get('execution_generation', 1),
                           'review_status': n.get('review_status')} for n in state.get('nodes', [])]}
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for row in results['artifacts']:
                if row.get('delivery_role') == 'internal':
                    continue
                if row['status'] != 'verified':
                    if not row.get('delivery_required', True) and not row.get('is_primary'):
                        manifest['omitted_optional_files'].append({'logical_path': row.get('logical_path') or row['path'], 'node_id': row['node_id'], 'status': row['status']})
                        continue
                    raise ValueError('文件证据不足：' + row['path'])
                source = (root / row['path']).resolve()
                if not source.is_relative_to(root) or source.is_symlink() or not source.is_file():
                    raise ValueError('交付文件路径无效')
                logical = row.get('logical_path') or row['path']
                parsed = PurePosixPath(logical)
                if parsed.is_absolute() or '..' in parsed.parts or ':' in logical or '\\' in logical:
                    raise ValueError('交付文件逻辑路径无效')
                name = 'files/' + row['node_id'] + '/generation-' + str(row['generation']) + '/' + logical
                digest = hashlib.sha256()
                with source.open('rb') as handle, archive.open(name, 'w', force_zip64=True) as output:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != row['sha256']:
                    raise ValueError('导出时文件已变化：' + logical)
                manifest['files'].append({'path': name, 'logical_path': logical,
                    'sha256': digest.hexdigest(), 'bytes': row['bytes'], 'node_id': row['node_id'],
                    'generation': row['generation'], 'role': 'primary' if row.get('is_primary') else 'attachment'})
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr('README.txt', '交付文件位于 files/，主成果及附件见 manifest.json。文件哈希为 SHA-256。\n')
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
