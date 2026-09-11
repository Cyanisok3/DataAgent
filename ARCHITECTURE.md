# Data Engineering Agent · 当前结构与接入边界

更新：2026-09-11。本文区分实际存在的内容与尚未实现的接入方案。

## 当前真实结构

`DataAgent/` 当前只有 `SCOPE.md`、`ARCHITECTURE.md`、`EXPERIMENT.md` 三份文档。尚无应用代码、依赖配置、Agent runner、dbt 环境或评测结果，因此暂不绘制已实现的运行架构图。

所有新增和修改限定在 `DataAgent/`。同级 `MaloneTalk_DataAgent/` 与 `signalpilot/` 只读参考，不是本项目的运行依赖。

## 首阶段接入方案（待实现）

接入顺序为：**TypeScript runner / Pi Coding Agent SDK → 独立 task workspace → dbt / DuckDB → 任务结束后由外部 official evaluator 评分**。其中 dbt 的执行反馈可以回到 Agent；官方评分和 gold 不进入 Agent 的修复循环。

| 部分 | 最小职责 | 当前状态 |
| --- | --- | --- |
| TypeScript runner + Pi Coding Agent SDK | 创建新会话、装载固定配置、复用读写与命令工具、执行停止条件、收集事件 | 待实现 |
| 每次运行的 workspace 与隔离环境 | 提供原始任务文件、独立数据库副本和受控工具运行位置 | 待实现 |
| Python / dbt / DuckDB | 必要的命令封装、验证与提交产物整理；通过进程退出码和输出交互 | 待接入 |
| 官方 evaluator | 在 Agent 结束后读取提交产物和 gold，保存官方判定 | 已核对上游入口，未在本地运行 |

SDK 优先使用 `@earendil-works/pi-coding-agent`，不从 Agent Core 重写通用 coding harness。官方 SDK 提供 `createAgentSession()`、会话事件和默认 `read/bash/edit/write` 工具；实际版本在接入时固定。[Pi SDK 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)

## 两个必要边界

**运行隔离。** 每个 run 使用全新的 repo、DuckDB 副本和会话。仅设置工作目录不构成文件访问隔离；工具进程应在仅挂载任务输入的容器或等效受限环境中执行。不加载宿主机个人会话、全局扩展和其他项目配置。

**评分隔离。** 完整 benchmark、gold 和评分输出由外部评测进程持有，不挂载给 Agent，也不允许 Agent 通过网络下载参考解。依赖提前准备，任务工具无需任意外网访问；模型 API 连接单独保留。Agent 结束后，runner 提取官方要求的提交产物，由评测适配层评分。

这两处是避免任务串扰和答案泄漏的基本条件，不扩展成权限平台或分布式 runtime。

## 产物与验证

可审计产物包括文件修改、工具输入与执行结果、dbt 状态，以及评分结束后补充的官方判定。receipt 只是轻量 JSON 记录，不承担签名或不可篡改证明；字段约定见 [EXPERIMENT.md](EXPERIMENT.md)。

`dbt build/test` 成功仅说明已执行的 dbt 检查通过，不能替代官方任务判定；Agent 自行添加的测试也不构成独立正确性证据。

实际代码接入后，再用真实入口和调用关系更新本文；不提前加入未来模块和虚构文件路径。
