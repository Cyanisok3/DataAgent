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
    task_count: 9;
    runs_per_task: 2;
    total_runs: 18;
    rounds: [1, 2];
    same_task_order: true;
    fresh_workspace_per_run: true;
    score_feedback_between_rounds: false;
  };
  preflight_path: string;
  runs: PlannedRun[];
  evaluation_paths: Partial<Record<1 | 2, string>>;
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
  const runs: PlannedRun[] = [];
  for (const round of [1, 2] as const) {
    for (const [index, task] of selected.entries()) {
      const instanceId = task.row.instance_id;
      runs.push({
        run_id: `${experimentIdValue}-r${round}-${String(index + 1).padStart(2, "0")}-${safeId(instanceId)}`,
        round,
        repeat: round,
        instance_id: instanceId,
        tier: task.row.tier as Tier,
        project: task.row.project,
        status: "pending",
      });
    }
  }
  return runs;
}

async function writePlan(experimentRoot: string, plan: ExperimentPlan): Promise<void> {
  await writeJson(path.join(experimentRoot, "plan.json"), plan);
}

async function writeFailureReceipt(options: {
  experimentId: string;
  planned: PlannedRun;
  runRoot: string;
  roundResultDir: string;
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
    round: options.planned.round,
    repeat: options.planned.repeat,
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
}): Promise<ExperimentResult> {
  const projectRoot = path.resolve(options.projectRoot);
  const experimentRoot = path.resolve(options.experimentRoot);
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
      task_count: 9,
      runs_per_task: 2,
      total_runs: 18,
      rounds: [1, 2],
      same_task_order: true,
      fresh_workspace_per_run: true,
      score_feedback_between_rounds: false,
    },
    preflight_path: path.join(experimentRoot, "preflight.json"),
    runs: initialRuns(selection, id),
    evaluation_paths: {},
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
  const submissionsByRound: Record<1 | 2, SubmissionBuildResult[]> = { 1: [], 2: [] };
  let blocked = false;
  let blockReason = "";
  for (const round of [1, 2] as const) {
    if (blocked) {
      for (const planned of plan.runs.filter((run) => run.round === round && (run.status === "pending" || run.status === "running"))) {
        planned.status = "not_started";
        planned.reason = blockReason;
      }
      await writePlan(experimentRoot, plan);
      break;
    }
    const roundResultDir = path.join(experimentRoot, "results", `round-${round}`);
    await ensureDir(roundResultDir);
    const roundRuns = plan.runs.filter((run) => run.round === round);
    for (const planned of roundRuns) {
      if (blocked) {
        planned.status = "not_started";
        planned.reason = blockReason;
        continue;
      }
      planned.status = "running";
      await writePlan(experimentRoot, plan);
      const sourceProject = path.resolve(plan.examples_root, planned.project);
      const runRoot = path.join(experimentRoot, "runs", planned.run_id);
      try {
        const task = selection.selected.find((item) => item.row.instance_id === planned.instance_id);
        if (!task) throw new Error(`Selected task disappeared: ${planned.instance_id}`);
        const execution = await executeRun({
          experimentId: plan.experiment_id,
          runId: planned.run_id,
          round,
          repeat: round,
          tier: planned.tier,
          project: planned.project,
          taskInstruction: task.instruction,
          instanceId: planned.instance_id,
          sourceProject,
          runRoot,
          roundResultDir,
          config: plan.fixed_config,
        });
        planned.status = execution.receipt.execution.run_status;
        planned.receipt_path = path.join(runRoot, "receipt.json");
        submissionsByRound[round].push(execution.submission);
        if (planned.status === "environment_failed") {
          blocked = true;
          blockReason = `environment failure in ${planned.instance_id}; remaining runs were not started`;
        }
      } catch (error) {
        const classified = classifyError(error);
        planned.status = classified.status;
        planned.reason = classified.reason;
        planned.receipt_path = await writeFailureReceipt({ experimentId: plan.experiment_id, planned, runRoot, roundResultDir, config: plan.fixed_config, error });
        if (classified.status === "environment_failed") {
          blocked = true;
          blockReason = `environment failure in ${planned.instance_id}; remaining runs were not started`;
        }
      }
      await writePlan(experimentRoot, plan);
    }

    for (const pending of plan.runs.filter((run) => run.round === round && run.status === "pending")) {
      pending.status = "not_started";
      pending.reason = blockReason || "not started after round execution";
    }
    await writeRoundMetadata(roundResultDir, submissionsByRound[round]);
    const evaluation = await evaluateRound({
      round,
      resultDir: roundResultDir,
      goldDir: plan.gold_dir,
      evaluatorScript: plan.evaluator_script,
      pythonCommand: plan.fixed_config.commands.python,
      timeoutMs: plan.fixed_config.limits.evaluatorTimeoutMs,
      expectedInstanceIds: selection.selected.map((item) => item.row.instance_id),
      logPath: path.join(experimentRoot, "evaluator-logs", `round-${round}.log`),
    });
    const evaluationPath = path.join(experimentRoot, "evaluations", `round-${round}.json`);
    await writeJson(evaluationPath, evaluation);
    plan.evaluation_paths[round] = evaluationPath;
    for (const planned of plan.runs.filter((run) => run.round === round && run.receipt_path)) {
      await updateReceiptJudgment(planned.receipt_path!, evaluation);
    }
    if (evaluation.status !== "completed") {
      blocked = true;
      blockReason = `official evaluator ${evaluation.status}; later runs were not started`;
      plan.status = "blocked";
      plan.block_reason = blockReason;
      const nextRoundIndex = plan.runs.findIndex((run) => run.round === round + 1);
      markNotStarted(plan, nextRoundIndex, blockReason);
      await writePlan(experimentRoot, plan);
      break;
    }
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
