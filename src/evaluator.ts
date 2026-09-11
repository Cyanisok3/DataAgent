import { copyFile, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { ensureDir, fileExists, readJsonl, writeJson } from "./files.js";
import { runCommand, safeChildEnv } from "./process.js";
import { validateRoundResultDirectory } from "./submission.js";
import type { EvaluationRecord, EvaluationTaskScore } from "./types.js";

export interface EvaluationOptions {
  round: 1 | 2;
  resultDir: string;
  goldDir: string;
  evaluatorScript: string;
  pythonCommand: string;
  timeoutMs: number;
  expectedInstanceIds: string[];
  logPath: string;
}

async function goldIds(goldDir: string): Promise<Set<string>> {
  const goldPath = path.join(goldDir, "spider2_eval.jsonl");
  const rows = await readJsonl<Record<string, unknown>>(goldPath);
  return new Set(rows.filter((row) => typeof row.instance_id === "string").map((row) => row.instance_id as string));
}

function successIds(stdout: string, knownIds: Set<string>): Set<string> {
  const successes = new Set<string>();
  for (const line of stdout.split(/\r?\n/)) {
    for (const id of knownIds) {
      if (line.trim() === id || line.includes(`'instance_id': '${id}'`) || line.includes(`"instance_id": "${id}"`)) successes.add(id);
    }
  }
  return successes;
}

function summary(stdout: string): { score: number; successful: number; evaluated: number } | undefined {
  const matches = [...stdout.matchAll(/^\s*(0(?:\.\d+)?|1(?:\.0+)?)\s+(\d+)\s+(\d+)\s*$/gm)];
  const match = matches.at(-1);
  if (!match) return undefined;
  return { score: Number(match[1]), successful: Number(match[2]), evaluated: Number(match[3]) };
}

function scoresFor(options: { expected: string[]; evaluatedIds: Set<string>; successes: Set<string>; scoringAvailable: boolean; noValidSubmissions: boolean }): EvaluationTaskScore[] {
  return options.expected.map((instanceId) => {
    if (options.noValidSubmissions) return { instance_id: instanceId, score: null, status: "not_evaluated" };
    if (!options.scoringAvailable) return { instance_id: instanceId, score: null, status: "scoring_unavailable" };
    if (!options.evaluatedIds.has(instanceId)) return { instance_id: instanceId, score: null, status: "not_evaluated" };
    if (options.successes.has(instanceId)) return { instance_id: instanceId, score: 1, status: "passed" };
    return { instance_id: instanceId, score: 0, status: "failed" };
  });
}

export async function evaluateRound(options: EvaluationOptions): Promise<EvaluationRecord> {
  const startedMs = Date.now();
  const startedAt = new Date(startedMs).toISOString();
  let submittedIds: string[] = [];
  let goldIdSet = new Set<string>();
  let setupError: string | undefined;
  try {
    const format = await validateRoundResultDirectory({ resultDir: options.resultDir, expectedInstanceIds: options.expectedInstanceIds });
    if (!format.ok) throw new Error(format.problems.join("; "));
    submittedIds = format.metadataIds;
    goldIdSet = await goldIds(options.goldDir);
  } catch (error) {
    setupError = error instanceof Error ? error.message : String(error);
  }
  const evaluatorDirectory = path.dirname(path.resolve(options.evaluatorScript));
  const result = setupError
    ? { command: [options.pythonCommand, options.evaluatorScript], started_at: startedAt, ended_at: new Date().toISOString(), duration_ms: 0, exit_code: null, timed_out: false, aborted: false, stdout: "", stderr: setupError }
    : submittedIds.length === 0
      ? { command: [options.pythonCommand, options.evaluatorScript], started_at: startedAt, ended_at: new Date().toISOString(), duration_ms: 0, exit_code: null, timed_out: false, aborted: false, stdout: "", stderr: "No valid submissions; official evaluator was not invoked." }
      : await runCommand(options.pythonCommand, [options.evaluatorScript, "--result_dir", options.resultDir, "--gold_dir", options.goldDir], {
      cwd: evaluatorDirectory,
      env: safeChildEnv({ PYTHONPATH: evaluatorDirectory }),
      timeoutMs: options.timeoutMs,
      maxCapturedChars: 1_000_000,
      });
  const logContent = `${result.command.join(" ")}\n\n[stdout]\n${result.stdout}\n\n[stderr]\n${result.stderr}\n`;
  await ensureDir(path.dirname(options.logPath));
  await writeFile(options.logPath, logContent, "utf8");
  const noValidSubmissions = !setupError && submittedIds.length === 0;
  const available = !setupError && !noValidSubmissions && result.exit_code === 0 && !result.timed_out && !result.aborted;
  const evaluatedIds = new Set(submittedIds.filter((instanceId) => goldIdSet.has(instanceId)));
  const successes = successIds(result.stdout, evaluatedIds);
  const parsedSummary = available ? summary(result.stdout) : undefined;
  const endedAt = new Date().toISOString();
  const status = noValidSubmissions ? "completed" : result.timed_out || result.aborted ? "timed_out" : available ? "completed" : "failed";
  return {
    round: options.round,
    result_dir: path.resolve(options.resultDir),
    gold_dir: path.resolve(options.goldDir),
    evaluator_script: path.resolve(options.evaluatorScript),
    evaluator_log: path.resolve(options.logPath),
    started_at: startedAt,
    ended_at: endedAt,
    duration_ms: Date.now() - startedMs,
    exit_code: result.exit_code,
    timed_out: result.timed_out,
    status,
    score: parsedSummary?.score ?? null,
    successful_runs: parsedSummary?.successful ?? (noValidSubmissions ? 0 : available ? successes.size : null),
    evaluated_runs: parsedSummary?.evaluated ?? (noValidSubmissions ? 0 : available ? evaluatedIds.size : null),
    task_scores: scoresFor({ expected: options.expectedInstanceIds, evaluatedIds, successes, scoringAvailable: available, noValidSubmissions }),
    ...(setupError ? { failure_label: "submission_or_gold_format" } : noValidSubmissions ? { failure_label: "no_valid_submissions" } : result.timed_out ? { failure_label: "evaluator_timeout" } : !available ? { failure_label: "evaluator_failure" } : {}),
  };
}

export async function retainEvaluatorCopy(options: { evaluatorScript: string; experimentRoot: string }): Promise<string> {
  const source = path.resolve(options.evaluatorScript);
  const referenceDir = path.join(path.resolve(options.experimentRoot), "evaluator-reference");
  await ensureDir(referenceDir);
  const destination = path.join(referenceDir, "evaluate.py");
  await copyFile(source, destination);
  const sibling = path.join(path.dirname(source), "eval_utils.py");
  if (await fileExists(sibling)) await copyFile(sibling, path.join(referenceDir, "eval_utils.py"));
  await writeJson(path.join(referenceDir, "source.json"), {
    source_path: source,
    copied_on: new Date().toISOString().slice(0, 10),
    official_url: "https://github.com/xlang-ai/Spider2/blob/main/spider2-dbt/evaluation_suite/evaluate.py",
  });
  return destination;
}
