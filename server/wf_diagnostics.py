"""Small, local diagnostics containing no prompts, host tokens or personal paths."""
import json
from pathlib import Path
import platform


def summary(base, build):
    root = Path(base)
    counts = {}
    for file in (root/'.runs').glob('*/state.json'):
        try:state=json.loads(file.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        status=str(state.get('status') or 'unknown');counts[status]=counts.get(status,0)+1
    return {'schema':'canvas.diagnostics/v1','build':build,'platform':platform.system()+' '+platform.release(),
            'run_status_counts':counts,'contains':['版本和构建','操作系统','运行状态计数'],
            'excluded':['机器路径','账号和令牌','提示词和聊天正文','用户资料和成果','原始工具参数和日志'],
            'automatic_upload':False}
