import { readFile, stat } from "node:fs/promises";
import path from "node:path";
import { createFixedConfig, loadSystemPrompt, PI_SDK_VERSION } from "./config.js";
import { copyTree, ensureDir, fileExists, isWithin, readJson, toPosix, writeJson } from "./files.js";
import { commandVersion, detectRestrictedBackend, runCommand, runRestrictedCommand, safeChildEnv } from "./process.js";
import { loadSelection } from "./selection.js";
import { runDatabaseInspection } from "./database-inspection.js";
import { validateRoundResultDirectory } from "./submission.js";
import type { FixedAgentConfig, SelectionManifest } from "./types.js";

export interface PreflightCheck {
  name: string;
  required: boolean;
  passed: boolean;
  details: string;
  command?: string[];
}

export interface PreflightResult {
  schema_version: "1.0";
  created_on: string;
  ok: boolean;
  selection_path: string;
  examples_root: string;
  experiment_root: string;
  evaluator_script: string;
  gold_dir: string;
  checks: PreflightCheck[];
  limitations: string[];
  config?: FixedAgentConfig;
}

function versionTuple(value: string): [number, number, number] | undefined {
  const match = value.match(/^(\d+)\.(\d+)(?:\.(\d+))?/);
  return match ? [Number(match[1]), Number(match[2]), Number(match[3] ?? 0)] : undefined;
}

function atLeast(actual: string, minimum: [number, number, number]): boolean {
  const parsed = versionTuple(actual);
  if (!parsed) return false;
  for (let index = 0; index < 3; index += 1) {
    if (parsed[index] !== minimum[index]) return parsed[index] > minimum[index];
  }
  return true;
}

async function isDirectory(directory: string): Promise<boolean> {
  try {
    return (await stat(directory)).isDirectory();
  } catch {
    return false;
  }
}

function compactCommandOutput(stdout: string, stderr: string): string {
  const output = `${stdout}\n${stderr}`.trim().replace(/\s+/g, " ");
  return output.slice(0, 600) || "no output";
}

function add(checks: PreflightCheck[], check: PreflightCheck): void {
  checks.push(check);
}

function skipSmokePath(relativePath: string): boolean {
  const segments = toPosix(relativePath).toLowerCase().split("/");
  return segments.includes(".git") || segments.includes("node_modules") || segments.includes("target");
}

async function profileDirectory(projectPath: string, fallback: string): Promise<string> {
  for (const directory of [projectPath, path.join(projectPath, ".dbt")]) {
    if (await fileExists(path.join(directory, "profiles.yml")) || await fileExists(path.join(directory, "profiles.yaml"))) return directory;
  }
  return fallback;
}

async function commandCheck(options: { name: string; command: string; cwd: string; required: boolean; checks: PreflightCheck[] }): Promise<void> {
  const result = await commandVersion(options.command, options.cwd);
  add(options.checks, {
    name: options.name,
    required: options.required,
    passed: result.exit_code === 0 && !result.timed_out && !result.aborted,
    details: compactCommandOutput(result.stdout, result.stderr),
    command: result.command,
  });
}

async function packageVersion(projectRoot: string, packageName: string): Promise<string | undefined> {
  const packagePath = path.join(projectRoot, "node_modules", ...packageName.split("/"), "package.json");
  try {
    const packageJson = JSON.parse(await readFile(packagePath, "utf8")) as { version?: unknown };
    return typeof packageJson.version === "string" ? packageJson.version : undefined;
  } catch {
    return undefined;
  }
}

function selectedProjectPath(manifest: SelectionManifest, examplesRoot: string, project: string): string {
  const projectPath = path.resolve(examplesRoot, project);
  if (!isWithin(projectPath, examplesRoot)) throw new Error(`Selected project escapes examples root: ${project}`);
  return projectPath;
}

async function visibleProfile(projectPath: string): Promise<boolean> {
  return (await fileExists(path.join(projectPath, "profiles.yml"))) ||
    (await fileExists(path.join(projectPath, "profiles.yaml"))) ||
    (await fileExists(path.join(projectPath, ".dbt", "profiles.yml"))) ||
    (await fileExists(path.join(projectPath, ".dbt", "profiles.yaml")));
}

export async function runPreflight(options: {
  projectRoot: string;
  selectionPath: string;
  experimentRoot: string;
  evaluatorScript: string;
  goldDir: string;
  model?: string;
  thinking?: string;
  pythonCommand?: string;
  dbtCommand?: string;
  duckdbCommand?: string;
}): Promise<{ result: PreflightResult; manifest?: SelectionManifest }> {
  const projectRoot = path.resolve(options.projectRoot);
  const selectionPath = path.resolve(options.selectionPath);
  const experimentRoot = path.resolve(options.experimentRoot);
  const evaluatorScript = path.resolve(options.evaluatorScript);
  const goldDir = path.resolve(options.goldDir);
  const checks: PreflightCheck[] = [];
  let manifest: SelectionManifest | undefined;
  let examplesRoot = "";
  let config: FixedAgentConfig | undefined;

  add(checks, {
    name: "node",
    required: true,
    passed: atLeast(process.versions.node, [22, 19, 0]),
    details: `node ${process.versions.node}; required >=22.19.0`,
  });

  try {
    manifest = await loadSelection(selectionPath);
    examplesRoot = path.resolve(manifest.examples_root);
    add(checks, { name: "selection_manifest", required: true, passed: true, details: "ready selection with exactly 12 tasks and a 3/6/3 low/medium/high tier split" });
  } catch (error) {
    add(checks, { name: "selection_manifest", required: true, passed: false, details: error instanceof Error ? error.message : String(error) });
  }

  if (manifest) {
    if (options.model && options.thinking) {
      try {
        const prompt = await loadSystemPrompt(projectRoot);
        config = createFixedConfig({
          projectRoot,
          model: options.model,
          thinking: options.thinking,
          systemPromptPath: prompt.path,
          systemPrompt: prompt.content,
          pythonCommand: options.pythonCommand,
          dbtCommand: options.dbtCommand,
          duckdbCommand: options.duckdbCommand,
        });
        add(checks, { name: "fixed_agent_config", required: true, passed: true, details: `model=${options.model}; thinking=${options.thinking}; SDK=${PI_SDK_VERSION}` });
      } catch (error) {
        add(checks, { name: "fixed_agent_config", required: true, passed: false, details: error instanceof Error ? error.message : String(error) });
      }
    } else {
      add(checks, { name: "fixed_agent_config", required: true, passed: false, details: "exact --model and --thinking are required" });
    }
  }

  add(checks, {
    name: "examples_root",
    required: true,
    passed: await isDirectory(examplesRoot),
    details: examplesRoot || "selection manifest could not provide examples root",
  });
  add(checks, {
    name: "experiment_root_not_inside_examples",
    required: true,
    passed: Boolean(examplesRoot) && !isWithin(experimentRoot, examplesRoot),
    details: `experiment_root=${experimentRoot}; examples_root=${examplesRoot || "unknown"}`,
  });

  if (manifest && examplesRoot) {
    for (const selected of manifest.selected) {
      let projectPath = "";
      let passed = true;
      let details = "";
      try {
        projectPath = selectedProjectPath(manifest, examplesRoot, selected.row.project);
        const projectDir = await isDirectory(projectPath);
        const dbtProject = await fileExists(path.join(projectPath, "dbt_project.yml"));
        const profile = await visibleProfile(projectPath);
        const database = selected.databaseFiles.length > 0 && (await Promise.all(selected.databaseFiles.map((file) => fileExists(path.join(projectPath, file))))).every(Boolean);
        passed = projectDir && dbtProject && profile && database;
        details = `project=${projectPath}; dbt_project=${dbtProject}; visible_profile=${profile}; database=${database}`;
      } catch (error) {
        passed = false;
        details = error instanceof Error ? error.message : String(error);
      }
      add(checks, { name: `selected_project:${selected.row.instance_id}`, required: true, passed, details });
    }
  }

  const restrictedBackend = detectRestrictedBackend();
  if (restrictedBackend === "docker") {
    const dockerInfo = await runCommand("docker", ["info", "--format", "{{.ServerVersion}}"], { cwd: projectRoot, env: safeChildEnv(), timeoutMs: 10_000, maxCapturedChars: 2_000 });
    add(checks, {
      name: "restricted_execution_backend",
      required: true,
      passed: dockerInfo.exit_code === 0 && !dockerInfo.timed_out && !dockerInfo.aborted,
      details: `backend=docker; ${compactCommandOutput(dockerInfo.stdout, dockerInfo.stderr)}`,
      command: dockerInfo.command,
    });
  } else if (restrictedBackend === "macos-sandbox-exec") {
    const probeRoot = path.join(experimentRoot, ".preflight-sandbox", "repo");
    const probeRuntime = path.join(experimentRoot, ".preflight-sandbox", "runtime");
    await Promise.all([ensureDir(probeRoot), ensureDir(probeRuntime)]);
    const sandboxProbe = await runRestrictedCommand("/usr/bin/true", [], {
      cwd: probeRoot,
      root: probeRoot,
      runtimeDir: probeRuntime,
      env: safeChildEnv({ TMPDIR: path.join(probeRuntime, "tmp"), HOME: path.join(probeRuntime, "home") }),
      timeoutMs: 10_000,
      maxCapturedChars: 2_000,
    });
    add(checks, {
      name: "restricted_execution_backend",
      required: true,
      passed: sandboxProbe.exit_code === 0 && !sandboxProbe.timed_out && !sandboxProbe.aborted,
      details: `backend=macos-sandbox-exec; ${compactCommandOutput(sandboxProbe.stdout, sandboxProbe.stderr)}`,
      command: sandboxProbe.command,
    });
  } else {
    add(checks, {
      name: "restricted_execution_backend",
      required: true,
      passed: restrictedBackend !== "unavailable",
      details: `backend=${restrictedBackend}; dbt subprocesses must run inside this restricted backend`,
    });
  }

  const smokeCommand = config?.commands.dbt ?? options.dbtCommand ?? "dbt";
  const smokeRoot = path.join(experimentRoot, ".preflight-dbt-smoke");
  const smokeRepo = path.join(smokeRoot, "repo");
  const smokeRuntime = path.join(smokeRoot, "runtime");
  let smokeArgs = ["--no-version-check", "debug"];
  let smokeResult: Awaited<ReturnType<typeof runRestrictedCommand>> | undefined;
  try {
    const smokeSelected = manifest?.selected[0];
    if (!smokeSelected || !examplesRoot || restrictedBackend === "unavailable") {
      throw new Error(restrictedBackend === "unavailable" ? "no restricted backend is available" : "no selected task is available for the dbt smoke test");
    }
    const sourceProject = selectedProjectPath(manifest!, examplesRoot, smokeSelected.row.project);
    await copyTree(sourceProject, smokeRepo, { skip: skipSmokePath });
    const smokeProfilesDir = await profileDirectory(smokeRepo, path.join(smokeRuntime, "profiles"));
    const targetDir = path.join(smokeRuntime, "target");
    const logDir = path.join(smokeRuntime, "logs");
    const userConfigDir = path.join(smokeRuntime, "user-config");
    const tempDir = path.join(smokeRuntime, "tmp");
    const homeDir = path.join(smokeRuntime, "home");
    const cacheDir = path.join(smokeRuntime, "cache");
    const pycacheDir = path.join(smokeRuntime, "pycache");
    await Promise.all([targetDir, logDir, userConfigDir, tempDir, homeDir, cacheDir, pycacheDir].map(ensureDir));
    smokeArgs = [
      "--no-version-check",
      "debug",
      "--project-dir",
      smokeRepo,
      "--profiles-dir",
      smokeProfilesDir,
      "--log-path",
      logDir,
    ];
    smokeResult = await runRestrictedCommand(smokeCommand, smokeArgs, {
      cwd: smokeRepo,
      env: safeChildEnv({
        DBT_PROFILES_DIR: smokeProfilesDir,
        DBT_TARGET_PATH: targetDir,
        DBT_LOG_PATH: logDir,
        DBT_USER_CONFIG_DIR: userConfigDir,
        TMPDIR: tempDir,
        TMP: tempDir,
        TEMP: tempDir,
        HOME: homeDir,
        XDG_CACHE_HOME: cacheDir,
        PYTHONPYCACHEPREFIX: pycacheDir,
        DATAAGENT_TASK_ROOT: smokeRepo,
        DATAAGENT_RUNTIME_DIR: smokeRuntime,
        DATAAGENT_DBT_COMMAND: smokeCommand,
        PYTHONUNBUFFERED: "1",
      }),
      timeoutMs: Math.min(config?.limits.commandTimeoutMs ?? 30_000, 30_000),
      maxCapturedChars: 4_000,
      root: smokeRepo,
      runtimeDir: smokeRuntime,
    });
  } catch (error) {
    add(checks, {
      name: "restricted_dbt_smoke",
      required: true,
      passed: false,
      details: error instanceof Error ? error.message : String(error),
      command: [smokeCommand, ...smokeArgs],
    });
  }
  if (smokeResult) {
    add(checks, {
      name: "restricted_dbt_smoke",
      required: true,
      passed: smokeResult.exit_code === 0 && !smokeResult.timed_out && !smokeResult.aborted,
      details: `backend=${restrictedBackend}; ${compactCommandOutput(smokeResult.stdout, smokeResult.stderr)}`,
      command: smokeResult.command,
    });
  }

  const inspectionPython = config?.commands.python ?? options.pythonCommand ?? "python3";
  const smokeSelected = manifest?.selected[0];
  const inspectionDatabase = smokeSelected?.databaseFiles.find((file) => file.toLowerCase().endsWith(".duckdb"));
  try {
    if (!inspectionDatabase) throw new Error("no selected .duckdb file is available for the database inspection smoke test");
    const inspection = await runDatabaseInspection({
      root: smokeRepo,
      runtimeDir: path.join(smokeRuntime, "database-inspection"),
      pythonCommand: inspectionPython,
      commandTimeoutMs: Math.min(config?.limits.commandTimeoutMs ?? 30_000, 30_000),
      remainingMs: () => 30_000,
      request: { path: inspectionDatabase, action: "tables" },
    });
    add(checks, {
      name: "restricted_database_inspection",
      required: true,
      passed: true,
      details: `backend=${inspection.sandboxBackend}; rows=${inspection.rowCount}; next_offset=${inspection.nextOffset ?? "none"}`,
      command: inspection.command,
    });
  } catch (error) {
    add(checks, {
      name: "restricted_database_inspection",
      required: true,
      passed: false,
      details: error instanceof Error ? error.message : String(error),
      command: [inspectionPython, "-I", "-c", "<fixed database inspection>", inspectionDatabase ?? ""],
    });
  }

  await commandCheck({ name: "python", command: config?.commands.python ?? options.pythonCommand ?? "python3", cwd: projectRoot, required: true, checks });
  const pythonCommand = config?.commands.python ?? options.pythonCommand ?? "python3";
  const duckdbImport = await runCommand(pythonCommand, ["-c", "import duckdb; print(duckdb.__version__)"], { cwd: projectRoot, env: safeChildEnv(), timeoutMs: 10_000, maxCapturedChars: 2_000 });
  add(checks, {
    name: "python_duckdb",
    required: true,
    passed: duckdbImport.exit_code === 0 && !duckdbImport.timed_out && !duckdbImport.aborted,
    details: compactCommandOutput(duckdbImport.stdout, duckdbImport.stderr),
    command: duckdbImport.command,
  });
  await commandCheck({ name: "duckdb_cli_optional", command: config?.commands.duckdb ?? options.duckdbCommand ?? "duckdb", cwd: projectRoot, required: false, checks });

  const sdkVersion = await packageVersion(projectRoot, "@earendil-works/pi-coding-agent");
  add(checks, { name: "pi_sdk_version", required: true, passed: sdkVersion === PI_SDK_VERSION, details: `installed=${sdkVersion ?? "missing"}; required=${PI_SDK_VERSION}` });
  add(checks, { name: "evaluator_script", required: true, passed: await fileExists(evaluatorScript), details: evaluatorScript });
  add(checks, { name: "gold_file", required: true, passed: await fileExists(path.join(goldDir, "spider2_eval.jsonl")), details: path.join(goldDir, "spider2_eval.jsonl") });

  const resultDir = path.join(experimentRoot, "results", "round-1");
  if (await fileExists(resultDir)) {
    const format = await validateRoundResultDirectory({ resultDir, expectedInstanceIds: manifest?.selected.map((item) => item.row.instance_id) ?? [] });
    add(checks, { name: "submission_format:round-1", required: true, passed: format.ok, details: format.problems.join("; ") || "one results_metadata.jsonl plus instance artifact directories" });
  }

  const result: PreflightResult = {
    schema_version: "1.0",
    created_on: new Date().toISOString(),
    ok: checks.filter((check) => check.required).every((check) => check.passed),
    selection_path: selectionPath,
    examples_root: examplesRoot,
    experiment_root: experimentRoot,
    evaluator_script: evaluatorScript,
    gold_dir: goldDir,
    checks,
    limitations: ["This is an environment and contract check only; it does not call the model or official evaluator."],
    ...(config ? { config } : {}),
  };
  await writeJson(path.join(experimentRoot, "preflight.json"), result);
  return { result, manifest };
}
