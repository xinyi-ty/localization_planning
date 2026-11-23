"""共享原文件读取、编码和快照，保证各阶段使用同一份原文。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re
import tokenize

from .config import files_under


@dataclass(frozen=True)
class PublicFile:
    relative_path: str
    content_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class PublicBundle:
    """一次读取的公测文件及资源，后续分析和组装共用这份快照。"""

    root: Path
    files: tuple[PublicFile, ...]


class SourceText:
    """解码原文并保留原编码，供提示词读取和组装写回复用。"""

    def __init__(self, raw: bytes, python=False):
        # Python编码声明与UTF-8 BOM会影响解码和字节位置，统一在此处理。
        self.encoding = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if python else 'utf-8'
        self.text = raw.decode(self.encoding)


def public_bundle(root, roots):
    root = Path(root).resolve(strict=True)
    roots = [Path(p).resolve(strict=True) for p in roots]
    if not roots or any(not p.is_dir() or not p.is_relative_to(root) for p in roots):
        raise ValueError('公测目录不存在或越界')
    files = {}
    for public in roots:
        for path in files_under(public):
            raw = path.read_bytes()
            name = path.relative_to(root).as_posix()
            files[name] = PublicFile(name, raw, hashlib.sha256(raw).hexdigest())
    values = tuple(files[k] for k in sorted(files))
    return PublicBundle(root, values)


def source_snapshot(bundle):
    return {'root': str(bundle.root), 'files': {f.relative_path: f.sha256 for f in bundle.files}}


def create_snapshot(root, roots):
    """源工程与公测共用的快照结构，roots使用相对root的目录路径。"""
    root = Path(root).resolve(strict=True)
    roots = [Path(p).resolve(strict=True) for p in roots]
    if any(not p.is_dir() or not p.is_relative_to(root) for p in roots):
        raise ValueError('快照目录不存在或越界')
    return {'root': str(root), 'roots': [p.relative_to(root).as_posix() for p in roots],
            'files': {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for directory in roots for p in files_under(directory)}}


def snapshot_roots(snapshot):
    root = Path(snapshot['root']).resolve()
    return [(root / p).resolve() for p in snapshot.get('roots', ['.'])]


def check_snapshot(snapshot, *, roots=None):
    """核对原文件清单和哈希，发现增删或改写时终止当前操作。"""
    root = Path(snapshot['root'])
    roots = roots or snapshot_roots(snapshot)
    if any(not p.is_dir() or not p.is_relative_to(root) for p in roots):
        raise ValueError('SOURCE_CHANGED：快照目录不存在或越界')
    current = {p.relative_to(root).as_posix(): p for public in roots for p in files_under(public)}
    if set(current) != set(snapshot['files']):
        raise ValueError('SOURCE_CHANGED：输入文件清单变化')
    for name, digest in snapshot['files'].items():
        if hashlib.sha256(current[name].read_bytes()).hexdigest() != digest:
            raise ValueError('SOURCE_CHANGED：输入文件变化：' + name)


def prompt_files(analysis, paths):
    """分批与组装共用的完整原文视图，位置基于原工程。"""
    names = list(dict.fromkeys(paths))
    if any(name not in analysis['files'] or 'source' not in analysis['files'][name] for name in names):
        raise ValueError('提示词中的代码原文不属于当前分析快照')
    return [{'filePath': name, 'startLine': 1, 'source': analysis['files'][name]['source']} for name in names]


def python_statements(text):
    """用词法逻辑行保留多行导入和装饰器的原始范围。"""
    result, current = [], []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.NEWLINE:
            if current:
                result.append(current)
                current = []
        elif token.type not in {tokenize.COMMENT, tokenize.NL, tokenize.INDENT,
                                tokenize.DEDENT, tokenize.ENDMARKER}:
            current.append(token)
    return result


def code_text(source, language):
    """屏蔽注释和字符串但保留行号，供候选名称过滤与上游key检索使用。"""
    strings = r'"""[\s\S]*?"""|\x27\x27\x27[\s\S]*?\x27\x27\x27|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27'
    comments = r'\#[^\n]*' if language == 'Python' else r'//[^\n]*|/\*[\s\S]*?\*/'
    return re.sub(strings + '|' + comments, lambda match: re.sub(r'[^\n]', ' ', match[0]), source)
