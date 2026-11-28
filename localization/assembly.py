"""通过 LLM 组装累计公测文件，统一校验发布。"""
import base64
from collections import Counter
from collections.abc import Iterator
from copy import deepcopy
import logging
import os
import symtable
from pathlib import Path
import tempfile

from . import llm
from .codegraph import CodeGraph, GraphError, request_scope
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


def prepare_round(
    batches: dict[int, list[dict]], batch_no: int, previous_files: dict[str, str],
    analysis: dict, graph: CodeGraph,
) -> dict:
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


def render_ranges(selection: dict, analysis: dict) -> dict[str, str]:
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


def _validate_files(
    files: dict[str, str], constraints: dict, analysis: dict, graph: CodeGraph,
) -> None:
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


def _assemble_round(stage: dict, analysis: dict, graph: CodeGraph, number: int) -> dict[str, str]:
    """向模型请求源码范围，仅对候选校验失败发起一次修正。"""
    constraints = stage['constraints']
    payload = {**stage['context'], 'constraints': constraints}
    for attempt in (1, 2):
        check_snapshot(analysis['snapshot'])
        response = llm._call_model(build_messages(RANGE_ASSEMBLY_PROMPT, payload))
        try:
            files = render_ranges(llm.json_response(response), analysis)
            logger.info('LLM组装完成，用量：%s', response.get('usage'))
            _validate_files(files, constraints, analysis, graph)
        except (ValueError, SyntaxError) as exc:
            if attempt == 2:
                raise
            check_snapshot(analysis['snapshot'])
            logger.warning('第%s轮原文范围组装未通过校验，交给模型修正一次：%s', number, exc)
            payload['validation_error'] = str(exc)
        else:
            break
    return files


def _write_round(
    candidate_files: dict[str, str], constraints: dict, analysis: dict, round_dir: Path,
) -> list[str]:
    """写入已通过校验的文件及原始公测资源。"""
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


def _assembly_output(output: Path, analysis: dict) -> Path:
    """统一校验独立组装与逐批组装的输出目录。"""
    if not output.is_absolute():
        raise ValueError('输出目录须为绝对路径')
    if output.is_symlink():
        raise FileExistsError('不覆盖已有输出目录：' + str(output))
    output = output.resolve()
    roots = snapshot_roots(analysis['snapshot'])
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in roots):
        raise ValueError('输出目录不能覆盖或位于原测试目录内')
    if output.exists():
        raise FileExistsError('不覆盖已有输出目录：' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


@request_scope
def _write_checked_round(
    batches: dict[int, list[dict]], index: int, previous: dict[str, str],
    analysis: dict, graph: CodeGraph, target: Path,
) -> list[str]:
    """单轮查询、组装、校验和写入。"""
    stage = prepare_round(batches, index, previous, analysis, graph)
    candidate = _assemble_round(stage, analysis, graph, index)
    return _write_round(candidate, stage['constraints'], analysis, target)


def iter_assembled_tests(
    batches: dict[int | str, list[dict]], analysis: dict, graph: CodeGraph,
    *, output_dir: Path,
) -> Iterator[tuple[int, list[str]]]:
    """只在调用方请求下一轮时生成下一套累计公测。"""
    check_snapshot(analysis['snapshot'])
    batches = finalize_batches(batches, analysis, require_all=True)
    output = _assembly_output(output_dir, analysis)
    output.mkdir()
    previous: dict[str, str] = {}
    for index in batches:
        round_dir = output / f'round_{index:03d}'
        try:
            with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output) as temporary:
                staging = Path(temporary) / 'round'
                target = staging / 'target_project'
                paths = _write_checked_round(batches, index, previous, analysis, graph, target)
                previous = {
                    path.relative_to(target).as_posix():
                        SourceText(path.read_bytes(), analysis['language'] == 'Python').text
                    for path in target.rglob('*') if path.is_file()
                    and 'source' in analysis['files'].get(path.relative_to(target).as_posix(), {})
                }
                check_snapshot(analysis['snapshot'])
                staging.rename(round_dir)
        except (ValueError, SyntaxError, GraphError, OSError) as exc:
            logger.error('第%s轮组装失败：%s', index, exc)
            exc.add_note(f'第 {index} 轮累计公测组装失败')
            raise
        yield index, [str(round_dir / Path(path).relative_to(staging)) for path in paths]


def assemble_tests(
    batches: dict[int | str, list[dict]], analysis: dict, graph: CodeGraph,
    *, output_dir: str | Path | None = None,
) -> dict[int, list[str]]:
    """独立组装接口在全部轮次验收后统一发布。"""
    output = (
        Path(output_dir) if output_dir is not None
        else Path(tempfile.gettempdir()) / ('localization-rounds-' + os.urandom(8).hex())
    )
    output = _assembly_output(output, analysis)
    with tempfile.TemporaryDirectory(prefix='.assemble-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'result'
        rounds = dict(iter_assembled_tests(batches, analysis, graph, output_dir=staging))
        staging.rename(output)
    return {
        number: [str(output / Path(path).relative_to(staging)) for path in paths]
        for number, paths in rounds.items()
    }
