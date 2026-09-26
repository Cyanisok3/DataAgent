# Benchmark

Evaluation adapters, pinned BIRD Mini-Dev inputs, and run artifacts live here.
The general-purpose `DataSource` injection point remains in the application
layer; the Agent still chooses metadata tools and writes SQL through its normal
validated execution path.

## Layout

- `bird_data.py`: fetch and hash-check pinned questions and official scorer code;
  split model-visible prompts from the separate scoring oracle.
- `bird_eval.py`: run fixed question scopes through the production session loop.
- `bird_score.py`: run the pinned official EX comparison in a read-only process.
- `eval_budget.py`: reserve request usage before each provider call.
- `regression_9q.py`: run the business-question trace on a database snapshot with
  an injected Asia/Shanghai clock; this command is separate from BIRD.
- `data/bird-mini-dev/`: cached official questions and scorer files.
- `data/minidev/`: extracted Mini-Dev SQLite databases supplied by the user.
- `runs/`: manifests, append-only question records, isolated session databases,
  predictions, and official scores.

The `data/` and `runs/` paths are git-ignored. Keep the original downloadable
package and run evidence local; the pinned revisions and hashes are recorded in
the source and each run manifest.

## BIRD Mini-Dev

From the repository root, prepare or verify the pinned lightweight assets:

```sh
.venv/bin/python -m benchmark.bird_data
```

The default debug scope is the frozen 20-question, 11-database stratified
selection. Preflight makes no model calls:

```sh
.venv/bin/python -m benchmark.bird_eval \
  --databases benchmark/data/minidev/MINIDEV/dev_databases \
  --output benchmark/runs/bird-debug-<unique-id>
```

An actual run requires the explicit `--execute` and either a monetary cap or
`--unlimited-cost`. Model defaults to `llm.MODEL`; per-question limits default
to 14 calls and 120 seconds. If no provider price is configured, reports retain
provider-reported token usage and leave monetary cost unknown. Failures remain
in the score denominator. Run directories are never overwritten.

Score a completed run once with the pinned official EX implementation:

```sh
.venv/bin/python -m benchmark.bird_score \
  --run benchmark/runs/bird-debug-<unique-id> \
  --databases benchmark/data/minidev/MINIDEV/dev_databases
```

The `full` scope requires a scored 20-question debug run with at least one
official exact match and exactly matching model, code, prompt, catalog, data,
budget, and runtime configuration. A change to the action parser or execution
code requires a fresh debug run before the 500-question gate can open.

## Business regression

Preflight or run the nine fixed business questions independently:

```sh
.venv/bin/python -m benchmark.regression_9q \
  --output benchmark/runs/business-9q-<unique-id>
```

Use `--execute` for model calls. `--input-budget N` lowers the projection
watermark to exercise compaction; it does not guarantee a summary will be
committed. Each execution snapshots the business database and writes its own
session log. Review SQL and result evidence for numeric correctness; a completed
natural-language answer alone is not a correctness measure.
