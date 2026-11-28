"""准备全仓分批和组装结果，按需定位当前批源码。"""

from localization.config import public_directories


def localize_repository(locator, pair, repository_name, repository, output):
    """分批和组装只执行一次，返回每批新增用例和组装文件。"""
    if output.is_relative_to(repository):
        raise ValueError("组装输出目录须位于数据集原仓库之外")
    plan = locator.plan_tests(pair, repository_name)
    files = locator.assemble_tests(plan, public_directories(repository / "target_project"),
                                   output_dir=output)
    return {number: {"new_cases": cases, "test_files": files[number]}
            for number, cases in plan.items()}


def localize_batch(locator, repository_name, number, cases):
    """仅在当前批开始时映射用例并获取源语言入口和依赖。"""
    functions = locator.map_tests(repository_name, {number: cases})[number]
    if not functions:
        raise ValueError(f"{repository_name} 第 {number} 批映射未返回源函数")
    ranges = locator.extract_source(repository_name, functions)
    if not ranges.get("entry_units"):
        raise ValueError(f"{repository_name} 第 {number} 批源码获取未返回入口范围")
    return {"source_functions": functions, "source_ranges": ranges}
