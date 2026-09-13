import { chmod, mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { loadSelection, pythonSeedSample, prepareSelection } from "../src/selection.js";
import { existingPathWithin, fileExists, walkFiles } from "../src/files.js";
import { buildSubmission, hasValidSubmissionArtifact, validateRoundResultDirectory, writeRoundMetadata } from "../src/submission.js";
import { runExperiment } from "../src/experiment.js";
import { markNotStarted, type ExperimentPlan } from "../src/experiment.js";
import { writeDiagnosticReport } from "../src/report.js";
import { evaluateRound } from "../src/evaluator.js";
import { assistantMessageError, lastAssistantText, modelRequestLimitReached, selectFinalArtifactResponse, wrapModelStreamFunction } from "../src/agent.js";
import { runCommand, runRestrictedCommand, safeChildEnv } from "../src/process.js";
import { validationStatus } from "../src/receipt.js";
import type { FixedAgentConfig, RunReceipt } from "../src/types.js";
import { createSafeTools } from "../src/safe-tools.js";
import { runDatabaseInspection } from "../src/database-inspection.js";
import { createFixedConfig } from "../src/config.js";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";

function toolText(result: unknown): string {
  const content = (result as { content?: Array<{ type?: string; text?: string }> }).content ?? [];
  return content.filter((item) => item.type === "text").map((item) => item.text ?? "").join("");
}

function toolDetails(result: unknown): Record<string, unknown> {
  return ((result as { details?: Record<string, unknown> }).details ?? {});
}

function offlineToolConfig(): FixedAgentConfig {
  return {
    name: "test",
    model: "test/model",
    thinking: "off",
    systemPromptPath: "",
    systemPrompt: "",
    tools: [],
    sdk: { package: "test", version: "test", compactionEnabled: false, retryEnabled: false, maxRetries: 0 },
    parameters: { temperature: "N/A: not exposed by the SDK session API", top_p: "N/A: not exposed by the SDK session API" },
    limits: { wallClockMs: 10_000, maxModelRequestAttempts: 30, commandTimeoutMs: 10_000, evaluatorTimeoutMs: 10_000 },
    commands: { python: "python3", dbt: "dbt", duckdb: "duckdb" },
  };
}

type TestContextLike = { skip: (message?: string) => void };

function testPythonCommand(): string {
  return process.env.DATAAGENT_TEST_PYTHON?.trim() || offlineToolConfig().commands.python;
}

function restrictedProbeFailure(result: { exit_code: number | null; timed_out: boolean; aborted: boolean; stderr: string; sandbox_backend?: string }): string {
  const detail = result.stderr.trim().replace(/\s+/g, " ").slice(0, 240);
  return `backend=${result.sandbox_backend ?? "unavailable"}; exit_code=${result.exit_code ?? "null"}; timed_out=${result.timed_out}; aborted=${result.aborted}${detail ? `; ${detail}` : ""}`;
}

async function requireRestrictedBackend(t: TestContextLike, root: string, runtimeDir: string): Promise<boolean> {
  const result = await runRestrictedCommand("/bin/true", [], {
    cwd: root,
    root,
    runtimeDir,
    env: safeChildEnv({ TMPDIR: path.join(runtimeDir, "tmp"), HOME: path.join(runtimeDir, "home") }),
    timeoutMs: 10_000,
    maxCapturedChars: 2_000,
  });
  const usable = (result.sandbox_backend === "macos-sandbox-exec" || result.sandbox_backend === "docker") && result.exit_code === 0 && !result.timed_out && !result.aborted;
  if (!usable) {
    t.skip(`UNACCEPTED: restricted backend unavailable or failed: ${restrictedProbeFailure(result)}`);
    return false;
  }
  return true;
}

async function requireLiveDuckDb(t: TestContextLike, root: string): Promise<string | undefined> {
  const pythonCommand = testPythonCommand();
  const hostProbe = await runCommand(pythonCommand, ["-I", "-c", "import duckdb"], { cwd: root, env: safeChildEnv(), timeoutMs: 10_000, maxCapturedChars: 2_000 });
  if (hostProbe.exit_code !== 0 || hostProbe.timed_out || hostProbe.aborted) {
    t.skip(`UNACCEPTED: test Python/DuckDB unavailable: ${hostProbe.stderr.trim().replace(/\s+/g, " ").slice(0, 240) || `exit_code=${hostProbe.exit_code ?? "null"}`}`);
    return undefined;
  }
  const restrictedProbe = await runRestrictedCommand(pythonCommand, ["-I", "-c", "import duckdb"], {
    cwd: root,
    root,
    runtimeDir: path.join(root, "restricted-python-probe"),
    env: safeChildEnv({ PYTHONUNBUFFERED: "1" }),
    timeoutMs: 10_000,
    maxCapturedChars: 2_000,
  });
  const usable = (restrictedProbe.sandbox_backend === "macos-sandbox-exec" || restrictedProbe.sandbox_backend === "docker") && restrictedProbe.exit_code === 0 && !restrictedProbe.timed_out && !restrictedProbe.aborted;
  if (!usable) {
    t.skip(`UNACCEPTED: restricted Python/DuckDB probe failed: ${restrictedProbeFailure(restrictedProbe)}`);
    return undefined;
  }
  return pythonCommand;
}

function assertSuccessfulRestrictedTool(result: unknown): Record<string, unknown> {
  const details = toolDetails(result);
  assert.equal(details.exit_code, 0);
  assert.equal(details.timed_out, false);
  assert.equal(details.aborted, false);
  assert.ok(details.sandbox_backend === "macos-sandbox-exec" || details.sandbox_backend === "docker");
  return details;
}

test("fixed seed sampler matches Python random.Random.sample for the protocol seed", () => {
  const population = Array.from({ length: 69 }, (_, index) => index);
  assert.deepEqual(pythonSeedSample(population, 12), [46, 1, 57, 11, 22, 52, 20, 31, 16, 8, 36, 48]);
});

test("selection emits 12 tasks with a fixed 3/6/3 tier split from a local-only pool", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-selection-"));
  try {
    const examplesRoot = path.join(root, "examples");
    const outputDir = path.join(root, "selection");
    await mkdir(examplesRoot, { recursive: true });
    const tasks: string[] = [];
    for (let index = 0; index < 12; index += 1) {
      const instanceId = `task-${String(index).padStart(2, "0")}`;
      const project = path.join(examplesRoot, instanceId);
      await mkdir(path.join(project, "models"), { recursive: true });
      await writeFile(path.join(project, "dbt_project.yml"), `name: ${instanceId}\nversion: '1.0'\nprofile: local\n`, "utf8");
      await writeFile(path.join(project, "profiles.yml"), "local:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: data.duckdb\n", "utf8");
      await writeFile(path.join(project, "data.duckdb"), "fixture\n", "utf8");
      const modelCount = index % 3 === 0 ? 1 : index % 3 === 1 ? 2 : 4;
      for (let modelIndex = 0; modelIndex < modelCount; modelIndex += 1) {
        const name = `model_${modelIndex}`;
        const ref = modelIndex > 0 ? `select * from {{ ref('model_${modelIndex - 1}') }}` : "select 1 as id";
        const extra = modelIndex === modelCount - 1 && index % 3 === 2 ? " row_number() over (partition by id order by id) as rn, case when id = 1 then 1 else 0 end as flag" : "";
        await writeFile(path.join(project, "models", `${name}.sql`), `${ref}${extra}\n`, "utf8");
      }
      tasks.push(JSON.stringify({ instance_id: instanceId, instruction: `Update model_${modelCount - 1}`, type: "DBT" }));
    }
    const taskFile = path.join(root, "tasks.jsonl");
    await writeFile(taskFile, `${tasks.join("\n")}\n`, "utf8");
    const manifest = await prepareSelection({ taskFile, examplesRoot, outputDir, poolSize: 12 });
    assert.equal(manifest.status, "ready");
    assert.equal(manifest.sampled_pool_size, 12);
    assert.equal(manifest.selected.length, 12);
    assert.deepEqual(Object.fromEntries(Object.entries(manifest.tiers).map(([tier, ids]) => [tier, ids.length])), { low: 3, medium: 6, high: 3 });
    assert.equal((await readFile(path.join(outputDir, "selected_tasks.jsonl"), "utf8")).trim().split("\n").length, 12);
    assert.equal((await loadSelection(path.join(outputDir, "selection.json"))).selected.length, 12);
    const malformedTiers = JSON.parse(await readFile(path.join(outputDir, "selection.json"), "utf8"));
    malformedTiers.tiers.medium = malformedTiers.tiers.medium.slice(0, 5);
    const malformedTiersPath = path.join(root, "selection-bad-tiers.json");
    await writeFile(malformedTiersPath, JSON.stringify(malformedTiers), "utf8");
    await assert.rejects(() => loadSelection(malformedTiersPath), /exactly 3 low, 6 medium, and 3 high/);
    const oneTask = JSON.parse(await readFile(path.join(outputDir, "selection.json"), "utf8"));
    oneTask.selected = oneTask.selected.slice(0, 1);
    const oneTaskPath = path.join(root, "selection-one-task.json");
    await writeFile(oneTaskPath, JSON.stringify(oneTask), "utf8");
    await assert.rejects(() => loadSelection(oneTaskPath), /exactly 12 sampled and selected tasks/);
    const duplicateIds = JSON.parse(await readFile(path.join(outputDir, "selection.json"), "utf8"));
    duplicateIds.selected[1].row.instance_id = duplicateIds.selected[0].row.instance_id;
    const duplicateIdsPath = path.join(root, "selection-duplicate-ids.json");
    await writeFile(duplicateIdsPath, JSON.stringify(duplicateIds), "utf8");
    await assert.rejects(() => loadSelection(duplicateIdsPath), /duplicate or inconsistent task IDs/);

    const confirmationPath = path.join(root, "scope-confirmations.jsonl");
    await writeFile(confirmationPath, `${JSON.stringify({
      instance_id: "task-00",
      related_models: ["models/model_0.sql"],
      scope_observations: "score=0; estimated_related_files=4; models=1",
      dependency_observations: "score=0; visible_longest_chain_edges=0; branching=false; unknown=false",
      constraint_observations: "score=0; indicators=none",
    })}\n`, "utf8");
    const confirmedManifest = await prepareSelection({
      taskFile,
      examplesRoot,
      outputDir: path.join(root, "selection-confirmed"),
      poolSize: 12,
      scopeConfirmationPath: confirmationPath,
    });
    assert.equal(confirmedManifest.status, "ready");
    const loadedConfirmedManifest = await loadSelection(path.join(root, "selection-confirmed", "selection.json"));
    assert.equal(loadedConfirmedManifest.scope_confirmation_path, path.join(root, "selection-confirmed", "scope-confirmations.jsonl"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("fixed protocol rejects non-fixed model and thinking settings", () => {
  assert.throws(() => createFixedConfig({ projectRoot: process.cwd(), model: "other/model", thinking: "off", systemPromptPath: "config/system-prompt.md", systemPrompt: "" }), /requires model deepseek\/deepseek-v4-flash/);
  assert.throws(() => createFixedConfig({ projectRoot: process.cwd(), model: "deepseek\/deepseek-v4-flash", thinking: "medium", systemPromptPath: "config/system-prompt.md", systemPrompt: "" }), /requires thinking off/);
});

test("submission artifacts are copied into an official round directory and path escapes fail", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-submission-"));
  try {
    const repo = path.join(root, "repo");
    const resultDir = path.join(root, "results", "round-1");
    await mkdir(repo, { recursive: true });
    await writeFile(path.join(repo, "answer.duckdb"), "fixture\n", "utf8");
    await writeFile(path.join(repo, "answer.sql"), "select 1\n", "utf8");
    await writeFile(path.join(repo, "answer.csv"), "id,value\n1,ok\n", "utf8");
    await writeFile(path.join(repo, "second.duckdb"), "fixture\n", "utf8");
    await mkdir(path.join(repo, "artifact-directory"), { recursive: true });
    const submission = await buildSubmission({ instanceId: "task-01", finalResponse: "answer.duckdb", workspaceRepo: repo, roundResultDir: resultDir });
    assert.equal(submission.record.answer_type, "file");
    assert.equal(await readFile(path.join(resultDir, "task-01", "answer.duckdb"), "utf8"), "fixture\n");
    for (const [instanceId, finalResponse] of [
      ["task-sql", "answer.sql"],
      ["task-csv", "answer.csv"],
      ["task-dir", "artifact-directory"],
      ["task-multiple", "[\"answer.duckdb\",\"second.duckdb\"]"],
    ] as const) {
      const invalid = await buildSubmission({ instanceId, finalResponse, workspaceRepo: repo, roundResultDir: resultDir });
      assert.equal(invalid.metadataEntry, undefined);
      assert.equal(invalid.record.failure_label, "invalid_submission_artifact");
    }
    const format = await validateRoundResultDirectory({ resultDir, expectedInstanceIds: ["task-01"] });
    assert.equal(format.ok, false);
    await writeFile(path.join(resultDir, "results_metadata.jsonl"), `${JSON.stringify(submission.metadataEntry)}\n`, "utf8");
    const validFormat = await validateRoundResultDirectory({ resultDir, expectedInstanceIds: ["task-01"] });
    assert.equal(validFormat.ok, true);
    await assert.rejects(() => existingPathWithin(repo, path.join(repo, "..", "outside.txt")), /escapes|ENOENT/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("inspect_database returns stable table and column metadata with pagination", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-database-inspection-"));
  try {
    const pythonCommand = await requireLiveDuckDb(t, root);
    if (!pythonCommand) return;
    const databasePath = path.join(root, "fixture.duckdb");
    const setup = [
      "import sys",
      "import duckdb",
      "connection = duckdb.connect(sys.argv[1])",
      "connection.execute('CREATE SCHEMA analytics')",
      "connection.execute('CREATE TABLE main.\"weird table\" (\"not null\" INTEGER NOT NULL, \"has space\" VARCHAR)')",
      "connection.execute('CREATE VIEW analytics.\"Sales View\" AS SELECT 1 AS \"View Column\"')",
      "for index in range(105): connection.execute(f'CREATE TABLE main.\"table_{index:03d}\" (id INTEGER)')",
      "connection.close()",
      "",
    ].join("\n");
    const setupResult = await runCommand(pythonCommand, ["-I", "-c", setup, databasePath], { cwd: root, env: safeChildEnv(), timeoutMs: 10_000, maxCapturedChars: 4_000 });
    assert.equal(setupResult.exit_code, 0, setupResult.stderr);
    const before = await readFile(databasePath);
    const inspect = (request: Parameters<typeof runDatabaseInspection>[0]["request"]) => runDatabaseInspection({
      root,
      runtimeDir: path.join(root, "runtime"),
      pythonCommand,
      commandTimeoutMs: 30_000,
      remainingMs: () => 30_000,
      request,
    });

    const firstPage = await inspect({ path: "fixture.duckdb", action: "tables" });
    const firstPayload = JSON.parse(firstPage.output) as { rows: Array<Record<string, unknown>>; next_offset: number | null };
    assert.equal(firstPayload.rows.length, 100);
    assert.equal(firstPayload.next_offset, 100);
    assert.equal(firstPage.output.length <= 32_000, true);
    assert.equal(firstPayload.rows.some((row) => row.table_name === "Sales View" && row.table_type === "VIEW"), true);

    const secondPage = await inspect({ path: "fixture.duckdb", action: "tables", offset: firstPayload.next_offset ?? 0 });
    const secondPayload = JSON.parse(secondPage.output) as { rows: Array<Record<string, unknown>>; next_offset: number | null };
    assert.equal(secondPayload.rows.length > 0, true);
    assert.equal(secondPayload.next_offset, null);
    assert.equal(new Set(firstPayload.rows.map((row) => row.table_name)).size + new Set(secondPayload.rows.map((row) => row.table_name)).size, 107);

    const schemaPage = JSON.parse((await inspect({ path: "fixture.duckdb", action: "tables", schema: "analytics" })).output) as { rows: Array<Record<string, unknown>>; next_offset: number | null };
    assert.deepEqual(schemaPage.rows, [{ database_name: "fixture", schema_name: "analytics", table_name: "Sales View", table_type: "VIEW" }]);
    assert.equal(schemaPage.next_offset, null);
    const columns = JSON.parse((await inspect({ path: "fixture.duckdb", action: "columns", schema: "main", table: "weird table" })).output) as { rows: Array<Record<string, unknown>>; next_offset: number | null };
    assert.deepEqual(columns.rows, [
      { column_name: "not null", data_type: "INTEGER", is_nullable: "NO", ordinal_position: 1 },
      { column_name: "has space", data_type: "VARCHAR", is_nullable: "YES", ordinal_position: 2 },
    ]);
    assert.equal(columns.next_offset, null);
    assert.deepEqual(await readFile(databasePath), before);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("inspect_database rejects unsafe inputs and reports restricted failures", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-database-inspection-errors-"));
  try {
    const pythonCommand = await requireLiveDuckDb(t, root);
    if (!pythonCommand) return;
    const databasePath = path.join(root, "fixture.duckdb");
    const setupResult = await runCommand(pythonCommand, ["-I", "-c", "import duckdb, sys; c=duckdb.connect(sys.argv[1]); c.execute('create table main.items (id integer)'); c.close()", databasePath], { cwd: root, env: safeChildEnv(), timeoutMs: 10_000, maxCapturedChars: 2_000 });
    assert.equal(setupResult.exit_code, 0, setupResult.stderr);
    await symlink(databasePath, path.join(root, "linked.duckdb"));
    await writeFile(path.join(root, "broken.duckdb"), "not a DuckDB database\n", "utf8");
    const inspect = (request: Parameters<typeof runDatabaseInspection>[0]["request"], overrides: Partial<Parameters<typeof runDatabaseInspection>[0]> = {}) => runDatabaseInspection({
      root,
      runtimeDir: path.join(root, "runtime"),
      pythonCommand,
      commandTimeoutMs: 30_000,
      remainingMs: () => 30_000,
      request,
      ...overrides,
    });
    await assert.rejects(() => inspect({ path: databasePath, action: "tables" }), /relative database path/);
    await assert.rejects(() => inspect({ path: "../fixture.duckdb", action: "tables" }), /escapes its allowed root/);
    await assert.rejects(() => inspect({ path: "linked.duckdb", action: "tables" }), /symbolic link/);
    await assert.rejects(() => inspect({ path: "missing.duckdb", action: "tables" }), /does not exist/);
    await assert.rejects(() => inspect({ path: "fixture.db", action: "tables" }), /only \.duckdb/);
    await assert.rejects(() => inspect({ path: "fixture.duckdb", action: "columns", schema: "main", table: "missing" }), /Table not found/);
    await assert.rejects(() => inspect({ path: "fixture.duckdb", action: "columns", schema: "main", table: "items' OR 1=1 --" }), /Table not found/);
    await assert.rejects(() => inspect({ path: "broken.duckdb", action: "tables" }), /inspect_database failed/);
    await assert.rejects(() => inspect({ path: "fixture.duckdb", action: "tables", offset: -1 }), /non-negative integer/);
    await assert.rejects(() => inspect({ path: "fixture.duckdb", action: "tables" }, { remainingMs: () => 0 }), /wall-clock budget/);

    const sleeper = "./sleep-inspection.sh";
    await writeFile(path.join(root, "sleep-inspection.sh"), "#!/bin/sh\nsleep 2\n", "utf8");
    await chmod(path.join(root, "sleep-inspection.sh"), 0o755);
    await assert.rejects(() => inspect({ path: "fixture.duckdb", action: "tables" }, { pythonCommand: sleeper, remainingMs: () => 50 }), /timed out/);
    const controller = new AbortController();
    const cancelled = inspect({ path: "fixture.duckdb", action: "tables" }, { pythonCommand: sleeper, remainingMs: () => 2_000, signal: controller.signal });
    controller.abort();
    await assert.rejects(() => cancelled, /cancelled/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("dbt debug omits unsupported target arguments and rejects selectors", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-dbt-arguments-"));
  try {
    if (!await requireRestrictedBackend(t, root, path.join(root, "restricted-probe"))) return;
    const config = offlineToolConfig();
    config.commands.dbt = "/bin/true";
    const tools = createSafeTools({ root, runtimeDir: path.join(root, "runtime"), config, remainingMs: () => 10_000, onDbtValidation: async () => undefined });
    const execute = async (name: string, params: Record<string, unknown>) => {
      const tool = tools.find((candidate) => candidate.name === name);
      if (!tool) throw new Error(`missing ${name}`);
      return tool.execute("test", params as never, undefined, undefined, undefined as never);
    };
    assert.equal(tools.some((tool) => tool.name === "inspect_database"), true);
    const debug = assertSuccessfulRestrictedTool(await execute("dbt_build", { action: "debug" }));
    const debugCommand = (debug.command as string[] | undefined) ?? [];
    assert.equal(debugCommand.includes("--target-path"), false);
    assert.equal(debugCommand.includes("--log-path"), true);
    await assert.rejects(() => execute("dbt_build", { action: "debug", select: "model" }), /select is not supported/);
    const build = assertSuccessfulRestrictedTool(await execute("dbt_build", { action: "build", select: "model" }));
    const buildCommand = (build.command as string[] | undefined) ?? [];
    assert.equal(buildCommand.includes("--target-path"), true);
    assert.equal(buildCommand.includes("--log-path"), true);
    assert.equal(buildCommand.includes("--select"), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("task input symlinks are rejected by the isolation copy scanner", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-symlink-"));
  try {
    const taskRoot = path.join(root, "task");
    const outside = path.join(root, "outside.txt");
    await mkdir(taskRoot, { recursive: true });
    await writeFile(outside, "outside\n", "utf8");
    await symlink(outside, path.join(taskRoot, "linked.txt"));
    await assert.rejects(() => walkFiles(taskRoot), /Symbolic links are not allowed/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("experiment fails closed before model calls and records all 12 runs when preflight fails", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-preflight-"));
  try {
    const examplesRoot = path.join(root, "examples");
    const selectionDir = path.join(root, "selection");
    const taskSource = path.join(selectionDir, "official-task-list.jsonl");
    const evaluator = path.join(root, "evaluate.py");
    const goldDir = path.join(root, "gold");
    const experimentRoot = path.join(root, "experiment");
    await mkdir(selectionDir, { recursive: true });
    await mkdir(goldDir, { recursive: true });
    await writeFile(evaluator, "print('fake evaluator')\n", "utf8");
    await writeFile(path.join(goldDir, "spider2_eval.jsonl"), "\n", "utf8");
    const selected: Array<Record<string, unknown>> = [];
    const tiers = { low: [] as string[], medium: [] as string[], high: [] as string[] };
    for (let index = 0; index < 12; index += 1) {
      const instanceId = `preflight-${String(index).padStart(2, "0")}`;
      const tier = index < 3 ? "low" : index < 9 ? "medium" : "high";
      tiers[tier].push(instanceId);
      const project = path.join(examplesRoot, instanceId);
      await mkdir(path.join(project, "models"), { recursive: true });
      await writeFile(path.join(project, "dbt_project.yml"), "name: local\nprofile: local\n", "utf8");
      await writeFile(path.join(project, "profiles.yml"), "local:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: data.duckdb\n", "utf8");
      await writeFile(path.join(project, "data.duckdb"), "fixture\n", "utf8");
      await writeFile(path.join(project, "models", "model.sql"), "select 1 as id\n", "utf8");
      selected.push({
        row: { instance_id: instanceId, project: instanceId, scope_observations: "score=0", dependency_observations: "score=0; unknown=false", constraint_observations: "score=0", tier, selection_reason: "fixture" },
        eligible: true,
        scopeScore: 0,
        dependencyScore: 0,
        constraintScore: 0,
        totalScore: 0,
        estimatedRelatedFiles: 4,
        modelCount: 1,
        visibleLongestDependencyChainEdges: 0,
        hasUnknownDependencies: false,
        databaseFiles: ["data.duckdb"],
        modelFiles: ["models/model.sql"],
        instruction: `Update ${instanceId}`,
      });
    }
    await writeFile(taskSource, `${selected.map((item) => JSON.stringify({ instance_id: (item.row as { instance_id: string }).instance_id, instruction: item.instruction, type: "DBT" })).join("\n")}\n`, "utf8");
    await writeFile(path.join(selectionDir, "selection.json"), `${JSON.stringify({
      schema_version: "1.0",
      created_on: new Date().toISOString(),
      seed: 20260911,
      task_source: { path: taskSource, official_url: "fixture", retrieved_on: "2026-09-11", copied_to: taskSource },
      examples_root: examplesRoot,
      requested_pool_size: 12,
      sampled_pool_size: 12,
      candidate_pool: selected,
      excluded: [],
      tiers,
      selected,
      limitations: [],
      status: "ready",
    }, null, 2)}\n`, "utf8");
    const result = await runExperiment({
      projectRoot: path.resolve(process.cwd()),
      selectionPath: path.join(selectionDir, "selection.json"),
      examplesRoot,
      experimentRoot,
      evaluatorScript: evaluator,
      goldDir,
      model: "deepseek/deepseek-v4-flash",
      thinking: "off",
      dbtCommand: "dataagent-command-that-does-not-exist",
      duckdbCommand: "dataagent-duckdb-that-does-not-exist",
    });
    assert.equal(result.plan.status, "blocked");
    assert.equal(result.plan.runs.length, 12);
    assert.equal(result.plan.runs.filter((run) => run.status === "not_started").length, 12);
    assert.equal(result.preflight.ok, false);
    const smokeCheck = result.preflight.checks.find((check) => check.name === "restricted_dbt_smoke");
    assert.equal(smokeCheck?.required, true);
    assert.equal(smokeCheck?.passed, false);
    assert.equal(smokeCheck?.command?.includes("debug"), true);
    assert.equal(await fileExists(path.join(experimentRoot, "plan.json")), true);
    const reportPath = await writeDiagnosticReport(experimentRoot);
    assert.match(await readFile(reportPath, "utf8"), /12 tasks, one run each = 12 planned runs/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("official evaluator adapter keeps evaluated, passed, and unavailable distinct", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-evaluator-"));
  try {
    const resultDir = path.join(root, "results");
    const goldDir = path.join(root, "gold");
    const evaluator = path.join(root, "evaluate.py");
    await mkdir(resultDir, { recursive: true });
    await mkdir(goldDir, { recursive: true });
    await mkdir(path.join(resultDir, "task-01"), { recursive: true });
    await mkdir(path.join(resultDir, "task-02"), { recursive: true });
    await mkdir(path.join(resultDir, "task-03"), { recursive: true });
    await writeFile(path.join(resultDir, "task-01", "answer.duckdb"), "fixture\n", "utf8");
    await writeFile(path.join(resultDir, "task-02", "answer.sql"), "select 1\n", "utf8");
    await writeFile(path.join(resultDir, "task-03", "answer.duckdb"), "fixture\n", "utf8");
    await writeFile(path.join(resultDir, "results_metadata.jsonl"), [
      JSON.stringify({ instance_id: "task-01", answer_type: "file", answer_or_path: "answer.duckdb" }),
      JSON.stringify({ instance_id: "task-02", answer_type: "file", answer_or_path: "answer.sql" }),
      JSON.stringify({ instance_id: "task-03", answer_type: "file", answer_or_path: "answer.duckdb" }),
      "",
    ].join("\n"), "utf8");
    await writeFile(path.join(goldDir, "spider2_eval.jsonl"), [
      JSON.stringify({ instance_id: "task-01", evaluation: { func: "string_match", parameters: {} } }),
      JSON.stringify({ instance_id: "task-02", evaluation: { func: "string_match", parameters: {} } }),
      "",
    ].join("\n"), "utf8");
    await writeFile(evaluator, [
      "import argparse, json",
      "parser = argparse.ArgumentParser()",
      "parser.add_argument('--result_dir')",
      "parser.add_argument('--gold_dir')",
      "args = parser.parse_args()",
      "rows = [json.loads(line) for line in open(args.result_dir + '/results_metadata.jsonl') if line.strip()]",
      "if len(rows) != 1 or rows[0].get('instance_id') != 'task-01' or any(row.get('answer_or_path') == 'answer.sql' for row in rows): raise SystemExit('filtered submission is incorrect')",
      "print('task-01')",
      "print('1 1 1')",
      "",
    ].join("\n"), "utf8");
    const evaluation = await evaluateRound({
      resultDir,
      goldDir,
      evaluatorScript: evaluator,
      pythonCommand: "python3",
      timeoutMs: 10_000,
      expectedInstanceIds: ["task-01", "task-02"],
      logPath: path.join(root, "logs", "evaluator.log"),
    });
    assert.equal(evaluation.status, "completed");
    assert.equal(evaluation.score, 1);
    assert.deepEqual(evaluation.task_scores, [
      { instance_id: "task-01", score: 1, status: "passed" },
      { instance_id: "task-02", score: null, status: "not_evaluated" },
    ]);
    assert.equal(evaluation.submission_exclusions.length, 2);
    assert.equal(evaluation.submission_exclusions.find((item) => item.instance_id === "task-02")?.reason.includes(".duckdb"), true);
    assert.equal(evaluation.submission_exclusions.find((item) => item.instance_id === "task-03")?.reason, "unexpected instance_id: task-03");
    const originalMetadata = (await readFile(path.join(resultDir, "results_metadata.jsonl"), "utf8")).trim().split("\n").map((line) => JSON.parse(line) as Record<string, unknown>);
    assert.equal(originalMetadata.length, 3);
    assert.equal(originalMetadata[1]?.answer_or_path, "answer.sql");
    assert.equal(originalMetadata[2]?.instance_id, "task-03");

    const invalidResultDir = path.join(root, "invalid-results");
    const invalidEvaluator = path.join(root, "invalid-evaluate.py");
    const evaluatorMarker = path.join(root, "evaluator-called");
    await mkdir(path.join(invalidResultDir, "task-02"), { recursive: true });
    await writeFile(path.join(invalidResultDir, "task-02", "answer.sql"), "select 1\n", "utf8");
    await writeFile(path.join(invalidResultDir, "results_metadata.jsonl"), `${JSON.stringify({ instance_id: "task-02", answer_type: "file", answer_or_path: "answer.sql" })}\n`, "utf8");
    await writeFile(invalidEvaluator, `from pathlib import Path\nPath(${JSON.stringify(evaluatorMarker)}).write_text('called')\n`, "utf8");
    const invalidEvaluation = await evaluateRound({
      resultDir: invalidResultDir,
      goldDir,
      evaluatorScript: invalidEvaluator,
      pythonCommand: "python3",
      timeoutMs: 10_000,
      expectedInstanceIds: ["task-02"],
      logPath: path.join(root, "logs", "invalid-evaluator.log"),
    });
    assert.equal(invalidEvaluation.status, "completed");
    assert.equal(invalidEvaluation.failure_label, "no_valid_submissions");
    assert.equal(invalidEvaluation.exit_code, null);
    assert.equal(invalidEvaluation.submission_exclusions.length, 1);
    assert.equal(await fileExists(evaluatorMarker), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("invalid DBT submissions are omitted while valid DuckDB submissions are scored", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-submission-mix-"));
  try {
    const repo = path.join(root, "repo");
    const resultDir = path.join(root, "results");
    const goldDir = path.join(root, "gold");
    const evaluator = path.join(root, "evaluate.py");
    await mkdir(repo, { recursive: true });
    await mkdir(goldDir, { recursive: true });
    await writeFile(path.join(repo, "valid.duckdb"), "fixture\n", "utf8");
    await writeFile(path.join(repo, "invalid.sql"), "select 1\n", "utf8");
    await writeFile(path.join(goldDir, "spider2_eval.jsonl"), `${JSON.stringify({ instance_id: "task-valid", evaluation: { func: "string_match", parameters: {} } })}\n`, "utf8");
    await writeFile(evaluator, "print('task-valid')\nprint('1 1 1')\n", "utf8");
    const valid = await buildSubmission({ instanceId: "task-valid", finalResponse: "valid.duckdb", workspaceRepo: repo, roundResultDir: resultDir });
    const invalid = await buildSubmission({ instanceId: "task-invalid", finalResponse: "invalid.sql", workspaceRepo: repo, roundResultDir: resultDir });
    assert.equal(invalid.record.failure_label, "invalid_submission_artifact");
    await writeRoundMetadata(resultDir, [valid, invalid]);
    const evaluation = await evaluateRound({
      resultDir,
      goldDir,
      evaluatorScript: evaluator,
      pythonCommand: "python3",
      timeoutMs: 10_000,
      expectedInstanceIds: ["task-valid", "task-invalid"],
      logPath: path.join(root, "logs", "evaluator.log"),
    });
    assert.equal(evaluation.status, "completed");
    assert.deepEqual(evaluation.task_scores, [
      { instance_id: "task-valid", score: 1, status: "passed" },
      { instance_id: "task-invalid", score: null, status: "not_evaluated" },
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("assistant errors are recorded as errors and a one-request budget permits the first request", () => {
  const errorMessage = { role: "assistant", stopReason: "error", errorMessage: "401 Unauthorized", content: [{ type: "text", text: "401 Unauthorized" }] };
  assert.equal(assistantMessageError(errorMessage), "401 Unauthorized");
  assert.equal(lastAssistantText([{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "answer" }] }, errorMessage]), "");
  assert.equal(lastAssistantText([{ role: "assistant", stopReason: "stop", content: [{ type: "text", text: "answer" }] }]), "answer");
  assert.equal(modelRequestLimitReached(0, 1), false);
  assert.equal(modelRequestLimitReached(1, 1), true);
});

test("text tools skip unreadable files and bound returned output", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-safe-tools-"));
  try {
    await writeFile(path.join(root, "a-large.json"), `{"needle":"${"x".repeat(5_000_001)}"}`, "utf8");
    await writeFile(path.join(root, "b-binary.bin"), Buffer.from([0, ...Buffer.from("needle", "utf8")]));
    await writeFile(path.join(root, "c-long.json"), `${"x".repeat(1_200)} needle ${"y".repeat(100)}\n`, "utf8");
    await writeFile(path.join(root, "large.sql"), "select 1;\n".repeat(5_000), "utf8");
    await writeFile(path.join(root, "zz-many-lines.sql"), ("needle " + "x".repeat(1_200) + "\n").repeat(100), "utf8");
    await writeFile(path.join(root, "z-normal.sql"), "select 1 as needle;\n", "utf8");
    await writeFile(path.join(root, "paged.sql"), "line 1\nline 2\nline 3\n", "utf8");
    const tools = createSafeTools({ root, runtimeDir: path.join(root, "runtime"), config: offlineToolConfig(), remainingMs: () => 10_000, onDbtValidation: async () => undefined });
    const execute = async (name: string, params: Record<string, unknown>) => {
      const tool = tools.find((candidate) => candidate.name === name);
      if (!tool) throw new Error(`missing tool ${name}`);
      return tool.execute("test", params as never, undefined, undefined, undefined as never);
    };

    const readOutput = toolText(await execute("read_file", { path: "large.sql" }));
    assert.ok(readOutput.length <= 32_000);
    assert.match(readOutput, /output truncated/);
    assert.match(readOutput, /select 1/);
    assert.match(toolText(await execute("read_file", { path: "paged.sql", offset: 2, limit: 1 })), /line 2/);
    assert.match(toolText(await execute("read_file", { path: "paged.sql", offset: 3, limit: 1 })), /line 3/);
    await assert.rejects(() => execute("read_file", { path: "b-binary.bin" }), /Binary file detected.*inspect_database/);

    const boundedSearch = toolText(await execute("search_files", { path: "zz-many-lines.sql", pattern: "needle", literal: true, limit: 200 }));
    assert.ok(boundedSearch.length <= 32_000);
    assert.match(boundedSearch, /search output truncated/);
    const searchOutput = toolText(await execute("search_files", { pattern: "needle", literal: true, limit: 10 }));
    assert.ok(searchOutput.length <= 32_000);
    assert.match(searchOutput, /c-long\.json:1:/);
    assert.match(searchOutput, /z-normal\.sql:1:/);
    assert.doesNotMatch(searchOutput, /a-large\.json/);
    assert.doesNotMatch(searchOutput, /b-binary\.bin/);
    const longMatch = searchOutput.split("\n").find((line) => line.startsWith("c-long.json:1: "));
    assert.ok(longMatch);
    assert.ok(longMatch.length <= 1_000);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("runtime API keys stay in memory and out of the run config", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-runtime-auth-"));
  try {
    const configDir = path.join(root, "agent-config");
    await mkdir(configDir, { recursive: true });
    const authPath = path.join(configDir, "auth.json");
    await writeFile(path.join(configDir, "models.json"), JSON.stringify({ providers: { deepseek: { baseUrl: "https://api.deepseek.com" } } }), "utf8");
    const runtime = await ModelRuntime.create({
      authPath,
      modelsPath: path.join(configDir, "models.json"),
      modelsStorePath: path.join(configDir, "models-store.json"),
      refreshOnCreate: false,
      allowModelNetwork: false,
    });
    await runtime.setRuntimeApiKey("deepseek", "fake-test-key");
    assert.equal(runtime.getProviderAuthStatus("deepseek").source, "runtime");
    assert.equal((await readFile(authPath, "utf8")).includes("fake-test-key"), false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("final artifact selection prefers explicit paths and only uniquely falls back on timeout", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-artifact-selection-"));
  try {
    const repo = path.join(root, "repo");
    await mkdir(repo, { recursive: true });
    await writeFile(path.join(repo, "input.duckdb"), "input\n", "utf8");
    await writeFile(path.join(repo, "result.duckdb"), "result\n", "utf8");
    await mkdir(path.join(repo, "nested"), { recursive: true });
    await writeFile(path.join(repo, "nested", "cache.duckdb"), "cache\n", "utf8");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "result.duckdb" }), "result.duckdb");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "NO_ARTIFACT" }), "NO_ARTIFACT");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "../escape.duckdb" }), "../escape.duckdb");
    assert.equal(await hasValidSubmissionArtifact(repo, "../escape.duckdb"), false);

    await rm(path.join(repo, "result.duckdb"));
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "main.fct_sales" }), "input.duckdb");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "model_request_limit", explicitResponse: "" }), "input.duckdb");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "answer.sql" }), "input.duckdb");
    await writeFile(path.join(repo, "result.duckdb"), "result\n", "utf8");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "agent_end", explicitResponse: "main.fct_sales" }), "main.fct_sales");
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "wall_clock_timeout", explicitResponse: "" }), "");
    await rm(path.join(repo, "input.duckdb"));
    await rm(path.join(repo, "result.duckdb"));
    assert.equal(await selectFinalArtifactResponse({ workspaceRepo: repo, stopReason: "wall_clock_timeout", explicitResponse: "" }), "");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("model request budget counts actual transport dispatches", async () => {
  let requestCount = 0;
  let dispatchCount = 0;
  let budgetStops = 0;
  const transport: typeof fetch = async () => {
    dispatchCount += 1;
    throw new Error("fake transport failure");
  };
  const base = (async (_model: unknown, _context: unknown, options?: { fetch?: typeof fetch }) => {
    await options?.fetch?.("https://example.test");
    return {};
  }) as unknown as Parameters<typeof wrapModelStreamFunction>[0];
  const wrapped = wrapModelStreamFunction(base, {
    maximumRequests: 1,
    requestCount: () => requestCount,
    onRequest: () => { requestCount += 1; },
    onBudgetExhausted: () => { budgetStops += 1; },
  });
  const invoke = (signal?: AbortSignal) => Promise.resolve(wrapped(undefined as never, undefined as never, { fetch: transport, signal } as never) as unknown as Promise<unknown>);

  await assert.rejects(invoke(), /fake transport failure/);
  assert.equal(requestCount, 1);
  assert.equal(dispatchCount, 1);
  await assert.rejects(invoke(), /aborted/);
  assert.equal(requestCount, 1);
  assert.equal(dispatchCount, 1);
  assert.equal(budgetStops, 1);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(invoke(controller.signal), /aborted/);
  assert.equal(requestCount, 1);
  assert.equal(dispatchCount, 1);
  assert.equal(budgetStops, 1);
});

test("selection ignores unrelated documentation and marks dynamic refs as unknown", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-selection-scope-"));
  try {
    const examplesRoot = path.join(root, "examples");
    const projectId = (index: number) => `scope-task-${String(index).padStart(2, "0")}`;
    const project = path.join(examplesRoot, projectId(0));
    const taskFile = path.join(root, "tasks.jsonl");
    const tasks = Array.from({ length: 12 }, (_, index) => ({ instance_id: projectId(index), instruction: "Update the target model", type: "DBT" }));
    for (const task of tasks) {
      const taskProject = path.join(examplesRoot, task.instance_id);
      await mkdir(path.join(taskProject, "models"), { recursive: true });
      await writeFile(path.join(taskProject, "dbt_project.yml"), "name: scope_task\nprofile: local\n", "utf8");
      await writeFile(path.join(taskProject, "profiles.yml"), "local:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: data.duckdb\n", "utf8");
      await writeFile(path.join(taskProject, "data.duckdb"), "fixture\n", "utf8");
      await writeFile(path.join(taskProject, "models", "target.sql"), "select 1 as id\n", "utf8");
    }
    await writeFile(taskFile, `${tasks.map((task) => JSON.stringify(task)).join("\n")}\n`, "utf8");
    const analysisFor = (manifest: Awaited<ReturnType<typeof prepareSelection>>, instanceId: string) => {
      const item = manifest.candidate_pool.find((candidate) => candidate.row.instance_id === instanceId);
      assert.ok(item);
      return item;
    };
    const withoutDocumentation = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-a"), poolSize: 12 });
    await writeFile(path.join(project, "README.md"), "unrelated documentation with join window timestamp\n", "utf8");
    const withDocumentation = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-b"), poolSize: 12 });
    assert.equal(analysisFor(withoutDocumentation, projectId(0)).row.scope_observations, analysisFor(withDocumentation, projectId(0)).row.scope_observations);
    assert.equal(analysisFor(withoutDocumentation, projectId(0)).row.constraint_observations, analysisFor(withDocumentation, projectId(0)).row.constraint_observations);

    const ambiguousTaskFile = path.join(root, "ambiguous-tasks.jsonl");
    await writeFile(ambiguousTaskFile, `${tasks.map((task) => JSON.stringify({ ...task, instruction: "Update the business report" })).join("\n")}\n`, "utf8");
    const withoutUnrelatedModel = await prepareSelection({ taskFile: ambiguousTaskFile, examplesRoot, outputDir: path.join(root, "selection-d"), poolSize: 12 });
    await writeFile(path.join(project, "models", "unrelated.sql"), "select * from {{ ref(model_name) }}\n", "utf8");
    const withUnrelatedModel = await prepareSelection({ taskFile: ambiguousTaskFile, examplesRoot, outputDir: path.join(root, "selection-e"), poolSize: 12 });
    assert.equal(analysisFor(withoutUnrelatedModel, projectId(0)).eligible, false);
    assert.equal(analysisFor(withUnrelatedModel, projectId(0)).eligible, false);
    assert.equal(analysisFor(withoutUnrelatedModel, projectId(0)).totalScore, analysisFor(withUnrelatedModel, projectId(0)).totalScore);
    assert.equal(analysisFor(withoutUnrelatedModel, projectId(0)).row.dependency_observations, analysisFor(withUnrelatedModel, projectId(0)).row.dependency_observations);
    assert.match(analysisFor(withUnrelatedModel, projectId(0)).row.selection_reason, /manual candidate-pool confirmation/);

    const confirmationPath = path.join(root, "scope-confirmations.jsonl");
    await writeFile(confirmationPath, `${tasks.map((task) => JSON.stringify({
      instance_id: task.instance_id,
      related_models: ["models/target.sql"],
      scope_observations: "score=0; estimated_related_files=4; models=1; manual_scope_confirmed=true",
      dependency_observations: "score=0; visible_longest_chain_edges=0; branching=false; unknown=false; manual_scope_confirmed=true",
      constraint_observations: "score=0; indicators=none; manual_scope_confirmed=true",
    })).join("\n")}\n`, "utf8");
    const confirmed = await prepareSelection({
      taskFile: ambiguousTaskFile,
      examplesRoot,
      outputDir: path.join(root, "selection-f"),
      poolSize: 12,
      scopeConfirmationPath: confirmationPath,
    });
    assert.equal(analysisFor(confirmed, projectId(0)).eligible, true);
    assert.deepEqual(analysisFor(confirmed, projectId(0)).modelFiles, ["models/target.sql"]);
    assert.equal(analysisFor(confirmed, projectId(0)).row.scope_observations, "score=0; estimated_related_files=4; models=1; manual_scope_confirmed=true");
    assert.equal(confirmed.scope_confirmation_path, path.join(root, "selection-f", "scope-confirmations.jsonl"));
    assert.equal(await fileExists(confirmed.scope_confirmation_path), true);

    const invalidConfirmationPath = path.join(root, "invalid-scope-confirmations.jsonl");
    await writeFile(invalidConfirmationPath, `${JSON.stringify({
      instance_id: projectId(0),
      related_models: ["models/missing.sql"],
      scope_observations: "score=0; estimated_related_files=1",
      dependency_observations: "score=0; visible_longest_chain_edges=0; branching=false; unknown=false",
      constraint_observations: "score=0; indicators=none",
    })}\n`, "utf8");
    await assert.rejects(() => prepareSelection({
      taskFile: ambiguousTaskFile,
      examplesRoot,
      outputDir: path.join(root, "selection-g"),
      poolSize: 12,
      scopeConfirmationPath: invalidConfirmationPath,
    }), /references missing local model/);

    await writeFile(path.join(project, "models", "target.sql"), "select * from {{ ref(model_name) }}\n", "utf8");
    const dynamicReference = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-c"), poolSize: 12 });
    assert.equal(analysisFor(dynamicReference, projectId(0)).hasUnknownDependencies, true);
    assert.match(analysisFor(dynamicReference, projectId(0)).row.dependency_observations, /unknown=true/);
    assert.equal(analysisFor(dynamicReference, projectId(0)).eligible, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("final dbt validation is authoritative while historical failures stay countable", () => {
  const finalPass = {
    action: "build",
    command: ["dbt", "build"],
    started_at: "2026-09-11T00:00:00.000Z",
    ended_at: "2026-09-11T00:00:01.000Z",
    duration_ms: 1_000,
    exit_code: 0,
    timed_out: false,
    log_path: "/tmp/final.log",
    stdout_summary: "",
    stderr_summary: "",
    source: "runner_final_validation",
    sandbox_backend: "macos-sandbox-exec",
  } as RunReceipt["validation"]["commands"][number];
  assert.equal(validationStatus(finalPass), "passed");
  assert.equal(validationStatus(undefined), "not_run");
});

test("process timeout terminates descendants and a failed evaluator leaves no invalid next index", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-process-timeout-"));
  try {
    const marker = path.join(root, "descendant-marker.txt");
    const childCode = `setTimeout(() => require("node:fs").writeFileSync(${JSON.stringify(marker)}, "descendant"), 700);`;
    const parentCode = `require("node:child_process").spawn(process.execPath, ["-e", ${JSON.stringify(childCode)}], {stdio: "ignore"}); setTimeout(() => {}, 5000);`;
    const result = await runCommand(process.execPath, ["-e", parentCode], { cwd: root, env: safeChildEnv(), timeoutMs: 100 });
    assert.equal(result.timed_out, true);
    assert.ok(result.duration_ms < 2_500);
    await new Promise((resolve) => setTimeout(resolve, 900));
    assert.equal(await fileExists(marker), false);

    const lateMarker = path.join(root, "late-descendant-marker.txt");
    const lateChildCode = `setTimeout(() => require("node:fs").writeFileSync(${JSON.stringify(lateMarker)}, "descendant"), 700);`;
    const exitingParentCode = `require("node:child_process").spawn(process.execPath, ["-e", ${JSON.stringify(lateChildCode)}], {stdio: "inherit"});`;
    const exitingParent = await runCommand(process.execPath, ["-e", exitingParentCode], { cwd: root, env: safeChildEnv(), timeoutMs: 100 });
    assert.equal(exitingParent.timed_out, true);
    assert.ok(exitingParent.duration_ms < 2_500);
    await new Promise((resolve) => setTimeout(resolve, 900));
    assert.equal(await fileExists(lateMarker), false);

    const plan = { runs: [{ status: "pending" }] } as unknown as ExperimentPlan;
    assert.doesNotThrow(() => markNotStarted(plan, -1, "evaluator failed"));
    assert.equal(plan.runs[0].status, "pending");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("restricted command never falls back to reading a marker outside the task root", async (t) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-restricted-command-"));
  try {
    const repo = path.join(root, "repo");
    const runtime = path.join(root, "runtime");
    const outside = path.join(root, "outside.txt");
    await mkdir(repo, { recursive: true });
    await mkdir(runtime, { recursive: true });
    await writeFile(outside, "outside-marker\n", "utf8");
    if (!await requireRestrictedBackend(t, repo, runtime)) return;
    const result = await runRestrictedCommand("/bin/cat", [outside], {
      cwd: repo,
      root: repo,
      runtimeDir: runtime,
      env: safeChildEnv({ TMPDIR: path.join(runtime, "tmp"), HOME: path.join(runtime, "home") }),
      timeoutMs: 3_000,
    });
    assert.equal(result.stdout.includes("outside-marker"), false);
    assert.notEqual(result.exit_code, 0);
    if (result.sandbox_backend === "macos-sandbox-exec") {
      const profile = await readFile(path.join(runtime, "sandbox.sb"), "utf8");
      assert.equal(profile.includes('(subpath "/")'), false);
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("missing submissions are omitted and an empty result stays unscored", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-missing-submission-"));
  try {
    const repo = path.join(root, "repo");
    const resultDir = path.join(root, "results");
    const goldDir = path.join(root, "gold");
    const evaluator = path.join(root, "evaluate.py");
    await mkdir(repo, { recursive: true });
    await mkdir(goldDir, { recursive: true });
    await writeFile(path.join(goldDir, "spider2_eval.jsonl"), `${JSON.stringify({ instance_id: "task-01", evaluation: { func: "string_match", parameters: {} } })}\n`, "utf8");
    await writeFile(evaluator, "raise RuntimeError('should not run')\n", "utf8");
    const noArtifact = await buildSubmission({ instanceId: "task-01", finalResponse: "NO_ARTIFACT", workspaceRepo: repo, roundResultDir: resultDir });
    const missingPath = await buildSubmission({ instanceId: "task-02", finalResponse: "missing.duckdb", workspaceRepo: repo, roundResultDir: resultDir });
    await mkdir(path.join(repo, "artifact-directory"), { recursive: true });
    const directorySubmission = await buildSubmission({ instanceId: "task-03", finalResponse: "artifact-directory", workspaceRepo: repo, roundResultDir: resultDir });
    assert.equal(noArtifact.metadataEntry, undefined);
    assert.equal(missingPath.metadataEntry, undefined);
    assert.equal(directorySubmission.metadataEntry, undefined);
    await writeRoundMetadata(resultDir, [noArtifact, missingPath, directorySubmission]);
    const evaluation = await evaluateRound({
      resultDir,
      goldDir,
      evaluatorScript: evaluator,
      pythonCommand: process.execPath,
      timeoutMs: 10_000,
      expectedInstanceIds: ["task-01", "task-02"],
      logPath: path.join(root, "logs", "evaluator.log"),
    });
    assert.equal(evaluation.status, "completed");
    assert.equal(evaluation.failure_label, "no_valid_submissions");
    assert.deepEqual(evaluation.task_scores, [
      { instance_id: "task-01", score: null, status: "not_evaluated" },
      { instance_id: "task-02", score: null, status: "not_evaluated" },
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
