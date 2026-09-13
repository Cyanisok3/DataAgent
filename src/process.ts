import { access, constants, readFile, realpath, writeFile } from "node:fs/promises";
import { accessSync, existsSync } from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { ensureDir, isoNow, truncateText } from "./files.js";

export type SandboxBackend = "macos-sandbox-exec" | "docker" | "unavailable";

export interface CommandResult {
  executable: string;
  args: string[];
  command: string[];
  started_at: string;
  ended_at: string;
  duration_ms: number;
  exit_code: number | null;
  timed_out: boolean;
  aborted: boolean;
  stdout: string;
  stderr: string;
  sandbox_backend?: SandboxBackend;
}

export interface RunCommandOptions {
  cwd: string;
  env?: NodeJS.ProcessEnv;
  timeoutMs: number;
  signal?: AbortSignal;
  maxCapturedChars?: number;
}

export interface RestrictedCommandOptions extends RunCommandOptions {
  root: string;
  runtimeDir: string;
}

function appendBounded(current: string, chunk: string, maxChars: number): string {
  if (current.length >= maxChars) return current;
  const remaining = maxChars - current.length;
  return current + chunk.slice(0, remaining);
}

function signalProcessGroup(child: ChildProcess, signal: NodeJS.Signals): void {
  try {
    if (process.platform !== "win32" && child.pid !== undefined) {
      process.kill(-child.pid, signal);
      return;
    }
  } catch {
    // The process may have exited between the status check and the signal.
  }
  try {
    child.kill(signal);
  } catch {
    // The process may have exited; the close event will finish the result.
  }
}

export function runCommand(executable: string, args: string[], options: RunCommandOptions): Promise<CommandResult> {
  const maxCapturedChars = options.maxCapturedChars ?? 64_000;
  const startedAt = Date.now();
  const startedIso = isoNow();

  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let aborted = false;
    let settled = false;
    let terminating = false;
    let timeout: NodeJS.Timeout | undefined;
    let killTimeout: NodeJS.Timeout | undefined;
    let forceFinishTimeout: NodeJS.Timeout | undefined;

    const child = spawn(executable, args, {
      cwd: options.cwd,
      env: options.env,
      shell: false,
      detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"],
    });

    const finish = (exitCode: number | null): void => {
      if (settled) return;
      settled = true;
      if (timeout) clearTimeout(timeout);
      if (killTimeout) clearTimeout(killTimeout);
      if (forceFinishTimeout) clearTimeout(forceFinishTimeout);
      options.signal?.removeEventListener("abort", onAbort);
      const endedAt = Date.now();
      resolve({
        executable,
        args,
        command: [executable, ...args],
        started_at: startedIso,
        ended_at: isoNow(),
        duration_ms: endedAt - startedAt,
        exit_code: exitCode,
        timed_out: timedOut,
        aborted,
        stdout: truncateText(stdout, maxCapturedChars),
        stderr: truncateText(stderr, maxCapturedChars),
      });
    };

    const terminate = (): void => {
      if (terminating) return;
      terminating = true;
      signalProcessGroup(child, "SIGTERM");
      killTimeout = setTimeout(() => {
        signalProcessGroup(child, "SIGKILL");
        forceFinishTimeout = setTimeout(() => finish(null), 1_000);
      }, 1_000);
    };

    const onAbort = (): void => {
      aborted = true;
      terminate();
    };

    if (options.signal?.aborted) {
      aborted = true;
      terminate();
    } else {
      options.signal?.addEventListener("abort", onAbort, { once: true });
    }

    child.stdout?.on("data", (chunk: Buffer | string) => {
      stdout = appendBounded(stdout, chunk.toString(), maxCapturedChars);
    });
    child.stderr?.on("data", (chunk: Buffer | string) => {
      stderr = appendBounded(stderr, chunk.toString(), maxCapturedChars);
    });
    child.on("error", (error) => {
      stderr = appendBounded(stderr, error.message, maxCapturedChars);
      finish(null);
    });
    child.on("close", (code) => finish(code));

    timeout = setTimeout(() => {
      timedOut = true;
      terminate();
    }, Math.max(1, options.timeoutMs));
  });
}

export function safeChildEnv(overrides: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const key of ["PATH", "PATHEXT", "SystemRoot", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TZ", "VIRTUAL_ENV"]) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  Object.assign(env, overrides);
  // Do not inherit ambient Python injection/import paths from the host.
  delete env.PYTHONHOME;
  delete env.PYTHONPATH;
  return env;
}

export async function commandVersion(command: string, cwd: string, timeoutMs = 10_000): Promise<CommandResult> {
  return runCommand(command, ["--version"], { cwd, timeoutMs, env: safeChildEnv() });
}

function commandOnPath(command: string): boolean {
  if (path.isAbsolute(command)) {
    try {
      accessSync(command, constants.X_OK);
      return true;
    } catch {
      return false;
    }
  }
  const pathValue = process.env.PATH ?? "";
  return pathValue.split(path.delimiter).some((directory) => {
    try {
      accessSync(path.join(directory || ".", command), constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}

export function detectRestrictedBackend(): SandboxBackend {
  const forced = process.env.DATAAGENT_SANDBOX_BACKEND?.trim().toLowerCase();
  if (forced === "macos" || forced === "macos-sandbox-exec") return "macos-sandbox-exec";
  if (forced === "docker") return commandOnPath("docker") ? "docker" : "unavailable";
  if (process.platform === "darwin" && existsSync("/usr/bin/sandbox-exec")) return "macos-sandbox-exec";
  if (process.env.DATAAGENT_SANDBOX_IMAGE?.trim() && commandOnPath("docker")) return "docker";
  return "unavailable";
}

interface ResolvedCommandPath {
  lexicalPath: string;
  canonicalPath: string;
}

async function resolveCommandPath(command: string, env: NodeJS.ProcessEnv): Promise<ResolvedCommandPath | undefined> {
  const candidates = path.isAbsolute(command)
    ? [command]
    : (env.PATH ?? process.env.PATH ?? "").split(path.delimiter).filter(Boolean).map((directory) => path.join(directory, command));
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return { lexicalPath: path.resolve(candidate), canonicalPath: await realpath(candidate) };
    } catch {
      // Try the next PATH entry.
    }
  }
  return undefined;
}

async function resolveShebang(commandPath: ResolvedCommandPath, env: NodeJS.ProcessEnv): Promise<ResolvedCommandPath | undefined> {
  try {
    const firstLine = (await readFile(commandPath.lexicalPath, "utf8")).slice(0, 256).split(/\r?\n/, 1)[0];
    if (!firstLine.startsWith("#!")) return undefined;
    const parts = firstLine.slice(2).trim().split(/\s+/);
    const interpreter = parts[0];
    if (!interpreter) return undefined;
    if (path.basename(interpreter) === "env" && parts[1]) return resolveCommandPath(parts[1], env);
    if (!path.isAbsolute(interpreter)) return resolveCommandPath(interpreter, env);
    const lexicalPath = path.resolve(interpreter);
    return { lexicalPath, canonicalPath: await realpath(lexicalPath) };
  } catch {
    return undefined;
  }
}

function addPath(set: Set<string>, value: string | undefined): void {
  if (value) set.add(value);
}

function addCommandDependencyRoots(readRoots: Set<string>, ...commands: Array<ResolvedCommandPath | undefined>): void {
  for (const command of commands) {
    if (!command) continue;
    for (const executable of [command.lexicalPath, command.canonicalPath]) {
      const parent = path.dirname(executable);
      if (path.basename(parent) !== "bin") continue;
      const prefix = path.dirname(parent);
      if (path.parse(prefix).root === prefix) continue;
      // A command under <prefix>/bin may need its restricted runtime prefix,
      // but never infer the filesystem root or a broader parent directory.
      addPath(readRoots, prefix);
    }
  }
}

function seatbeltQuote(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

async function existingCanonicalPath(value: string): Promise<string | undefined> {
  try {
    return await realpath(value);
  } catch {
    return undefined;
  }
}

async function macSandboxProfile(options: { root: string; runtimeDir: string; command: string; env: NodeJS.ProcessEnv; profilePath: string }): Promise<void> {
  const root = await realpath(options.root);
  const runtimeDir = await realpath(options.runtimeDir);
  const commandPath = await resolveCommandPath(options.command, options.env);
  const interpreterPath = commandPath ? await resolveShebang(commandPath, options.env) : undefined;
  const readRoots = new Set<string>([
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/Library",
    "/private/etc",
    "/private/var/db",
    "/dev",
    root,
    runtimeDir,
  ]);
  addCommandDependencyRoots(readRoots, commandPath, interpreterPath);
  const existingReadRoots: string[] = [];
  for (const candidate of readRoots) {
    const canonical = await existingCanonicalPath(candidate);
    if (canonical && !existingReadRoots.includes(canonical)) existingReadRoots.push(canonical);
  }
  const executableLiterals = [
    commandPath?.lexicalPath,
    commandPath?.canonicalPath,
    interpreterPath?.lexicalPath,
    interpreterPath?.canonicalPath,
  ].filter((value): value is string => Boolean(value));
  const readRules = existingReadRoots.map((value) => `  (subpath "${seatbeltQuote(value)}")`).join("\n");
  const executableRules = executableLiterals.map((value) => `  (literal "${seatbeltQuote(value)}")`).join("\n");
  const profile = [
    "(version 1)",
    "(deny default)",
    "(allow process-fork)",
    "(allow process-exec)",
    "(allow signal)",
    "(allow process-info-pidinfo)",
    "(allow sysctl-read)",
    "(allow mach-lookup)",
    "(allow ipc-posix-shm)",
    "(allow file-read-metadata)",
    "(allow file-read*",
    readRules,
    executableRules,
    "  (literal \"/dev/null\")",
    "  (literal \"/dev/urandom\")",
    "  (literal \"/dev/random\")",
    ")",
    `(allow file-write* (subpath "${seatbeltQuote(root)}"))`,
    `(allow file-write* (subpath "${seatbeltQuote(runtimeDir)}"))`,
    "(deny network*)",
    "",
  ].join("\n");
  await writeFile(options.profilePath, profile, "utf8");
}

function syntheticResult(executable: string, args: string[], stderr: string, backend: SandboxBackend, signal?: AbortSignal): CommandResult {
  const now = isoNow();
  return {
    executable,
    args,
    command: [executable, ...args],
    started_at: now,
    ended_at: now,
    duration_ms: 0,
    exit_code: null,
    timed_out: false,
    aborted: Boolean(signal?.aborted),
    stdout: "",
    stderr,
    sandbox_backend: backend,
  };
}

function mapContainerPath(value: string, root: string, runtimeDir: string): string {
  const resolved = path.resolve(value);
  if (resolved === root || resolved.startsWith(`${root}${path.sep}`)) return `/workspace/repo${resolved.slice(root.length).split(path.sep).join("/")}`;
  if (resolved === runtimeDir || resolved.startsWith(`${runtimeDir}${path.sep}`)) return `/workspace/runtime${resolved.slice(runtimeDir.length).split(path.sep).join("/")}`;
  return value;
}

async function runDockerRestricted(executable: string, args: string[], options: RestrictedCommandOptions): Promise<CommandResult> {
  const image = process.env.DATAAGENT_SANDBOX_IMAGE?.trim();
  if (!image) return syntheticResult(executable, args, "DATAAGENT_SANDBOX_IMAGE is required for the Docker restricted backend.", "unavailable", options.signal);
  const root = await realpath(options.root);
  const runtimeDir = await realpath(options.runtimeDir);
  const dockerArgs = [
    "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
    "--security-opt", "no-new-privileges", "--pids-limit", "256",
    "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
    "-v", `${root}:/workspace/repo:rw`,
    "-v", `${runtimeDir}:/workspace/runtime:rw`,
    "-w", "/workspace/repo",
    "-e", "TMPDIR=/tmp",
    "-e", "TMP=/tmp",
    "-e", "TEMP=/tmp",
    "-e", "HOME=/tmp/dataagent-home",
    "-e", "XDG_CACHE_HOME=/tmp/dataagent-cache",
  ];
  for (const key of ["DBT_PROFILES_DIR", "DBT_TARGET_PATH", "DBT_LOG_PATH", "DBT_USER_CONFIG_DIR", "DATAAGENT_TASK_ROOT", "DATAAGENT_RUNTIME_DIR", "DATAAGENT_DBT_COMMAND", "PYTHONUNBUFFERED"]) {
    if (options.env?.[key] !== undefined) dockerArgs.push("-e", `${key}=${mapContainerPath(options.env[key]!, root, runtimeDir)}`);
  }
  const containerExecutable = path.isAbsolute(executable) ? path.basename(executable) : executable;
  dockerArgs.push(image, containerExecutable, ...args.map((arg) => mapContainerPath(arg, root, runtimeDir)));
  const result = await runCommand("docker", dockerArgs, {
    cwd: options.cwd,
    env: safeChildEnv(),
    timeoutMs: options.timeoutMs,
    signal: options.signal,
    maxCapturedChars: options.maxCapturedChars,
  });
  return { ...result, executable, args, command: [executable, ...args], sandbox_backend: "docker" };
}

async function runMacRestricted(executable: string, args: string[], options: RestrictedCommandOptions): Promise<CommandResult> {
  const runtimeDir = await realpath(options.runtimeDir);
  const profilePath = path.join(runtimeDir, "sandbox.sb");
  await macSandboxProfile({ root: options.root, runtimeDir, command: executable, env: options.env ?? safeChildEnv(), profilePath });
  const result = await runCommand("/usr/bin/sandbox-exec", ["-f", profilePath, executable, ...args], {
    cwd: options.cwd,
    env: options.env,
    timeoutMs: options.timeoutMs,
    signal: options.signal,
    maxCapturedChars: options.maxCapturedChars,
  });
  return { ...result, executable, args, command: [executable, ...args], sandbox_backend: "macos-sandbox-exec" };
}

export async function runRestrictedCommand(executable: string, args: string[], options: RestrictedCommandOptions): Promise<CommandResult> {
  const restrictedOptions: RestrictedCommandOptions = { ...options, env: safeChildEnv(options.env ?? {}) };
  const backend = detectRestrictedBackend();
  if (backend === "unavailable") {
    return syntheticResult(executable, args, "No restricted subprocess backend is available; refusing to execute the task command on the host.", backend, restrictedOptions.signal);
  }
  try {
    await ensureDir(restrictedOptions.runtimeDir);
    if (backend === "docker") return await runDockerRestricted(executable, args, restrictedOptions);
    return await runMacRestricted(executable, args, restrictedOptions);
  } catch (error) {
    return syntheticResult(executable, args, `Restricted subprocess setup failed: ${error instanceof Error ? error.message : String(error)}`, backend, restrictedOptions.signal);
  }
}
