"""把仓库定位结果按批交给翻译主流程。"""

from pathlib import Path

from localization import Localization
from localization.config import pair_name, public_directories, repository


def localize_repository(config, translation_pair, repository_name, output_dir):
    """组装各批测试并定位需要翻译的源码。

    使用 config 中的原始数据集，累计测试文件写入 output_dir。
    返回以整数为批次键的结果，以及解释相对源码路径所需的源工程根目录。
    单批缺少映射或入口范围时，抛出带仓库名和批次号的异常。
    """
    pair = pair_name(translation_pair)
    source_language, target_language = pair.split("_to_")
    # 实例必须绑定当前请求的翻译对。复制配置项，便于调用方用同一份配置处理其他仓库。
    dataset = {**config["dataset"], "source_lang": source_language, "target_lang": target_language}
    dataset_root = Path(dataset["path"]).expanduser().resolve()
    repo = repository(dataset_root, pair, repository_name)
    output = Path(output_dir).expanduser().resolve()
    # 组装先于映射。将生成文件放在原仓库外，避免后续索引读到组装产物。
    if output.is_relative_to(repo):
        raise ValueError("组装输出目录须位于数据集原仓库之外")
    test_dirs = public_directories(repo / "target_project")

    # 同一实例复用该仓库的公测分析和源工程索引。
    locator = Localization({**config, "dataset": dataset})
    plan = locator.plan_tests(pair, repository_name)
    # plan 保存每批新增用例，组装结果则保存截至该批的累计测试文件。
    test_files = locator.assemble_tests(plan, test_dirs, output_dir=output)
    # 映射接口校验批次从 1 连续递增，整份计划一次传入。
    matches = locator.map_tests(repository_name, plan)

    batches = {}
    for number, new_cases in plan.items():
        functions = matches[number]
        if not functions:
            raise ValueError(f"{repository_name} 第 {number} 批映射未返回源函数")
        # 映射结果已是源码获取接口接受的三元 key，保留路径和行号以区分同名函数。
        ranges = locator.extract_source(repository_name, functions)
        # 源码获取接口已记录具体失败原因，并以空字典表示这类失败。
        if not ranges.get("entry_units"):
            raise ValueError(f"{repository_name} 第 {number} 批源码获取未返回入口范围")
        batches[number] = {
            "new_cases": new_cases,
            "test_files": test_files[number],
            "source_functions": functions,
            "source_ranges": ranges,
        }

    # 下游通过 source_root 和各区间的 filePath 读取对应的原语言源码。
    return {
        "translation_pair": pair,
        "repository": repository_name,
        "source_root": str((repo / "source_project").resolve()),
        "batches": batches,
    }
