# 当前架构

TypeScript + Pi Coding Agent SDK（0.85.1）读取和修改 dbt 工程，通过受限子进程执行 dbt/DuckDB，最后由官方 Spider 2.0-DBT evaluator 评分。

| 模块 | 职责 |
| --- | --- |
| `cli.ts` / `experiment.ts` | 加载选择清单和固定配置，预检、默认串行或按批并发运行（`--parallel N`）、调用评测 |
| `workspace.ts` / `agent.ts` | 创建独立 repo、数据库副本与会话，执行 Pi 工具循环和请求限制 |
| `safe-tools.ts` / `process.ts` / `validation.ts` | 受限文件工具、dbt 执行与最终构建验证 |
| `database-inspection.ts` | 受限、只读的 DuckDB 表与列元数据探查 |
| `submission.ts` / `evaluator.ts` / `receipt.ts` / `report.ts` | 整理提交，保存官方结果、diff、工具事件与 receipt，生成诊断报告 |

代码均位于 `src/`。单任务顺序为：新工作区 → Agent 读改文件与 dbt 验证 → runner 最终构建 → 提交产物；整轮结束后统一评分。

当前工具为 `read_file`、`edit_file`、`write_file`、`list_files`、`search_files`、`dbt_build` 和只读的 `inspect_database`。后者只接受工作区内相对 `.duckdb` 文件，固定查询表、视图及列元数据，不接受用户 SQL。

工具结果返回同一 Pi 会话。dbt 返回退出状态与限长日志摘要，超长时分别保留 stdout/stderr 尾部；执行日志另存于 Agent 不可读的 evidence 目录，供外部审计。

文件工具仅访问任务 repo；dbt 在 Docker 或 macOS 沙箱中执行，无可用后端则停止。Agent 不启用任意 shell，gold、evaluator 和评分结果不进入其工具可读范围或修复循环。API key 从环境变量注入 SDK 内存。

`dbt build/test` 通过与官方成功分别记录。实验协议见 [EXPERIMENT.md](EXPERIMENT.md)，修复计划见 [REVIEW.md](REVIEW.md)，技术反思见 [REFLECTIONS.md](REFLECTIONS.md)。
