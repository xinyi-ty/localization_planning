"""从项目配置逐批翻译一个仓库，运行累计 Docker 公测。"""

import argparse
import json
import logging
from pathlib import Path
import shutil

from localization import Localization
from localization.codegraph import symbol_identity
from localization.config import load_config, pair_name, repository

from .localization_step import localize_batch, localize_repository
from .target_dependencies import find_target_dependencies
from .target_project_validation import prepare_target_test_config, run_target_tests
from .translate_batch import translate_batch


def write_summary(path, summary):
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def publish_project(candidate, project, previous):
    """发布通过公测的候选目录，移动失败时恢复上一次正式目录。"""
    if project.exists():
        project.rename(previous)
    try:
        candidate.rename(project)
    except OSError as exc:
        if previous.exists():
            try:
                previous.rename(project)
            except OSError as restore_error:
                exc.add_note(f"正式目录恢复失败，上一批仍保留在 {previous}：{restore_error}")
        raise


def run_repository(config):
    """先分批和组装，再逐批定位、翻译、测试和发布。"""
    phase, repo_name, number = "配置", "<未读取>", None
    logs, batch_logs, graph_log = None, None, None
    batch_summary = {}
    summary = {"status": "running", "last_passed_batch": None, "batches": {}}
    try:
        dataset = config["dataset"]
        repo_name = dataset["repository"]
        pair = pair_name(f"{dataset['source_lang']}_to_{dataset['target_lang']}")
        repo = repository(Path(dataset["path"]).expanduser().resolve(), pair, repo_name)
        root = Path(config["output_dir"]).expanduser().resolve()
        if root.is_relative_to(repo):
            raise ValueError("运行输出须位于数据集原仓库之外")

        phase = "初始化运行目录"
        root.mkdir(parents=True, exist_ok=False)
        project = root / "workspace" / pair / repo_name / "target_project"
        logs = root / "logs" / pair / repo_name
        logs.mkdir(parents=True)
        project.parent.mkdir(parents=True)
        summary.update(repository=repo_name, translation_pair=pair, target_project=str(project))

        # 部分定位接口用空结果报告失败，保留接口本身写出的诊断。
        graph_log = logging.FileHandler(logs / "localization.log", encoding="utf-8")
        logging.getLogger("localization").addHandler(graph_log)
        locator = Localization(config)
        source_root = (repo / "source_project").resolve(strict=True)
        original_tests = (repo / "target_project").resolve(strict=True)
        assembly = logs / "assembly"

        phase = "分批和组装"
        batches = localize_repository(locator, pair, repo_name, repo, assembly)
        summary["batches"] = dict.fromkeys(batches, "not_run")
        write_summary(logs / "summary.json", summary)
        cumulative_cases, business_files, implemented = [], set(), set()
        source_to_target = {}
        for number, planned in batches.items():
            phase = "准备批次日志"
            batch_summary = {"repository": repo_name, "translation_pair": pair, "batch": number}
            batch_logs = logs / f"batch_{number:03d}"
            batch_logs.mkdir()
            summary["batches"][number] = "running"

            phase = "本批映射和源码获取"
            new_cases = planned["new_cases"]
            cumulative_cases.extend(new_cases)
            located = localize_batch(locator, repo_name, number, new_cases)

            phase = "准备候选工程"
            candidate = batch_logs / "target_project"
            assembled = assembly / f"round_{number:03d}" / "target_project"
            if not planned["test_files"]:
                raise ValueError("组装未提供本批公测文件")
            for filename in planned["test_files"]:
                path = Path(filename).resolve(strict=True)
                if not path.is_file() or not path.is_relative_to(assembled):
                    raise ValueError(f"公测文件路径无效：{filename}")
            shutil.copytree(assembled, candidate)
            test_paths = sorted(path for path in candidate.rglob("*") if path.is_file())
            for filename in sorted(business_files):
                destination = candidate / filename
                if destination.exists():
                    raise ValueError(f"业务文件与本批公测或资源冲突：{filename}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(project / filename, destination)

            phase = "准备构建配置"
            build_files = prepare_target_test_config(locator.target, cumulative_cases, candidate, config)
            phase = "查询已有目标依赖"
            target_dependencies = (
                find_target_dependencies(locator.graph, locator.target, original_tests, candidate,
                                         new_cases, test_paths, business_files)
                if number > 1 else []
            )

            # 索引是候选工程的临时数据，测试报告留在批次日志目录。
            graph_index = candidate / ".codegraph"
            if graph_index.is_dir():
                shutil.rmtree(graph_index)

            phase = "翻译"
            translation = translate_batch(
                {"repository": repo_name, "batch": number, "source_root": source_root,
                 "source_language": locator.source, "target_language": locator.target,
                 "new_cases": new_cases, **located},
                candidate, test_paths, build_files, batch_logs,
                {"business_files": business_files, "source_to_target": source_to_target,
                 "implemented": implemented, "target_dependencies": target_dependencies},
            )
            batch_summary.update(translation)

            phase = "累计公测验证"
            tested = run_target_tests(locator.target, cumulative_cases, original_tests, candidate, batch_logs)
            batch_summary.update(tested)

            phase = "发布本批结果"
            previous = batch_logs / "previous_target_project"
            publish_project(candidate, project, previous)
            business_files.update(translation["generated_files"])
            implemented.update(symbol_identity(key) for key in translation["implemented"])
            for path, targets in translation["source_to_target"].items():
                source_to_target.setdefault(path, set()).update(targets)
            summary["last_passed_batch"] = number

            phase = "保存批次结果"
            write_summary(batch_logs / "summary.json", {**batch_summary, "status": "passed", "stage": "完成"})
            summary["batches"][number] = "passed"
            write_summary(logs / "summary.json", summary)
            phase = "清理上一批目录"
            if previous.exists():
                shutil.rmtree(previous)

        number, batch_logs = None, None
        phase = "清理组装临时目录"
        shutil.rmtree(assembly)
        phase = "保存仓库结果"
        summary["status"] = "passed"
        write_summary(logs / "summary.json", summary)
        return summary
    except Exception as exc:
        error = "\n".join([str(exc), *getattr(exc, "__notes__", [])])
        batch_label = f"第 {number} 批" if number is not None else "批次未开始"
        exc.add_note(f"仓库 {repo_name}，{batch_label}，阶段：{phase}")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        config = load_config()
    except Exception as exc:
        exc.add_note("仓库未读取，阶段：读取 default.yaml")
        raise
    print(json.dumps(run_repository(config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
