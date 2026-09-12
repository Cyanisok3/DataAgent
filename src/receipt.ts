import { writeFile } from "node:fs/promises";
import path from "node:path";
import { runAgent, type AgentExecutionResult } from "./agent.js";
import { buildSubmission, type SubmissionBuildResult } from "./submission.js";
import { ensureDir, writeJson } from "./files.js";
import { detectRestrictedBackend } from "./process.js";
import { runFinalDbtValidation } from "./validation.js";
import { writeDiff, prepareRunWorkspace, type RunWorkspace } from "./workspace.js";
import type { FixedAgentConfig, RunReceipt, StaticAnalysis, Tier } from "./types.js";

function runStatus(agent: AgentExecutionResult): RunReceipt["execution"]["run_status"] {
  if (agent.stopReason === "wall_clock_timeout" || agent.stopReason === "model_request_limit") return "timed_out";
  if (agent.error) return /not found|permission|profile|command|path escapes|API key|authentication|unauthorized|\b401\b|\b403\b/i.test(agent.error) ? "environment_failed" : "agent_failed";
  return "completed";
}

export function validationStatus(finalValidation: RunReceipt["validation"]["commands"][number] | undefined): RunReceipt["validation"]["dbt_status"] {
  if (!finalValidation) return "not_run";
  if (finalValidation.timed_out) return "timed_out";
  return finalValidation.exit_code === 0 ? "passed" : "failed";
}

function failureLabels(agent: AgentExecutionResult, submission: SubmissionBuildResult, validation: RunReceipt["validation"]): string[] {
  const labels: string[] = [];
  if (agent.stopReason === "wall_clock_timeout") labels.push("wall_clock_timeout");
  if (agent.stopReason === "model_request_limit") labels.push("model_request_limit");
  if (agent.error) labels.push("agent_error");
  if (agent.stopReason === "model_error" || agent.stopReason === "agent_aborted") labels.push("model_error");
  if (validation.historical_failure_count > 0) labels.push("dbt_historical_attempt_failed");
  if (validation.dbt_status === "failed") labels.push("dbt_validation_failed");
  if (validation.dbt_status === "timed_out") labels.push("dbt_validation_timeout");
  if (submission.record.failure_label) labels.push(submission.record.failure_label);
  return [...new Set(labels)];
}

export interface ExecuteRunOptions {
  experimentId: string;
  runId: string;
  tier: Tier;
  project: string;
  taskInstruction: string;
  instanceId: string;
  sourceProject: string;
  runRoot: string;
  roundResultDir: string;
  config: FixedAgentConfig;
}

export async function executeRun(options: ExecuteRunOptions): Promise<{ receipt: RunReceipt; workspace: RunWorkspace; submission: SubmissionBuildResult }> {
  const workspace = await prepareRunWorkspace({ runRoot: options.runRoot, sourceProject: options.sourceProject });
  const agent = await runAgent({ workspace, config: options.config, taskInstruction: options.taskInstruction, instanceId: options.instanceId });
  const finalValidation = await runFinalDbtValidation({ workspace, config: options.config, startedMs: Date.parse(agent.startedAt) });
  const validations = finalValidation ? [...agent.dbtValidations, finalValidation] : [...agent.dbtValidations];
  const historicalFailureCount = agent.dbtValidations.filter((record) => record.timed_out || record.exit_code !== 0).length;
  const finalErrorSummary = finalValidation !== undefined && (finalValidation.timed_out || finalValidation.exit_code !== 0)
    ? (finalValidation.stderr_summary || finalValidation.stdout_summary || "dbt final validation failed")
    : undefined;
  const validation: RunReceipt["validation"] = {
    commands: validations,
    scope: "the copied task repository; dbt target and logs are stored outside the submitted artifact directory",
    dbt_status: validationStatus(finalValidation),
    historical_failure_count: historicalFailureCount,
    ...(finalErrorSummary ? { error_summary: finalErrorSummary } : {}),
  };
  const submission = await buildSubmission({ instanceId: options.instanceId, finalResponse: agent.finalResponse, workspaceRepo: workspace.repo, roundResultDir: options.roundResultDir });
  const diffResult = await writeDiff(workspace);
  await writeFile(workspace.diffPath, diffResult.diff, "utf8");
  await ensureDir(path.dirname(workspace.receiptPath));
  const executionEndedAt = validations.at(-1)?.ended_at ?? agent.endedAt;
  const executionDuration = Math.max(0, Date.parse(executionEndedAt) - Date.parse(agent.startedAt));
  const receipt: RunReceipt = {
    schema_version: "1.0",
    run_id: options.runId,
    experiment_id: options.experimentId,
    instance_id: options.instanceId,
    tier: options.tier,
    project: options.project,
    fixed_config_name: options.config.name,
    fixed_config: options.config,
    execution: {
      started_at: agent.startedAt,
      ended_at: executionEndedAt,
      duration_ms: executionDuration,
      model_request_attempts: agent.modelRequestAttempts,
      retry_attempts: agent.retryAttempts,
      ...(agent.tokenUsage ? { token_usage: agent.tokenUsage } : {}),
      stop_reason: agent.stopReason,
      run_status: runStatus(agent),
      isolation: {
        mode: "restricted-subprocess",
        sandbox_backend: finalValidation?.sandbox_backend ?? agent.dbtValidations.at(-1)?.sandbox_backend ?? detectRestrictedBackend(),
        workspace: workspace.repo,
        agent_config_dir: workspace.agentConfigDir,
        gold_visible_to_agent: false,
        evaluator_visible_to_agent: false,
        arbitrary_shell_enabled: false,
      },
    },
    modification_and_tools: {
      diff_path: workspace.diffPath,
      events_path: workspace.eventsPath,
      final_response_path: workspace.finalResponsePath,
      changed_files: diffResult.changedFiles,
    },
    validation,
    submission: submission.record,
    judgment: {
      score: null,
      verdict: "pending",
      scoring_status: "pending",
      failure_labels: failureLabels(agent, submission, validation),
    },
    attachments: {
      workspace_repo: workspace.repo,
      baseline_repo: workspace.baselineRepo,
      dbt_logs_dir: workspace.dbtEvidenceDir,
    },
  };
  await writeJson(workspace.receiptPath, receipt);
  return { receipt, workspace, submission };
}
