import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { loadSelection, pythonSeedSample, prepareSelection } from "../src/selection.js";
import { existingPathWithin, fileExists, walkFiles } from "../src/files.js";
import { buildSubmission, validateRoundResultDirectory, writeRoundMetadata } from "../src/submission.js";
import { runExperiment } from "../src/experiment.js";
import { markNotStarted, type ExperimentPlan } from "../src/experiment.js";
import { writeDiagnosticReport } from "../src/report.js";
import { evaluateRound } from "../src/evaluator.js";
import { assistantMessageError, lastAssistantText, modelRequestLimitReached } from "../src/agent.js";
import { runCommand, runRestrictedCommand, safeChildEnv } from "../src/process.js";
import { validationStatus } from "../src/receipt.js";
import type { RunReceipt } from "../src/types.js";

test("fixed seed sampler matches Python random.Random.sample for the protocol seed", () => {
  const population = Array.from({ length: 69 }, (_, index) => index);
  assert.deepEqual(pythonSeedSample(population, 18), [46, 1, 57, 11, 22, 52, 20, 31, 16, 8, 36, 48, 17, 66, 32, 5, 27, 24]);
});

test("selection emits nine tasks with three relative tiers from a local-only pool", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-selection-"));
  try {
    const examplesRoot = path.join(root, "examples");
    const outputDir = path.join(root, "selection");
    await mkdir(examplesRoot, { recursive: true });
    const tasks: string[] = [];
    for (let index = 0; index < 18; index += 1) {
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
    const manifest = await prepareSelection({ taskFile, examplesRoot, outputDir, poolSize: 18 });
    assert.equal(manifest.status, "ready");
    assert.equal(manifest.sampled_pool_size, 18);
    assert.equal(manifest.selected.length, 9);
    assert.deepEqual(Object.fromEntries(Object.entries(manifest.tiers).map(([tier, ids]) => [tier, ids.length])), { low: 3, medium: 3, high: 3 });
    assert.equal((await readFile(path.join(outputDir, "selected_tasks.jsonl"), "utf8")).trim().split("\n").length, 9);

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
      poolSize: 18,
      scopeConfirmationPath: confirmationPath,
    });
    assert.equal(confirmedManifest.status, "ready");
    const loadedConfirmedManifest = await loadSelection(path.join(root, "selection-confirmed", "selection.json"));
    assert.equal(loadedConfirmedManifest.scope_confirmation_path, path.join(root, "selection-confirmed", "scope-confirmations.jsonl"));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("submission artifacts are copied into an official round directory and path escapes fail", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-submission-"));
  try {
    const repo = path.join(root, "repo");
    const resultDir = path.join(root, "results", "round-1");
    await mkdir(repo, { recursive: true });
    await writeFile(path.join(repo, "answer.csv"), "id,value\n1,ok\n", "utf8");
    const submission = await buildSubmission({ instanceId: "task-01", finalResponse: "answer.csv", workspaceRepo: repo, roundResultDir: resultDir });
    assert.equal(submission.record.answer_type, "file");
    assert.equal(await readFile(path.join(resultDir, "task-01", "answer.csv"), "utf8"), "id,value\n1,ok\n");
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

test("experiment fails closed before model calls and records all 18 runs when preflight fails", async () => {
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
    for (let index = 0; index < 9; index += 1) {
      const instanceId = `preflight-${String(index).padStart(2, "0")}`;
      const tier = index < 3 ? "low" : index < 6 ? "medium" : "high";
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
      requested_pool_size: 18,
      sampled_pool_size: 18,
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
      model: "fake/model",
      thinking: "medium",
      dbtCommand: "dataagent-command-that-does-not-exist",
      duckdbCommand: "dataagent-duckdb-that-does-not-exist",
    });
    assert.equal(result.plan.status, "blocked");
    assert.equal(result.plan.runs.length, 18);
    assert.equal(result.plan.runs.filter((run) => run.status === "not_started").length, 18);
    assert.equal(result.preflight.ok, false);
    const smokeCheck = result.preflight.checks.find((check) => check.name === "restricted_dbt_smoke");
    assert.equal(smokeCheck?.required, true);
    assert.equal(smokeCheck?.passed, false);
    assert.equal(smokeCheck?.command?.includes("debug"), true);
    assert.equal(await fileExists(path.join(experimentRoot, "plan.json")), true);
    const reportPath = await writeDiagnosticReport(experimentRoot);
    assert.match(await readFile(reportPath, "utf8"), /9 tasks × 2 identical rounds = 18 planned runs/);
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
    await writeFile(path.join(resultDir, "results_metadata.jsonl"), `${JSON.stringify({ instance_id: "task-01", answer_type: "answer", answer_or_path: "ok" })}\n`, "utf8");
    await writeFile(path.join(goldDir, "spider2_eval.jsonl"), `${JSON.stringify({ instance_id: "task-01", evaluation: { func: "string_match", parameters: {} } })}\n`, "utf8");
    await writeFile(evaluator, "print('task-01')\nprint('1 1 1')\n", "utf8");
    const evaluation = await evaluateRound({
      round: 1,
      resultDir,
      goldDir,
      evaluatorScript: evaluator,
      pythonCommand: "python3",
      timeoutMs: 10_000,
      expectedInstanceIds: ["task-01", "missing-task"],
      logPath: path.join(root, "logs", "evaluator.log"),
    });
    assert.equal(evaluation.status, "completed");
    assert.equal(evaluation.score, 1);
    assert.deepEqual(evaluation.task_scores, [
      { instance_id: "task-01", score: 1, status: "passed" },
      { instance_id: "missing-task", score: null, status: "not_evaluated" },
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

test("selection ignores unrelated documentation and marks dynamic refs as unknown", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-selection-scope-"));
  try {
    const examplesRoot = path.join(root, "examples");
    const project = path.join(examplesRoot, "scope-task");
    const taskFile = path.join(root, "tasks.jsonl");
    await mkdir(path.join(project, "models"), { recursive: true });
    await writeFile(path.join(project, "dbt_project.yml"), "name: scope_task\nprofile: local\n", "utf8");
    await writeFile(path.join(project, "profiles.yml"), "local:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: data.duckdb\n", "utf8");
    await writeFile(path.join(project, "data.duckdb"), "fixture\n", "utf8");
    await writeFile(path.join(project, "models", "target.sql"), "select 1 as id\n", "utf8");
    await writeFile(taskFile, `${JSON.stringify({ instance_id: "scope-task", instruction: "Update the target model", type: "DBT" })}\n`, "utf8");
    const withoutDocumentation = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-a"), poolSize: 1 });
    await writeFile(path.join(project, "README.md"), "unrelated documentation with join window timestamp\n", "utf8");
    const withDocumentation = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-b"), poolSize: 1 });
    assert.equal(withoutDocumentation.candidate_pool[0].row.scope_observations, withDocumentation.candidate_pool[0].row.scope_observations);
    assert.equal(withoutDocumentation.candidate_pool[0].row.constraint_observations, withDocumentation.candidate_pool[0].row.constraint_observations);

    const ambiguousTaskFile = path.join(root, "ambiguous-tasks.jsonl");
    await writeFile(ambiguousTaskFile, `${JSON.stringify({ instance_id: "scope-task", instruction: "Update the business report", type: "DBT" })}\n`, "utf8");
    const withoutUnrelatedModel = await prepareSelection({ taskFile: ambiguousTaskFile, examplesRoot, outputDir: path.join(root, "selection-d"), poolSize: 1 });
    await writeFile(path.join(project, "models", "unrelated.sql"), "select * from {{ ref(model_name) }}\n", "utf8");
    const withUnrelatedModel = await prepareSelection({ taskFile: ambiguousTaskFile, examplesRoot, outputDir: path.join(root, "selection-e"), poolSize: 1 });
    assert.equal(withoutUnrelatedModel.candidate_pool[0].eligible, false);
    assert.equal(withUnrelatedModel.candidate_pool[0].eligible, false);
    assert.equal(withoutUnrelatedModel.candidate_pool[0].totalScore, withUnrelatedModel.candidate_pool[0].totalScore);
    assert.equal(withoutUnrelatedModel.candidate_pool[0].row.dependency_observations, withUnrelatedModel.candidate_pool[0].row.dependency_observations);
    assert.match(withUnrelatedModel.candidate_pool[0].row.selection_reason, /manual candidate-pool confirmation/);

    const confirmationPath = path.join(root, "scope-confirmations.jsonl");
    await writeFile(confirmationPath, `${JSON.stringify({
      instance_id: "scope-task",
      related_models: ["models/target.sql"],
      scope_observations: "score=0; estimated_related_files=4; models=1; manual_scope_confirmed=true",
      dependency_observations: "score=0; visible_longest_chain_edges=0; branching=false; unknown=false; manual_scope_confirmed=true",
      constraint_observations: "score=0; indicators=none; manual_scope_confirmed=true",
    })}\n`, "utf8");
    const confirmed = await prepareSelection({
      taskFile: ambiguousTaskFile,
      examplesRoot,
      outputDir: path.join(root, "selection-f"),
      poolSize: 1,
      scopeConfirmationPath: confirmationPath,
    });
    assert.equal(confirmed.candidate_pool[0].eligible, true);
    assert.deepEqual(confirmed.candidate_pool[0].modelFiles, ["models/target.sql"]);
    assert.equal(confirmed.candidate_pool[0].row.scope_observations, "score=0; estimated_related_files=4; models=1; manual_scope_confirmed=true");
    assert.equal(confirmed.scope_confirmation_path, path.join(root, "selection-f", "scope-confirmations.jsonl"));
    assert.equal(await fileExists(confirmed.scope_confirmation_path), true);

    const invalidConfirmationPath = path.join(root, "invalid-scope-confirmations.jsonl");
    await writeFile(invalidConfirmationPath, `${JSON.stringify({
      instance_id: "scope-task",
      related_models: ["models/missing.sql"],
      scope_observations: "score=0; estimated_related_files=1",
      dependency_observations: "score=0; visible_longest_chain_edges=0; branching=false; unknown=false",
      constraint_observations: "score=0; indicators=none",
    })}\n`, "utf8");
    await assert.rejects(() => prepareSelection({
      taskFile: ambiguousTaskFile,
      examplesRoot,
      outputDir: path.join(root, "selection-g"),
      poolSize: 1,
      scopeConfirmationPath: invalidConfirmationPath,
    }), /references missing local model/);

    await writeFile(path.join(project, "models", "target.sql"), "select * from {{ ref(model_name) }}\n", "utf8");
    const dynamicReference = await prepareSelection({ taskFile, examplesRoot, outputDir: path.join(root, "selection-c"), poolSize: 1 });
    assert.equal(dynamicReference.candidate_pool[0].hasUnknownDependencies, true);
    assert.match(dynamicReference.candidate_pool[0].row.dependency_observations, /unknown=true/);
    assert.equal(dynamicReference.candidate_pool[0].eligible, false);
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

test("process timeout terminates descendants and evaluator failure at round two has no invalid next index", async () => {
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
    assert.doesNotThrow(() => markNotStarted(plan, -1, "round two evaluator failed"));
    assert.equal(plan.runs[0].status, "pending");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("restricted command never falls back to reading a marker outside the task root", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "dataagent-restricted-command-"));
  try {
    const repo = path.join(root, "repo");
    const runtime = path.join(root, "runtime");
    const outside = path.join(root, "outside.txt");
    await mkdir(repo, { recursive: true });
    await mkdir(runtime, { recursive: true });
    await writeFile(outside, "outside-marker\n", "utf8");
    const result = await runRestrictedCommand("/bin/cat", [outside], {
      cwd: repo,
      root: repo,
      runtimeDir: runtime,
      env: safeChildEnv({ TMPDIR: path.join(runtime, "tmp"), HOME: path.join(runtime, "home") }),
      timeoutMs: 3_000,
    });
    assert.equal(result.stdout.includes("outside-marker"), false);
    assert.notEqual(result.exit_code, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("missing submissions are omitted and an empty round stays unscored", async () => {
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
      round: 1,
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
