"""Small JSON contracts with actionable field locations and no extra runtime."""
def validate_json(value, schema, location='$'):
    if not isinstance(schema, dict):
        raise ValueError('JSON规范必须是对象：' + location)
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool,
             'integer': int, 'number': (int, float), 'null': type(None)}
    kind = schema.get('type')
    if kind and (kind not in types or not isinstance(value, types[kind]) or
                 (kind in {'number', 'integer'} and isinstance(value, bool))):
        raise ValueError('结构化输入 ' + location + ' 应为 ' + str(kind))
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError('结构化输入 ' + location + ' 不在允许值中')
    if isinstance(value, dict):
        for name in schema.get('required', []):
            if name not in value:
                raise ValueError('结构化输入缺少字段 ' + location + '.' + name)
        for name, rule in schema.get('properties', {}).items():
            if name in value:
                validate_json(value[name], rule, location + '.' + name)
    if isinstance(value, list) and schema.get('items'):
        for index, item in enumerate(value):
            validate_json(item, schema['items'], location + '[' + str(index) + ']')


def validate_inputs(node, inputs, workspace):
    import json
    from pathlib import Path
    schemas = node.get('input_schemas') or {}
    for relative, schema in schemas.items():
        record = next((r for r in inputs if r['path'] == relative), None)
        if record is None:
            raise ValueError('没有经过验证的结构化输入：' + relative)
        path = (Path(workspace) / relative).resolve()
        if not path.is_relative_to(Path(workspace).resolve()):
            raise ValueError('结构化输入路径越界')
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('结构化输入不是有效JSON：' + relative) from exc
        validate_json(value, schema, relative)
        record['schema_verified'] = True
