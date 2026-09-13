import { readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";
import { createDatabaseInspectionTool } from "./database-inspection.js";
import { assertWithin, ensureDir, existingPathWithin, fileExists, toPosix, truncateText, walkFiles, writablePathWithin } from "./files.js";
import { runRestrictedCommand, safeChildEnv } from "./process.js";
import type { DbtValidationRecord, FixedAgentConfig } from "./types.js";

function rootPath(root: string, input: string): string {
  return assertWithin(path.resolve(root, input || "."), root, "tool path");
}

async function safeReadPath(root: string, input: string): Promise<string> {
  return existingPathWithin(root, rootPath(root, input));
}

async function readTextAt(root: string, input: string): Promise<{ absolutePath: string; content: string }> {
  const absolutePath = await safeReadPath(root, input);
  const info = await stat(absolutePath);
  if (!info.isFile()) throw new Error(`Not a file: ${input}`);
  if (info.size > 5_000_000) {
    throw new Error(`File too large to read as text (${info.size} bytes); inspect it with dbt_build or search_files instead.`);
  }
  const buffer = await readFile(absolutePath);
  if (buffer.subarray(0, 8192).includes(0)) {
    throw new Error(`Binary file detected (${input}); use inspect_database for .duckdb tables and columns instead of read_file.`);
  }
  return { absolutePath, content: buffer.toString("utf8") };
}

const MAX_TOOL_TEXT_CHARS = 32_000;
const MAX_SEARCH_LINE_CHARS = 1_000;

function truncateWithNotice(value: string, maxChars: number, notice: string): string {
  if (value.length <= maxChars) return value;
  const suffix = notice.slice(0, maxChars);
  return `${value.slice(0, Math.max(0, maxChars - suffix.length))}${suffix}`;
}

function findLineRange(content: string, offset?: number, limit?: number): string {
  const lines = content.split("\n");
  const start = Math.max(0, (offset ?? 1) - 1);
  if (start >= lines.length) throw new Error(`offset ${offset} is beyond the end of the file`);
  const end = limit === undefined ? lines.length : Math.min(lines.length, start + Math.max(0, limit));
  let output = lines.slice(start, end).join("\n");
  if (end < lines.length) output += `\n\n[${lines.length - end} more lines; use offset=${end + 1}]`;
  return truncateWithNotice(output, MAX_TOOL_TEXT_CHARS, "\n\n[output truncated at 32000 characters; use offset/limit to continue or narrow the file]");
}

function replaceExact(content: string, edits: Array<{ oldText: string; newText: string }>): string {
  const locations: Array<{ start: number; end: number; newText: string }> = [];
  for (const edit of edits) {
    if (!edit.oldText) throw new Error("each edit must provide non-empty oldText");
    const first = content.indexOf(edit.oldText);
    if (first < 0 || first !== content.lastIndexOf(edit.oldText)) throw new Error("each oldText must match exactly once");
    const location = { start: first, end: first + edit.oldText.length, newText: edit.newText };
    if (locations.some((other) => location.start < other.end && other.start < location.end)) throw new Error("edits must not overlap");
    locations.push(location);
  }
  locations.sort((left, right) => right.start - left.start);
  let result = content;
  for (const edit of locations) result = `${result.slice(0, edit.start)}${edit.newText}${result.slice(edit.end)}`;
  return result;
}

function globToRegExp(pattern: string): RegExp {
  let source = "^";
  for (let index = 0; index < pattern.length; index += 1) {
    const character = pattern[index];
    if (character === "*" && pattern[index + 1] === "*") {
      source += ".*";
      index += 1;
    } else if (character === "*") {
      source += "[^/]*";
    } else if (character === "?") {
      source += "[^/]";
    } else {
      source += character.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
  }
  return new RegExp(`${source}$`);
}

function safeTaskEnv(config: FixedAgentConfig, root: string, runtimeDir: string, profilesDir: string): NodeJS.ProcessEnv {
  const targetDir = path.join(runtimeDir, "target");
  const logDir = path.join(runtimeDir, "logs");
  const userConfigDir = path.join(runtimeDir, "user-config");
  const tempDir = path.join(runtimeDir, "tmp");
  const homeDir = path.join(runtimeDir, "home");
  const cacheDir = path.join(runtimeDir, "cache");
  const pycacheDir = path.join(runtimeDir, "pycache");
  return safeChildEnv({
    DBT_PROFILES_DIR: profilesDir,
    DBT_TARGET_PATH: targetDir,
    DBT_LOG_PATH: logDir,
    DBT_USER_CONFIG_DIR: userConfigDir,
    TMPDIR: tempDir,
    TMP: tempDir,
    TEMP: tempDir,
    HOME: homeDir,
    XDG_CACHE_HOME: cacheDir,
    PYTHONPYCACHEPREFIX: pycacheDir,
    DATAAGENT_TASK_ROOT: root,
    DATAAGENT_RUNTIME_DIR: runtimeDir,
    PYTHONUNBUFFERED: "1",
    DATAAGENT_DBT_COMMAND: config.commands.dbt,
  });
}

async function visibleProfilesDirectory(root: string, fallback: string): Promise<string> {
  for (const directory of [root, path.join(root, ".dbt")]) {
    if (await fileExists(path.join(directory, "profiles.yml")) || await fileExists(path.join(directory, "profiles.yaml"))) return directory;
  }
  return fallback;
}

export function createSafeTools(options: {
  root: string;
  runtimeDir: string;
  config: FixedAgentConfig;
  remainingMs: () => number;
  onDbtValidation: (record: DbtValidationRecord) => Promise<void>;
}): ToolDefinition[] {
  const root = path.resolve(options.root);
  const readFileTool = defineTool({
    name: "read_file",
    label: "read_file",
    description: "Read a text file inside the current task repository.",
    parameters: Type.Object({
      path: Type.String({ description: "Relative path inside the task repository" }),
      offset: Type.Optional(Type.Number({ description: "1-indexed starting line" })),
      limit: Type.Optional(Type.Number({ description: "Maximum number of lines" })),
    }),
    execute: async (_toolCallId, params) => {
      const file = await readTextAt(root, params.path);
      return { content: [{ type: "text", text: findLineRange(file.content, params.offset, params.limit) }], details: { path: params.path } };
    },
  });

  const editFileTool = defineTool({
    name: "edit_file",
    label: "edit_file",
    description: "Make exact, non-overlapping text replacements in one file inside the current task repository.",
    parameters: Type.Object({
      path: Type.String({ description: "Relative path inside the task repository" }),
      edits: Type.Array(Type.Object({ oldText: Type.String(), newText: Type.String() })),
    }),
    execute: async (_toolCallId, params) => {
      const file = await readTextAt(root, params.path);
      const newContent = replaceExact(file.content, params.edits);
      const absolutePath = await writablePathWithin(root, file.absolutePath);
      await writeFile(absolutePath, newContent, "utf8");
      return { content: [{ type: "text", text: `Updated ${params.path} with ${params.edits.length} exact replacement(s).` }], details: { path: params.path } };
    },
  });

  const writeFileTool = defineTool({
    name: "write_file",
    label: "write_file",
    description: "Create or overwrite a text file inside the current task repository.",
    parameters: Type.Object({
      path: Type.String({ description: "Relative path inside the task repository" }),
      content: Type.String(),
    }),
    execute: async (_toolCallId, params) => {
      const absolutePath = await writablePathWithin(root, rootPath(root, params.path));
      await ensureDir(path.dirname(absolutePath));
      await writeFile(absolutePath, params.content, "utf8");
      return { content: [{ type: "text", text: `Wrote ${params.path}.` }], details: { path: params.path } };
    },
  });

  const listFilesTool = defineTool({
    name: "list_files",
    label: "list_files",
    description: "List files inside the current task repository; paths outside it are rejected.",
    parameters: Type.Object({
      path: Type.Optional(Type.String({ description: "Relative directory inside the task repository" })),
      limit: Type.Optional(Type.Number({ description: "Maximum number of files" })),
    }),
    execute: async (_toolCallId, params) => {
      const directory = await safeReadPath(root, params.path ?? ".");
      const info = await stat(directory);
      if (!info.isDirectory()) throw new Error(`Not a directory: ${params.path ?? "."}`);
      const limit = Math.min(500, Math.max(1, params.limit ?? 200));
      const entries = await walkFiles(directory, { skip: (relativePath) => relativePath === ".git" || relativePath.startsWith(".git/") || relativePath === "node_modules" || relativePath.startsWith("node_modules/") || relativePath === "target" || relativePath.startsWith("target/") });
      const output = entries.filter((entry) => entry.kind === "file").slice(0, limit).map((entry) => entry.relativePath);
      if (entries.filter((entry) => entry.kind === "file").length > limit) output.push(`[limit=${limit}]`);
      return { content: [{ type: "text", text: output.join("\n") || "(empty directory)" }], details: { path: params.path ?? "." } };
    },
  });

  const searchFilesTool = defineTool({
    name: "search_files",
    label: "search_files",
    description: "Search text files inside the current task repository without invoking a shell.",
    parameters: Type.Object({
      pattern: Type.String({ description: "Regular expression or literal text" }),
      path: Type.Optional(Type.String({ description: "Relative file or directory" })),
      glob: Type.Optional(Type.String({ description: "Relative glob filter such as **/*.sql" })),
      literal: Type.Optional(Type.Boolean()),
      ignoreCase: Type.Optional(Type.Boolean()),
      limit: Type.Optional(Type.Number()),
    }),
    execute: async (_toolCallId, params) => {
      const searchPath = await safeReadPath(root, params.path ?? ".");
      const searchInfo = await stat(searchPath);
      const files = searchInfo.isDirectory()
        ? (await walkFiles(searchPath, { skip: (relativePath) => relativePath === ".git" || relativePath.startsWith(".git/") || relativePath === "node_modules" || relativePath.startsWith("node_modules/") || relativePath === "target" || relativePath.startsWith("target/") })).filter((entry) => entry.kind === "file")
        : [{ absolutePath: searchPath, relativePath: path.basename(searchPath), kind: "file" as const, size: searchInfo.size }];
      const pattern = params.literal ? undefined : new RegExp(params.pattern, params.ignoreCase ? "i" : "");
      const needle = params.ignoreCase ? params.pattern.toLowerCase() : params.pattern;
      const matches: string[] = [];
      const glob = params.glob ? globToRegExp(toPosix(params.glob)) : undefined;
      const limit = Math.min(200, Math.max(1, params.limit ?? 100));
      for (const file of files) {
        if (glob && !glob.test(toPosix(file.relativePath))) continue;
        if (file.size > 5_000_000) continue;
        const buffer = await readFile(file.absolutePath).catch(() => undefined);
        if (!buffer || buffer.subarray(0, 8192).includes(0)) continue;
        const content = buffer.toString("utf8");
        const lines = content.split(/\r?\n/);
        for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
          const line = lines[lineIndex];
          const haystack = params.ignoreCase ? line.toLowerCase() : line;
          const matched = pattern ? pattern.test(line) : haystack.includes(needle);
          if (!matched) continue;
          const prefix = `${toPosix(file.relativePath)}:${lineIndex + 1}: `;
          matches.push(`${prefix}${truncateWithNotice(line, Math.max(0, MAX_SEARCH_LINE_CHARS - prefix.length), "…[line truncated]")}`);
          if (matches.length >= limit) break;
        }
        if (matches.length >= limit) break;
      }
      if (matches.length >= limit) matches.push(`[limit=${limit}]`);
      const output = matches.join("\n") || "No matches found";
      return {
        content: [{ type: "text", text: truncateWithNotice(output, MAX_TOOL_TEXT_CHARS, "\n[search output truncated at 32000 characters; narrow the path/pattern or lower limit]") }],
        details: { path: params.path ?? "." },
      };
    },
  });

  let dbtCommandNumber = 0;
  const dbtBuildTool = defineTool({
    name: "dbt_build",
    label: "dbt_build",
    description: "Run one bounded local dbt action in the current task repository. Allowed actions: build, test, run, compile, debug.",
    parameters: Type.Object({
      action: Type.Union([Type.Literal("build"), Type.Literal("test"), Type.Literal("run"), Type.Literal("compile"), Type.Literal("debug")]),
      select: Type.Optional(Type.String({ description: "Optional simple dbt selector; shell syntax is not accepted" })),
    }),
    execute: async (_toolCallId, params, signal) => {
      const remainingMs = options.remainingMs();
      if (remainingMs <= 0) throw new Error("The run wall-clock budget is exhausted.");
      if (params.select && !/^[A-Za-z0-9_.*+@:/,-]+$/.test(params.select)) throw new Error("select contains unsupported shell characters");
      dbtCommandNumber += 1;
      const runtimeDir = path.join(options.runtimeDir, "dbt");
      const fallbackProfilesDir = path.join(runtimeDir, "profiles");
      const profilesDir = await visibleProfilesDirectory(root, fallbackProfilesDir);
      const targetDir = path.join(runtimeDir, "target");
      const logDir = path.join(runtimeDir, "logs");
      const userConfigDir = path.join(runtimeDir, "user-config");
      await Promise.all([
        ensureDir(profilesDir),
        ensureDir(targetDir),
        ensureDir(logDir),
        ensureDir(userConfigDir),
        ensureDir(path.join(runtimeDir, "tmp")),
        ensureDir(path.join(runtimeDir, "home")),
        ensureDir(path.join(runtimeDir, "cache")),
        ensureDir(path.join(runtimeDir, "pycache")),
      ]);
      if (params.action === "debug" && params.select) throw new Error("select is not supported for dbt debug");
      const commandArgs = ["--no-version-check", params.action, "--profiles-dir", profilesDir];
      if (params.action !== "debug") commandArgs.push("--target-path", targetDir);
      commandArgs.push("--log-path", logDir);
      if (params.select) commandArgs.push("--select", params.select);
      const startedAt = Date.now();
      const result = await runRestrictedCommand(options.config.commands.dbt, commandArgs, {
        cwd: root,
        env: safeTaskEnv(options.config, root, runtimeDir, profilesDir),
        timeoutMs: Math.min(options.config.limits.commandTimeoutMs, remainingMs),
        signal,
        maxCapturedChars: 256_000,
        root,
        runtimeDir,
      });
      const logPath = path.join(options.runtimeDir, `dbt-${String(dbtCommandNumber).padStart(2, "0")}-${params.action}.log`);
      await writeFile(logPath, `${result.command.join(" ")}\n\n[stdout]\n${result.stdout}\n\n[stderr]\n${result.stderr}\n`, "utf8");
      const record: DbtValidationRecord = {
        action: params.action,
        command: result.command,
        started_at: result.started_at,
        ended_at: result.ended_at,
        duration_ms: result.duration_ms,
        exit_code: result.exit_code,
        timed_out: result.timed_out,
        log_path: logPath,
        stdout_summary: truncateText(result.stdout, 16_000),
        stderr_summary: truncateText(result.stderr, 16_000),
        source: "agent_tool",
        sandbox_backend: result.sandbox_backend ?? "unavailable",
      };
      await options.onDbtValidation(record);
      const status = result.timed_out ? "timed out" : `exit_code=${result.exit_code ?? "null"}`;
      return {
        content: [{ type: "text", text: `dbt ${params.action} ${status}\n${truncateText(`${result.stdout}\n${result.stderr}`.trim(), 16_000)}` }],
        details: { command: result.command, exit_code: result.exit_code, timed_out: result.timed_out, aborted: result.aborted, sandbox_backend: result.sandbox_backend ?? "unavailable", log_path: logPath, duration_ms: Date.now() - startedAt },
      };
    },
  });

  const databaseInspectionTool = createDatabaseInspectionTool({
    root,
    runtimeDir: options.runtimeDir,
    pythonCommand: options.config.commands.python,
    commandTimeoutMs: options.config.limits.commandTimeoutMs,
    remainingMs: options.remainingMs,
  });
  return [readFileTool, editFileTool, writeFileTool, listFilesTool, searchFilesTool, dbtBuildTool, databaseInspectionTool];
}
