# 当前审查与修复计划

更新：2026-09-12。基于当前代码、18 项通过的测试及独立离线反例。已完成项和旧审查结论已删除；当前只剩一项 P1。

## P1：非预期 instance_id 会阻断合法项评分

位置：`src/submission.ts` 的 `validateRoundResultDirectory`，以及 `src/evaluator.ts` 的 `evaluateRound`。

当前已经能逐项排除 SQL、CSV、缺失文件等无效产物，但 `expectedInstanceIds` 之外的合法 DuckDB 仍被加入全局 `problems`。`evaluateRound` 遇到任何全局问题都会设置 `setupError` 并跳过官方评分器，因此一个非预期任务仍会使所有合法预期任务变成 `scoring_unavailable`。

离线反例：metadata 同时包含一个预期任务 DuckDB 和一个非预期任务 DuckDB，结果为 `status=failed`、`failure_label=submission_or_gold_format`、`evaluated_runs=null`，预期任务未进入评分。

### 明确修复措施

1. 在现有 `validateRoundResultDirectory` 内按 `expectedInstanceIds` 对已经通过文件校验的记录再次分组。非预期记录写入 `excluded`，原因为 `unexpected instance_id`；不要再写入全局 `problems`。
2. 从 `metadataIds` 和 `validEntries` 中移除非预期记录，使 `createFilteredSubmissionDirectory` 只复制预期且合法的提交。原始 `results_metadata.jsonl` 保持不变。
3. 保持 `evaluateRound` 的现有边界：有合法子集时只将临时过滤目录交给未修改的官方 evaluator；没有合法项时不调用 evaluator；`submission_exclusions` 保留每个排除项的 `instance_id` 和原因。
4. 不新增模块、评分器、动态规则或恢复框架，不修改 gold、原始提交目录及官方 evaluator。
5. 在 `test/contract.test.ts` 增加独立入口回归用例：一个预期 DuckDB 与一个非预期 DuckDB 混合，假 evaluator 必须只看到预期记录；同时断言预期项获得评分、非预期项进入 `submission_exclusions`、原始 metadata 仍保留两条记录。

### 验收标准

- 上述混合反例返回 `status=completed`，预期项正常评分，非预期项不进入官方 evaluator。
- 全部非预期或全部无效时不调用官方 evaluator，并保留排除原因。
- 全部合法且属于预期任务时，行为与当前实现一致。
- `npm run build`、全部现有测试及新增回归测试通过。

## 修复后的补评边界

P1 通过后，复用 tickit001、marketo001 的现存 DuckDB，在新目录（例如 `experiment-12/submission-recovery-1/`）仅对这两个产物补评。不重新调用模型、不重新执行 dbt、不修改数据库内容，也不覆盖原有结果、receipt、报告或成绩单。

补评标记为“同一轮产物的提交修复”，不是新一轮 Agent 成绩。当前正式记录仍为 **1 成功、9 官方失败、2 未评分**；补评前不预设可追回分数。
