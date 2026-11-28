"""HTTP接口的薄客户端；不创建或管理Node进程。"""
import json
from urllib import request, error
from contextvars import ContextVar
from functools import wraps
from copy import deepcopy
from pathlib import Path, PurePosixPath


def symbol_key(value):
    """从上游节点或映射候选提取三元key，附加分数和节点元数据留在原记录中。"""
    if not isinstance(value, dict) or not {'name', 'filePath', 'startLine'} <= value.keys():
        raise ValueError('符号引用必须包含name、filePath、startLine')
    name, file, line = value['name'], value['filePath'], value['startLine']
    if not isinstance(name, str) or not name.strip() or not isinstance(file, str) or not file:
        raise ValueError('符号名称和路径不能为空')
    path = PurePosixPath(file)
    if path.is_absolute() or '..' in path.parts or '\\' in file or ':' in file or path.as_posix() != file:
        raise ValueError('filePath须为规范的工程内相对路径')
    if type(line) is not int or line < 1:
        raise ValueError('startLine须为正整数')
    return {'name': name, 'filePath': file, 'startLine': line}


def symbol_identity(value):
    key = symbol_key(value)
    return key['filePath'], key['name'], key['startLine']


def index_key(value):
    """保留上游对同一索引中的冲突符号提供的定位信息。"""
    key = symbol_key(value)
    if 'symbolId' in value:
        token = value['symbolId']
        if not isinstance(token, str) or not token:
            raise ValueError('symbolId须为非空字符串')
        key['symbolId'] = token
    return key


def index_identity(value):
    key = index_key(value)
    return key['filePath'], key['name'], key['startLine'], key.get('symbolId', '')


def rebase_key(value, from_root, to_root):
    """在工程与公测目录之间转换公开定位键，索引标识留在原索引。"""
    key = symbol_key(value)
    origin, target = Path(from_root).resolve(strict=True), Path(to_root).resolve(strict=True)
    path = (origin / key['filePath']).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(origin) or not path.is_relative_to(target):
        raise ValueError('符号路径越过工程或索引范围')
    return {**key, 'filePath': path.relative_to(target).as_posix()}

_request_cache = ContextVar('localization_graph_cache', default=None)

def request_scope(function):
    """只复用一次公开接口调用内的图查询，结束即清空，不跨请求复用旧源码。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _request_cache.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _request_cache.reset(token)
    return wrapped



class GraphError(RuntimeError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class CodeGraph:
    def __init__(self, config):
        if not isinstance(config, dict) or not {"base_url", "timeout_seconds"} <= config.keys():
            raise ValueError("CodeGraph配置需要base_url和timeout_seconds")
        self.base_url = config["base_url"]
        self.timeout = config["timeout_seconds"]
        if not isinstance(self.base_url, str) or not self.base_url.startswith(("http://", "https://")) or type(self.timeout) not in {int, float} or self.timeout <= 0:
            raise ValueError("CodeGraph地址或超时配置不合法")
        self.base_url = self.base_url.rstrip("/")

    def call(self, method, root, **parameters):
        cache = _request_cache.get()
        # 文件原文查询用于末尾一致性核验，必须重新读取。
        reusable = method in {'getAllFunctions', 'getCallees', 'getLocation', 'getTypeContext', 'getDependencies'} or (
            method == 'getContent' and parameters.get('input', {}).get('kind') != 'file')
        key = (id(self), str(root), method, json.dumps(parameters, sort_keys=True))
        if cache is not None and reusable and key in cache:
            return deepcopy(cache[key])
        result = self._call_uncached(method, root, **parameters)
        if cache is not None and reusable:
            cache[key] = deepcopy(result)
        if cache is not None and method in {'sync', 'initialize'}:
            for old in list(cache):
                if old[:2] == key[:2]:
                    del cache[old]
        return result

    def _call_uncached(self, method, root, **parameters):
        body = {"projectRoot": str(root), **parameters}
        req = request.Request(self.base_url + "/" + method, data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return json.load(response)
        except error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {})
            except (ValueError, UnicodeError):
                detail = {}
            raise GraphError(detail.get("code", "HTTP_ERROR"), detail.get("message", "HTTP " + str(exc.code)), detail) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise GraphError("GRAPH_CONNECTION_ERROR", "CodeGraph连接失败或超时；索引超时不表示服务器操作已取消") from exc
        except (ValueError, UnicodeError) as exc:
            raise GraphError("GRAPH_PROTOCOL_ERROR", "CodeGraph未返回有效JSON") from exc

    def prepare(self, root, language):
        """同步已有索引，首次读取工程时按指定语言建库。"""
        try:
            result = self.call("sync", root)
        except GraphError as exc:
            if exc.code != "NOT_INITIALIZED":
                raise
            try:
                result = self.call("initialize", root, options={"language": ["cpp", "c"] if language == "cpp" else language})
            except GraphError as initialized:
                if initialized.code != "ALREADY_INITIALIZED":
                    raise
                result = self.call("sync", root)
        if not isinstance(result, dict) or result.get("success") is False or any(result.get(k) for k in ("filesErrored", "errors", "failedFilePaths")):
            raise GraphError("GRAPH_INDEX_ERROR", "CodeGraph索引未完整完成")

    def function_keys(self, root):
        rows = self.call("getAllFunctions", root)
        if not isinstance(rows, list):
            raise GraphError("GRAPH_PROTOCOL_ERROR", "函数清单须为数组")
        for row in rows:
            index_key(row)
        return rows

    def functions(self, root):
        """查出函数的原生范围，并保留上游附带的消歧键和测试身份。"""
        nodes = []
        for row in self.function_keys(root):
            result = self.content(root, node=row)
            node = result['node']
            if 'id' not in node:
                raise GraphError('GRAPH_PROTOCOL_ERROR', '函数节点缺少原生ID')
            if 'testIdentity' in result:
                node['testIdentity'] = result['testIdentity']
            nodes.append({**node, **row})
        return nodes

    def callees(self, root, node):
        result = self.call("getCallees", root, key=index_key(node))
        if not isinstance(result, dict) or any(
                not isinstance(result.get(field), list)
                for field in ('functions', 'decorators', 'instantiates', 'edges')):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '调用结果缺少关系数组')
        if any(not isinstance(edge, dict) or not {'kind', 'source', 'target'} <= edge.keys()
               for edge in result['edges']):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '调用结果包含无效关系边')
        edges = [edge for edge in result['edges']
                 if (edge.get('metadata') or {}).get('synthesizedBy') != 'interface-impl']
        if len(edges) != len(result['edges']):
            # 接口声明到全部实现的分派边不能说明某个实现实际被调用。
            direct = {edge['target'] for edge in edges if edge['kind'] == 'calls'}
            result = {**result, 'edges': edges,
                      'functions': [key for key in result['functions']
                                    if self.content(root, node=key)['node']['id'] in direct]}
        return result

    def type_context(self, root, node):
        result = self.call("getTypeContext", root, key=index_key(node))
        if not isinstance(result, dict) or any(
                not isinstance(result.get(field), list)
                for field in ('members', 'bases', 'initializers', 'bindings', 'unresolved')):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '类型上下文缺少关系数组')
        return result

    def location(self, root, node):
        result = self.call("getLocation", root, key=index_key(node))
        if (not isinstance(result, dict)
                or not isinstance(result.get('filePath'), str)
                or not isinstance(result.get('key'), dict)
                or index_key(result['key']) != index_key(node)
                or not (result.get('container') is None or isinstance(result.get('container'), dict))):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '所属类型结果缺少合法位置')
        return result

    def content(self, root, node=None, file=None):
        selector = {"kind": "file", "filePath": file} if file is not None else index_key(node)
        result = self.call("getContent", root, input=selector)
        if not isinstance(result, dict) or not isinstance(result.get('source'),str) or not isinstance(result.get('filePath'),str):
            raise GraphError('GRAPH_PROTOCOL_ERROR', '源码结果缺少合法source或filePath')
        if node is not None:
            resolved = result.get('node')
            if not isinstance(resolved, dict) or symbol_key(resolved) != symbol_key(selector):
                raise GraphError('GRAPH_PROTOCOL_ERROR', '源码节点与请求的定位键不一致')
            if 'symbolId' in selector and selector['symbolId'] != resolved.get('id'):
                raise GraphError('GRAPH_PROTOCOL_ERROR', '源码节点与请求的symbolId不一致')
            result = {**result, 'node': {**resolved, **selector}}
        return result
