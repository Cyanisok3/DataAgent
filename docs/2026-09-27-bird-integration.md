# A–E 收尾与 F 批接入记录

日期：2026-09-27。未提交代码，未运行真实模型；本文不是 BIRD 成绩报告。

## 范围与状态

保留 NL2SQL、细粒度工具与生产会话执行器。F 只注入数据库、元数据和时钟，
不引入查询 DSL，不放宽产品查询护栏，不用 gold 构建指标。
按照 minimal-diff 限定增量范围：运行时注入、外部评测脚本、对应测试及本文。
产品数据库未迁移或修改；评测输出使用新目录和独立会话库。

A–E 的查询护栏、结构化证据、终态、请求日志、统一预算视图和保守摘录压缩已落地。
上一批 97 项测试通过，包括真实本机 HTTP 断流；实际旧会话库的临时副本通过幂等迁移。
旧课程和评审是历史快照，不代表当前实现。仍未完成真实模型九题验收。

F 当前已实现：

- 按 db_id 注入只读 SQLite 数据源，可信内省列/主外键，读取官方 CSV 字段说明；未知指标不回退到业务库口径。
- 回答动作选择 final_query_id；引用须属于所选成功证据。最终引用随 done 事务落库，不取最后一条探查 SQL。
- 模型输入仅 question＋evidence；官方 gold 独立作为评分 oracle。完整 SQL 与 200 行预览 SQL 分开保存。
- 固定官方数据/评分版本、文件哈希、题目顺序；20 题按数据库轮转选择并恢复官方顺序，覆盖 11 库，不按答案挑题。
- 官方 EX 的 calculate_ex 和 execute_sql 函数体原样加载，先校验 SHA；只替换只读连接，并在独立进程外实施硬超时。
- 发送前预留整批 token/费用预算；错误或缺 usage 不退预留。未运行/失败题保留记录，不从分母剔除。
- 固定时钟能注入提示；固定模式拒绝依赖机器 now/CURRENT_DATE 的 SQL，要求模型改用绝对日期，不静默替换日期。

**F 尚未全部验收**：11 个官方数据库未就绪；真实模型范围和费用上限待确认；
20 题真实调试、配置冻结后单次 500 题、业务九题正常/强制压缩双轨尚未执行。
现有 regression_9q.py 是旧 HTTP 探针，不能拿“有回答”统计充当新版验收；
独立快照、固定时钟的双轨命令和冻结配置门禁仍需收尾。

## 已固定的官方来源

经典 500 题 SQLite 版本，不混入含 CRUD 的 V2：
[官方说明](https://github.com/bird-bench/mini_dev)、
[canonical 数据](https://huggingface.co/datasets/birdsql/bird_mini_dev)。

- 数据 revision：f65faf4ae3b638c1fa6df1d3370c8d92c8366301。
- 评分 revision：abd11b6db92a1c9f809b32f7564c7c71b34d67f0。
- 三个下载文件的 SHA-256 固定在 scripts/bird_data.py；本轮已下载并核验。
- 官方 EX 使用结果集合相等，不证明顺序、重复行语义或产品预览完整性；不自行改变评分算法。
- 数据库包需从官方 README 指定入口取得，目录为 dev_databases/<db_id>/<db_id>.sqlite。

## 命令与产物

在 DataAgent 根目录执行。默认预检不会调用模型。

```sh
.venv/bin/python -m scripts.bird_data
.venv/bin/python -m scripts.bird_eval --databases /绝对路径/dev_databases --output data/bird-runs/debug-01
```

真实运行必须显式加 --execute、--model、--max-tokens、--max-cost、
--input-price、--output-price；价格单位为每百万 tokens，货币由 --currency 指定。
默认每题最多 14 次模型调用、120 秒；模型窗口和输出限制沿用 DATAAGENT 环境配置。
费用字段是按用户提供价格计算的保守预留，不是供应商账单。缺少正预算会拒绝运行。
最终回答和摘要也占调用与费用预算，不因失败而无限重试。

运行输出 manifest.json、records.jsonl、summary.json、predict_mini_dev.json 和独立 sessions.db。
输出目录不得已存在，避免覆盖前一轮或无界重跑。评分必须在生成完成后单独调用：

```sh
.venv/bin/python -m scripts.bird_score --run data/bird-runs/debug-01 --databases /绝对路径/dev_databases
```

评分核对题数、顺序、db_id、oracle 与数据库哈希；缺有效最终查询记 0 分。
单次评分超时杀死子进程，原业务库不可写。Linux 子进程额外设置地址空间上限；
macOS 没有同等硬内存隔离保证，不能把执行超时解释为全面资源隔离。

## 本次实际验证

109 项测试通过，其中新增 12 项 F 批离线测试。官方源码自检使用临时微型库，
不是对 500 题运行 gold，也不是模型 EX。源码尚未准备时这组官方自检会明确 skip，
验收需先运行 bird_data，再确认无 skip。

F 修改模块 Ruff 与增量 mypy 通过；前端 TypeScript 通过。
db.py 导入辅助代码的两项既有时区 lint 告警未在本轮顺带修改。
未购买服务、未运行付费模型、未修改原 sessions.db/business.db。

后续必须分别报告工程测试、真实九题、20 题调试与 500 题官方 EX，不能相互替代。
