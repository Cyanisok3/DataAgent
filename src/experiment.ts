import { copyFile } from "node:fs/promises";
import path from "node:path";
import { createFixedConfig, loadSystemPrompt } from "./config.js";
import { evaluateRound, retainEvaluatorCopy } from "./evaluator.js";
import { ensureDir, fileExists, readJson, truncateText, writeJson } from "./files.js";
import { runPreflight, type PreflightResult } from "./preflight.js";
import { executeRun } from "./receipt.js";
import { loadSelection } from "./selection.js";
import { detectRestrictedBackend } from "./process.js";
import { buildSubmission, writeRoundMetadata, type SubmissionBuildResult } from "./submission.js";
import type { FixedAgentConfig, PlannedRun, RunReceipt, RunStatus, SelectionManifest, Tier } from "./types.js";

export interface ExperimentPlan {
  schema_version: "1.0";
  experiment_id: string;
  created_on: string;
  status: "running" | "completed" | "blocked" | "incomplete";
  selection_path: string;
  examples_root: string;
  evaluator_script: string;
  gold_dir: string;
  fixed_config: FixedAgentConfig;
  protocol: {
    task_count: number;
    total_runs: number;
    parallelism: number;
    same_task_order: true;
    fresh_workspace_per_run: true;
  };
  preflight_path: string;
  runs: PlannedRun[];
  evaluation_path?: string;
  block_reason?: string;
}

export interface ExperimentResult {
  plan: ExperimentPlan;
  preflight: PreflightResult;
}

function safeId(value: string): string {
  const id = value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  return id || "task";
}

function experimentId(): string {
  return `exp-${new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14)}`;
}

function classifyError(error: unknown): { status: RunStatus; label: string; reason: string } {
  const reason = truncateText(error instanceof Error ? error.message : String(error), 2_000);
  const environment = /not found|permission|profile|command|path escapes|symbolic link|ENOENT|EACCES|API key|authentication|unauthorized|\b401\b|\b403\b|network/i.test(reason);
  return { status: environment ? "environment_failed" : "agent_failed", label: environment ? "runner_environment_error" : "runner_exception", reason };
}

function initialRuns(manifest: SelectionManifest, experimentIdValue: string): PlannedRun[] {
  const selected = [...manifest.selected].sort((left, right) => left.row.instance_id.localeCompare(right.row.instance_id));
  return selected.map((task, index) => {
    const instanceId = task.row.instance_id;
    return {
      run_id: `${experimentIdValue}-r1-${String(index + 1).padStart(2, "0")}-${safeId(instanceId)}`,
      instance_id: instanceId,
      tier: task.row.tier as Tier,
      project: task.row.project,
      status: "pending",
    };
  });
}

async function writePlan(experimentRoot: string, plan: ExperimentPlan): Promise<void> {
  await writeJson(path.join(experimentRoot, "plan.json"), plan);
}

async function writeFailureReceipt(options: {
  experimentId: string;
  planned: PlannedRun;
  runRoot: string;
  config: FixedAgentConfig;
  error: unknown;
}): Promise<string> {
  const startedAt = new Date().toISOString();
  const endedAt = new Date().toISOString();
  const classified = classifyError(options.error);
  const evidenceDir = path.join(options.runRoot, "evidence");
  const receiptPath = path.join(options.runRoot, "receipt.json");
  await ensureDir(evidenceDir);
  const receipt: RunReceipt = {
    schema_version: "1.0",
    run_id: options.planned.run_id,
    experiment_id: options.experimentId,
    instance_id: options.planned.instance_id,
    tier: options.planned.tier,
    project: options.planned.project,
    fixed_config_name: options.config.name,
    fixed_config: options.config,
    execution: {
      started_at: startedAt,
      ended_at: endedAt,
      duration_ms: 0,
      model_request_attempts: 0,
      retry_attempts: 0,
      stop_reason: classified.label,
      run_status: classified.status,
      isolation: {
        mode: "restricted-subprocess",
        sandbox_backend: detectRestrictedBackend(),
        workspace: path.join(options.runRoot, "workspace", "repo"),
        agent_config_dir: path.join(options.runRoot, "workspace", "agent-config"),
        gold_visible_to_agent: false,
        evaluator_visible_to_agent: false,
        arbitrary_shell_enabled: false,
      },
    },
    modification_and_tools: {
      diff_path: path.join(evidenceDir, "diff.patch"),
      events_path: path.join(evidenceDir, "events.jsonl"),
      final_response_path: path.join(evidenceDir, "final-response.txt"),
      changed_files: [],
    },
    validation: {
      commands: [],
      scope: "not reached",
      dbt_status: "not_run",
      historical_failure_count: 0,
      error_summary: classified.reason,
    },
    submission: {
      instance_id: options.planned.instance_id,
      answer_type: "answer",
      answer_or_path: "",
      artifact_status: "missing",
      failure_label: "run_failed_before_submission",
    },
    judgment: {
      score: null,
      verdict: "not_scored",
      scoring_status: "pending",
      failure_labels: [classified.label],
    },
    attachments: {
      workspace_repo: path.join(options.runRoot, "workspace", "repo"),
      baseline_repo: path.join(options.runRoot, "evidence", "baseline-repo"),
      dbt_logs_dir: path.join(evidenceDir, "dbt"),
    },
  };
  await writeJson(receiptPath, receipt);
  return receiptPath;
}

async function updateReceiptJudgment(receiptPath: string, evaluation: Awaited<ReturnType<typeof evaluateRound>>): Promise<void> {
  const receipt = await readJson<RunReceipt>(receiptPath);
  const taskScore = evaluation.task_scores.find((score) => score.instance_id === receipt.instance_id);
  const score = taskScore?.score ?? null;
  const completed = evaluation.status === "completed";
  receipt.judgment = {
    score,
    verdict: completed && score === 1 ? "passed" : completed && score === 0 ? "failed" : "not_scored",
    scoring_status: completed ? "completed" : evaluation.timed_out ? "timed_out" : "failed",
    ...(evaluation.evaluator_log ? { evaluator_log: evaluation.evaluator_log } : {}),
    failure_labels: [...new Set([
      ...receipt.judgment.failure_labels,
      ...(completed && score === 0 ? ["official_evaluator_failed"] : []),
      ...(evaluation.failure_label ? [evaluation.failure_label] : []),
    ])],
  };
  await writeJson(receiptPath, receipt);
}

export function markNotStarted(plan: ExperimentPlan, fromIndex: number, reason: string): void {
  if (fromIndex < 0) return;
  for (let index = fromIndex; index < plan.runs.length; index += 1) {
    if (plan.runs[index].status === "pending" || plan.runs[index].status === "running") {
      plan.runs[index].status = "not_started";
      plan.runs[index].reason = reason;
    }
  }
}

export async function runExperiment(options: {
  projectRoot: string;
  selectionPath: string;
  examplesRoot?: string;
  experimentRoot: string;
  evaluatorScript: string;
  goldDir: string;
  model: string;
  thinking: string;
  pythonCommand?: string;
  dbtCommand?: string;
  duckdbCommand?: string;
  parallelism?: number;
}): Promise<ExperimentResult> {
  const projectRoot = path.resolve(options.projectRoot);
  const experimentRoot = path.resolve(options.experimentRoot);
  const parallelism = Math.max(1, Math.floor(options.parallelism ?? 1));
  if (await fileExists(path.join(experimentRoot, "plan.json"))) throw new Error(`Experiment already exists: ${path.join(experimentRoot, "plan.json")}`);
  const selection = await loadSelection(options.selectionPath);
  if (options.examplesRoot && path.resolve(options.examplesRoot) !== path.resolve(selection.examples_root)) {
    throw new Error("--examples-root must match the examples_root recorded in selection.json; prepare a new selection for a different root.");
  }
  await ensureDir(experimentRoot);
  const preflightCall = await runPreflight({
    projectRoot,
    selectionPath: options.selectionPath,
    experimentRoot,
    evaluatorScript: options.evaluatorScript,
    goldDir: options.goldDir,
    model: options.model,
    thinking: options.thinking,
    pythonCommand: options.pythonCommand,
    dbtCommand: options.dbtCommand,
    duckdbCommand: options.duckdbCommand,
  });
  const preflight = preflightCall.result;
  if (!preflight.config) throw new Error("Preflight did not produce a fixed agent config.");
  const id = experimentId();
  const plan: ExperimentPlan = {
    schema_version: "1.0",
    experiment_id: id,
    created_on: new Date().toISOString(),
    status: preflight.ok ? "running" : "blocked",
    selection_path: path.resolve(options.selectionPath),
    examples_root: path.resolve(selection.examples_root),
    evaluator_script: path.resolve(options.evaluatorScript),
    gold_dir: path.resolve(options.goldDir),
    fixed_config: preflight.config,
    protocol: {
      task_count: selection.selected.length,
      total_runs: selection.selected.length,
      parallelism,
      same_task_order: true,
      fresh_workspace_per_run: true,
    },
    preflight_path: path.join(experimentRoot, "preflight.json"),
    runs: initialRuns(selection, id),
  };
  await writePlan(experimentRoot, plan);
  await copyFile(selection.task_source.copied_to, path.join(experimentRoot, "task-source.jsonl"));
  if (!preflight.ok) {
    plan.status = "blocked";
    plan.block_reason = "preflight_failed; no model calls were made";
    markNotStarted(plan, 0, plan.block_reason);
    await writePlan(experimentRoot, plan);
    return { plan, preflight };
  }

  await retainEvaluatorCopy({ evaluatorScript: options.evaluatorScript, experimentRoot });
  const submissions: SubmissionBuildResult[] = [];
  let blocked = false;
  let blockReason = "";
  const resultDir = path.join(experimentRoot, "results", "round-1");
  await ensureDir(resultDir);
  for (let index = 0; index < plan.runs.length; index += parallelism) {
    const batch = plan.runs.slice(index, index + parallelism);
    if (blocked) {
      markNotStarted(plan, index, blockReason);
      await writePlan(experimentRoot, plan);
      break;
    }
    for (const planned of batch) planned.status = "running";
    await writePlan(experimentRoot, plan);
    await Promise.all(batch.map(async (planned) => {
      const sourceProject = path.resolve(plan.examples_root, planned.project);
      const runRoot = path.join(experimentRoot, "runs", planned.run_id);
      try {
        const task = selection.selected.find((item) => item.row.instance_id === planned.instance_id);
        if (!task) throw new Error(`Selected task disappeared: ${planned.instance_id}`);
        const execution = await executeRun({
          experimentId: plan.experiment_id,
          runId: planned.run_id,
          tier: planned.tier,
          project: planned.project,
          taskInstruction: task.instruction,
          instanceId: planned.instance_id,
          sourceProject,
          runRoot,
          roundResultDir: resultDir,
          config: plan.fixed_config,
        });
        planned.status = execution.receipt.execution.run_status;
        planned.receipt_path = path.join(runRoot, "receipt.json");
        submissions.push(execution.submission);
        if (planned.status === "environment_failed") {
          blocked = true;
          blockReason = `environment failure in ${planned.instance_id}; remaining runs were not started`;
        }
      } catch (error) {
        const classified = classifyError(error);
        planned.status = classified.status;
        planned.reason = classified.reason;
        planned.receipt_path = await writeFailureReceipt({ experimentId: plan.experiment_id, planned, runRoot, config: plan.fixed_config, error });
        if (classified.status === "environment_failed") {
          blocked = true;
          blockReason = `environment failure in ${planned.instance_id}; remaining runs were not started`;
        }
      }
    }));
    await writePlan(experimentRoot, plan);
  }

  for (const pending of plan.runs.filter((run) => run.status === "pending")) {
    pending.status = "not_started";
    pending.reason = blockReason || "not started after experiment execution";
  }
  await writeRoundMetadata(resultDir, submissions);
  const evaluation = await evaluateRound({
    resultDir,
    goldDir: plan.gold_dir,
    evaluatorScript: plan.evaluator_script,
    pythonCommand: plan.fixed_config.commands.python,
    timeoutMs: plan.fixed_config.limits.evaluatorTimeoutMs,
    expectedInstanceIds: selection.selected.map((item) => item.row.instance_id),
    logPath: path.join(experimentRoot, "evaluator-logs", "round-1.log"),
  });
  const evaluationPath = path.join(experimentRoot, "evaluations", "round-1.json");
  await writeJson(evaluationPath, evaluation);
  plan.evaluation_path = evaluationPath;
  for (const planned of plan.runs.filter((run) => run.receipt_path)) {
    await updateReceiptJudgment(planned.receipt_path!, evaluation);
  }
  if (evaluation.status !== "completed") {
    blocked = true;
    blockReason = `official evaluator ${evaluation.status}; experiment is incomplete`;
    plan.status = "blocked";
    plan.block_reason = blockReason;
    await writePlan(experimentRoot, plan);
  }

  if (blocked) {
    plan.status = "blocked";
    plan.block_reason = blockReason;
    for (const planned of plan.runs) if (planned.status === "pending" || planned.status === "running") {
      planned.status = "not_started";
      planned.reason = blockReason;
    }
  } else {
    plan.status = plan.runs.every((run) => run.status !== "pending" && run.status !== "running") ? "completed" : "incomplete";
  }
  await writePlan(experimentRoot, plan);
  return { plan, preflight };
}
