# Data Engineering Agent

当前结构与实验约定见 `ARCHITECTURE.md` 和 `EXPERIMENT.md`。实现只依赖 `DataAgent/` 自身；同级项目不是运行依赖。

## 闭环

1. `prepare` 从官方 task JSONL 复制任务描述，按固定种子抽取 12 题并输出 low/medium/high 为 3/6/3 的清单。
2. `preflight` 在不调用模型的情况下检查固定 12 题协议、模型 `deepseek/deepseek-v4-flash`、thinking `off`、Node、Python、dbt、DuckDB、SDK 和提交目录约束。
3. `experiment` 为每个任务建立新的 repo/数据库副本和内存会话，使用固定配置运行 Agent，记录工具事件、dbt 验证、diff 和 receipt，并把 gold/evaluator 留在 Agent 隔离边界之外。
4. 单轮生成 `results/round-1/results_metadata.jsonl` 和提交产物，随后调用用户提供的官方 `evaluate.py`。
5. `report` 汇总 12 个计划 run 的状态、单次官方结果和 dbt 状态；未评分项保留为未评分。

## 安装

```sh
npm install --no-package-lock
npm run build
npm test
```

Pi SDK 要求 Node `>=22.19.0`。本项目不把模型 ID、API key、benchmark 数据或 gold 写入代码；正式运行时必须显式提供准确模型 ID 和路径。

## 典型用法

```sh
npm run dev -- prepare \
  --tasks /path/to/Spider2/spider2-dbt/examples/spider2-dbt.jsonl \
  --examples-root /path/to/Spider2/spider2-dbt/examples \
  --output-dir /path/to/selection

# 对描述未精确命中 model 文件名的任务，额外提供人工范围确认 JSONL
npm run dev -- prepare \
  --tasks /path/to/Spider2/spider2-dbt/examples/spider2-dbt.jsonl \
  --examples-root /path/to/Spider2/spider2-dbt/examples \
  --output-dir /path/to/selection \
  --scope-confirmation /path/to/scope-confirmations.jsonl

npm run dev -- preflight \
  --selection /path/to/selection/selection.json \
  --experiment-root /path/to/experiment \
  --model deepseek/deepseek-v4-flash \
  --thinking off \
  --evaluator /path/to/Spider2/spider2-dbt/evaluation_suite/evaluate.py \
  --gold-dir /path/to/Spider2/spider2-dbt/evaluation_suite/gold

npm run dev -- experiment \
  --selection /path/to/selection/selection.json \
  --examples-root /path/to/Spider2/spider2-dbt/examples \
  --output-root /path/to/experiment \
  --model deepseek/deepseek-v4-flash \
  --thinking off \
  --evaluator /path/to/Spider2/spider2-dbt/evaluation_suite/evaluate.py \
  --gold-dir /path/to/Spider2/spider2-dbt/evaluation_suite/gold

npm run dev -- report --experiment-root /path/to/experiment
```

确认文件每行只允许 `instance_id`、`related_models`（项目内相对模型路径）以及带 `score` 和必要结构指标的三项观察字段；不允许答案、gold 或其他字段。生成的 `selection.json` 会连同确认文件副本，并在后续加载时重新校验。

`experiment` 默认 fail-closed：只启用受路径约束的读写/搜索工具；Agent 的 `dbt_build` 和 runner 最终验证都必须进入受限子进程后端（macOS `sandbox-exec`，或显式配置的 Docker），没有可用且可运行的后端就不会调用模型。Pi 的任意 `bash` 工具保持关闭，因此任务工具不能下载答案或访问 workspace 外部路径。自动 SDK 压缩关闭，模型请求数按实际 assistant 响应计数。官方 evaluator 只在 Agent 结束后由父进程调用。
