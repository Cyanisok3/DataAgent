# 代码审计：最小数据库探查工具

更新：2026-09-13。当前工作区没有独立 `SCOPE.md`；按现有文档“不新增 Scope 文档”的边界，本次以更新后的 `ARCHITECTURE.md` 和实施前 `REVIEW.md` 作为范围依据，审计实际代码、测试与受限运行路径。

## 状态更新（2026-09-13，复验后）

本文下方 P1 / P2-1 / P2-2 三项均已在代码中处理并通过 Docker 复验：

- **P1 已修复**：`process.ts` 新增 `path.parse(prefix).root === prefix` 拒绝守卫，命令依赖根目录不再写入 macOS 沙箱 profile；可执行文件同时保留 lexical/canonical 路径。Docker 受限后端 21/21 测试通过；macOS 沙箱受外层环境限制未做动态复验，放行仅针对 Docker。
- **P2-1 已修复**：探查测试统一读取 `DATAAGENT_TEST_PYTHON`，`dbt debug` 与受限命令用例均断言 `exit_code=0`、未超时、未取消且后端可用；Docker 全量测试无跳过。
- **P2-2 已修复**：`resolveDatabasePath` 仅返回绝对路径字符串，冗余 `relativePath` 字段已移除。

真实 preflight（含 `restricted_dbt_smoke` 与 `restricted_database_inspection`）及 playbook/divvy/tpch 数据库探查均通过，副本内容未改变。未发现阻止 Docker 评测的 P0/P1。

## 结论

`inspect_database`、`dbt debug` 参数修正及 Agent 接入均与当前目标一致。实现复用了现有工具注册、受限子进程、超时和事件记录，没有引入 Bash 控制器、任意 SQL、依赖安装、模型运行入口或新评分器，未发现模块级过度设计。

下方 P1、P2-1、P2-2 为审计时点发现，已在代码中处理并通过 Docker 复验（见“状态更新”）。Docker 受限后端已通过 21/21 测试、真实 preflight 及 playbook/divvy/tpch 数据库探查；macOS 隔离边界受外层环境限制未完成动态复验。

## P1：macOS 命令依赖路径会错误放宽读取边界

位置：`src/process.ts` 的 `addCommandDependencyRoots` 与 `macSandboxProfile`。

当前逻辑把所有 `<prefix>/bin/<executable>` 的 `prefix` 加入可读根。对 `/bin/cat`，计算结果是文件系统根目录 `/`，生成的 profile 因而包含 `(subpath "/")`。在没有外层沙箱代为阻止时，现有“禁止读取任务目录外 marker”测试实际失败，说明 DataAgent 自身的 macOS 沙箱可读取整个文件系统。

同一逻辑只记录命令的 canonical path，没有允许用户传入的虚拟环境符号链接路径。当前测试使用 `/private/tmp/dataagent-evaluator-env/bin/python` 时，宿主 Python 可导入 DuckDB，但进入 macOS 受限进程后无法执行，导致两项探查测试失败。

### 明确修复措施

1. 计算命令及 shebang 解释器时同时保留 lexical path 与 canonical path；profile 只加入实际需要的可执行文件和其受限运行时目录。
2. 从 `bin` 目录推导依赖前缀前，显式拒绝 `path.parse(prefix).root`，绝不能把 `/` 加入 `readRoots`。`/bin`、`/sbin`、`/usr` 等系统路径继续使用现有固定白名单。
3. 对绝对虚拟环境命令，仅允许该 lexical executable、canonical executable 和对应虚拟环境前缀；不要扩大到其父目录或用户目录。
4. 保留 Docker 行为不变，不增加宿主回退或第二套沙箱实现。
5. 回归测试必须在无外层隔离兜底的 macOS 环境确认：`/bin/cat` 不能读取任务根外文件；虚拟环境符号链接 Python 能在受限进程内完成只读 DuckDB 探查；生成的 profile 不包含 `(subpath "/")`。

## P2-1：测试依赖本机临时路径，且 dbt 用例可能假通过

位置：`test/contract.test.ts` 的数据库探查和 `dbt debug` 用例。

两项探查测试重复硬编码 `/private/tmp/dataagent-evaluator-env/bin/python`，其他机器可能直接跳过或失败。`dbt debug` 用例只检查返回的参数数组，没有断言受限命令 `exit_code=0`；本次审计中即使后端执行失败，该用例仍显示通过，不能证明真实调用成功。

### 明确修复措施

1. 用一个测试辅助函数统一读取显式测试变量（例如 `DATAAGENT_TEST_PYTHON`），否则使用协议中的 Python 命令；删除两处本机临时路径常量。
2. 环境缺少 DuckDB 或受限后端时，两项集成测试应统一明确标记为未验收；不得将跳过或后端失败写成完成。
3. `dbt debug` 与其他 action 的回归用例除检查参数外，还需断言 `exit_code=0`、未超时、未取消且 `sandbox_backend` 可用。可复用系统内无副作用的成功命令，不必继续创建假 dbt 脚本。
4. 保留一次真实 preflight 作为 dbt 与 DuckDB 的端到端验收，不新增测试框架或依赖管理模块。

## P2-2：探查路径返回未使用字段

位置：`src/database-inspection.ts` 的 `resolveDatabasePath`。

该函数返回 `{ absolutePath, relativePath }`，但调用方只使用 `absolutePath`。这是当前实现中确认的冗余数据流。

### 明确修复措施

让 `resolveDatabasePath` 只返回绝对路径字符串或仅含 `absolutePath` 的对象，并同步唯一调用点。保留现有路径、扩展名、普通文件和符号链接校验，不为这一清理新增类型或辅助层。

## 已通过的目标边界

- 工具接口限定为 `tables` / `columns`，使用固定参数化元数据查询，不接受用户 SQL。
- DuckDB 以只读模式打开，并关闭外部访问和扩展自动安装；输入限于任务目录内的相对 `.duckdb` 普通文件。
- 表、视图、列类型、可空性、原始列序、特殊字符和分页行为在 Docker 受限后端通过；fixture 探查前后字节一致。
- 超时取 30 秒、命令上限和剩余时间的最小值；取消、损坏数据库、缺失文件、越界与符号链接均明确失败。
- `dbt debug` 不再传 `--target-path`，并拒绝同时传 `select`；其他 action 保持原参数路径。
- 工具已同步注册到固定配置和提示词，沿用既有工具事件，不计入 dbt 验证结果。

## 复验与停止条件

按 **P1 → P2-1 → P2-2** 顺序修复。完成后运行构建、默认测试、显式 Docker 全量测试及 macOS 隔离回归；再运行一次真实 preflight，确认 `restricted_dbt_smoke` 和 `restricted_database_inspection` 同时通过。

本轮继续不调用模型或官方 evaluator，不重跑 12 题，不增加三题子集入口。上述项目全部通过后即停止，只能结论为“数据库结构可通过受限只读工具获取”，不能推断模型效果或官方成绩提升。
