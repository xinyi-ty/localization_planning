"""从源码原文补充已定位单元的导入、头文件和依赖候选。"""
from collections import defaultdict, deque
from functools import cache
import logging
from pathlib import Path, PurePosixPath
import posixpath
import re
import tokenize

from .codegraph import GraphError, index_identity
from .file_context import code_text

logger = logging.getLogger(__name__)


def collect_file_dependencies(
        spans, unit_name, language, project_files, nodes, identifiers, function_references, *,
        source, owning_unit, dependency_files,
):
    """在已解析的依赖文件中核对引用，并保留导入原文。"""
    candidates, dependency_ranges, external = {}, {}, []
    for span in spans:
        file = span['filePath']
        file_source = source(file)
        # 导入只作为名称解析上下文，不能据此把导入文件的全部定义纳入计划。
        imports = import_context(file_source, language)
        files = set(dependency_files(file))
        files.add(file)
        if language == 'C++':
            files.update(local_includes(file, file_source, project_files))
        matches_by_name = defaultdict(dict)
        for node in nodes:
            if node['filePath'] not in files:
                continue
            target = owning_unit(node)
            # 类成员调用必须关联其所属类型；独立函数按实际引用名称筛选。
            same_unit = index_identity(target) == index_identity(node)
            if ((not same_unit and target['name'] in identifiers)
                    or (same_unit and node['name'] in function_references)):
                matches_by_name[target['name']][index_identity(target)] = target
        # 文件级候选无法证明同名符号的身份，只补充唯一匹配。图中已定位的引用由调用方保留。
        for matches in matches_by_name.values():
            if len(matches) == 1:
                candidates.update(matches)
        for line, code in enumerate(file_source.splitlines(), 1):
            if code.strip() and code in imports.splitlines():
                dependency_ranges[(file, line, line)] = {
                    'filePath': file, 'name': unit_name, 'startLine': line, 'endLine': line}
        external.extend(external_imports(file, file_source, language, project_files))
    return candidates, dependency_ranges, external


def _python_references(statements):
    references = set()
    for statement in statements:
        values = [token.string for token in statement]
        if values[0] in {'from', 'import'}:
            continue
        if values[:2] == ['async', 'def']:
            statement = statement[3:]
        elif values[0] in {'class', 'def'}:
            statement = statement[2:]
        text = ' '.join(token.string if token.type in {tokenize.NAME, tokenize.OP} else ' '
                        for token in statement).replace(' . ', '.')
        references.update(re.findall(r'(?<![\w.])[^\W\d]\w*(?:\.[^\W\d]\w*)*', text))
    return references


def _python_imports(statement):
    values = [token.string for token in statement]
    split = values.index('import')
    module = ''.join(values[1:split]) if split else ''
    names = ' '.join(v for v in values[split + 1:] if v not in {'(', ')'})
    for part in names.split(','):
        original, _, alias = part.strip().partition(' as ')
        name = original.replace(' ', '')
        if name and name != '*':
            yield alias or name.split('.')[0], module, name


def _python_module(file, module, project_files):
    relative = len(module) - len(module.lstrip('.'))
    path = module.lstrip('.').replace('.', '/')
    if relative:
        parents = PurePosixPath(file).parents
        if relative > len(parents):
            return None
        path = (parents[relative - 1] / path).as_posix()
    module_file = path + '.py'
    package_file = (PurePosixPath(path) / '__init__.py').as_posix()
    for candidate in (package_file, module_file):
        if candidate in project_files:
            return candidate
    if not relative:
        matches = [name for name in project_files if name.endswith(('/' + module_file, '/' + package_file))]
        if len(matches) == 1:
            return matches[0]
    return None


def collect_python_dependencies(spans, unit_name, project_files, statements, source, content):
    """按模块绑定追踪实际引用，同时保留确认要执行的包初始化语句。"""
    candidates, ranges, external = {}, {}, []
    pending = deque((span['filePath'], _python_references(
        row for row in statements(span['filePath']) if span['startLine'] <= row[0].start[0] <= span['endLine']))
        for span in spans)
    # 每个文件维护已解析的引用名，重复到达时只处理新增引用。
    seen = defaultdict(set)

    @cache
    def external_by_line(file):
        return {row['startLine']: row for row in external_imports(file, source(file), 'Python', project_files)}

    def keep(file, first, last):
        ranges[(file, first, last)] = {'filePath': file, 'name': unit_name,
                                      'startLine': first, 'endLine': last}

    while pending:
        file, references = pending.popleft()
        new_file = file not in seen
        references = references - seen[file]
        if not (new_file or references):
            continue
        seen[file].update(references)
        if new_file:
            for parent in PurePosixPath(file).parents:
                initializer = (parent / '__init__.py').as_posix()
                if parent != PurePosixPath('.') and initializer != file and initializer in project_files:
                    pending.append((initializer, set()))

        rows = statements(file)
        top_level = [index for index, row in enumerate(rows) if row[0].start[1] == 0
                     and row[0].string not in {'elif', 'else', 'except', 'finally'}]
        by_name = defaultdict(set)
        for reference in references:
            by_name[reference.split('.')[0]].add(reference)
        for index, stop in zip(top_level, top_level[1:] + [len(rows)]):
            statement = rows[index]
            token = statement[0]
            first, last = token.start[0], statement[-1].end[0]
            if token.string in {'from', 'import'}:
                for binding, module, name in _python_imports(statement):
                    if binding not in by_name and module != '__future__':
                        continue
                    keep(file, first, last)
                    target = _python_module(file, module or name, project_files)
                    if not target:
                        if item := external_by_line(file).get(first):
                            external.append(item)
                        continue
                    if module:
                        requested = {name + ref[len(binding):] for ref in by_name[binding]}
                        # from pkg import submodule 可直接绑定子模块，不要求 __init__.py 中有再导出。
                        if target.endswith('/__init__.py') or target == '__init__.py':
                            submodule = _python_module(target, '.' + name, project_files)
                            if submodule:
                                pending.append((submodule, {ref[len(name) + 1:] for ref in requested
                                                           if ref.startswith(name + '.')}))
                                continue
                    else:
                        prefix = binding if binding != name.split('.')[0] else name
                        requested = {ref[len(prefix) + 1:] for ref in by_name[binding]
                                     if ref.startswith(prefix + '.')}
                    pending.append((target, requested))
                continue
            if token.string in {'def', 'class', 'async'}:
                name = statement[2 if token.string == 'async' else 1].string
                if name in by_name:
                    key = {'filePath': file, 'name': name, 'startLine': first}
                    try:
                        content(key)
                    except GraphError as exc:
                        if exc.code != 'NODE_NOT_FOUND':
                            raise
                        logger.warning('上游未索引 Python 依赖声明，无法确认范围：%s', key)
                    else:
                        candidates[index_identity(key)] = key
                continue
            # 保留用到的模块绑定及包加载时执行的普通顶层语句。
            bound = (token.type == tokenize.NAME and token.string in by_name
                     and len(statement) > 1 and statement[1].string in {'=', ':'})
            block = rows[index:stop]
            # 纯别名赋值是再导出，仅在名字被引用时保留。显式调用和控制语句可能在加载时生效。
            initializes_package = (new_file and PurePosixPath(file).name == '__init__.py' and (
                token.string in {'if', 'for', 'while', 'try', 'with', 'match'}
                or token.string != '@' and any(previous.type == tokenize.NAME and item.string == '('
                    for row in block for previous, item in zip(row, row[1:]))))
            # 条件定义必须连同条件和其他分支保留，例如 TYPE_CHECKING 下的类型及其运行时替代。
            conditional_definition = token.string == 'if' and len(block) > 1 and any(
                row[0].start[1] == block[1][0].start[1] != 0
                and row[0].string in {'class', 'def'} and row[1].string in by_name
                for row in block[1:])
            if bound or initializes_package or conditional_definition:
                keep(file, first, block[-1][-1].end[0])
                pending.append((file, _python_references(block)))
    return candidates, ranges, external


def import_context(source, language):
    patterns = {'Python': r'^\s*(?:from\s|import\s)', 'Java': r'^\s*import\s',
                'C#': r'^\s*(?:global\s+)?using\s', 'C++': r'^\s*#\s*include\s'}
    return '\n'.join(line for line in source.splitlines() if re.match(patterns[language], line))


def local_includes(file, source, project_files):
    """补齐原文中明确写出的项目头文件；多处同名时保留查询缺口。"""
    results = set()
    clean = re.sub(r'/\*[\s\S]*?\*/', '', source)
    for marker, name in re.findall(r'^\s*#\s*include\s*([<"])([^>"\n]+)[>"]', clean, re.MULTILINE):
        relative = posixpath.normpath(posixpath.join(posixpath.dirname(file), name.replace('\\', '/')))
        if marker == '"' and relative in project_files:
            results.add(relative)
            continue
        matches = [path for path in project_files if path == name or path.endswith('/' + name)]
        if len(matches) == 1:
            results.add(matches[0])
    return sorted(results)


def external_imports(file, source, language, project_files):
    """按导入语句记录可明确判定的外部模块，复杂别名与动态导入留给日志。"""
    results = []
    modules = {Path(p).stem for p in project_files}
    modules.update(Path(p).parts[0] for p in project_files)
    patterns = {
        'Python': r'^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))',
        'Java': r'^\s*import\s+(?:static\s+)?([\w.]+)',
        'C#': r'^\s*(?:global\s+)?using\s+([\w.]+)\s*;',
        'C++': r'^\s*#\s*include\s*<([^>]+)>',
    }
    for line, text in enumerate(source.splitlines(), 1):
        match = re.match(patterns[language], text)
        if not match:
            continue
        name = next(value for value in match.groups() if value is not None)
        if language == 'C++' and local_includes(file, text, project_files):
            continue
        if name.startswith('.') or name.split('.')[0] in modules or name.split('.')[-1] in modules:
            continue
        if language in {'Java', 'C#'} and any(p.endswith(name.replace('.', '/') + '.java') for p in project_files):
            continue
        results.append({'name': name, 'import': text.strip(), 'filePath': file, 'startLine': line})
    return results
