# 12 题诊断实验

固定 **轻量 3 + 常规 6 + 困难 3，每题一次，共 12 runs**。直接复用已有清单；难度标签是静态预分层，不是成功率校准结果。

| 项目 | 约定 |
| --- | --- |
| 输入 | `selection-12/selection.json` 及同目录任务、范围确认文件；名单见 `spider2dbt_slim_plan.csv` |
| 模型 | `deepseek/deepseek-v4-flash`，thinking `off`，Pi SDK `0.85.1` |
| 配置 | 固定 `config/system-prompt.md` 与工具集合，SDK 自动重试和压缩关闭 |
| 限制 | 每题 15 分钟、最多 30 次模型请求分派；单条 dbt 命令最多 5 分钟且不超过剩余时间，evaluator 最多 10 分钟 |
| 新一轮 | 四项修复离线验收及 `preflight` 通过后，DataAgent 仅重跑一次 12 题；官方框架不重跑 |
| 输出 | 新目录如 `experiment-12-rerun-1/`，新成绩单如 `spider2dbt_slim12_results_dataagent_rerun1.csv`；保留旧 CSV 与 `experiment-12/`，运行凭据按 review 清理 |

每题从清单记录的原始 `spider2-dbt/examples` 创建新的 repo、数据库副本和会话，不沿用上一轮修改。轮次中保持模型、prompt、工具和任务不变，gold 与历史评分不进入 Agent 输入。

运行结束后，用未修改的官方 evaluator 评分。每题保留停止原因、请求次数、diff、工具/dbt 日志、提交路径与官方判定；不记录密钥、哈希或费用上限。

旧轮次 DataAgent 为 **3 成功、6 官方失败、3 未评分**，成功任务与官方框架不同。新轮次分别报告成功数/12、已评分数/12 和 dbt 状态；未评分保留空值，不能补成官方 0 分。请求计数修正后不与旧日志中的“31 次”直接等同。

完成后停止，不扩全量、不为追分追加整轮重跑。若已修复的 P1 再次出现或环境阻塞，停止并保留不完整状态。这是已见诊断集上的工程回归，不用于证明稳定性或框架能力差异。

具体修复与离线验收见 [review.md](review.md)。
