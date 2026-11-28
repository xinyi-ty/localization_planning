"""分批接口及批次校验。"""
from copy import deepcopy
import logging

from . import llm
from .file_context import check_snapshot, prompt_files
from .prompts import PURE_PLAN_PROMPT, build_messages
from .public_test_analysis import reference_identity

logger = logging.getLogger(__name__)


def ordered_batches(batches, *, require_contiguous):
    """统一批次键，完整计划要求编号从 1 连续递增。"""
    if not isinstance(batches, dict) or not batches:
        raise ValueError('批次计划须为非空字典')
    result = {}
    for index, refs in batches.items():
        if isinstance(index, str) and index.isascii() and index.isdigit() and str(int(index)) == index:
            index = int(index)
        if type(index) is not int or index < 1 or index in result or not isinstance(refs, list) or not refs:
            raise ValueError('批次编号须为不重复正整数，每批须有用例')
        result[index] = refs
    if require_contiguous and sorted(result) != list(range(1, len(result) + 1)):
        raise ValueError('批次编号须从1连续递增')
    return dict(sorted(result.items()))


def _validate_batches(batches, cases, *, require_all):
    batches = ordered_batches(batches, require_contiguous=require_all)
    known = {reference_identity(case) for case in cases}
    selected = [reference_identity(ref) for refs in batches.values() for ref in refs]
    if len(selected) != len(set(selected)) or not set(selected) <= known:
        raise ValueError('批次用例重复或无法在当前公测中定位')
    if require_all and set(selected) != known:
        raise ValueError('批次遗漏公测用例')
    return batches


def finalize_batches(batches, analysis, *, require_all=False):
    """校验调用方批次，组装要求完整覆盖，映射可处理其中一部分。"""
    return deepcopy(_validate_batches(batches, (row['case'] for row in analysis['cases']),
                                      require_all=require_all))


def plan_tests(analysis):
    """纯模型分批只读取基本用例清单和原文，按通用覆盖规则验收。"""
    check_snapshot(analysis['snapshot'])
    cases = [row['case'] for row in analysis['cases']]
    files = [name for name, record in analysis['files'].items() if 'source' in record]
    payload = {'language': analysis['language'], 'cases': cases, 'files': prompt_files(analysis, files)}
    labels = [{'case': row['case'], 'testIdentity': row['testIdentity']}
              for row in analysis['cases'] if row.get('testIdentity')]
    if labels:
        payload['test_identities'] = labels
    messages = build_messages(PURE_PLAN_PROMPT, payload)
    try:
        response = llm._call_model(messages)
        result = _validate_batches(llm.json_response(response), cases, require_all=True)
    except (ValueError, TypeError, OSError) as exc:
        logger.error('纯LLM分批失败：%s', exc)
        raise
    check_snapshot(analysis['snapshot'])
    logger.info('纯LLM分批完成，用量：%s', response.get('usage'))
    return result
