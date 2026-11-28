#!/usr/bin/env bash
set -euo pipefail

language=$1
shift

run() {
    local phase=$1 status
    shift
    printf '\n[%s] %s\n' "$phase" "$*"
    "$@" || {
        status=$?
        printf '[%s] 命令失败，退出码 %s\n' "$phase" "$status" >&2
        exit "$status"
    }
}

# 工程作为只读输入，构建产物与缓存随容器退出清理。
run "准备测试工程" cp -a /target_project/. /workspace/
case "$language" in
    Python)
        run "公测收集" python -B -m pytest --collect-only "$@"
        run "公测执行" python -B -m pytest -o junit_family=xunit1 --junitxml=.public-results.xml "$@"
        run "保存公测报告" cp .public-results.xml /test_logs/
        ;;
    Java)
        run "编译" mvn -B -ntp test-compile
        run "公测执行" mvn -B -ntp surefire:test
        run "保存公测报告" cp --parents target/surefire-reports/TEST-*.xml /test_logs/
        ;;
    'C#')
        run "编译" dotnet build TargetTests.csproj
        run "公测执行" dotnet test TargetTests.csproj --no-build \
            --logger 'trx;LogFileName=results.trx' --results-directory .public-results
        run "保存公测报告" cp --parents .public-results/results.trx /test_logs/
        ;;
    'C++')
        run "构建配置" cmake -S . -B build
        run "编译" cmake --build build --parallel 2
        run "公测执行" ctest --test-dir build --output-on-failure
        run "保存公测报告" cp --parents build/results.xml /test_logs/
        ;;
    Rust)
        run "编译" cargo test --offline --test public_batch --no-run
        run "公测执行" cargo test --offline --test public_batch -- --test-threads=1
        ;;
    *)
        printf '不支持的目标语言：%s\n' "$language" >&2
        exit 2
        ;;
esac
