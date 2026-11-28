"""读取本批原文、请求翻译并写入通过校验的业务源码。"""

import json
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError

from localization.codegraph import symbol_identity, symbol_key
from localization.file_context import SourceText
from localization.llm import _call_model, json_response

from .system_prompt import build_translation_messages
from .target_project import PublishedTranslation


# 输出范围与对应的测试构建配置一致。Python 模块可以位于根目录或包目录。
SOURCE_LAYOUTS = {
    "Python": {".": {".py"}},
    "Java": {"src/main/java": {".java"}},
    "C#": {"src": {".cs"}},
    "C++": {"src": {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"},
            "include": {".h", ".hpp", ".hh", ".hxx"}},
    "Rust": {"src": {".rs"}},
}


def source_blocks(source_root: Path, ranges: dict, language: str) -> list[dict]:
    """按文件、行号带出入口与项目内依赖原文，合并重叠片段。"""
    intervals = {}
    for row in [*ranges["entry_units"], *ranges["dependencies"]["project"]]:
        key = symbol_key(row)
        end = row["endLine"]
        if type(end) is not int or end < key["startLine"]:
            raise ValueError(f"源码行范围无效：{row}")
        intervals.setdefault(key["filePath"], []).append((key["startLine"], end))

    blocks = []
    for name, spans in sorted(intervals.items()):
        path = (source_root / name).resolve(strict=True)
        if not path.is_relative_to(source_root):
            raise ValueError(f"源码路径越过源工程：{name}")
        lines = SourceText(path.read_bytes(), language == "Python").text.splitlines(keepends=True)
        merged = []
        for first, last in sorted(spans):
            if last > len(lines):
                raise ValueError(f"源码行范围越界：{name}:{first}-{last}")
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        for first, last in merged:
            blocks.append({"filePath": name, "startLine": first, "endLine": last,
                           "source": "".join(lines[first - 1:last])})
    return blocks


def test_texts(
    project: Path, test_paths: list[Path], language: str, case_paths: set[str],
) -> dict[str, str]:
    """收集组装文本。资源已复制到工程，能解码的辅助文本也交给模型。"""
    source_extensions = {
        extension for layout in SOURCE_LAYOUTS.values() for suffixes in layout.values()
        for extension in suffixes
    }
    texts = {}
    for path in test_paths:
        name = path.relative_to(project).as_posix()
        raw = path.read_bytes()
        if path.suffix.lower() in source_extensions or name in case_paths:
            texts[name] = SourceText(raw, language == "Python" and path.suffix == ".py").text
        elif b"\0" not in raw:
            try:
                texts[name] = raw.decode("utf-8")
            except UnicodeDecodeError:
                # 二进制资源仍按原样保存在目标工程中。
                continue
    return texts


def validated_files(
    response: dict, project: Path, language: str, number: int,
    source_paths: set[str], entries: list[dict], target_files: dict[str, str], business_files: set[str],
) -> tuple[dict[Path, bytes], dict[str, list[str]], list[dict]]:
    """检查完整回复、文件范围及关联，再交给写入阶段。"""
    payload = json_response(response)
    if set(payload) != {"files", "missing", "source_to_target", "implemented"}:
        raise ValueError("翻译回复须且仅含 files、missing、source_to_target 和 implemented")
    missing, files = payload["missing"], payload["files"]
    if not isinstance(missing, list) or any(not isinstance(v, str) or not v.strip() for v in missing):
        raise ValueError("missing 必须是非空字符串组成的列表或空列表")
    if missing:
        raise ValueError("模型声明缺少实现：" + "、".join(missing))
    if not isinstance(files, dict):
        raise ValueError("files 须为目标路径到完整源代码的字典")

    produced = {}
    for name, source in files.items():
        path = PurePosixPath(name)
        allowed = any(path.is_relative_to(root) and path.suffix in suffixes
                      for root, suffixes in SOURCE_LAYOUTS[language].items())
        if (not allowed or path.is_absolute() or path.as_posix() != name
                or any(part.startswith(".") for part in path.parts)
                or any(char in name for char in "\\:\0")
                or {"public_test", "public_tests", "tests"}.intersection(path.parts[:-1])):
            raise ValueError(f"业务源码路径不符合 {language} 工程布局：{name!r}")
        target = project / path
        if (not target.resolve().is_relative_to(project)
                or target.is_symlink()
                or (target.exists() and (name not in target_files or not target.is_file()))
                or any(parent.is_file() for parent in target.parents if parent != project)):
            raise ValueError(f"业务源码路径与现有工程或本批写入范围冲突：{name!r}")
        # Python 包标记允许为空，首批须包含实际业务实现。
        empty_package = language == "Python" and path.name == "__init__.py"
        if not isinstance(source, str) or (not source.strip() and not empty_package):
            raise ValueError(f"业务源码为空或格式无效：{name!r}")
        produced[target] = source.encode("utf-8")
    if number == 1 and not any(source.strip() for source in files.values()):
        raise ValueError("首批未返回非空业务源码文件")
    if any(parent in produced for target in produced for parent in target.parents):
        raise ValueError("模型返回的业务路径同时用作文件和目录")

    relationships = payload["source_to_target"]
    if not isinstance(relationships, dict) or not relationships.keys() <= source_paths:
        raise ValueError("source_to_target 包含未提供的源文件")
    known_targets = business_files | files.keys()
    linked = set()
    for source, targets in relationships.items():
        if (not isinstance(targets, list) or not targets
                or any(not isinstance(target, str) or target not in known_targets for target in targets)
                or len(set(targets)) != len(targets)):
            raise ValueError(f"源文件关联的目标路径无效：{source}")
        linked.update(targets)
    if not files.keys() <= linked:
        raise ValueError(f"交付文件缺少源文件关联：{sorted(files.keys() - linked)}")

    implemented = payload["implemented"]
    if not isinstance(implemented, list):
        raise ValueError("implemented 须为源函数定位键列表")
    known_entries = {symbol_identity(key) for key in entries}
    confirmed = set()
    for item in implemented:
        key = symbol_key(item)
        identity = symbol_identity(key)
        if item != key or identity not in known_entries or identity in confirmed:
            raise ValueError(f"已实现项不是本批待实现的唯一源函数定位键：{item}")
        if key["filePath"] not in relationships:
            raise ValueError(f"已实现函数缺少目标文件关联：{item}")
        confirmed.add(identity)
    return produced, relationships, implemented


def translate_batch(
    batch: dict,
    project: Path,
    test_paths: list[Path],
    build_files: dict[str, str],
    logs: Path,
    previous: PublishedTranslation,
    target_dependencies: list[dict],
) -> dict:
    """按批次独立翻译，只允许续写已向模型提供原文的业务文件。"""
    source_language = batch["source_language"]
    target_language = batch["target_language"]
    case_paths = {row["filePath"] for row in batch["new_cases"]}
    ranges = batch["source_ranges"]
    blocks = source_blocks(batch["source_root"], ranges, source_language)
    source_paths = {block["filePath"] for block in blocks}
    entries = [key for key in batch["source_functions"]
               if symbol_identity(key) not in previous.implemented]
    reference_entries = [key for key in batch["source_functions"]
                         if symbol_identity(key) in previous.implemented]

    associations = {name: sorted(paths) for name, paths in previous.source_to_target.items()
                    if name in source_paths}
    related_paths = {name for paths in associations.values() for name in paths}
    related_paths.update(symbol["filePath"] for row in target_dependencies
                         for symbol in row["dependencies"])
    target_files = {name: SourceText((project / name).read_bytes(), target_language == "Python").text
                    for name in sorted(related_paths)}
    context = {
        "repository": batch["repository"],
        "batch": batch["batch"],
        "source_language": source_language,
        "target_language": target_language,
        "source_entries": entries,
        "previously_implemented_entries": reference_entries,
        "source_blocks": blocks,
        "external_dependencies": ranges["dependencies"]["external"],
        "new_cases": batch["new_cases"],
        "public_test_files": test_texts(project, test_paths, target_language, case_paths),
        "target_files": target_files,
        "target_dependencies": target_dependencies,
        "existing_paths": sorted(previous.files),
        "source_to_target": associations,
        "production_layout": {path: sorted(suffixes) for path, suffixes in SOURCE_LAYOUTS[target_language].items()},
    }
    messages = build_translation_messages(context, build_files)
    request = json.dumps(messages, ensure_ascii=False, indent=2).encode("utf-8")
    (logs / "translation_request.json").write_bytes(request)
    try:
        response = _call_model(messages)
    except OSError as exc:
        exc.add_note(f"翻译请求规模 {len(request)} 字节，完整请求见 translation_request.json")
        if isinstance(exc, HTTPError):
            try:
                (logs / "translation_response.txt").write_bytes(exc.read())
            except OSError as log_error:
                exc.add_note(f"无法保存模型错误响应：{log_error}")
        raise
    (logs / "translation_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    files, relationships, implemented = validated_files(
        response, project, target_language, batch["batch"], source_paths, entries,
        target_files, previous.files,
    )
    for path, raw in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "wb" if path.relative_to(project).as_posix() in target_files else "xb"
        with path.open(mode) as stream:
            stream.write(raw)
    return {"generated_files": [path.relative_to(project).as_posix() for path in files],
            "source_to_target": relationships, "implemented": implemented}
