"""准备目标候选工程，并发布通过累计公测的结果。"""

from dataclasses import dataclass, field
from pathlib import Path
import shutil

from localization.codegraph import SymbolIdentity, symbol_identity


@dataclass
class PublishedTranslation:
    """当前正式工程已交付的业务文件、入口与文件关联。"""

    files: set[str] = field(default_factory=set)
    implemented: set[SymbolIdentity] = field(default_factory=set)
    source_to_target: dict[str, set[str]] = field(default_factory=dict)

    def record(self, result: dict) -> None:
        """更新已经通过公测并发布的本批交付。"""
        self.files.update(result["generated_files"])
        self.implemented.update(symbol_identity(key) for key in result["implemented"])
        for path, targets in result["source_to_target"].items():
            self.source_to_target.setdefault(path, set()).update(targets)


def prepare_candidate(
    assembled: Path,
    assembled_tests: list[str],
    candidate: Path,
    project: Path,
    business_files: set[str],
) -> list[Path]:
    """复制累计公测与资源，并加入前批已交付的业务文件。"""
    if not assembled_tests:
        raise ValueError("组装未提供本批公测文件")
    for filename in assembled_tests:
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
    return test_paths


def publish_project(candidate: Path, project: Path, previous: Path) -> None:
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
