"""识别公测用例和源码范围，按需查询辅助代码的调用关系。"""
import base64
from collections import defaultdict
import os
from pathlib import Path
import re

from .codegraph import CodeGraph, GraphError, symbol_identity, rebase_key
from .config import SDK_LANGUAGES, load_config, language as normalize_language
from .file_context import SourceText, public_bundle, source_snapshot, check_snapshot, snapshot_roots


EXTENSIONS = {'python': ('.py', '.pyi'), 'java': ('.java',), 'csharp': ('.cs',),
              'cpp': ('.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hh', '.hxx'), 'rust': ('.rs',)}


def reference_identity(ref):
    if not isinstance(ref, dict) or set(ref) != {'filePath', 'name', 'startLine'}:
        raise ValueError('用例必须包含filePath、name、startLine')
    return symbol_identity(ref)


def unit_reference(unit):
    return {key: unit[key] for key in ('filePath', 'startLine', 'endLine', 'startByte', 'endByte')}


def unit_identity(unit):
    return unit['filePath'], unit['startByte'], unit['endByte']


def _declaration_start(lines, node, language):
    """沿节点前的单行框架标记扩展位置，复杂声明保留给模型复核。"""
    start = node['startLine'] - 1
    marker = '@' if language in {'Python', 'Java'} else '[' if language == 'C#' else '#['
    while start > 0 and lines[start - 1].lstrip().startswith(marker) and not any(
            char in lines[start - 1] for char in '{};'):
        start -= 1
    return start


def is_test(node, source, file, language):
    name = node['name']
    if language == 'Python':
        return bool(re.fullmatch(r'(?:test_.*|.*_test)\.py', Path(file).name)) and name.startswith('test') and not re.search(r'@\w*\.?fixture\b', source)
    if language == 'Java':
        return bool(re.search(r'@(?:[\w.]+\.)?(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b', source))
    if language == 'C#':
        return bool(re.search(r'\[(?:[\w.]+\.)?(?:Fact|Theory|Test|TestCase|TestCaseSource|TestMethod|DataTestMethod)\b', source))
    if language == 'Rust':
        return bool(re.search(r'#\[\s*(?:[\w:]+::)?test\s*\]', source))
    return bool(re.search(r'\b(?:TEST|TEST_F|TEST_P|TYPED_TEST)\s*\(', source))


def import_bindings(line):
    """只接受可按绑定逐项核对的单行导入，复杂写法沿用完整原行。"""
    match = re.fullmatch(r'(\s*)(from\s+[\w.]+\s+import|import)\s+([^#;\n]+?)(\r?\n)?', line)
    if not match:
        return None
    bindings = [part.strip() for part in match[3].split(',')]
    if not all(re.fullmatch(r'[A-Za-z_]\w*(?:\.\w+)*(?:\s+as\s+[A-Za-z_]\w*)?', b) for b in bindings):
        return None
    return match[1], match[2], bindings, match[4] or ''


def binding_name(binding):
    parts = binding.split()
    return parts[-1] if len(parts) == 3 else binding.split('.')[0]


def _collect_test_nodes(directories, root, files, language, graph):
    """核对上游原文，收集公测用例、所属类型和辅助定义。"""
    units, rows = [], []
    for directory in directories:
        prefix = directory.relative_to(root)
        graph.prepare(directory, SDK_LANGUAGES[language])
        nodes = graph.functions(directory)
        for file, record in files.items():
            if 'source' not in record or not Path(file).is_relative_to(prefix):
                continue
            content = graph.content(directory, file=Path(file).relative_to(prefix).as_posix())
            if content['source'].lstrip('\ufeff') != record['source'].lstrip('\ufeff'):
                raise ValueError('SOURCE_CHANGED：上游原文与公测快照不一致')
        for node in nodes:
            file = (prefix / node['filePath']).as_posix()
            if file not in files or 'source' not in files[file]:
                continue
            source = files[file]['source']
            lines = source.splitlines(keepends=True)
            first = _declaration_start(lines, node, language)
            last = node.get('endLine', node['startLine'])
            if first < 0 or last > len(lines) or first >= last:
                raise ValueError('上游测试节点行号越界')
            start = len(''.join(lines[:first]).encode(files[file]['encoding']))
            end = len(''.join(lines[:last]).encode(files[file]['encoding']))
            text = ''.join(lines[first:last])
            test = is_test(node, text, file, language)
            unit = {'filePath': file, 'startByte': start, 'endByte': end,
                    'startLine': first + 1, 'endLine': last, 'source': text,
                    'kind': 'test' if test else 'helper', 'name': node['name'],
                    'node': node}
            units.append(unit)
            if test:
                try:
                    located = graph.location(directory, node)
                except GraphError as exc:
                    if exc.code != 'AMBIGUOUS_NODE':
                        raise
                    located = {}
                # 用例路径相对于目标工程，原生节点路径相对于公测索引目录。
                ref = rebase_key(node, directory, root)
                container = located.get('container')
                scope = ''
                if container:
                    parent = graph.content(directory, node=container)['node']
                    scope = parent.get('qualifiedName', container['name'])
                rows.append({'case': ref, 'unit': unit_reference(unit),
                             'container': scope})
    if not rows:
        raise ValueError('上游索引和测试标记未识别到公测用例')
    return units, rows


def analyze_tests(test_directories, language, graph):
    """在同一公测快照上识别用例、源码单元和文件内容。"""
    name = normalize_language(language)
    graph = graph or CodeGraph(load_config()['codegraph'])
    directories = [test_directories] if isinstance(test_directories, (str, Path)) else list(test_directories)
    directories = list(dict.fromkeys(Path(p).resolve(strict=True) for p in directories))
    if not directories or any(not p.is_dir() for p in directories):
        raise ValueError('须提供原公测目录')
    if any(a != b and a.is_relative_to(b) for a in directories for b in directories):
        raise ValueError('测试目录不能相互嵌套')
    root = Path(os.path.commonpath(directories))
    if root in directories:
        root = root.parent
    root = next((p for p in [root, *root.parents] if p.name == 'target_project'), root)

    extensions = EXTENSIONS[SDK_LANGUAGES[name]]
    bundle = public_bundle(root, directories)
    files = {}
    for file in bundle.files:
        row = {}
        if file.relative_path.endswith(extensions):
            source = SourceText(file.content_bytes, name == 'Python')
            row.update(source=source.text, encoding=source.encoding)
        else:
            row['content'] = base64.b64encode(file.content_bytes).decode('ascii')
        files[file.relative_path] = row

    units, rows = _collect_test_nodes(directories, root, files, name, graph)
    snapshot = {**source_snapshot(bundle), 'roots': [p.relative_to(root).as_posix() for p in directories]}
    result = {'language': name, 'cases': rows, 'units': units, 'files': files, 'snapshot': snapshot}
    check_snapshot(snapshot)
    return result


def _test_calls(analysis, graph):
    """从公测索引查询调用关系，每个目录独立匹配原生节点 ID。"""
    snapshot = analysis['snapshot']
    root = Path(snapshot['root'])
    calls = defaultdict(set)
    for directory in snapshot_roots(snapshot):
        prefix = directory.relative_to(root)
        units = {unit['node']['id']: unit for unit in analysis['units']
                 if Path(unit['filePath']).is_relative_to(prefix)}
        for unit in units.values():
            try:
                response = graph.callees(directory, unit['node'])
            except GraphError as exc:
                if exc.code != 'AMBIGUOUS_NODE':
                    raise
                continue
            for edge in response['edges']:
                owner, target = units.get(edge['source']), units.get(edge['target'])
                if owner and target:
                    calls[unit_identity(owner)].add(unit_identity(target))
    return dict(calls)


def required_units(analysis, cases, graph):
    """按需查询公测调用图，收集可达的必要代码单元。"""
    if 'calls' not in analysis:
        analysis['calls'] = _test_calls(analysis, graph)
    selected = {unit_identity(row['unit']) for row in analysis['cases'] if row['case'] in cases}
    pending = list(selected)
    while pending:
        for target in analysis['calls'].get(pending.pop(), ()):
            if target not in selected:
                selected.add(target)
                pending.append(target)
    return selected
