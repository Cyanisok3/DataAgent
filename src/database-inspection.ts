import { lstat, realpath } from "node:fs/promises";
import path from "node:path";
import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { assertWithin, existingPathWithin, toPosix } from "./files.js";
import { runRestrictedCommand, safeChildEnv } from "./process.js";

const PAGE_SIZE = 100;
const MAX_OUTPUT_CHARS = 32_000;

const INSPECTION_PYTHON = String.raw`import json
import sys

import duckdb

database_path = sys.argv[1]
action = sys.argv[2]
schema = sys.argv[3] or None
table = sys.argv[4] or None
offset = int(sys.argv[5])
page_size = int(sys.argv[6])
connection = None

try:
    connection = duckdb.connect(
        database=database_path,
        read_only=True,
        config={
            "enable_external_access": "false",
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        },
    )
    if action == "tables":
        rows = connection.execute(
            """
            SELECT table_catalog AS database_name,
                   table_schema AS schema_name,
                   table_name,
                   table_type
            FROM information_schema.tables
            WHERE (? IS NULL OR table_schema = ?)
            ORDER BY table_catalog, table_schema, table_name, table_type
            LIMIT ? OFFSET ?
            """,
            [schema, schema, page_size + 1, offset],
        ).fetchall()
        names = ["database_name", "schema_name", "table_name", "table_type"]
    elif action == "columns":
        exists = connection.execute(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = ? AND table_name = ?
            LIMIT 1
            """,
            [schema, table],
        ).fetchone()
        if exists is None:
            raise ValueError(f"Table not found: {schema}.{table}")
        rows = connection.execute(
            """
            SELECT column_name,
                   data_type,
                   is_nullable,
                   ordinal_position
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            LIMIT ? OFFSET ?
            """,
            [schema, table, page_size + 1, offset],
        ).fetchall()
        names = ["column_name", "data_type", "is_nullable", "ordinal_position"]
    else:
        raise ValueError(f"Unsupported inspection action: {action}")

    records = [dict(zip(names, row)) for row in rows]
    print(json.dumps({"rows": records[:page_size], "has_more": len(records) > page_size}, ensure_ascii=False, separators=(",", ":")))
finally:
    if connection is not None:
        connection.close()
`;

export type DatabaseInspectionAction = "tables" | "columns";

export interface DatabaseInspectionRequest {
  path: string;
  action: DatabaseInspectionAction;
  schema?: string;
  table?: string;
  offset?: number;
}

export interface DatabaseInspectionOptions {
  root: string;
  runtimeDir: string;
  pythonCommand: string;
  commandTimeoutMs: number;
  remainingMs: () => number;
  request: DatabaseInspectionRequest;
  signal?: AbortSignal;
}

export interface DatabaseInspectionResult {
  output: string;
  nextOffset: number | null;
  rowCount: number;
  command: string[];
  sandboxBackend: string;
  durationMs: number;
}

function isFileSystemError(error: unknown, code: string): boolean {
  return error !== null && typeof error === "object" && "code" in error && error.code === code;
}

async function rejectSymlinkComponents(root: string, candidate: string): Promise<void> {
  let current = root;
  for (const segment of path.relative(root, candidate).split(path.sep)) {
    if (!segment || segment === ".") continue;
    current = path.join(current, segment);
    if ((await lstat(current)).isSymbolicLink()) throw new Error(`Database path contains a symbolic link: ${toPosix(path.relative(root, current))}`);
  }
}

async function resolveDatabasePath(root: string, input: string): Promise<string> {
  const relativePath = input.trim();
  if (!relativePath) throw new Error("inspect_database path is required.");
  if (path.isAbsolute(relativePath)) throw new Error("inspect_database accepts only a relative database path inside the task repository.");
  if (!relativePath.toLowerCase().endsWith(".duckdb")) throw new Error("inspect_database accepts only .duckdb files.");
  const absolutePath = assertWithin(path.resolve(root, relativePath), root, "database path");
  try {
    await rejectSymlinkComponents(root, absolutePath);
    const info = await lstat(absolutePath);
    if (info.isSymbolicLink()) throw new Error("inspect_database does not accept symbolic links.");
    if (!info.isFile()) throw new Error(`Database path is not a regular file: ${relativePath}`);
    await existingPathWithin(root, absolutePath);
  } catch (error) {
    if (isFileSystemError(error, "ENOENT")) throw new Error(`Database file does not exist: ${relativePath}`);
    throw error;
  }
  return absolutePath;
}

function outputPage(rows: Array<Record<string, unknown>>, offset: number, hasMore: boolean): { output: string; nextOffset: number | null; rowCount: number } {
  const selected: Array<Record<string, unknown>> = [];
  for (const row of rows.slice(0, PAGE_SIZE)) {
    const candidate = [...selected, row];
    const more = hasMore || candidate.length < rows.length;
    const output = JSON.stringify({ rows: candidate, next_offset: more ? offset + candidate.length : null });
    if (output.length > MAX_OUTPUT_CHARS) {
      if (selected.length === 0) throw new Error("inspect_database output record exceeds 32000 characters; narrow the schema or table.");
      break;
    }
    selected.push(row);
  }
  const more = hasMore || selected.length < rows.length;
  return {
    output: JSON.stringify({ rows: selected, next_offset: more ? offset + selected.length : null }),
    nextOffset: more ? offset + selected.length : null,
    rowCount: selected.length,
  };
}

export async function runDatabaseInspection(options: DatabaseInspectionOptions): Promise<DatabaseInspectionResult> {
  const root = await realpath(path.resolve(options.root));
  const request = options.request;
  if (request.action !== "tables" && request.action !== "columns") throw new Error(`Unsupported inspection action: ${request.action}`);
  if (request.action === "tables" && request.table !== undefined) throw new Error("inspect_database table is only supported with action=columns.");
  if (request.action === "columns" && (!request.schema || !request.table)) throw new Error("inspect_database columns requires both schema and table.");
  const offset = request.offset ?? 0;
  if (!Number.isInteger(offset) || offset < 0) throw new Error("inspect_database offset must be a non-negative integer.");
  if (options.remainingMs() <= 0) throw new Error("The run wall-clock budget is exhausted.");
  const databasePath = await resolveDatabasePath(root, request.path);
  const runtimeDir = path.resolve(options.runtimeDir);
  const timeoutMs = Math.min(30_000, options.commandTimeoutMs, Math.max(1, options.remainingMs()));
  const result = await runRestrictedCommand(options.pythonCommand, ["-I", "-c", INSPECTION_PYTHON, databasePath, request.action, request.schema ?? "", request.table ?? "", String(offset), String(PAGE_SIZE)], {
    cwd: root,
    env: safeChildEnv({ PYTHONUNBUFFERED: "1", DATAAGENT_TASK_ROOT: root, DATAAGENT_RUNTIME_DIR: runtimeDir }),
    timeoutMs,
    signal: options.signal,
    maxCapturedChars: 1_000_000,
    root,
    runtimeDir,
  });
  if (result.timed_out) throw new Error("inspect_database timed out after 30 seconds or the configured remaining budget.");
  if (result.aborted) throw new Error("inspect_database was cancelled.");
  if (result.exit_code !== 0) {
    const detail = result.stderr.trim() || `restricted Python exited with code ${result.exit_code ?? "null"}`;
    throw new Error(`inspect_database failed: ${detail.slice(0, 4_000)}`);
  }
  let payload: { rows?: unknown; has_more?: unknown };
  try {
    payload = JSON.parse(result.stdout.trim()) as { rows?: unknown; has_more?: unknown };
  } catch {
    throw new Error("inspect_database returned invalid metadata output.");
  }
  if (!Array.isArray(payload.rows) || typeof payload.has_more !== "boolean") throw new Error("inspect_database returned an invalid metadata response.");
  const rows = payload.rows.filter((row): row is Record<string, unknown> => row !== null && typeof row === "object" && !Array.isArray(row));
  if (rows.length !== payload.rows.length) throw new Error("inspect_database returned an invalid metadata row.");
  const page = outputPage(rows, offset, payload.has_more);
  return {
    output: page.output,
    nextOffset: page.nextOffset,
    rowCount: page.rowCount,
    command: result.command,
    sandboxBackend: result.sandbox_backend ?? "unavailable",
    durationMs: result.duration_ms,
  };
}

export function createDatabaseInspectionTool(options: Omit<DatabaseInspectionOptions, "request" | "signal">): ToolDefinition {
  return defineTool({
    name: "inspect_database",
    label: "inspect_database",
    description: "Inspect tables or columns in one read-only DuckDB file inside the current task repository.",
    parameters: Type.Object({
      path: Type.String({ description: "Relative .duckdb path inside the current task repository" }),
      action: Type.Union([Type.Literal("tables"), Type.Literal("columns")]),
      schema: Type.Optional(Type.String({ description: "Schema name; optional for tables" })),
      table: Type.Optional(Type.String({ description: "Table or view name; required for columns" })),
      offset: Type.Optional(Type.Number({ description: "Zero-based page offset; defaults to 0" })),
    }),
    execute: async (_toolCallId, params, signal) => {
      const result = await runDatabaseInspection({ ...options, request: params, signal });
      return {
        content: [{ type: "text", text: result.output }],
        details: {
          path: params.path,
          action: params.action,
          offset: params.offset ?? 0,
          next_offset: result.nextOffset,
          row_count: result.rowCount,
          command: result.command,
          sandbox_backend: result.sandboxBackend,
          duration_ms: result.durationMs,
        },
      };
    },
  });
}
