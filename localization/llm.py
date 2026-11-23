"""共享模型配置、HTTP调用与JSON回复检查。"""
import json
import math
import os
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def load_model_settings(env_file=None):
    """固定项目根目录读取.env；进程变量优先，不修改全局环境。"""
    values = {**dotenv_values(env_file or ENV_FILE, encoding='utf-8-sig', interpolate=False), **os.environ}
    required = ('LLM_ENDPOINT', 'LLM_MODEL', 'LLM_API_KEY',
                'LLM_TIMEOUT_SECONDS', 'LLM_TEMPERATURE', 'LLM_MAX_OUTPUT_TOKENS')
    missing = [name for name in required if not isinstance(values.get(name), str) or not values[name].strip()]
    if missing:
        raise ValueError('.env缺少配置：' + '、'.join(missing))
    endpoint, model, key = (values[n] for n in required[:3])
    url = urlsplit(endpoint)
    if url.scheme not in {'http', 'https'} or not url.netloc:
        raise ValueError('LLM_ENDPOINT须为完整HTTP接口地址')
    if url.path.rstrip('/') in {'', '/v1'}:
        endpoint = urlunsplit(url._replace(path=url.path.rstrip('/') + '/chat/completions'))
    timeout = float(values['LLM_TIMEOUT_SECONDS'])
    temperature = float(values['LLM_TEMPERATURE'])
    max_tokens = int(values['LLM_MAX_OUTPUT_TOKENS'])
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(temperature) or not 0 <= temperature <= 2 or max_tokens <= 0:
        raise ValueError('LLM超时、温度或输出Token上限不合法')
    thinking = values.get('LLM_THINKING')
    if thinking and thinking not in {'enabled', 'disabled'}:
        raise ValueError('LLM_THINKING只支持enabled或disabled')
    return {'endpoint': endpoint, 'model': model, 'key': key, 'timeout': timeout,
            'temperature': temperature, 'max_tokens': max_tokens, 'thinking': thinking}


def _call_model(messages):
    """读取.env配置并调用模型，返回回复内容、结束原因和用量。"""
    settings = load_model_settings()
    payload = {'model': settings['model'], 'messages': messages,
               'temperature': settings['temperature'], 'max_tokens': settings['max_tokens']}
    if settings['thinking']:
        payload['thinking'] = {'type': settings['thinking']}
    req = request.Request(settings['endpoint'], data=json.dumps(payload).encode(), method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + settings['key']})
    with request.urlopen(req, timeout=settings['timeout']) as response:
        try:
            raw = json.load(response)
        except (ValueError, UnicodeError) as exc:
            raise OSError('模型接口未返回有效JSON') from exc
    choices = raw.get('choices') if isinstance(raw, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise OSError('模型接口响应缺少choices')
    message = choices[0].get('message')
    if not isinstance(message, dict) or not isinstance(message.get('content'), str):
        raise OSError('模型接口响应缺少文本content')
    return {'content': message['content'], 'finish_reason': choices[0].get('finish_reason'),
            'usage': raw.get('usage')}


def _json_object(pairs):
    """检查模型JSON的重复键，避免后面的值静默覆盖前面的值。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('模型JSON包含重复键')
        result[key] = value
    return result


def json_response(response):
    """检查完整模型回复并读取JSON对象，失败由业务路径决定处理方式。"""
    if not isinstance(response, dict) or 'content' not in response:
        raise ValueError('模型响应缺少content')
    if response.get('finish_reason') not in (None, 'stop'):
        raise ValueError('模型输出未完整结束')
    content = response['content']
    value = json.loads(content, object_pairs_hook=_json_object) if isinstance(content, str) else content
    if not isinstance(value, dict):
        raise ValueError('模型须返回JSON对象')
    return value
