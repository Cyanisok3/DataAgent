# Engineering Principles — DataAgent (Python)

## Product Scope

DataAgent is an **NL2SQL data agent with a configurable semantic layer**. Every
change is judged by production engineering standards — this is not a throwaway
teaching demo — but must stay within that product boundary: no scope creep into
general-purpose assistants, ETL engines, or full BI platforms.

When two designs conflict, prefer the one that is reliable, observable, and
reversible in production. Engineering value takes priority over teaching value;
teaching scaffolding must not weaken the production design.

## Code Simplicity

- Keep files under ~400 lines and functions under ~80 lines; split when crossed.
- **YAGNI** — no feature, interface, or parameter until it is actually needed.
- Prefer existing solutions, in order: (1) Python stdlib; (2) framework features
  (FastAPI, Pydantic validation); (3) installed dependencies (check
  `requirements.txt` first); (4) only then custom code.
- Avoid over-encapsulation: extract a shared helper only when a pattern appears
  ≥2 times. A one-off private method that clarifies intent is fine.
- Do not hand-write boilerplate: use `@dataclass`, Pydantic models, and tooling
  generation. Prefer keyword construction for records with many fields.
- Use explicit module-level imports; never `from module import *`. When a local
  name collides with a stdlib/third-party name, import under an explicit alias.

## Architecture & Layering

- Dependency direction is one-way (no cycles):

  ```
  context.py      (pure projection functions, zero internal imports)
       ↑
  session_store.py (storage + summary ledger)
       ↑
  compaction.py   (compression orchestration)
       ↑
  main.py         (HTTP layer only)

  react_loop.py → llm.py + tools.py
  tools.py      → db.py + semantic_layer.py + sql_guard.py
  ```

- **Tools are fine-grained** (aligned with the upstream Java design):
  `get_domains`, `get_tables`, `get_table_schema`, `get_metric_caliber`,
  `execute_sql`, `get_date_info`. Each returns a small, complete payload. Do not
  merge them back into one large context dump.
- **Two tracks**: the append-only log stores every fact — tool name, args, and
  result — and never deletes a row; the projection decides what the model sees
  next. Never truncate the log to save context; change the projection instead.
- Tool errors are returned as structured results so the agent can self-correct;
  never swallow an exception silently.
- Column-name correction relies on the model calling `get_table_schema` for the
  complete schema; do not reintroduce ad-hoc column-hint string patches.
- Final answers separate **computed facts, hypotheses, and suggestions**: trend,
  reversal, or cause claims require corresponding computed evidence; otherwise
  mark them explicitly as unverified or omit them.

## Context Budget & Compression

- The watermark is measured in **tokens**, not characters, and is set relative
  to the model's real context window (~70%). Compression fires only when the
  projected context actually crosses it — not at a fixed tiny size.
- Budget accounting must use the same selection logic as `project_history`: only
  facts that will actually be sent count toward a turn's cost.
- Summaries preserve every concrete number, are cut at sentence boundaries, and
  never carry stale task state such as "awaiting authorization".

## SQL & Data

- All generated SQL passes `sql_guard`; target dialect is SQLite (`business.db`):
  read-only SELECT, LIMIT enforcement, write-statement rejection, dialect rewrite
  via sqlglot.
- Never hardcode the production `sessions.db` in tests or `__main__` self-test
  blocks — always use a `/tmp` path. Treat `business.db` as read-only.

## Tooling

- Python 3.10+ syntax; development runtime 3.14.
- `ruff` for lint + format, `pytest` for tests, `mypy` for type checking
  (incremental — start with core modules).
- Frontend: strict TypeScript; verify with `npx tsc --noEmit`. Frontend changes
  follow minimal-diff.

## Commands

```bash
# Backend
python3 -m uvicorn main:app --reload --port 8000

# Tests / lint / format / types
pytest
ruff check .
ruff format .
mypy .

# Frontend type check
cd chat-frontend && npx tsc --noEmit
```
