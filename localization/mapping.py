"""先匹配原语言测试，再召回并核验各用例对应的源业务函数。"""
from collections import Counter
from collections.abc import Callable
from functools import cache
import json
import logging
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING

import torch
from torch.nn import functional as F
from transformers import AutoModel, AutoTokenizer

from . import llm
from .codegraph import CodeGraph, IndexIdentity, index_identity, index_key, symbol_identity, symbol_key
from .config import load_config
from .file_context import check_snapshot, code_text
from .prompts import MAPPING_REVIEW_PROMPT, SOURCE_TEST_REVIEW_PROMPT, build_messages
from .public_test_analysis import (
    call_evidence, is_test, reference_identity, required_units, test_label, unit_evidence, unit_identity,
)
from .public_test_plan import finalize_batches

if TYPE_CHECKING:
    from .api import Localization


logger = logging.getLogger(__name__)
CHUNK_OVERLAP = 256
SOURCE_TEST_LIMIT = 3


class JinaEmbeddings:
    """一个实例加载一次权重，逐块编码以控制8GB显卡上的显存使用。"""

    def __init__(self, model_path: str | Path, *, settings: dict | None = None) -> None:
        settings = settings or {}
        if not {'query_prompt', 'document_prompt', 'max_length'} <= settings.keys():
            settings = {**load_config()['mapping'], **settings}
        self.query_prompt = settings['query_prompt']
        self.document_prompt = settings['document_prompt']
        self.max_length = settings['max_length']
        path = Path(model_path).expanduser().resolve(strict=True)
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, padding_side='left')
        self.model = AutoModel.from_pretrained(
            path, local_files_only=True,
            dtype=torch.float16 if self.device.type == 'cuda' else torch.float32,
            attn_implementation='sdpa',
        ).to(self.device).eval()
        logger.info('Jina模型已加载 device=%s path=%s', self.device, path)

    def encode(self, texts: list[str], *, query: bool) -> dict:
        """返回归一化向量及所属输入编号，长代码分块保留全部Token。"""
        prompt = self.query_prompt if query else self.document_prompt
        prefix = self.tokenizer.encode(prompt, add_special_tokens=False)
        if type(self.max_length) is not int or self.max_length <= len(prefix) + CHUNK_OVERLAP:
            raise ValueError('mapping.max_length 必须是大于前缀长度和重叠 Token 数之和的整数')
        capacity = self.max_length - len(prefix)
        vectors, owners = [], []
        with torch.inference_mode():
            for index, text in enumerate(texts):
                tokens = self.tokenizer.encode(text, add_special_tokens=False)
                if not tokens:
                    raise ValueError('待编码源码不能为空')
                # 相邻块重叠，降低调用或断言刚好跨越切分点的影响。
                offset = 0
                while offset < len(tokens):
                    part = prefix + tokens[offset:offset + capacity]
                    input_ids = torch.tensor([part], dtype=torch.long, device=self.device)
                    attention_mask = torch.ones_like(input_ids)
                    output = self.model(input_ids=input_ids, attention_mask=attention_mask, use_cache=False)
                    # 官方模型采用最后一个有效Token的隐藏状态作为代码向量。
                    vector = F.normalize(output.last_hidden_state[:, -1].float(), p=2, dim=1)
                    vectors.append(vector.cpu())
                    owners.append(index)
                    del output, input_ids, attention_mask, vector
                    if offset + capacity >= len(tokens):
                        break
                    offset += capacity - CHUNK_OVERLAP
        return {'vectors': torch.cat(vectors), 'owners': owners}


def test_context(analysis: dict, case: dict, graph: CodeGraph) -> str:
    """复用共享分析中的调用证据，收集用例与可确认的辅助代码。"""
    units = {unit_identity(unit): unit for unit in analysis['units']}
    selected = required_units(analysis, [case['case']], graph)
    file = case['case']['filePath']
    selected.update(unit_identity(unit) for unit in analysis['units']
                    if unit['filePath'] == file and unit['kind'] != 'test')
    sections = [f"Test case: {test_label(case)}\n{units[unit_identity(case['unit'])]['source']}"]
    for identity in sorted(selected - {unit_identity(case['unit'])}):
        unit = units[identity]
        sections.append(f"Supporting code ({unit['filePath']}):\n{unit['source']}")
    # 文件中未被索引为函数的部分包含导入、类声明、参数数据和初始化上下文。
    lines = analysis['files'][file]['source'].splitlines(keepends=True)
    occupied = {line for unit in analysis['units'] if unit['filePath'] == file
                for line in range(unit['startLine'], unit['endLine'] + 1)}
    context = ''.join(line for number, line in enumerate(lines, 1) if number not in occupied)
    if context.strip():
        sections.append('File context:\n' + context)
    evidence = unit_evidence(analysis, selected)
    if evidence:
        sections.append('Resolved type and decorator references:\n' + json.dumps(evidence, ensure_ascii=False))
    return '\n\n'.join(sections)


def rank_candidates(
    query_vectors: torch.Tensor, documents: dict, functions: list[dict], top_k: int,
) -> list[dict]:
    """同一代码记录的多个向量按最高余弦相似度归并。"""
    similarities = (query_vectors @ documents['vectors'].T).amax(dim=0)
    scores = {}
    for score, owner in zip(similarities.tolist(), documents['owners']):
        scores[owner] = max(scores.get(owner, -1.0), score)
    # 稳定排序让同分候选按源码位置排列，结果可重复核对。
    ordered = sorted(scores, key=lambda i: (-scores[i], functions[i]['key']['filePath'],
                                           functions[i]['key']['startLine'], functions[i]['key']['name']))
    return [{**functions[i]['key'], 'score': round(scores[i], 6)} for i in ordered[:top_k]]


def is_test_file(file):
    return (file.name == 'conftest.py' or
            any(part.lower() in {'test', 'tests', 'public_test', 'public_tests'} or
                part.lower().endswith('.tests') for part in file.parts[:-1]))


def source_tests(inventory: dict, language: str) -> list[dict]:
    """从索引识别原语言测试，包括测试文件中带断言的 C++ main。"""
    tests = []
    for record in inventory['functions']:
        key, node = record['key'], record['node']
        file = PurePosixPath(key['filePath'])
        standalone = language == 'C++' and (is_test_file(file) or 'test' in file.stem.lower()) and (
            key['name'].startswith('test_') or key['name'] == 'main'
            and re.search(r'\bassert\s*\(', code_text(record['source'], language)))
        if standalone or is_test(node, record['source'], key['filePath'], language):
            tests.append(record)
    return tests


def owner_name(record):
    return record['node'].get('qualifiedName', '').rpartition('::')[0]


def python_instance_members(record, graph, root, records):
    """静态索引未跨 setUp 关联接收者时，以初始化关系和类型上下文核对显式访问。"""
    path = record['key']['filePath']
    owner = owner_name(record)
    setup = [item for item in records.values() if owner and owner_name(item) == owner
             and item['key']['filePath'] == path and item['key']['name'] == 'setUp']
    bindings = {}
    for item in [*setup, record]:
        body = code_text(item['source'], 'Python')
        assignments = re.findall(r'(?m)^\s*((?:self\.)?[A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(', body)
        if assignments:
            types = graph.callees(root, item['key'])['instantiates']
            for receiver, name in assignments:
                matches = [key for key in types if key['name'] == name]
                bindings[receiver] = (matches[0], item['key']) if len(matches) == 1 else None
    accesses = re.findall(r'(?<![\w.])((?:self\.)?[A-Za-z_]\w*)\.([A-Za-z_]\w*)[ \t]*(\()?',
                          code_text(record['source'], 'Python'))
    for receiver, member, invoked in accesses:
        if not (binding := bindings.get(receiver)):
            continue
        target, initializer = binding
        context = graph.type_context(root, target)
        owners = [(target, context['members'])]
        if not any(item['key']['name'] == member for item in context['members']):
            owners = [(base['key'], graph.type_context(root, base['key'])['members'])
                      for base in context['bases'] if base['node']['kind'] in {'class', 'struct', 'interface'}]
        matches = {}
        for scope, nodes in owners:
            for node in nodes:
                key = index_identity(node['key'])
                candidate = records.get(key)
                if not candidate or node['key']['name'] != member:
                    continue
                if not invoked:
                    lines = graph.content(root, file=node['key']['filePath'])['source'].splitlines()
                    start = node['key']['startLine'] - 1
                    if not any(line.strip() in {'@property', '@cached_property', '@functools.cached_property'}
                               for line in lines[max(0, start - 1):start + 1]):
                        continue
                matches[key] = (candidate, scope)
        if len(matches) == 1:
            candidate, scope = next(iter(matches.values()))
            evidence = [{'kind': 'instantiates', 'source': symbol_key(initializer), 'target': symbol_key(target)}]
            if index_identity(scope) != index_identity(target):
                evidence.append({'kind': 'base_member', 'source': symbol_key(target), 'target': symbol_key(scope)})
            yield candidate, evidence


def source_business_calls(
    test: dict, graph: CodeGraph, root: Path, language: str,
    records: dict[IndexIdentity, dict], test_files: set[str],
) -> tuple[list[dict], list[dict]]:
    """沿原测试及其辅助函数查找业务入口。"""
    pending, seen, functions, evidence = [test], set(), [], []
    while pending:
        record = pending.pop()
        identity = index_identity(record['key'])
        if identity in seen:
            continue
        seen.add(identity)
        file = PurePosixPath(record['key']['filePath'])
        if file.as_posix() in test_files or is_test_file(file):
            response = graph.callees(root, record['node'])
            evidence.extend(call_evidence(record['key'], response))
            pending.extend(records[key] for target in response['functions']
                           if (key := index_identity(target)) in records)
            if language == 'Python':
                for candidate, relations in python_instance_members(record, graph, root, records):
                    pending.append(candidate)
                    evidence.extend(relations)
        else:
            source = record['source']
            if (source.strip() and not record['node'].get('isAbstract')
                    and (language == 'Python' or '{' in source or '=>' in source)):
                functions.append(record)
    return functions, evidence


def verify_candidates(case, context, candidates, records, source_language, target_language, evidence):
    """使用已有函数原文核验候选，仅将有效编号转换回三元key。"""
    payload = {'source_language': source_language, 'target_language': target_language,
               'case': case, 'test_context': context, 'call_evidence': evidence,
               'candidates': [
                   {'number': number, 'key': symbol_key(candidate),
                    'qualified_name': records[index_identity(candidate)]['node'].get('qualifiedName', candidate['name']),
                    'source': records[index_identity(candidate)]['source']}
                   for number, candidate in enumerate(candidates, 1)]}
    selected, usage = review_numbers(MAPPING_REVIEW_PROMPT, payload, len(candidates), '映射')
    result = [index_key(candidates[i - 1]) for i in selected]
    if result:
        logger.info('映射核验完成 case=%s selected=%s usage=%s', case, result, usage)
    return result


def verify_source_tests(case, context, shortlist, results, records, source_language, target_language):
    payload = {'source_language': source_language, 'target_language': target_language,
               'case': case, 'test_context': context, 'source_tests': [
                   {'number': i, 'key': symbol_key(test), 'source': records[index_identity(test)]['source'],
                    'business_entries': [r['node'].get('qualifiedName', r['key']['name']) for r in entries],
                    'call_evidence': evidence}
                   for i, (test, (entries, evidence)) in enumerate(zip(shortlist, results), 1)]}
    selected, _ = review_numbers(SOURCE_TEST_REVIEW_PROMPT, payload, len(shortlist), '原测试')
    return [shortlist[i - 1] for i in selected]


def review_numbers(prompt: str, payload: dict, count: int, stage: str) -> tuple[list[int], dict | None]:
    case = payload['case']
    messages = build_messages(prompt, payload)
    try:
        response = llm._call_model(messages)
    except OSError as exc:
        exc.add_note(f'{stage}核验调用失败 case={case}')
        raise
    try:
        if not isinstance(response, dict) or response.get('finish_reason') != 'stop':
            raise ValueError('模型回复未完整结束')
        text = response.get('content')
        if not isinstance(text, str):
            raise ValueError('模型回复缺少文本content')
        selected = None if text.strip() == 'None' else json.loads(text)
        if selected is None or selected == []:
            logger.warning('%s核验未确认候选 case=%s', stage, case)
            return [], None
        if not isinstance(selected, list) or any(type(i) is not int or not 1 <= i <= count for i in selected):
            raise ValueError('模型须返回候选编号列表或None')
        if len(set(selected)) != len(selected):
            raise ValueError('模型回复包含重复候选编号')
    except ValueError as exc:
        raise ValueError(f'{stage}核验回复无效 case={case}：{exc}') from exc
    return selected, response.get('usage')


def _map_case(
    batch_no: int,
    ref: dict,
    context: str,
    shortlist: list[dict],
    records: dict[IndexIdentity, dict],
    business_functions: Callable[[IndexIdentity], tuple[list[dict], list[dict]]],
    source_language: str,
    target_language: str,
) -> list[dict]:
    """按原测试的业务调用候选，确认目标测试实际覆盖的源函数。"""
    results = [business_functions(index_identity(test)) for test in shortlist]
    entry_sets = [{index_identity(r['key']) for r in items} for items, _ in results]
    if not any(entry_sets):
        logger.warning('原语言测试未定位到候选业务函数 case=%s source_tests=%s', ref, shortlist)
        return []
    # 候选测试导向不同业务入口时，再调用模型复核测试匹配结果。
    matches = shortlist[:1]
    if any(keys != entry_sets[0] for keys in entry_sets[1:]):
        matches = verify_source_tests(ref, context, shortlist, results, records,
                                      source_language, target_language)
    keys, evidence = set(), []
    for test in matches:
        entries, relations = business_functions(index_identity(test))
        keys.update(index_identity(row['key']) for row in entries)
        evidence.extend(relations)
    if not keys:
        logger.warning('选中的原语言测试没有业务候选 case=%s source_tests=%s', ref, matches)
        return []
    candidates = [records[key]['key'] for key in sorted(keys)]
    logger.info('映射候选 batch=%s case=%s source_tests=%s candidates=%s', batch_no, ref, matches, candidates)
    return verify_candidates(ref, context, candidates, records, source_language, target_language, evidence)


def map_tests(
    service: "Localization", project: dict, batches: dict[int | str, list[dict]],
) -> dict[int, list[dict]]:
    """匹配原语言测试，沿调用链召回业务函数，再按目标测试核验。"""
    analysis = service._test_analysis(project)
    batches = finalize_batches(batches, analysis)
    refs = [ref for cases in batches.values() for ref in cases]
    identities = [reference_identity(ref) for ref in refs]
    cases = {reference_identity(row['case']): row for row in analysis['cases']}

    inventory = service._source_analysis(project)
    originals = source_tests(inventory, service.source)
    if not originals:
        logger.warning('映射未识别到原语言测试 root=%s', inventory['root'])
        check_snapshot(analysis['snapshot'])
        return {batch_no: [] for batch_no in batches}
    records = {index_identity(record['key']): record for record in inventory['functions']}
    public_counts = Counter(symbol_identity(record['key']) for record in records.values())
    test_files = {record['key']['filePath'] for record in originals}
    root = Path(inventory['root'])
    if service._embedding_model is None:
        settings = service.config['mapping']
        service._embedding_model = JinaEmbeddings(settings['model_path'], settings=settings)
    encoder = service._embedding_model
    test_documents = encoder.encode([record['source'] for record in originals], query=False)
    queries = [test_context(analysis, cases[identity], service.graph) for identity in identities]
    encoded_queries = encoder.encode(queries, query=True)
    query_indices = {identity: index for index, identity in enumerate(identities)}

    @cache
    def business_functions(key: IndexIdentity) -> tuple[list[dict], list[dict]]:
        return source_business_calls(records[key], service.graph, root, service.source, records, test_files)

    mapped = {}
    for batch_no, batch_refs in batches.items():
        selected = {}
        for ref in batch_refs:
            index = query_indices[reference_identity(ref)]
            vectors = encoded_queries['vectors'][
                [i for i, owner in enumerate(encoded_queries['owners']) if owner == index]
            ]
            shortlist = rank_candidates(vectors, test_documents, originals, top_k=SOURCE_TEST_LIMIT)
            check_snapshot(analysis['snapshot'])
            check_snapshot(inventory['snapshot'])
            matches = _map_case(
                batch_no, ref, queries[index], shortlist, records, business_functions,
                service.source, service.target,
            )
            selected.update((index_identity(key), key) for key in matches)
        for key in selected.values():
            if public_counts[symbol_identity(key)] > 1:
                raise ValueError(f'业务入口的三元定位键无法区分同名节点：{symbol_key(key)}')
        mapped[batch_no] = [symbol_key(key) for key in selected.values()]
    check_snapshot(analysis['snapshot'])
    check_snapshot(inventory['snapshot'])
    return mapped
