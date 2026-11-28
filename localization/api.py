"""对外提供分批、组装、测试映射和源码获取。"""
from copy import deepcopy
import logging
from pathlib import Path

from .assembly import assemble_tests
from .codegraph import CodeGraph, GraphError, request_scope, symbol_key, symbol_identity
from .config import language, pair_name, repository, public_directories
from .mapping import map_tests
from .source_extract import extract_source, analyze_source
from .file_context import check_snapshot
from .public_test_analysis import analyze_tests
from .public_test_plan import plan_tests


class Localization:
    """绑定翻译对和上游客户端，提供规划、组装、映射与源码提取入口。"""

    def __init__(self, config, *, target_directory="target_project"):
        self.config = deepcopy(config)
        data = self.config["dataset"]
        # 相对数据集路径以进程启动目录为基准。服务器配置使用绝对路径。
        self.root = Path(data["path"]).expanduser().resolve()
        self._target_directory = target_directory
        self.source, self.target = language(data["source_lang"]), language(data["target_lang"])
        self.pair = pair_name(self.source + "_to_" + self.target)
        self.graph = CodeGraph(self.config["codegraph"])
        self._embedding_model = None
        self._current_project = None

    def _project(self, name, *, fresh=False):
        if fresh or self._current_project is None or self._current_project['name'] != name:
            repo = repository(self.root, self.pair, name)
            source = (repo / 'source_project').resolve(strict=True)
            target = (repo / self._target_directory).resolve(strict=True)
            if any(not path.is_dir() or not path.is_relative_to(repo) for path in (source, target)):
                raise ValueError('工程目录不存在或越界')
            self._current_project = {'name': name, 'repo': repo, 'source_root': source,
                                     'target_root': target, 'analysis': None, 'source_analysis': None}
        return self._current_project

    def _test_analysis(self, project):
        """复用同一仓库的公测分析，每次调用都核对文件快照。"""
        if project['analysis'] is None:
            project['analysis'] = analyze_tests(public_directories(project['target_root']), self.target, self.graph)
        check_snapshot(project['analysis']['snapshot'])
        return project['analysis']

    def _source_analysis(self, project):
        if project['source_analysis'] is None:
            project['source_analysis'] = analyze_source(project['source_root'], self.source, self.graph)
        check_snapshot(project['source_analysis']['snapshot'])
        return project['source_analysis']

    @request_scope
    def map_tests(self, repository_name, batches):
        """返回各批次经LLM核验并去重的源函数三元key。"""
        return map_tests(self, self._project(repository_name), batches)

    @request_scope
    def plan_tests(self, translation_pair, repository_name):
        """分批接口，返回批次编号到本批新增用例列表的映射。"""
        if pair_name(translation_pair) != self.pair:
            raise ValueError('请求翻译对与实例绑定的翻译对不一致')
        return plan_tests(self._test_analysis(self._project(repository_name, fresh=True)))

    @request_scope
    def assemble_tests(self, batches, test_directories, *, output_dir=None):
        """根据分批结果返回各轮累计测试文件的路径。"""
        directories = [test_directories] if isinstance(test_directories, (str, Path)) else list(test_directories)
        directories = sorted({Path(p).resolve(strict=True) for p in directories})
        project = self._current_project
        if project is not None and directories == sorted(public_directories(project['target_root'])):
            analysis = self._test_analysis(project)
        else:
            analysis = analyze_tests(directories, self.target, self.graph)
        return assemble_tests(batches, analysis, self.graph, output_dir=output_dir)


    @request_scope
    def extract_source(self, repository_name, functions):
        """源码获取接口，返回入口源码范围和项目内外依赖。预期提取失败时记录原因并返回空字典。"""
        try:
            if not isinstance(functions, list) or not functions:
                raise ValueError('functions须为非空列表')
            normalized = {}
            for function in functions:
                key = symbol_key(function)
                normalized[symbol_identity(key)] = key
            return extract_source(self, self._project(repository_name), list(normalized.values()))
        except (ValueError, OSError, GraphError) as exc:
            logging.getLogger(__name__).error('源码提取失败 repo=%s entries=%s [%s]：%s',
                repository_name, functions, getattr(exc, 'code', type(exc).__name__), exc)
            if isinstance(exc, GraphError) and exc.details.get('candidates'):
                logging.getLogger(__name__).error('上游歧义候选：%s', exc.details['candidates'])
            return {}
