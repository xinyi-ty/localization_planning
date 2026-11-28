"""维护默认构建配置，并为候选工程写入实际使用的配置。"""

import json
from pathlib import Path
import re

from localization.file_context import code_text


DEFAULT_RUST_LIBRARY = 'translation_target'
PYTHON_TEST_REQUIREMENTS = "pytest==9.2.0\n"


# 默认配置是工作区和系统提示词共用的原文。
BUILD_CONFIGS = {
    "Python": ("pytest.ini", """[pytest]
pythonpath = . src
addopts = -q -p no:cacheprovider
"""),
    "Java": ("pom.xml", """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>localization.translation</groupId>
  <artifactId>translation-target</artifactId>
  <version>1</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.10.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
        <configuration>
          <failIfNoTests>true</failIfNoTests>
          <includes>
            <include>**/public_tests/**/*.java</include>
            <include>**/public_test/**/*.java</include>
          </includes>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""),
    "C#": ("TargetTests.csproj", """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings>
    <IsTestProject>true</IsTestProject>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="src/**/*.cs;public_test/**/*.cs;public_tests/**/*.cs" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.7.2" />
    <PackageReference Include="xunit" Version="2.5.3" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.5.3" />
  </ItemGroup>
</Project>
"""),
    "C++": ("CMakeLists.txt", """cmake_minimum_required(VERSION 3.16)
project(translation_target LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(GTest CONFIG REQUIRED)
file(GLOB_RECURSE public_tests CONFIGURE_DEPENDS public_test/*.cpp public_tests/*.cpp public_test/*.cc public_tests/*.cc public_test/*.cxx public_tests/*.cxx)
file(GLOB_RECURSE production CONFIGURE_DEPENDS src/*.cpp src/*.cc src/*.cxx)
enable_testing()
add_executable(public_suite ${public_tests} ${production})
target_include_directories(public_suite PRIVATE . src include)
target_link_libraries(public_suite PRIVATE GTest::gtest_main)
add_test(NAME public_suite COMMAND public_suite --gtest_output=xml:${CMAKE_BINARY_DIR}/results.xml)
"""),
    "Rust": ("Cargo.toml", """[package]
name = "translation_target"
version = "0.0.0"
edition = "2021"
autotests = false

[[test]]
name = "public_batch"
path = "tests/public_batch.rs"
"""),
}


def rust_modules(cases: list[dict]) -> dict[str, str]:
    """用稳定的模块名把 Cargo 的报告标识对应回原测试文件。"""
    return {name: f"batch_{index}"
            for index, name in enumerate(sorted({case["filePath"] for case in cases}))}


def infer_rust_library(repository: str, test_roots: list[Path]) -> str:
    """根据全量公测中的仓库名绑定确定跨批次共用的 Rust 库名。"""
    imports: dict[str, set[Path]] = {}
    local_modules: set[str] = set()
    for root in test_roots:
        for file in sorted(root.rglob('*.rs')):
            if not file.is_file():
                continue
            source = code_text(file.read_text(encoding='utf-8'), 'Rust')
            local_modules.update(re.findall(r'\bmod\s+([A-Za-z_]\w*)\s*[{;]', source))
            for via_use, via_extern in re.findall(
                r'\b(?:use\s+(?:::)?([A-Za-z_]\w*)::|extern\s+crate\s+([A-Za-z_]\w*))', source,
            ):
                imports.setdefault(via_use or via_extern, set()).add(file)

    repo_names = {
        re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
        for name in (repository, repository.split('_', 1)[-1], DEFAULT_RUST_LIBRARY)
    }
    builtin_names = {'std', 'core', 'alloc', 'crate', 'self', 'super', 'test'}
    matches = {
        name for name in imports.keys() - local_modules - builtin_names
        if re.sub(r'_(?:rs|rust)$', '', name.lower()) in repo_names or name.lower() in repo_names
    }
    if len(matches) > 1:
        evidence = {name: [str(path) for path in sorted(imports[name])] for name in sorted(matches)}
        raise ValueError(f'无法唯一确认 Rust 库名，候选及来源：{evidence}')
    return next(iter(matches), DEFAULT_RUST_LIBRARY)


def prepare_target_test_config(
    language: str, cases: list[dict], project: Path, *, rust_library: str | None = None,
) -> dict[str, str]:
    """根据本批累计公测生成通用构建文件，返回翻译请求使用的同一份原文。"""
    filename, source = BUILD_CONFIGS[language]
    files = {filename: source}
    if language == 'Python':
        files['requirements.txt'] = PYTHON_TEST_REQUIREMENTS
    elif language == 'Rust':
        if rust_library and rust_library != DEFAULT_RUST_LIBRARY:
            files[filename] += f'\n[lib]\nname = {json.dumps(rust_library)}\npath = "src/lib.rs"\n'
        modules = rust_modules(cases)
        for name in modules:
            if not (project / name).is_file():
                raise ValueError(f'Rust 公测入口文件不在本批产物中：{name}')
        files['tests/public_batch.rs'] = ''.join(
            f'#[path = {json.dumps(Path(name).relative_to("tests").as_posix())}]\nmod {module};\n'
            for name, module in modules.items()
        )

    for name, content in files.items():
        destination = project / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x', encoding='utf-8', newline='') as stream:
            stream.write(content)
    return files
