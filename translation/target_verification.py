"""执行 Docker 公测，核对报告中的用例和实例状态。"""

from collections.abc import Iterator
from pathlib import Path
import re
import shlex
import subprocess
import xml.etree.ElementTree as ET

from localization.codegraph import CodeGraph, request_scope
from localization.public_test_analysis import indexed_test_key

from .build_config import rust_modules

TestIdentity = tuple[str, str]


@request_scope
def expected_tests(language: str, cases: list[dict], original_tests: Path, graph: CodeGraph) -> set[TestIdentity]:
    """使用原文件行号读取测试标识，报告按框架的展示规则比对。"""
    modules = rust_modules(cases) if language == "Rust" else {}
    expected = set()
    for case in cases:
        file, name, line = case["filePath"], case["name"], case["startLine"]
        if language == "Python":
            key = file, name
        elif language == "Rust":
            key = modules[file], name
        elif language == "C++":
            _, node = indexed_test_key(graph, original_tests, case)
            identity = node["testIdentity"]
            key = identity["suite"], identity["name"]
        else:
            original = (original_tests / file).read_text(encoding="utf-8-sig")
            lines = original.splitlines()
            if language == "Java":
                package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", original)
                classname = Path(file).stem
                key = (f"{package[1]}.{classname}" if package else classname), name
            else:
                prefix = "\n".join(lines[:line])
                namespaces = re.findall(r"(?m)^\s*namespace\s+([\w.]+)\s*[;{]", prefix)
                classes = re.findall(r"\bclass\s+(\w+)", prefix)
                if not classes:
                    raise ValueError(f"无法从原公测识别 .NET 测试类：{file}:{line}")
                key = ".".join([*namespaces, classes[-1]]), name
        if key in expected:
            raise ValueError(f"测试报告无法区分同名累计用例：{file}:{line} {key}")
        expected.add(key)
    if not expected:
        raise ValueError("累计预期用例为空")
    return expected


def normalized_name(name: str) -> str:
    """去掉报告中的参数文本或参数实例编号。"""
    return re.split(r"[\[(]", name, maxsplit=1)[0].strip()


def xml_results(language: str, reports: list[Path], project: Path) -> Iterator[tuple[TestIdentity, bool]]:
    """保留报告中每个实例的状态，确保跳过、失败和额外用例都可见。"""
    for report in reports:
        root = ET.parse(report).getroot()
        if language == "C#":
            definitions = {node.attrib["id"]: node.find("{*}TestMethod").attrib
                           for node in root.findall(".//{*}UnitTest")}
            for row in root.findall(".//{*}UnitTestResult"):
                method = definitions[row.attrib["testId"]]
                key = method["className"].split(",", 1)[0], normalized_name(method["name"])
                yield key, row.attrib["outcome"] == "Passed"
            continue

        for row in root.iter("testcase"):
            name = row.attrib["name"]
            if language == "Python":
                path = Path(row.attrib["file"])
                if path.is_absolute():
                    path = path.relative_to("/workspace") if path.is_relative_to("/workspace") else path.relative_to(project)
                key = path.as_posix(), normalized_name(name)
            elif language == "Java":
                key = row.attrib["classname"], normalized_name(name)
            else:
                suite = row.attrib["classname"].split("/")
                # 参数化套件展示为 Prefix/Suite，也可能在末尾附加类型编号。
                suite_name = suite[-2] if suite[-1].isdigit() and len(suite) > 1 else suite[-1]
                key = suite_name, name.split("/", 1)[0]
            passed = (not any(row.find(status) is not None for status in ("failure", "error", "skipped"))
                      and row.attrib.get("status") not in {"notrun", "disabled"}
                      and row.attrib.get("result") not in {"skipped", "suppressed"})
            yield key, passed


def run_target_tests(
    language: str,
    cases: list[dict],
    original_tests: Path,
    project: Path,
    logs: Path,
    graph: CodeGraph,
) -> dict:
    """在统一镜像运行本批累计公测，并按原用例身份核对报告。"""
    expected = expected_tests(language, cases, original_tests, graph)
    script = Path(__file__).resolve().parents[1] / "docker" / "run_tests.sh"
    log = logs / "tests.log"
    case_files = sorted({case["filePath"] for case in cases})
    command = [
        "docker", "run", "--rm", "--pull", "never",
        "--mount", f"type=bind,source={project},target=/target_project,readonly",
        "--mount", f"type=bind,source={logs},target=/test_logs",
        "--mount", f"type=bind,source={script},target=/run_tests.sh,readonly",
        "--workdir", "/workspace", "localization-test:latest",
        "bash", "/run_tests.sh", language, *case_files,
    ]
    with log.open("w", encoding="utf-8") as stream:
        stream.write(f"$ {shlex.join(command)}\n")
        stream.flush()
        try:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=240, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"容器公测命令失败，退出码 {exc.returncode}，详见 {log}") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"容器公测未完成，详见 {log}") from exc

    if language == "Python":
        reports = [logs / ".public-results.xml"]
    elif language == "Java":
        reports = sorted((logs / "target/surefire-reports").glob("TEST-*.xml"))
    elif language == "C#":
        reports = [logs / ".public-results/results.trx"]
    elif language == "C++":
        reports = [logs / "build/results.xml"]
    else:
        output = log.read_text(encoding="utf-8", errors="replace")
        results = [
            ((name.split("::", 1)[0], name.rsplit("::", 1)[-1]), status == "ok")
            for name, status in re.findall(r"(?m)^test (\S+) \.\.\. (ok|FAILED|ignored)\b", output)
        ]
        counts = re.findall(r"(?m)^running (\d+) tests?$", output)
        if len(counts) != 1 or int(counts[0]) != len(results):
            raise RuntimeError(f"Cargo 公测报告缺失或不完整，详见 {log}")

    if language != "Rust":
        if not reports or any(not path.is_file() for path in reports):
            raise RuntimeError(f"公测报告缺失，详见 {log}")
        results = list(xml_results(language, reports, project))
    observed = {key for key, _ in results}
    failed = {key for key, passed in results if not passed}
    if observed != expected or failed:
        raise RuntimeError(f"公测报告未通过：缺少 {sorted(expected - observed)}，"
                           f"额外 {sorted(observed - expected)}，失败或跳过 {sorted(failed)}，详见 {log}")
    return {"passed_cases": sorted(observed), "passed_instances": len(results)}
