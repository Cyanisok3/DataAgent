import { writeFile } from "node:fs/promises";
import path from "node:path";
import { fileExists, readJson } from "./files.js";
import type { ExperimentPlan } from "./experiment.js";
import type { EvaluationRecord, RunReceipt, Tier } from "./types.js";

function cell(value: string | number | null | undefined): string {
  return String(value ?? "—").replaceAll("|", "\\|").replaceAll("\n", " ");
}

function scoreFor(evaluation: EvaluationRecord | undefined, instanceId: string): string {
  const score = evaluation?.task_scores.find((item) => item.instance_id === instanceId);
  if (!score) return "—";
  if (score.score !== null) return String(score.score);
  return score.status === "scoring_unavailable" ? "unavailable" : "not_evaluated";
}

function scoreValue(evaluation: EvaluationRecord | undefined, instanceId: string): number | null {
  return evaluation?.task_scores.find((item) => item.instance_id === instanceId)?.score ?? null;
}

function countBy<T extends string>(values: T[]): Map<T, number> {
  const counts = new Map<T, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return counts;
}

export async function writeDiagnosticReport(experimentRoot: string): Promise<string> {
  const root = path.resolve(experimentRoot);
  const plan = await readJson<ExperimentPlan>(path.join(root, "plan.json"));
  const evaluations = new Map<1 | 2, EvaluationRecord>();
  for (const round of [1, 2] as const) {
    const evaluationPath = plan.evaluation_paths[round];
    if (evaluationPath && await fileExists(evaluationPath)) evaluations.set(round, await readJson<EvaluationRecord>(evaluationPath));
  }
  const receipts = new Map<string, RunReceipt>();
  for (const run of plan.runs) {
    if (run.receipt_path && await fileExists(run.receipt_path)) receipts.set(run.run_id, await readJson<RunReceipt>(run.receipt_path));
  }
  const statusCounts = countBy(plan.runs.map((run) => run.status));
  const officialSuccessful = [...evaluations.values()].filter((evaluation) => evaluation.status === "completed").reduce((sum, evaluation) => sum + (evaluation.successful_runs ?? 0), 0);
  const officialEvaluated = [...evaluations.values()].filter((evaluation) => evaluation.status === "completed").reduce((sum, evaluation) => sum + (evaluation.evaluated_runs ?? 0), 0);
  const dbtCounts = countBy([...receipts.values()].map((receipt) => receipt.validation.dbt_status));
  const failureLabels = [...receipts.values()].flatMap((receipt) => receipt.judgment.failure_labels);
  const labelCounts = countBy(failureLabels);
  const repeated = [...labelCounts.entries()].filter(([, count]) => count >= 2).sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]));
  const selected = [...new Map(plan.runs.map((run) => [run.instance_id, run])).values()].sort((left, right) => left.instance_id.localeCompare(right.instance_id));

  const lines: string[] = [
    "# DataAgent 诊断报告",
    "",
    `- experiment_id: ${plan.experiment_id}`,
    `- protocol: 9 tasks × 2 identical rounds = 18 planned runs` ,
    `- plan_status: ${plan.status}`,
    `- official success: ${officialSuccessful}/18` ,
    `- official evaluated: ${officialEvaluated}/18` ,
    "- dbt validation and official evaluator results are reported separately; dbt pass is not official success.",
    "",
    "## Run status",
    "",
    "| status | count |",
    "|---|---:|",
  ];
  for (const [status, count] of [...statusCounts.entries()].sort((left, right) => left[0].localeCompare(right[0]))) lines.push(`| ${cell(status)} | ${count} |`);
  lines.push("", "## Per-task official results", "", "| instance_id | tier | round 1 | round 2 |", "|---|---|---:|---:|");
  for (const task of selected) lines.push(`| ${cell(task.instance_id)} | ${cell(task.tier)} | ${cell(scoreFor(evaluations.get(1), task.instance_id))} | ${cell(scoreFor(evaluations.get(2), task.instance_id))} |`);

  lines.push("", "## Tier totals", "", "| tier | planned | successful | evaluated |", "|---|---:|---:|---:|");
  for (const tier of ["low", "medium", "high"] as Tier[]) {
    const taskIds = new Set(selected.filter((task) => task.tier === tier).map((task) => task.instance_id));
    let successful = 0;
    let evaluated = 0;
    for (const evaluation of evaluations.values()) {
      for (const taskId of taskIds) {
        const value = scoreValue(evaluation, taskId);
        if (value !== null) evaluated += 1;
        if (value === 1) successful += 1;
      }
    }
    lines.push(`| ${tier} | ${taskIds.size * 2} | ${successful} | ${evaluated} |`);
  }

  lines.push("", "## dbt status", "", "| status | count |", "|---|---:|");
  for (const [status, count] of [...dbtCounts.entries()].sort((left, right) => left[0].localeCompare(right[0]))) lines.push(`| ${cell(status)} | ${count} |`);
  lines.push("", "## All 18 planned runs", "", "| round | repeat | instance_id | tier | run status | dbt status | receipt |", "|---:|---:|---|---|---|---|---|");
  for (const run of plan.runs) {
    const receipt = receipts.get(run.run_id);
    lines.push(`| ${run.round} | ${run.repeat} | ${cell(run.instance_id)} | ${cell(run.tier)} | ${cell(run.status)} | ${cell(receipt?.validation.dbt_status)} | ${receipt ? cell(path.relative(root, run.receipt_path ?? "")) : "—"} |`);
  }

  lines.push("", "## Failure diagnosis", "");
  if (repeated.length > 0) {
    const description = repeated.slice(0, 3).map(([label, count]) => `${label} (${count})`).join(", ");
    lines.push(`重复出现的 runner-visible 机制：${description}。这是基于执行日志、提交契约和 dbt 状态的轻量归因，仍需人工检查具体日志；不会把 dbt 通过且官方失败直接解释为语义 verifier 机制。`);
  } else {
    lines.push("证据不足：没有在至少两个 run 中重复出现的 runner-visible 失败机制。官方失败、未评测、超时和 dbt 失败保持分开，不能仅凭 dbt 通过/官方失败推断语义原因。");
  }
  lines.push("", "### Failure labels", "", "| label | count |", "|---|---:|");
  if (labelCounts.size === 0) lines.push("| none recorded | 0 |");
  for (const [label, count] of [...labelCounts.entries()].sort((left, right) => left[0].localeCompare(right[0]))) lines.push(`| ${cell(label)} | ${count} |`);
  lines.push("", "## Limitations", "", "- Machine receipts and official evaluator output are not human confirmation.", "- Missing, timeout, and unscored runs remain distinct; no best-of-two score is reported.", "- This report is diagnostic evidence for this fixed 18-run experiment, not a production benchmark claim.", "");
  const reportPath = path.join(root, "diagnostic-report.md");
  await writeFile(reportPath, lines.join("\n"), "utf8");
  return reportPath;
}
