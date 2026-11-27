"""读取首批原文、请求翻译并写入通过校验的业务源码。"""

import json
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError

from localization.codegraph import symbol_key
from localization.file_context import SourceText
from localization.llm import _call_model, json_response

from .system_prompt import build_translation_messages


# 输出范围与对应的测试构建配置一致。Python 模块可以位于根目录或包目录。
SOURCE_LAYOUTS = {
    "Python": {".": {".py"}},
    "Java": {"src/main/java": {".java"}},
    "C#": {"src": {".cs"}},
    "C++": {"src": {".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"},
            "include": {".h", ".hpp", ".hh", ".hxx"}},
    "Rust": {"src": {".rs"}},
}


def source_blocks(source_root, ranges, language):
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


def test_texts(project, test_paths, language, case_paths):
    """收集组装文本。资源已复制到工程，能解码的辅助文本也交给模型。"""
    texts = {}
    for path in test_paths:
        name = path.relative_to(project).as_posix()
        raw = path.read_bytes()
        source_file = path.suffix.lower() in {
            extension for layout in SOURCE_LAYOUTS.values() for suffixes in layout.values()
            for extension in suffixes
        }
        if source_file or name in case_paths:
            texts[name] = SourceText(raw, language == "Python" and path.suffix == ".py").text
        elif b"\0" not in raw:
            try:
                texts[name] = raw.decode("utf-8")
            except UnicodeDecodeError:
                # 二进制资源仍按原样保存在目标工程中。
                continue
    return texts


def validated_files(response, project, language):
    """核对整个模型回复，再把内容交给写入阶段。"""
    payload = json_response(response)
    if set(payload) != {"files", "missing"}:
        raise ValueError("翻译回复必须包含且仅包含 files 和 missing")
    missing, files = payload["missing"], payload["files"]
    if not isinstance(missing, list) or any(not isinstance(v, str) or not v.strip() for v in missing):
        raise ValueError("missing 必须是非空字符串组成的列表或空列表")
    if missing:
        raise ValueError("模型声明缺少实现：" + "、".join(missing))
    if not isinstance(files, dict) or not files:
        raise ValueError("模型未返回业务源码文件")

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
        if (not target.resolve().is_relative_to(project) or target.exists()
                or any(parent.is_file() for parent in target.parents if parent != project)):
            raise ValueError(f"业务源码路径与现有工程冲突：{name!r}")
        # Python 包标记允许为空，整批仍须交付实际实现。
        empty_package = language == "Python" and path.name == "__init__.py"
        if not isinstance(source, str) or (not source.strip() and not empty_package):
            raise ValueError(f"业务源码为空或格式无效：{name!r}")
        produced[target] = source.encode("utf-8")
    if not any(source.strip() for source in files.values()):
        raise ValueError("模型未返回非空业务源码文件")
    if any(parent in produced for target in produced for parent in target.parents):
        raise ValueError("模型返回的业务路径同时用作文件和目录")
    return produced


def translate_batch(localization, project, test_paths, build_files, logs):
    """使用首批定位与组装结果完成一次翻译，返回写入的相对路径。"""
    source_language, target_language = localization["translation_pair"].split("_to_")
    batch = localization["batches"][1]
    case_paths = {row["filePath"] for row in batch["new_cases"]}
    ranges = batch["source_ranges"]
    context = {
        "repository": localization["repository"],
        "batch": 1,
        "source_language": source_language,
        "target_language": target_language,
        "source_entries": batch["source_functions"],
        "source_blocks": source_blocks(Path(localization["source_root"]), ranges, source_language),
        "external_dependencies": ranges["dependencies"]["external"],
        "new_cases": batch["new_cases"],
        "public_test_files": test_texts(project, test_paths, target_language, case_paths),
        "production_layout": {path: sorted(suffixes) for path, suffixes in SOURCE_LAYOUTS[target_language].items()},
    }
    messages = build_translation_messages(context, build_files)
    (logs / "translation_request.json").write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        response = _call_model(messages)
    except HTTPError as exc:
        (logs / "translation_response.txt").write_bytes(exc.read())
        raise
    (logs / "translation_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")

    files = validated_files(response, project, target_language)
    for path, raw in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(raw)
    return [path.relative_to(project).as_posix() for path in files]
