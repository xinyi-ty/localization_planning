"""从项目配置启动仓库首批翻译和 Docker 公测。"""

import argparse
import json
from pathlib import Path
import shutil

from localization.config import load_config, pair_name, repository

from .localization_step import localize_repository
from .target_project_validation import prepare_target_test_config, run_target_tests
from .translate_batch import translate_batch


def run_first_batch(config):
    """依次执行定位、目标工程准备、翻译和验证，写入首批运行摘要。"""
    phase, repo_name, logs = "配置", "<未读取>", None
    try:
        dataset = config["dataset"]
        repo_name = dataset["repository"]
        pair = pair_name(f"{dataset['source_lang']}_to_{dataset['target_lang']}")
        repo = repository(Path(dataset["path"]).expanduser().resolve(), pair, repo_name)
        root = Path(config["output_dir"]).expanduser().resolve()
        if root.is_relative_to(repo):
            raise ValueError("运行输出须位于数据集原仓库之外")

        phase = "初始化运行目录"
        # 运行目录归本次调用所有，拒绝覆盖任何已有结果。
        root.mkdir(parents=True, exist_ok=False)
        project = root / "workspace" / pair / repo_name / "target_project"
        run_logs = root / "logs" / pair / repo_name / "batch_001"
        run_logs.mkdir(parents=True)
        logs = run_logs
        assembly_output = logs / "assembly"
        summary_path = logs / "summary.json"

        phase = "首批定位"
        located = localize_repository(config, pair, repo_name, assembly_output, through_batch=1)
        batch = located["batches"][1]
        cases = batch["new_cases"]

        phase = "复制首批公测"
        # 接口返回测试用例所在文件，组装目录还存有辅助文件与资源。
        assembled = assembly_output / "round_001/target_project"
        for file in batch["test_files"]:
            path = Path(file)
            if not path.resolve().is_relative_to(assembled) or not path.is_file():
                raise ValueError(f"首批测试文件缺失或越界：{file}")
        shutil.copytree(assembled, project)
        test_paths = sorted(path for path in project.rglob("*") if path.is_file())
        if not test_paths or not batch["test_files"]:
            raise ValueError("首批组装未提供公测文件")
        shutil.rmtree(assembly_output)

        target = pair.split("_to_")[1]
        phase = "准备构建配置"
        build_files = prepare_target_test_config(target, cases, project, config)

        phase = "翻译"
        generated = translate_batch(located, project, test_paths, build_files, logs)

        phase = "目标公测验证"
        tests = run_target_tests(target, cases, repo / "target_project", project, logs)

        phase = "保存运行摘要"
        summary = {"repository": repo_name, "translation_pair": pair, "batch": 1, "stage": "完成",
                   "status": "passed", "target_project": str(project), "generated_files": generated,
                   **tests}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    except Exception as exc:
        # 摘要是附加记录，原异常和异常链仍由命令行输出。
        errors = [str(exc), *getattr(exc, "__notes__", [])]
        exc.add_note(f"仓库 {repo_name} 第 1 批，阶段：{phase}")
        if logs is not None:
            summary = {"repository": repo_name, "translation_pair": pair, "batch": 1, "stage": phase,
                       "status": "failed", "error": "\n".join(errors)}
            try:
                (logs / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as summary_error:
                exc.add_note(f"运行摘要写入失败：{summary_error}")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        config = load_config()
    except Exception as exc:
        exc.add_note("仓库未读取，第 1 批，阶段：读取 default.yaml")
        raise
    print(json.dumps(run_first_batch(config), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
