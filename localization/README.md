# 定位工具

提供测试分批、累计测试文件组装、测试到源函数映射、源码范围与依赖定位

## 接口调用说明

### 准备与配置

使用 Python 3.11 及以上版本，在项目根目录安装定位模块。
CodeGraph HTTP 服务通过独立部署提供

```bash
python3 -m venv .venv
source .venv/bin/activate
# Blackwell GPU 服务器先安装 CUDA 12.8 构建
python -m pip install "torch==2.7.1+cu128" --extra-index-url https://download.pytorch.org/whl/cu128
python -m pip install -e .
```

将 `default.yaml` 的 `codegraph.base_url` 填为实际启动的上游HTTP地址，客户端和服务需要能够访问同一份工程文件及组装输出目录

数据集按 `数据集根目录/翻译对/仓库名/source_project`、`target_project` 存放，公测放在目标工程内的 `public_test` 或 `public_tests` 目录中，可位于子目录

支持 `C#_to_Java`、`C++_to_Python`、`Java_to_C#`、`Java_to_Python`、`Python_to_C++`、`Python_to_Java`、`Python_to_Rust`

### 共享配置

首次部署时从配置样例创建本机配置，再填写数据集、模型目录和 API 参数

~~~bash
cp -n default.example.yaml default.yaml
cp -n .env.example .env
~~~

在仓库根目录的 `default.yaml` 中配置数据集、源语言、目标语言、仓库、输出目录和上游服务。服务器上的 `dataset.path` 使用项目下 `dataset/` 的绝对路径

```yaml
dataset:
  path: /path/to/dataset
  source_lang: Java
  target_lang: Python
  repository: aventrix_jnanoid
output_dir: translation/output/rounds
codegraph:
  base_url: http://127.0.0.1:3000
  timeout_seconds: 120
```

正式入口读取仓库、翻译对和运行目录。分批覆盖全部公测，组装、映射、源码获取、翻译和 Docker 公测只执行首批。相对输出路径以启动目录为基准，运行目录须尚未存在

在项目根目录启动，运行前确认模型服务、CodeGraph 和 Docker 可用

```bash
.venv/bin/python -m translation.main
```

首批项目构建配置保存在 `translation/system_prompt.py`。Python 目标工程会生成 `requirements.txt`，声明测试依赖。程序把实际使用的配置写入目标工程，并将同一份原文放入翻译系统提示词。仓库需要额外依赖或特定构建参数时，可在 `default.yaml` 中通过 `test_config_path` 指向自行编写的配置

首次部署或修改工具链后，在项目根目录构建统一的本地测试镜像

```bash
bash docker/build_image.sh
```

`docker/Dockerfile` 在同一个镜像中安装五种目标语言的测试工具链，并在构建时检查工具版本。Dockerfile 直接安装 pytest，构建和测试使用固定的 `localization-test:latest` 标签。测试时复用镜像，容器退出后自动删除

运行结果写到 `workspace/<翻译对>/<仓库>/target_project/`，首批请求、模型回复、测试日志和摘要写到 `logs/<翻译对>/<仓库>/batch_001/`。失败时命令返回非零并保留已产生的文件

### 功能调用

分批由 LLM 根据用例清单和完整公测原文完成，调用或结果校验失败时记录原因并报错

组装由 LLM 选择要保留的原文范围，程序按范围复制并校验，每轮校验失败时最多由模型修正一次

独立调用时读取同一份 `default.yaml`，示例中的仓库名 `sample` 和源函数名 `run` 应替换为实际名称

```python
from localization import Localization
from localization.config import load_config

config = load_config()
locator = Localization(config)

# 分批接口，获取每批新增测试用例
batches = locator.plan_tests("Java_to_Python", "sample")

# 组装，使用分批接口结果生成各轮累计测试文件
rounds = locator.assemble_tests(
    batches,
    ["/path/to/dataset/Java_to_Python/sample/target_project/public_tests"],
    output_dir="/tmp/localization_sample_rounds",
)

# 源码获取接口，按源函数三元 key 查找所属类和依赖
source = locator.extract_source("sample", [{"name": "run", "filePath": "App.java", "startLine": 3}])

# 映射，经原语言测试查找候选业务函数并由 LLM 核验
matches = locator.map_tests("sample", batches)
```

| 调用 | 输入 | 返回值 |
| --- | --- | --- |
| `plan_tests(translation_pair, repository_name)` | 翻译对、仓库目录名 | `{批次编号: 本批新增测试用例列表}` |
| `assemble_tests(batches, test_directories, *, output_dir=None)` | 分批接口结果、一个或多个原公测目录、输出目录 | `{批次编号: 本轮累计测试文件绝对路径列表}` |
| `extract_source(repository_name, functions)` | 仓库目录名、源函数三元key列表 | 入口源码范围、项目内依赖范围、外部依赖记录 |
| `map_tests(repository_name, batches)` | 仓库名、分批接口返回的批次字典 | `{批次编号: 已核验并去重的源函数三元key列表}` |

`plan_tests` 的批次可交给组装和映射。映射内部完成LLM核验，输出的非空列表可直接交给 `extract_source`，`name` 使用上游原名，`filePath` 相对于源工程，`startLine` 为原始定义起始行

```python
for batch_no, functions in matches.items():
    if functions:
        source = locator.extract_source("sample", functions)
```

同一 `locator` 按仓库复用原公测分析和源函数记录，调用 `plan_tests` 开始新的分析期。分批接口确定最终批次，组装与映射校验引用并沿用输入批次。复用前核对快照，原文件变化后重新规划或创建新的实例

启用LLM时，在仓库根目录 `.env` 配置模型

```dotenv
LLM_ENDPOINT=https://your-provider.example/v1/chat/completions
LLM_MODEL=your-model
LLM_API_KEY=your-key
LLM_TIMEOUT_SECONDS=120
LLM_TEMPERATURE=0
LLM_MAX_OUTPUT_TOKENS=2048
```

以上六项为必填，`LLM_THINKING` 可选。分批、组装和映射核验共用该模型配置

## 本地测试到源码映射

默认使用官方 [jina-code-embeddings-1.5b](https://huggingface.co/jinaai/jina-code-embeddings-1.5b)，下载版本见 `default.yaml` 的 `mapping.model_revision`，许可为 CC BY-NC 4.0

在 `default.yaml` 的 `mapping` 中管理模型目录、版本、检索前缀和编码长度。模型从服务器现有权重目录加载

程序自动选择 CUDA 或 CPU，模型按实例复用。长代码按 `mapping.max_length` 分块，同一代码记录的多个分块按最高余弦相似度合并

每条目标语言用例先用 Jina 召回最相近的原语言测试。原测试导向不同业务候选时，由 LLM 复核所覆盖的行为，可选择多条。程序沿这些测试及辅助代码的调用关系查找业务函数，合并去重后将全部候选交给 LLM 核验

随后按用例调用 LLM。模型读取目标测试及辅助上下文、候选函数三元 key、限定名、原文和可确认的继承声明，选择目标测试明确验证了其行为的候选。模型可返回多个候选编号或 `None`，程序按批次汇总已确认的三元 key 并去重

输出示例

```python
{
    1: [{"name": "add", "filePath": "App.java", "startLine": 2}],
    2: [],
}
```

日志记录匹配到的原语言测试及其相似度，以及供核验的业务候选。没有可识别的原语言测试、没有候选业务函数、核验失败或模型弃权时，该用例贡献空列表。整批无确认函数时返回空列表，调用方检查后再进行源码获取。运行期间源码或公测变化会报错，由调用方重新运行

权重存放在代码仓库外，项目内的 `models/` 已加入 `.gitignore`

```text
Localization.map_tests(repository_name, batches)
  │
  └─ mapping.map_tests(...)
       ├─ service._test_analysis(...) → finalize_batches(...)
       │    取得共享公测分析，校验批次用例引用
       ├─ service._source_analysis(...) → source_tests(...)
       │    取得整个源工程的函数记录，从中识别原语言测试
       ├─ test_context(...) → JinaEmbeddings.encode(...)
       │    取得用例及辅助代码，编码目标测试和原语言测试
       ├─ 逐批对新增用例调用 map_case(...)
       │    → rank_candidates(...) 召回相近的原语言测试
       │    → source_business_calls(...) 查找业务函数，补查明确的 Python 对象绑定
       │    → verify_source_tests(...) 在业务候选不同时核验原测试
       │    → 合并去重，保留全部候选业务函数
       │    → verify_candidates(...) 核验用例实际验证了哪些函数
       └─ check_snapshot(...)
            核对公测和源码快照，返回各批次已核验并去重的源函数三元key
```

## 共享测试分析

`analyze_tests(test_directories, language, graph)` 接收一个或多个公测目录、目标语言和上游HTTP客户端，返回同一文件快照上的分析字典

| 输出项 | 内容 |
| --- | --- |
| `cases` | 由上游函数节点和测试标记识别的用例、源码位置及所属类型 |
| `units` | 上游已索引的函数和方法原文、行范围及测试候选标记 |
| `files` | 源码文件原文、编码及资源内容 |
| `snapshot` | 输入文件快照 |

分批使用用例清单、目标语言和完整公测原文。组装和映射按需查询公测调用关系，复用同一快照上的结果

| 内部数据 | 统一结构 |
| --- | --- |
| 符号引用 | `{name, filePath, startLine}`，测试路径相对于目标工程，源码路径相对于源工程，调用上游时按索引根目录转换 |
| 代码范围 | `{filePath, startLine, endLine, startByte, endByte}`，行号从1开始且包含结束行，字节范围为原文件编码下的左闭右开区间 |
| 原始文件 | `{相对路径: {source, encoding}}`，资源文件使用content保存原始字节的Base64 |
| 生成文件 | `{相对路径: 完整源码字符串}` |
| 源函数记录 | `{key: 三元key, source: 原文, node: 上游原生节点}` |
| 快照 | `{root: 绝对根目录, roots: 相对目录列表, files: 相对路径到SHA-256的字典}` |
| 编码结果 | `{vectors: 向量张量, owners: 对应输入记录的下标列表}` |

用例key定位原始定义，unit给出包含装饰器的裁剪范围。生成文件按所属类型、用例名、原文及出现次数关联原用例。约束字典保存规则，原始分析、快照及上游客户端作为程序参数传递

## 分批接口

输出示例

```python
{
    1: [{"filePath": "public_tests/test_service.py", "name": "test_basic", "startLine": 4}],
    2: [{"filePath": "public_tests/test_service.py", "name": "test_combined", "startLine": 8}],
}
```

路径相对于 `target_project`，批次从1连续编号，每批非空，每个用例只出现一次

```text
Localization.plan_tests(translation_pair, repository_name)
  ├─ _project(...) → _test_analysis(...)
  │    读取原公测，复用基础用例清单、位置和原文快照
  └─ test_plan.plan_tests(analysis)
       ├─ 构造提示词，传入目标语言、用例清单和完整公测原文
       ├─ llm._call_model(...) → llm.json_response(...)
       │    获取模型给出的每批新增用例
       └─ _validate_batches(...)
            校验用例身份、覆盖、重复和编号，失败时记录原因并报错
```

## 组装：累计测试文件

返回示例

```python
{
    1: ["/tmp/localization_sample_rounds/round_001/target_project/public_tests/test_service.py"],
    2: ["/tmp/localization_sample_rounds/round_002/target_project/public_tests/test_service.py"],
}
```

传入本轮累计用例、本轮新增用例、上一轮已验收的文件和带行号的公测原文。沿上游已解析的调用关系补充必须保留的辅助定义

模型选择原文范围的输出示例

```json
{
  "public_tests/test_service.py": [
    {"line": 1, "bindings": ["BasicService"]},
    [3, 6]
  ]
}
```

行号从 1 开始且包含结束行，范围按原文件顺序排列，彼此不重叠。Python 的简单单行导入可以选择原有绑定，例如从 `from app import BasicService, AdvancedService` 中保留 `BasicService`。其他代码由程序从原文复制，资源文件按原字节复制

```text
Localization.assemble_tests(...)
  ├─ _test_analysis(...) / analyze_tests(...)  取得共享公测分析
  └─ assembly.assemble_tests(...)
       ├─ finalize_batches(...)
       │    校验输入批次和用例引用
       ├─ 逐批累计
       │    ├─ prepare_round(...)
       │    │    准备累计用例、上轮文件、公测内容和校验约束
       │    ├─ llm_assemble(...)
       │    │    LLM 选择原文范围，调用 render_ranges(...) 复制原文
       │    └─ validate_and_write(...)
       │         统一校验并写入暂存目录
       │         校验失败时反馈原因并修正一次
       └─ check_snapshot(...) → os.rename(...)
             全部轮次通过后统一发布，返回各轮路径
```

| 小接口 | 输入 | 输出 |
| --- | --- | --- |
| `prepare_round` | 批次字典、当前轮次、上一轮实际文件、共享分析、上游客户端 | `{context, constraints}` |
| `llm_assemble` | 本轮阶段数据、共享分析，可选的校验失败原因 | `{相对路径: 完整源码}` |
| `validate_and_write` | 本轮阶段数据、候选文件、共享分析、上游客户端、暂存目录 | 本轮累计测试文件路径列表 |

校验累计用例、上游已确认的辅助定义、测试正文、原始路径、Python 编译指令与导入以及原文顺序。修正后仍未通过时，组装记录原因并报错。实际编译、框架收集和运行由翻译后的测试阶段验证

`test_directories` 使用绝对路径，可传单个目录或彼此独立的目录列表，批次须覆盖这些目录的全部用例

`output_dir` 须为尚未存在的绝对路径，与原公测目录相互独立，省略时生成临时输出目录，由调用方使用完后清理

返回列表包含本轮累计用例所在文件，辅助文件与资源位于同轮目录，运行时将该轮公测目录覆盖到目标工程副本，使用配套业务实现、构建配置和依赖执行测试

## 源码获取接口

返回示例

```python
{
    "entry_units": [
        {"filePath": "Service.java", "name": "Service", "startLine": 1, "endLine": 20},
    ],
    "dependencies": {
        "project": [
            {"filePath": "Repository.java", "name": "Repository", "startLine": 1, "endLine": 12},
        ],
        "external": [],
    },
}
```

`entry_units` 为入口所属类或独立函数的原始行号范围，`dependencies.project` 为经引用核验的项目内依赖单元及导入上下文，`dependencies.external` 为按导入语句及项目文件名整理的外部依赖候选。复杂导入归属与未解析引用写入日志

路径相对于 `source_project`，行号从1开始且包含结束行，跨文件实现及必要导入可能对应多个片段，调用方按范围读取原文

三元key按名称、文件和开始行精确定位。参数错误、入口缺失、歧义及预期提取失败会记录原因并返回 `{}`，调用方须检查空结果

```text
Localization.extract_source(...)
  └─ source_extract.extract_source(...)
       ├─ service._source_analysis(...)
       │    取得当前快照的共享源函数记录
       ├─ _resolve_entries(...)
       │    按三元 key 匹配入口，查询所属类，归并同类入口
       ├─ 循环处理源码单元
       │    → source_ranges(...) 定位完整源码范围及跨文件片段
       │    → graph.callees(...) / graph.call("getDependencies", ...) 查询调用和类型依赖
       │    → source_dependencies.collect_file_dependencies(...) / collect_python_dependencies(...)
       │      从文件原文补齐依赖、导入和初始化
       │    → 将已确认的依赖加入队列
       └─ 汇总并去重源码范围，经 checked_file(...) 和 check_snapshot(...) 核验后返回
```

本地通过上游HTTP读取节点、所属类、源码及直接依赖，普通源码单元使用节点坐标，C# partial和C++类外实现按getContent合并原文恢复各片段位置。候选文件中的定义只有获得当前源码单元的引用证据才继续提取，注释、字符串和抽象方法声明不作为调用证据。依赖完整性取决于上游索引覆盖，合并片段无法唯一定位时记录失败并返回空字典

## 启动首批冒烟测试

冒烟测试对一个样例仓库生成全部测试批次，再执行首批组装、映射、源码获取、翻译和运行公测

1. 按上文完成虚拟环境、`default.yaml` 和 `.env` 配置。在一个终端启动上游 CodeGraph。若服务已在 `codegraph.base_url` 指定的地址运行，直接执行下一步

   ```bash
   cd /home/srp/SRP/localization_planning/codegraph_api
   node --version
   npm start
   ```

   上游使用 Node.js 24。默认监听 `http://127.0.0.1:3000`，需与 `default.yaml` 中的服务地址一致

2. 在另一个终端回到项目根目录，运行指定翻译对的样例

   ```bash
   cd /home/srp/SRP/localization_planning
   .venv/bin/python -B -m translation.smoke.run --pair 'C#_to_Java'
   ```

   每次命令接收一个 `--pair`。运行 `.venv/bin/python -B -m translation.smoke.run --help` 可查看七个可选翻译对。仓库名和目标源码路径由 `translation/smoke/run.py` 的 `SAMPLES` 指定。换仓库时，更新对应翻译对的仓库名与目标源码路径，并确认数据集存在该仓库

   测试工具按目标语言选择。Python 使用虚拟环境中的 pytest，Java 需要 JDK 和 Maven，C# 需要 .NET 8 SDK，C++ 需要编译器、CMake 和 GoogleTest，Rust 需要 Cargo 和 Rust 工具链

结果默认写入 `translation/smoke/output/<运行时间>/`，终端会打印本次样例目录。查看 `summary.json` 确认结果，在翻译对子目录的 `target_project/` 查看翻译代码和组装后的测试文件，在 `tests.log` 查看编译与测试输出。分批、组装清单、映射和源码范围会按执行进度保存为 JSON

## 文件结构

```text
localization/
├─ README.md              使用说明、输入输出和主流程
├─ __init__.py            导出 Localization
├─ api.py                 四项功能入口、仓库上下文与共享分析、模型实例管理
├─ config.py              配置读取、语言与项目路径规则
├─ codegraph.py           上游HTTP调用、符号key与路径转换、请求内缓存
├─ file_context.py        原文读取、编码、词法处理和文件快照
├─ public_test_analysis.py       测试识别、代码范围和按需查询调用关系
├─ public_test_plan.py           LLM 分批、批次及用例覆盖校验
├─ assembly.py            LLM 组装、原文范围复制与校验发布
├─ source_extract.py      共享源函数记录、入口匹配、依赖遍历与源码范围输出
├─ source_dependencies.py 从文件原文补充导入、头文件和依赖候选
├─ mapping.py             Jina编码与召回、LLM核验及批次源函数汇总
├─ prompts.py             分批、组装与映射核验的英文提示词和消息构造
└─ llm.py                 通用模型配置、HTTP调用与完整JSON回复检查
```

测试分批、组装和映射复用 `public_test_analysis.py` 的用例清单和原文快照。源码获取和映射共用源函数记录

上游查询统一通过 `codegraph.py`，业务提示词与结果校验放在各功能文件中
