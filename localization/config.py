"""共享配置读取、语言路由与数据集位置。"""
import os
from pathlib import Path

import yaml

ALIASES = {"python": "Python", "java": "Java", "c#": "C#", "csharp": "C#",
           "c++": "C++", "cpp": "C++", "rust": "Rust"}
SDK_LANGUAGES = {"Python": "python", "Java": "java", "C#": "csharp", "C++": "cpp", "Rust": "rust"}
PAIRS = {"C#_to_Java", "C++_to_Python", "Java_to_C#", "Java_to_Python",
         "Python_to_C++", "Python_to_Java", "Python_to_Rust"}
# 公测发现和资源复制采用同一排除范围，避免把索引、环境和构建产物带入批次。
SKIP = {".git", ".codegraph", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "target", "bin", "obj"}


def load_config(path=None):
    """默认读取项目根目录的配置，调用方也可显式指定文件路径。"""
    config_path = Path(__file__).resolve().parents[1] / "default.yaml" if path is None else Path(path)
    return yaml.safe_load(config_path.read_text(encoding="utf-8-sig"))


def language(value):
    if not isinstance(value, str) or value.lower() not in ALIASES:
        raise ValueError("未知语言：" + str(value))
    return ALIASES[value.lower()]


def pair_name(value):
    if not isinstance(value, str) or value.lower().count("_to_") != 1:
        raise ValueError("翻译对格式应为Java_to_Python等")
    source, target = value.lower().split("_to_")
    pair = language(source) + "_to_" + language(target)
    if pair not in PAIRS:
        raise ValueError("不支持的翻译对：" + pair)
    return pair



def repository(root, pair, name):
    if not isinstance(name, str) or not name.strip() or name in {".", ".."} or any(c in name for c in "/\\:\0"):
        raise ValueError("仓库名必须是单个目录名")
    base = (root / pair).resolve()
    result = (base / name).resolve()
    if result.parent != base or not result.is_relative_to(root) or not result.is_dir():
        raise ValueError("仓库不存在或路径越界：" + str(result))
    return result


def public_directories(target):
    """保留Java包目录和Rust tests/public_tests，不进入其他测试文件。"""
    roots = []
    for current, dirs, _ in os.walk(target, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (here / d).is_symlink())
        if here.name in {"public_test", "public_tests"}:
            roots.append(here)
            dirs[:] = []
    if not roots:
        raise ValueError("未找到public_test或public_tests目录：" + str(target))
    return roots


def files_under(root):
    paths = []
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (here / d).is_symlink())
        for name in sorted(names):
            path = here / name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("输入文件链接不受支持：" + str(path))
            if path.suffix in {".pyc", ".pyo", ".log"}:
                continue
            paths.append(path)
    return paths
