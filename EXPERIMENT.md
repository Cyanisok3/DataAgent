# 诊断实验与当前验收边界

固定诊断集为 **轻量 3 + 常规 6 + 困难 3，每题一次，共 12 runs**。沿用现有清单；难度标签是静态预分层，不是成功率校准结果。

| 项目 | 约定 |
| --- | --- |
| 输入 | `selection-12/selection.json` 及同目录任务、范围确认文件；名单见 `spider2dbt_slim_plan.csv` |
| 模型 | `deepseek/deepseek-v4-flash`，thinking `off`，Pi SDK `0.85.1`；SDK 自动重试和压缩关闭 |
| 限制 | 每题 15 分钟、最多 30 次模型请求分派；dbt 命令最多 5 分钟且不超过剩余时间，evaluator 最多 10 分钟 |

最近一轮 `exp-20260912114012` 的[原报告](experiment-12/diagnostic-report.md)为 **1 成功、9 官方失败、2 未评分**。tickit、marketo 的原有数据库[补评](experiment-12/submission-recovery-1/evaluation.json)均通过，同轮产物经过提交修复后合计 **3/12**；补评没有调用模型或执行 dbt，属于提交修复，不是新一轮 Agent 成绩或模型能力提升。原记录保持不变。

**本轮已完成 [REVIEW.md](REVIEW.md) 的工具补齐与离线验收，未调用模型或官方 evaluator，也未重跑 12 题。** 使用 playbook、divvy、tpch 数据库的临时副本验证表结构探查，并在临时项目中验证 `dbt debug`；未执行任务的 `dbt build/test`。三题六次模型对照留待下一步，本轮不增加子集运行入口。

后续若另行开展模型实验，每题使用原始任务的新工作区、数据库副本与会话；组内保持模型、prompt、工具及限制一致，组间明确记录被比较的机制差异。gold、历史评分和参考答案不进入 Agent 输入，判分继续使用未修改的官方 evaluator。

保留停止原因、请求次数、diff、工具/dbt 日志、提交路径与官方判定；不新增密钥、哈希或费用上限记录。新产物使用独立目录，不覆盖已有 CSV、receipt 或报告。未评分保持空值，dbt 通过与官方成功分别报告。

工具离线验收只证明可用性；已见诊断集上的单次成绩不能证明稳定提升或框架优劣。不扩全量、不为追分追加整轮重跑。
