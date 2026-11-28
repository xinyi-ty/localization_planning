"""通过 LLM 组装累计公测文件，统一校验发布。"""
import base64
from collections import Counter
from copy import deepcopy
import logging
import os
import symtable
from pathlib import Path
import tempfile

from . import llm
from .codegraph import GraphError
from .file_context import SourceText, check_snapshot, snapshot_roots, prompt_files, python_statements
from .prompts import RANGE_ASSEMBLY_PROMPT, build_messages
from .public_test_analysis import (analyze_tests, reference_identity, unit_identity, unit_reference,
                            import_bindings, binding_name, required_units, test_label, unit_evidence)
from .public_test_plan import finalize_batches

logger = logging.getLogger(__name__)


def source_items(text, language):
    """导入按原绑定核验，其他非空行按原文顺序核验。"""
    items = []
    for line in text.splitlines(keepends=True):
        parts = import_bindings(line) if language == 'Python' else None
        if parts:
            indent, prefix, bindings, _ = parts
            items.extend(('import', indent, prefix, binding) for binding in bindings)
        elif line.strip() and line.strip() != 'pass':
            items.append(('source', line.rstrip()))
    return items


def prepare_round(batches, batch_no, previous_files, analysis, graph):
    """准备累计用例、原文和验收规则。"""
    cases = [ref for index, refs in batches.items() if index <= batch_no for ref in refs]
    paths = [name for name, record in analysis['files'].items() if 'source' in record]
    files = prompt_files(analysis, paths)
    needed = required_units(analysis, cases, graph)
    if any(row['case'] not in cases and unit_identity(row['unit']) in needed for row in analysis['cases']):
        raise ValueError('本轮累计测试调用后批用例，当前分批无法组装')
    required = [unit_reference(unit) for unit in analysis['units'] if unit_identity(unit) in needed]
    # 提示词标注行号，模型返回的范围可以定位到原文件。
    for file in files:
        file['source'] = '\n'.join(f'{i}: {line}' for i, line in enumerate(file['source'].splitlines(), 1))
    return {
        'context': {'language': analysis['language'], 'cases': cases,
                    'previous_files': deepcopy(previous_files), 'new_cases': batches[batch_no],
                    'files': files, 'call_evidence': unit_evidence(analysis, needed)},
        'constraints': {'language': analysis['language'], 'cases': cases,
                        'required_units': required,
                        'target_layout': list(analysis['snapshot']['roots'])},
    }


def render_ranges(selection, analysis):
    """按原始行号复制片段，Python 单行导入可按原绑定裁剪。"""
    originals = {name: row['source'] for name, row in analysis['files'].items() if 'source' in row}
    if not isinstance(selection, dict) or not selection or not selection.keys() <= originals.keys():
        raise ValueError('组装范围为空或包含未知路径')
    files = {}
    for name, spans in selection.items():
        if not isinstance(spans, list) or not spans:
            raise ValueError(f'{name} 的保留范围须为非空列表')
        lines = originals[name].splitlines(keepends=True)
        end = 0
        chunks = []
        for span in spans:
            import_selection = (analysis['language'] == 'Python'
                                and isinstance(span, dict) and span.keys() == {'line', 'bindings'})
            if import_selection:
                first = last = span['line']
            elif isinstance(span, list) and len(span) == 2:
                first, last = span
            else:
                raise ValueError(f'{name} 的保留范围须为 [起始行, 结束行] 或 Python 单行导入选择')
            if type(first) is not int or type(last) is not int or not end < first <= last <= len(lines):
                raise ValueError(f'{name} 的保留范围越界、重叠或未按原文顺序排列：{first}–{last}')
            end = last
            if not import_selection:
                chunks.extend(lines[first - 1:last])
                continue
            parts = import_bindings(lines[first - 1])
            bindings = span['bindings']
            if parts is None or not isinstance(bindings, list) or not bindings or any(
                    not isinstance(binding, str) for binding in bindings):
                raise ValueError(f'{name}:{first} 须指定原有 Python 单行导入的绑定')
            indent, prefix, original_bindings, ending = parts
            if len(set(bindings)) != len(bindings) or not set(bindings) <= set(original_bindings):
                raise ValueError(f'{name}:{first} 的导入选择包含重复或未知绑定')
            kept = [binding for binding in original_bindings if binding in bindings]
            chunks.append(lines[first - 1] if kept == original_bindings
                          else indent + prefix + ' ' + ', '.join(kept) + ending)
        files[name] = ''.join(chunks)
    return files


def llm_assemble(stage, analysis, *, validation_error=None):
    """读取模型输出，范围路径按原文生成文件。"""
    payload = {**stage['context'], 'constraints': stage['constraints']}
    if validation_error is not None:
        payload['validation_error'] = validation_error
    check_snapshot(analysis['snapshot'])
    try:
        response = llm._call_model(build_messages(RANGE_ASSEMBLY_PROMPT, payload))
        files = render_ranges(llm.json_response(response), analysis)
    except (ValueError, TypeError, OSError) as exc:
        logger.error('LLM组装失败：%s', exc)
        raise
    logger.info('LLM组装完成，用量：%s', response.get('usage'))
    return files


def _python_references(text, name):
    """保留原有导入仍被引用的校验。"""
    table = symtable.symtable(text, name, 'exec')
    references = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_referenced()}
    scopes = table.get_children()
    while scopes:
        scope = scopes.pop()
        references.update(symbol.get_name() for symbol in scope.get_symbols()
                          if symbol.is_global() and symbol.is_referenced())
        scopes.extend(scope.get_children())
    return references


def _future_imports(text):
    if '__future__' not in text:
        return []
    return [tuple(token.string for token in statement) for statement in python_statements(text)
            if [token.string for token in statement[:3]] == ['from', '__future__', 'import']]


def _validate_files(files, constraints, analysis, graph):
    """通过隔离的上游索引检查用例，按原文核对已识别的定义。"""
    originals = {name: row for name, row in analysis['files'].items() if 'source' in row}
    if not isinstance(files, dict) or not files or not files.keys() <= originals.keys():
        raise ValueError('组装文件为空或包含未知路径')
    if any(not isinstance(text, str) for text in files.values()):
        raise ValueError('组装文件须为完整源码字符串')
    # 校验始终使用新目录，避免同步覆盖原公测索引或使用上一轮节点缓存。
    with tempfile.TemporaryDirectory(prefix='localization-check-') as temporary:
        target = Path(temporary) / 'target_project'
        roots = [target / name for name in constraints['target_layout']]
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = text.encode(originals[name]['encoding'])
            if constraints['language'] == 'Python':
                compile(raw, name, 'exec', dont_inherit=True)
            path.write_bytes(raw)
        observed = analyze_tests(roots, constraints['language'], graph)
    # 生成文件的行号会变化，以所属类型、名称和原文关联原始用例，保留出现次数。
    def case_signature(result, row):
        unit = next(unit for unit in result['units'] if unit_identity(unit) == unit_identity(row['unit']))
        return row['case']['filePath'], row['container'], test_label(row), unit['source'].strip()
    expected = Counter(case_signature(analysis, row) for row in analysis['cases'] if row['case'] in constraints['cases'])
    actual = Counter(case_signature(observed, row) for row in observed['cases'])
    if actual != expected:
        raise ValueError('上游复核发现未来用例、遗漏或重复用例')
    # 按原始定义核对正文，保留辅助代码与当前轮测试的实际实现。
    required = {unit_identity(ref) for ref in constraints['required_units']}
    present = Counter((unit['filePath'], unit['source'].strip()) for unit in observed['units'])
    required_counts = Counter((unit['filePath'], unit['source'].strip()) for unit in analysis['units']
                              if unit_identity(unit) in required)
    if required_counts - present:
        raise ValueError('组装改变或遗漏上游已识别的测试或辅助定义')
    original_helpers = Counter((unit['filePath'], unit['source'].strip()) for unit in analysis['units']
                               if unit['kind'] != 'test')
    actual_helpers = Counter((unit['filePath'], unit['source'].strip()) for unit in observed['units']
                             if unit['kind'] != 'test')
    if actual_helpers - original_helpers:
        raise ValueError('组装改变了辅助定义的原始实现')
    for name, text in files.items():
        original = originals[name]['source']
        allowed = iter(source_items(original, constraints['language']))
        for item in source_items(text, constraints['language']):
            if not any(candidate == item for candidate in allowed):
                raise ValueError('组装新增或重排了原文件之外的代码或导入绑定：' + name)
        if constraints['language'] == 'Python':
            retained = _python_references(text, name)
            before = Counter(item for item in source_items(original, 'Python') if item[0] == 'import')
            after = Counter(item for item in source_items(text, 'Python') if item[0] == 'import')
            if any(binding_name(item[-1]) in retained for item in before - after):
                raise ValueError('组装删除了保留代码仍引用的导入：' + name)
            # 按逻辑行比对整个编译指令，覆盖带括号的多行导入。
            if _future_imports(original) != _future_imports(text):
                raise ValueError('组装删除或改变了原有编译指令：' + name)
    check_snapshot(analysis['snapshot'])


def validate_and_write(stage, candidate_files, analysis, graph, round_dir):
    """验收本轮文件并写入暂存目录。"""
    constraints = stage['constraints']
    try:
        _validate_files(candidate_files, constraints, analysis, graph)
    except (ValueError, GraphError, SyntaxError) as exc:
        logger.error('组装校验失败：%s', exc)
        raise
    target = Path(round_dir)
    target.mkdir(parents=True, exist_ok=True)
    for name, text in candidate_files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(analysis['files'][name]['encoding']))
    for name, row in analysis['files'].items():
        if 'content' in row:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(row['content']))
    names = sorted({ref['filePath'] for ref in constraints['cases']})
    return [str(target / name) for name in names]


def assemble_tests(batches, analysis, graph, *, output_dir=None):
    """按模型所选原文范围逐轮组装，统一发布通过校验的文件。"""
    check_snapshot(analysis['snapshot'])
    batches = finalize_batches(batches, analysis, require_all=True)
    language = analysis['language']
    output = Path(output_dir) if output_dir is not None else Path(tempfile.gettempdir()) / ('localization-rounds-' + os.urandom(8).hex())
    if not output.is_absolute():
        raise ValueError('输出目录须为绝对路径')
    output = output.resolve()
    roots = snapshot_roots(analysis['snapshot'])
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in roots):
        raise ValueError('输出目录不能覆盖或位于原测试目录内')
    if output.exists() or output.is_symlink():
        raise FileExistsError('不覆盖已有输出目录：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    result, previous = {}, {}
    # 完成轮次尝试后，统一发布通过校验的文件。
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        staging.mkdir()
        for index in batches:
            target = staging / f'round_{index:03d}' / 'target_project'
            stage = prepare_round(batches, index, previous, analysis, graph)
            try:
                candidate = llm_assemble(stage, analysis)
                paths = validate_and_write(stage, candidate, analysis, graph, target)
            except (ValueError, SyntaxError) as exc:
                check_snapshot(analysis['snapshot'])
                logger.warning('第%s轮原文范围组装未通过校验，交给模型修正一次：%s', index, exc)
                candidate = llm_assemble(stage, analysis, validation_error=str(exc))
                paths = validate_and_write(stage, candidate, analysis, graph, target)
            # 下一轮读取实际验收并写入的文件。
            previous = {p.relative_to(target).as_posix(): SourceText(p.read_bytes(), language == 'Python').text
                        for p in target.rglob('*') if p.is_file() and 'source' in analysis['files'].get(p.relative_to(target).as_posix(), {})}
            result[index] = [str(output / Path(path).relative_to(staging)) for path in paths]
        check_snapshot(analysis['snapshot'])
        os.rename(staging, output)
    return result
