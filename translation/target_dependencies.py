"""从本批候选工程查询新公测依赖的已有目标业务符号。"""

from collections import defaultdict, deque

from localization.codegraph import rebase_key, request_scope, symbol_identity, symbol_key
from localization.config import public_directories, SDK_LANGUAGES


@request_scope
def find_target_dependencies(graph, language, original_tests, project, cases, test_paths, business_files):
    """以组装后的实际位置查询，对外仍使用原始公测的三元键。"""
    graph.prepare(project, SDK_LANGUAGES[language])
    test_files = {path.relative_to(project).as_posix() for path in test_paths}
    nodes = defaultdict(list)
    functions = graph.call("getAllFunctions", project)
    if not isinstance(functions, list):
        raise ValueError("目标函数清单不是列表")
    for row in functions:
        key = symbol_key(row)
        if key["filePath"] in test_files:
            nodes[key["filePath"], key["name"]].append(key)

    dependencies = []
    for case in cases:
        candidates = nodes[case["filePath"], case["name"]]
        if len(candidates) > 1:
            # 组装可能改变行号。同文件同名时按原始声明内容区分。
            origin = next(root for root in public_directories(original_tests)
                          if (original_tests / case["filePath"]).is_relative_to(root))
            original_key = rebase_key(case, original_tests, origin)
            original_source = graph.content(origin, node=original_key)["source"].strip()
            candidates = [key for key in candidates
                          if graph.content(project, node=key)["source"].strip() == original_source]
            if len(candidates) > 1:
                original_container = graph.location(origin, original_key)["container"]
                original_name = original_container["name"] if original_container else None
                scoped = []
                for key in candidates:
                    container = graph.location(project, key)["container"]
                    if (container["name"] if container else None) == original_name:
                        scoped.append(key)
                candidates = scoped
        if len(candidates) != 1:
            raise ValueError(f"无法唯一定位组装后的公测：{case}，候选：{candidates}")

        confirmed, visited = {}, set()
        queue = deque(candidates)
        while queue:
            key = queue.popleft()
            identity = symbol_identity(key)
            if identity in visited:
                continue
            visited.add(identity)
            # 继续追踪公测辅助代码，到达已交付的业务定义时记录该依赖。
            calls = graph.callees(project, key)["functions"]
            if not isinstance(calls, list):
                raise ValueError(f"目标调用关系不是列表：{key}")
            for call in calls:
                callee = symbol_key(call)
                path = callee["filePath"]
                if path in business_files:
                    confirmed[symbol_identity(callee)] = callee
                elif path in test_files:
                    queue.append(callee)
        # 上游仅提供已解析的边，空列表表示未确认到已有目标依赖。
        dependencies.append({"case": case, "dependencies": list(confirmed.values())})
    return dependencies
