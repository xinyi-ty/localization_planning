"""源码获取接口的入口定位、依赖遍历与原文范围整理。"""
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from os.path import commonprefix
from pathlib import Path
import re
import tokenize
from typing import TYPE_CHECKING

from .config import SDK_LANGUAGES, files_under
from .codegraph import CodeGraph, IndexIdentity, index_identity, index_key, symbol_identity
from .file_context import create_snapshot, check_snapshot, python_statements, code_text
from .source_dependencies import collect_file_dependencies, collect_python_dependencies

if TYPE_CHECKING:
    from .api import Localization


@dataclass
class _SourceLookup:
    """在一次提取内共用源码和位置查询，保留用于一致性检查的原文。"""

    root: Path
    graph: CodeGraph
    contents: dict[IndexIdentity, dict]
    sources: dict[str, str] = field(default_factory=dict)
    python_lines: dict[str, list[list[tokenize.TokenInfo]]] = field(default_factory=dict)

    def source(self, file: str) -> str:
        if file not in self.sources:
            self.sources[file] = checked_file(self.graph, self.root, file)
        return self.sources[file]

    def statements(self, file: str) -> list[list[tokenize.TokenInfo]]:
        if file not in self.python_lines:
            source = self.source(file)
            try:
                self.python_lines[file] = python_statements(source)
            except (tokenize.TokenError, IndentationError) as exc:
                raise ValueError('无法确认 Python 源码逻辑行：' + file) from exc
        return self.python_lines[file]

    def content(self, key: dict) -> dict:
        identity = index_identity(key)
        if identity not in self.contents:
            self.contents[identity] = self.graph.content(self.root, node=key)
        return self.contents[identity]

    def owning_unit(self, node: dict) -> dict:
        return self.graph.location(self.root, node).get('container') or node

    def dependency_files(self, file: str) -> set[str]:
        response = self.graph.call(
            'getDependencies', self.root, level='file', input={'kind': 'file', 'filePath': file},
        )
        return {row['filePath'] for row in response.get('dependencies', [])}


def analyze_source(root: Path, language: str, graph: CodeGraph) -> dict:
    """供映射与提取复用的源函数记录，每条同时保留key、原文和原生节点。"""
    snapshot = create_snapshot(root, [root])
    graph.prepare(root, SDK_LANGUAGES[language])
    functions = []
    for node in graph.functions(root):
        content = graph.content(root, node=node)
        functions.append({'key': index_key(node), 'node': node, 'source': content['source']})
    check_snapshot(snapshot)
    return {'root': str(root), 'language': language, 'functions': functions, 'snapshot': snapshot}


def extract_source(service: "Localization", project: dict, functions: list[dict]) -> dict:
    """按函数 key 定位入口单元，并汇总源码范围和外部导入依赖。"""
    inventory = service._source_analysis(project)
    root = Path(inventory['root'])
    graph, language = service.graph, service.source
    nodes = [row['node'] for row in inventory['functions']]
    lookup = _SourceLookup(root, graph, {
        index_identity(row['key']): {'node': row['node'], 'source': row['source']}
        for row in inventory['functions']
    })
    project_files = [p.relative_to(root).as_posix() for p in files_under(root)]
    entries = _resolve_entries(functions, nodes, lookup.owning_unit)
    pending, visited = deque((selector, True) for selector in entries), set()
    result = {'entry_units': [], 'dependencies': {'project': [], 'external': []}}
    entry_ranges, dependency_ranges = {}, {}
    # 队列中的每项都是已确认的源码单元。文件查询只提供候选，不直接扩大交付范围。
    while pending:
        selector, entry = pending.popleft()
        identity = index_identity(selector)
        if identity in visited:
            continue
        visited.add(identity)
        unit = lookup.content(selector)
        spans = source_ranges(unit, lookup.source, project_files, language, root)
        if language == 'Python':
            for span in spans:
                span['startLine'] = _python_declaration_start(
                    lookup.statements(span['filePath']), span['startLine'])
        destination = entry_ranges if entry else dependency_ranges
        for span in spans:
            key = (span['filePath'], span['startLine'], span['endLine'])
            destination[key] = span
        identifiers = referenced_names(unit['source'], language)
        candidates, function_references = _collect_unit_dependencies(
            lookup, selector, unit, spans, nodes, language, identifiers,
        )

        if language == 'Python':
            file_candidates, file_ranges, external = collect_python_dependencies(
                spans, selector['name'], project_files, lookup.statements, lookup.source, lookup.content)
        else:
            file_candidates, file_ranges, external = collect_file_dependencies(
                spans, selector['name'], language, project_files, nodes, identifiers, function_references,
                source=lookup.source, owning_unit=lookup.owning_unit, dependency_files=lookup.dependency_files,
            )
        candidates.update(file_candidates)
        dependency_ranges.update(file_ranges)
        result['dependencies']['external'].extend(external)
        for target in candidates.values():
            node = lookup.content(target)['node']
            if node['kind'] in {'function', 'method', 'constructor'}:
                target = lookup.owning_unit(index_key(target))
            pending.append((target, False))

    # 多个入口和依赖可能覆盖同一单元，入口优先，范围按原始坐标去重。
    result['entry_units'] = sorted(entry_ranges.values(), key=lambda r: (r['filePath'], r['startLine']))
    result['dependencies']['project'] = sorted(
        [span for key, span in dependency_ranges.items() if not any(
            span['filePath'] == entry['filePath'] and entry['startLine'] <= span['startLine']
            and span['endLine'] <= entry['endLine'] for entry in entry_ranges.values())],
        key=lambda r: (r['filePath'], r['startLine']))
    result['dependencies']['external'] = list({tuple(row.items()): row for row in result['dependencies']['external']}.values())
    for file, original in lookup.sources.items():
        if checked_file(graph, root, file) != original:
            raise ValueError('SOURCE_CHANGED：依赖遍历期间文件变化：' + file)
    check_snapshot(inventory['snapshot'])
    return result


def _collect_unit_dependencies(
    lookup: _SourceLookup,
    selector: dict,
    unit: dict,
    spans: list[dict],
    nodes: list[dict],
    language: str,
    identifiers: set[str],
) -> tuple[dict[IndexIdentity, dict], set[str]]:
    """整理成员调用、类型关系以及文件依赖筛选所需的引用名。"""
    candidates = {}
    members = [node for node in nodes if any(
        node['filePath'] == span['filePath'] and span['startLine'] <= node['startLine'] <= span['endLine']
        for span in spans)]
    function_references = set()
    for member in members:
        body = lookup.content(member)['source']
        clean_body = code_text(body, language)
        if language == 'Python':
            body_start = clean_body.find(':')
        else:
            body_start = clean_body.find('{')
            if body_start < 0:
                body_start = clean_body.find('=>')
        # 抽象方法只有声明。声明中的方法名不能充当调用另一实现的证据。
        if body_start >= 0:
            function_references.update(referenced_names(clean_body[body_start + 1:], language))
        response = lookup.graph.callees(lookup.root, member)
        for field in ('functions', 'decorators', 'instantiates'):
            for target in response[field]:
                candidates[index_identity(target)] = target
    if unit['node']['kind'] in {'class', 'struct', 'interface'}:
        context = lookup.graph.type_context(lookup.root, selector)
        for field in ('bases', 'initializers', 'bindings'):
            for relation in context[field]:
                target = relation['key']
                candidates[index_identity(target)] = target
        dependencies = lookup.graph.call('getDependencies', lookup.root, level='class', input=index_key(selector))
        for target in dependencies['dependencies']:
            if target['name'] in identifiers:
                candidates[index_identity(target)] = target
    return candidates, function_references


def _resolve_entries(functions: list[dict], nodes: list[dict], owning_unit: Callable[[dict], dict]) -> list[dict]:
    """公开输入用三元键定位，索引查询沿用上游的完整定位键。"""
    entries = []
    for function in functions:
        candidates = [node for node in nodes if symbol_identity(function) == symbol_identity(node)]
        if not candidates:
            raise ValueError('未找到入口函数：' + repr(function))
        if len(candidates) != 1:
            raise ValueError('入口函数三元定位键存在歧义：' + repr(function))
        entries.append(owning_unit(candidates[0]))
    return entries


def _python_declaration_start(statements, start):
    for index, statement in enumerate(statements):
        first = statement[0]
        if first.start[0] != start:
            continue
        for previous in reversed(statements[:index]):
            if previous[0].string != '@' or previous[0].start[1] != first.start[1]:
                break
            start = previous[0].start[0]
        break
    return start


def referenced_names(source: str, language: str) -> set[str]:
    return set(re.findall(r'\b[A-Za-z_]\w*\b', code_text(source, language)))


def source_ranges(
    content: dict, source: Callable[[str], str], project_files: list[str], language: str, root: Path,
) -> list[dict]:
    """普通单元采用SDK坐标，合并源码按原文逐段定位，不能定位时明确失败。"""
    node = content['node']
    span = {'filePath': node['filePath'], 'name': node.get('qualifiedName', node['name']),
            'startLine': node['startLine'], 'endLine': node['endLine']}
    original = source(node['filePath'])
    lines = original.splitlines()
    if not 1 <= span['startLine'] <= span['endLine'] <= len(lines):
        raise ValueError('上游源码范围无效：' + node['filePath'])
    merged = node['kind'] in {'class', 'struct', 'interface'} and (
        language == 'C++' or language == 'C#' and re.search(r'\bpartial\s+(?:class|struct|interface)\b', content['source']))
    if not merged:
        return [span]
    # 上游合并内容使用两个换行连接原文片段。以原文匹配恢复坐标，保持其项目归属判断。
    remaining = content['source'].strip()
    files = [node['filePath']] + [file for file in project_files
        if file != node['filePath'] and Path(file).suffix in {'.cs', '.h', '.hpp', '.hh', '.hxx', '.cc', '.cpp', '.cxx', '.c'}]
    spans, used = [], set()
    while remaining:
        first_line = remaining.split('\n', 1)[0]
        matches = []
        for file in files:
            # 先在本地原文中筛选，只有可能匹配的文件才通过HTTP核对。
            local = (root / file).read_bytes().decode('utf-8-sig')
            if first_line not in local:
                continue
            text = source(file)
            offset = text.find(first_line)
            while offset >= 0:
                shared = commonprefix([remaining, text[offset:]]).rstrip()
                if shared and (file, offset) not in used and (
                        len(shared) == len(remaining) or remaining[len(shared):].startswith('\n\n')):
                    matches.append((len(shared), file, offset))
                offset = text.find(first_line, offset + 1)
        if not matches:
            raise ValueError('无法从原文确认上游合并源码的位置：' + node['filePath'])
        longest = max(match[0] for match in matches)
        matches = [match for match in matches if match[0] == longest]
        primary = [match for match in matches if match[1] == node['filePath']
                   and source(match[1])[:match[2]].count('\n') + 1 == node['startLine']]
        if len(matches) > 1 and not primary:
            raise ValueError('上游合并源码的原文位置存在歧义：' + node['filePath'])
        length, file, offset = (primary or matches)[0]
        start = source(file)[:offset].count('\n') + 1
        spans.append({'filePath': file, 'name': span['name'], 'startLine': start,
                      'endLine': start + remaining[:length].count('\n')})
        used.add((file, offset))
        remaining = remaining[length:].lstrip()
    return spans


def checked_file(graph: CodeGraph, root: Path, file: str) -> str:
    path = (root / file).resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(root):
        raise ValueError('上游源码文件越界：' + file)
    returned = graph.content(root, file=file)['source']
    original = path.read_bytes().decode('utf-8-sig')
    if original != returned.lstrip('\ufeff'):
        raise ValueError('SOURCE_CHANGED：上游文件原文不一致：' + file)
    return original
