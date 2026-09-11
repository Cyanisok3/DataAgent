import { copyFile, readFile } from "node:fs/promises";
import path from "node:path";
import { ensureDir, copyTree, fileExists, isoNow, walkFiles, writeJson } from "./files.js";
import { runCommand, safeChildEnv } from "./process.js";

export interface RunWorkspace {
  runRoot: string;
  repo: string;
  baselineRepo: string;
  agentConfigDir: string;
  evidenceDir: string;
  dbtEvidenceDir: string;
  eventsPath: string;
  finalResponsePath: string;
  diffPath: string;
  receiptPath: string;
  databaseFiles: string[];
}

export async function prepareRunWorkspace(options: { runRoot: string; sourceProject: string }): Promise<RunWorkspace> {
  const runRoot = path.resolve(options.runRoot);
  const repo = path.join(runRoot, "workspace", "repo");
  const baselineRepo = path.join(runRoot, "evidence", "baseline-repo");
  const agentConfigDir = path.join(runRoot, "workspace", "agent-config");
  const evidenceDir = path.join(runRoot, "evidence");
  const dbtEvidenceDir = path.join(evidenceDir, "dbt");
  const eventsPath = path.join(evidenceDir, "events.jsonl");
  const finalResponsePath = path.join(evidenceDir, "final-response.txt");
  const diffPath = path.join(evidenceDir, "diff.patch");
  const receiptPath = path.join(runRoot, "receipt.json");
  if (await fileExists(runRoot)) throw new Error(`Run directory already exists; use a new experiment root: ${runRoot}`);
  await ensureDir(runRoot);
  await Promise.all([
    copyTree(options.sourceProject, repo, { skip: skipInputPath }),
    copyTree(options.sourceProject, baselineRepo, { skip: skipInputPath }),
    ensureDir(agentConfigDir),
    ensureDir(dbtEvidenceDir),
  ]);
  const sourceFiles = await walkFiles(options.sourceProject, { skip: skipInputPath });
  const databaseFiles = sourceFiles.filter((entry) => /\.(duckdb|db)$/i.test(entry.relativePath)).map((entry) => entry.relativePath).sort();
  for (const relativePath of databaseFiles) {
    const targetPath = path.join(repo, relativePath);
    await ensureDir(path.dirname(targetPath));
    await copyFile(path.join(options.sourceProject, relativePath), targetPath);
  }
  await writeJson(path.join(evidenceDir, "workspace.json"), {
    created_at: isoNow(),
    source_project: path.resolve(options.sourceProject),
    repo,
    baseline_repo: baselineRepo,
    database_files: databaseFiles,
    gold_visible_to_agent: false,
    evaluator_visible_to_agent: false,
  });
  return { runRoot, repo, baselineRepo, agentConfigDir, evidenceDir, dbtEvidenceDir, eventsPath, finalResponsePath, diffPath, receiptPath, databaseFiles };
}

function skipInputPath(relativePath: string): boolean {
  return relativePath === ".git" || relativePath.startsWith(".git/") || relativePath === "node_modules" || relativePath.startsWith("node_modules/") || relativePath === "target" || relativePath.startsWith("target/");
}

export async function collectChangedFiles(workspace: RunWorkspace): Promise<string[]> {
  const before = await walkFiles(workspace.baselineRepo, { skip: skipInputPath });
  const after = await walkFiles(workspace.repo, { skip: skipInputPath });
  const beforeMap = new Map(before.filter((entry) => entry.kind === "file").map((entry) => [entry.relativePath, entry]));
  const afterMap = new Map(after.filter((entry) => entry.kind === "file").map((entry) => [entry.relativePath, entry]));
  const paths = new Set([...beforeMap.keys(), ...afterMap.keys()]);
  const changed: string[] = [];
  for (const relativePath of [...paths].sort()) {
    const oldEntry = beforeMap.get(relativePath);
    const newEntry = afterMap.get(relativePath);
    if (!oldEntry || !newEntry || oldEntry.size !== newEntry.size) {
      changed.push(relativePath);
      continue;
    }
    const [oldContent, newContent] = await Promise.all([readFile(oldEntry.absolutePath), readFile(newEntry.absolutePath)]);
    if (!oldContent.equals(newContent)) changed.push(relativePath);
  }
  return changed;
}

export async function writeDiff(workspace: RunWorkspace): Promise<{ changedFiles: string[]; diff: string }> {
  const changedFiles = await collectChangedFiles(workspace);
  const result = await runCommand("diff", ["-ruN", "--exclude=.git", "--exclude=node_modules", "--exclude=target", workspace.baselineRepo, workspace.repo], {
    cwd: workspace.runRoot,
    timeoutMs: 30_000,
    env: safeChildEnv(),
    maxCapturedChars: 1_000_000,
  });
  const diff = result.stdout || (changedFiles.length > 0 ? `Changed files (text diff unavailable):\n${changedFiles.join("\n")}\n` : "");
  return { changedFiles, diff };
}

