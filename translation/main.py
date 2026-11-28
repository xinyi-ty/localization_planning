"""从项目配置逐批翻译一个仓库，运行累计 Docker 公测。"""

import argparse
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import shutil

from localization import Localization
from localization.config import load_config, pair_name, public_directories, repository
from localization.llm import load_model_settings

from .build_config import infer_rust_library, prepare_target_test_config
from .localization_step import localize_batch
from .target_dependencies import find_target_dependencies
from .target_project import PublishedTranslation, prepare_candidate, publish_project
from .target_verification import run_target_tests
from .translate_batch import translate_batch


@dataclass(frozen=True)
class RepositoryPaths:
    """仓库工作区内的源工程、原公测和正式目标工程。"""

    source: Path
    original_tests: Path
    target: Path


def write_summary(path: Path, summary: dict) -> None:
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def run_repository(config: dict) -> dict:
    """一次分批，再逐批组装、定位、翻译、测试和发布。"""
    phase, repo_name, number = "配置", "<未读取>", None
    logs, batch_logs, graph_log = None, None, None
    batch_summary = {}
    summary = {"status": "running", "last_passed_batch": None, "batches": {}}
    try:
        # 1. 准备隔离的工作区和日志目录。
        dataset = config["dataset"]
        repo_name = dataset["repository"]
        pair = pair_name(f"{dataset['source_lang']}_to_{dataset['target_lang']}")
        dataset_root = Path(dataset["path"]).expanduser().resolve()
        repo = repository(dataset_root, pair, repo_name)
        model = Path(load_model_settings()["model"].strip()).name
        if not model or model == "..":
            raise ValueError("LLM_MODEL缺少可用的模型目录名")
        root = (Path(config["output_dir"]).expanduser() / model).resolve()
        workspace = root / "workspace"
        work_repo = workspace / pair / repo_name
        log_dir = root / "logs" / pair / repo_name
        if any(path.resolve().is_relative_to(dataset_root) for path in (root, work_repo, log_dir)):
            raise ValueError("运行目录须位于数据集之外")

        phase = "初始化运行目录"
        work_repo.mkdir(parents=True)
        log_dir.mkdir(parents=True)
        logs = log_dir
        target_directory = "input/target_project"
        paths = RepositoryPaths(
            source=work_repo / "source_project",
            original_tests=work_repo / target_directory,
            target=work_repo / "target_project",
        )
        summary.update(repository=repo_name, translation_pair=pair, target_project=str(paths.target))

        # 部分定位接口用空结果报告失败，保留接口本身写出的诊断。
        graph_log = logging.FileHandler(logs / "localization.log", encoding="utf-8")
        logging.getLogger("localization").addHandler(graph_log)

        phase = "复制仓库到工作区"
        # 原公测供后续批次核对位置，索引始终在工作区重建。
        ignore_index = shutil.ignore_patterns(".codegraph")
        shutil.copytree(repo / "source_project", paths.source, ignore=ignore_index)
        shutil.copytree(repo / "target_project", paths.original_tests, ignore=ignore_index)
        locator = Localization({**config, "dataset": {**dataset, "path": str(workspace)}},
                               target_directory=target_directory)
        assembly = logs / "assembly"

        # 2. 确定全仓分批，组装迭代器由批次循环按需推进。
        phase = "分批"
        batches = locator.plan_tests(pair, repo_name)
        test_roots = public_directories(paths.original_tests)
        assembled_rounds = locator.iter_assembled_tests(batches, test_roots, output_dir=assembly)
        summary["batches"] = dict.fromkeys(batches, "not_run")
        write_summary(logs / "summary.json", summary)

        rust_library = None
        if locator.target == "Rust":
            phase = "确认 Rust 库名"
            rust_library = infer_rust_library(repo_name, test_roots)

        # 3. 按批准备输入、翻译、验收和发布。
        cumulative_cases: list[dict] = []
        published = PublishedTranslation()
        for number, new_cases in batches.items():
            phase = "准备批次日志"
            batch_summary = {"repository": repo_name, "translation_pair": pair, "batch": number}
            batch_logs = logs / f"batch_{number:03d}"
            batch_logs.mkdir()
            summary["batches"][number] = "running"

            phase = "本批累计公测组装"
            round_number, assembled_files = next(assembled_rounds)
            if round_number != number:
                raise RuntimeError(f"组装轮次 {round_number} 与当前批次 {number} 不一致")

            phase = "本批映射和源码获取"
            cumulative_cases.extend(new_cases)
            located = localize_batch(locator, repo_name, number, new_cases)
            if located is None:
                if summary["last_passed_batch"] is None:
                    raise ValueError(f"{repo_name} 第 {number} 批映射未返回源函数，尚无已通过的译文")
                batch_summary.update(mode="reuse_previous", mapping="no_confirmed_functions",
                                     generated_files=[], source_to_target={}, implemented=[])

            phase = "准备候选工程"
            candidate = batch_logs / "target_project"
            assembled = assembly / f"round_{number:03d}" / "target_project"
            test_paths = prepare_candidate(
                assembled, assembled_files, candidate, paths.target, published.files,
            )

            phase = "准备构建配置"
            build_files = prepare_target_test_config(
                locator.target, cumulative_cases, candidate, rust_library=rust_library,
            )
            batch_summary["build_config"] = {"files": list(build_files)}
            if rust_library is not None:
                batch_summary["build_config"]["rust_library"] = rust_library
            if located is not None:
                phase = "查询已有目标依赖"
                target_dependencies = (
                    find_target_dependencies(
                        locator.graph, locator.target, paths.original_tests, candidate,
                        new_cases, test_paths, published.files,
                    )
                    if number > 1 else []
                )

                # 索引是候选工程的临时数据，测试报告留在批次日志目录。
                graph_index = candidate / ".codegraph"
                if graph_index.is_dir():
                    shutil.rmtree(graph_index)

                phase = "翻译"
                translation = translate_batch(
                    {"repository": repo_name, "batch": number, "source_root": paths.source,
                     "source_language": locator.source, "target_language": locator.target,
                     "new_cases": new_cases, **located},
                    candidate, test_paths, build_files, batch_logs, published, target_dependencies,
                )
                batch_summary.update(translation)

            phase = "累计公测验证"
            tested = run_target_tests(
                locator.target, cumulative_cases, paths.original_tests, candidate, batch_logs, locator.graph,
            )
            batch_summary.update(tested)

            phase = "发布本批结果"
            previous = batch_logs / "previous_target_project"
            publish_project(candidate, paths.target, previous)
            published.record(batch_summary)
            summary["last_passed_batch"] = number

            phase = "保存批次结果"
            write_summary(batch_logs / "summary.json", {**batch_summary, "status": "passed", "stage": "完成"})
            summary["batches"][number] = "passed"
            write_summary(logs / "summary.json", summary)
            phase = "清理上一批目录"
            if previous.exists():
                shutil.rmtree(previous)

        # 4. 所有批次发布后清理组装暂存目录。
        number, batch_logs = None, None
        phase = "清理组装临时目录"
        shutil.rmtree(assembly)
        phase = "保存仓库结果"
        summary["status"] = "passed"
        write_summary(logs / "summary.json", summary)
        return summary
    except Exception as exc:
        # 仓库入口记录当前进度，再保留原异常和堆栈交给调用者。
        diagnostics = batch_logs or logs
        batch_label = f"第 {number} 批" if number is not None else "批次未开始"
        location = f"仓库 {repo_name}，{batch_label}，阶段：{phase}"
        if diagnostics is not None:
            location += f"，日志目录：{diagnostics}"
        error = "\n".join([str(exc), *getattr(exc, "__notes__", []), location])
        exc.add_note(location)
        if logs is not None:
            summary.update(status="failed", stage=phase, error=error)
            batch_failed = number is not None and summary["batches"].get(number) != "passed"
            if batch_failed:
                summary["failed_batch"] = number
                summary["batches"][number] = "failed"
            try:
                if batch_failed and batch_logs is not None and batch_logs.is_dir():
                    write_summary(batch_logs / "summary.json", {
                        **batch_summary, "status": "failed", "stage": phase, "error": error,
                    })
                write_summary(logs / "summary.json", summary)
            except OSError as summary_error:
                exc.add_note(f"运行摘要写入失败：{summary_error}")
        raise
    finally:
        if graph_log is not None:
            logging.getLogger("localization").removeHandler(graph_log)
            graph_log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(run_repository(load_config()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
