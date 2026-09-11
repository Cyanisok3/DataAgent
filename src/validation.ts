import { writeFile } from "node:fs/promises";
import path from "node:path";
import { ensureDir, fileExists, truncateText } from "./files.js";
import { runRestrictedCommand, safeChildEnv } from "./process.js";
import type { DbtValidationRecord, FixedAgentConfig } from "./types.js";
import type { RunWorkspace } from "./workspace.js";

export async function runFinalDbtValidation(options: {
  workspace: RunWorkspace;
  config: FixedAgentConfig;
  startedMs: number;
}): Promise<DbtValidationRecord | undefined> {
  const remainingMs = options.config.limits.wallClockMs - (Date.now() - options.startedMs);
  if (remainingMs <= 0) return undefined;
  const runtimeDir = path.join(options.workspace.dbtEvidenceDir, "final");
  const fallbackProfilesDir = path.join(runtimeDir, "profiles");
  const profilesDir = await visibleProfilesDirectory(options.workspace.repo, fallbackProfilesDir);
  const targetDir = path.join(runtimeDir, "target");
  const logDir = path.join(runtimeDir, "logs");
  const userConfigDir = path.join(runtimeDir, "user-config");
  const tempDir = path.join(runtimeDir, "tmp");
  const homeDir = path.join(runtimeDir, "home");
  const cacheDir = path.join(runtimeDir, "cache");
  const pycacheDir = path.join(runtimeDir, "pycache");
  await Promise.all([
    ensureDir(profilesDir),
    ensureDir(targetDir),
    ensureDir(logDir),
    ensureDir(userConfigDir),
    ensureDir(tempDir),
    ensureDir(homeDir),
    ensureDir(cacheDir),
    ensureDir(pycacheDir),
  ]);
  const args = ["--no-version-check", "build", "--profiles-dir", profilesDir, "--target-path", targetDir, "--log-path", logDir];
  const result = await runRestrictedCommand(options.config.commands.dbt, args, {
    cwd: options.workspace.repo,
    env: safeChildEnv({
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
      DATAAGENT_TASK_ROOT: options.workspace.repo,
      DATAAGENT_RUNTIME_DIR: runtimeDir,
      PYTHONUNBUFFERED: "1",
    }),
    timeoutMs: Math.min(options.config.limits.commandTimeoutMs, Math.max(1, remainingMs)),
    maxCapturedChars: 256_000,
    root: options.workspace.repo,
    runtimeDir,
  });
  const logPath = path.join(options.workspace.dbtEvidenceDir, "final-build.log");
  await writeFile(logPath, `${result.command.join(" ")}\n\n[stdout]\n${result.stdout}\n\n[stderr]\n${result.stderr}\n`, "utf8");
  return {
    action: "build",
    command: result.command,
    started_at: result.started_at,
    ended_at: result.ended_at,
    duration_ms: result.duration_ms,
    exit_code: result.exit_code,
    timed_out: result.timed_out,
    log_path: logPath,
    stdout_summary: truncateText(result.stdout, 16_000),
    stderr_summary: truncateText(result.stderr, 16_000),
    source: "runner_final_validation",
    sandbox_backend: result.sandbox_backend ?? "unavailable",
  };
}

async function visibleProfilesDirectory(root: string, fallback: string): Promise<string> {
  for (const directory of [root, path.join(root, ".dbt")]) {
    if (await fileExists(path.join(directory, "profiles.yml")) || await fileExists(path.join(directory, "profiles.yaml"))) return directory;
  }
  return fallback;
}
