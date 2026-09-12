import path from "node:path";
import { evaluateRound } from "./evaluator.js";
import { fileExists, writeJson } from "./files.js";
import { runExperiment } from "./experiment.js";
import { runPreflight } from "./preflight.js";
import { writeDiagnosticReport } from "./report.js";
import { prepareSelection, loadSelection } from "./selection.js";

interface ParsedOptions {
  command: string;
  values: Map<string, string>;
  flags: Set<string>;
}

function parseArgs(argv: string[]): ParsedOptions {
  const command = argv[0] ?? "help";
  const values = new Map<string, string>();
  const flags = new Set<string>();
  for (let index = 1; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) throw new Error(`Unexpected argument: ${token}`);
    const key = token.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      values.set(key, next);
      index += 1;
    } else {
      flags.add(key);
    }
  }
  return { command, values, flags };
}

function required(options: ParsedOptions, name: string): string {
  const value = options.values.get(name);
  if (!value?.trim()) throw new Error(`Missing required option --${name}`);
  return value;
}

function optional(options: ParsedOptions, name: string): string | undefined {
  return options.values.get(name);
}

function numberOption(options: ParsedOptions, name: string, fallback: number): number {
  const value = optional(options, name);
  if (!value) return fallback;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new Error(`--${name} must be a positive integer`);
  return parsed;
}

function printHelp(): void {
  console.log(`DataAgent diagnostic runner

Commands:
  prepare   --tasks FILE --examples-root DIR --output-dir DIR [--pool-size 12] [--scope-confirmation FILE]
  preflight --selection FILE --experiment-root DIR --evaluator FILE --gold-dir DIR --model ID --thinking LEVEL
  experiment --selection FILE --examples-root DIR --output-root DIR --model ID --thinking LEVEL --evaluator FILE --gold-dir DIR
  evaluate  --result-dir DIR --gold-dir DIR --evaluator FILE --selection FILE [--output FILE]
  report    --experiment-root DIR
`);
}

async function projectRootFromCwd(): Promise<string> {
  const cwd = path.resolve(process.cwd());
  if (await fileExists(path.join(cwd, "package.json"))) return cwd;
  const nested = path.join(cwd, "DataAgent");
  if (await fileExists(path.join(nested, "package.json"))) return nested;
  return cwd;
}

async function main(): Promise<void> {
  const options = parseArgs(process.argv.slice(2));
  if (options.command === "help" || options.command === "--help" || options.command === "-h") {
    printHelp();
    return;
  }
  const projectRoot = await projectRootFromCwd();
  if (options.command === "prepare") {
    const manifest = await prepareSelection({
      taskFile: required(options, "tasks"),
      examplesRoot: required(options, "examples-root"),
      outputDir: required(options, "output-dir"),
      poolSize: numberOption(options, "pool-size", 12),
      scopeConfirmationPath: optional(options, "scope-confirmation"),
    });
    console.log(JSON.stringify({ status: manifest.status, selected: manifest.selected.length, excluded: manifest.excluded.length, output_dir: path.resolve(required(options, "output-dir")) }, null, 2));
    if (manifest.status !== "ready") process.exitCode = 2;
    return;
  }
  if (options.command === "preflight") {
    const preflight = await runPreflight({
      projectRoot,
      selectionPath: required(options, "selection"),
      experimentRoot: required(options, "experiment-root"),
      evaluatorScript: required(options, "evaluator"),
      goldDir: required(options, "gold-dir"),
      model: required(options, "model"),
      thinking: required(options, "thinking"),
      pythonCommand: optional(options, "python"),
      dbtCommand: optional(options, "dbt"),
      duckdbCommand: optional(options, "duckdb"),
    });
    console.log(JSON.stringify({ ok: preflight.result.ok, preflight_path: path.join(path.resolve(required(options, "experiment-root")), "preflight.json") }, null, 2));
    if (!preflight.result.ok) process.exitCode = 2;
    return;
  }
  if (options.command === "experiment") {
    const result = await runExperiment({
      projectRoot,
      selectionPath: required(options, "selection"),
      examplesRoot: required(options, "examples-root"),
      experimentRoot: required(options, "output-root"),
      evaluatorScript: required(options, "evaluator"),
      goldDir: required(options, "gold-dir"),
      model: required(options, "model"),
      thinking: required(options, "thinking"),
      pythonCommand: optional(options, "python"),
      dbtCommand: optional(options, "dbt"),
      duckdbCommand: optional(options, "duckdb"),
    });
    console.log(JSON.stringify({ status: result.plan.status, experiment_id: result.plan.experiment_id, plan_path: path.join(path.resolve(required(options, "output-root")), "plan.json"), preflight_ok: result.preflight.ok }, null, 2));
    if (result.plan.status !== "completed") process.exitCode = 2;
    return;
  }
  if (options.command === "evaluate") {
    const selection = await loadSelection(required(options, "selection"));
    const resultDir = path.resolve(required(options, "result-dir"));
    const output = path.resolve(optional(options, "output") ?? path.join(resultDir, "..", "evaluation.json"));
    const evaluation = await evaluateRound({
      resultDir,
      goldDir: required(options, "gold-dir"),
      evaluatorScript: required(options, "evaluator"),
      pythonCommand: optional(options, "python") ?? "python3",
      timeoutMs: 10 * 60 * 1000,
      expectedInstanceIds: selection.selected.map((item) => item.row.instance_id),
      logPath: path.resolve(optional(options, "log") ?? path.join(path.dirname(output), "evaluator-logs", "evaluator.log")),
    });
    await writeJson(output, evaluation);
    console.log(JSON.stringify({ status: evaluation.status, score: evaluation.score, evaluated: evaluation.evaluated_runs, output }, null, 2));
    if (evaluation.status !== "completed") process.exitCode = 2;
    return;
  }
  if (options.command === "report") {
    const reportPath = await writeDiagnosticReport(required(options, "experiment-root"));
    console.log(JSON.stringify({ report: reportPath }, null, 2));
    return;
  }
  throw new Error(`Unknown command: ${options.command}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
