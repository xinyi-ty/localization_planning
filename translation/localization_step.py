"""为当前批新增用例映射并提取源码。"""

from localization import Localization


def localize_batch(
    locator: Localization, repository_name: str, number: int, cases: list[dict],
) -> dict | None:
    """获取当前批的源码范围，正常空映射交由主流程决定如何验收。"""
    functions = locator.map_tests(repository_name, {number: cases})[number]
    if not functions:
        return None
    ranges = locator.extract_source(repository_name, functions)
    if not ranges.get("entry_units"):
        raise ValueError(f"{repository_name} 第 {number} 批源码获取未返回入口范围")
    return {"source_functions": functions, "source_ranges": ranges}
