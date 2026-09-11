import { copyFile, readFile } from "node:fs/promises";
import path from "node:path";
import { SELECTION_SEED } from "./config.js";
import { ensureDir, fileExists, isWithin, readJsonl, toPosix, walkFiles, writeJson, writeJsonl } from "./files.js";
import type { OfficialTask, SelectionManifest, SelectionRow, StaticAnalysis, Tier } from "./types.js";

const OFFICIAL_TASK_URL = "https://github.com/xlang-ai/Spider2/blob/main/spider2-dbt/examples/spider2-dbt.jsonl";
const RETRIEVED_ON = "2026-09-11";
const GOLD_KEYS = new Set(["gold", "evaluation", "answer", "reference", "reference_answer", "gold_sql"]);
const DATABASE_EXTENSIONS = new Set([".duckdb", ".db"]);
const EXTERNAL_ADAPTERS = ["bigquery", "snowflake", "redshift", "postgres", "spark", "databricks", "trino", "presto"];
const FORBIDDEN_INPUT_DIRECTORIES = new Set(["gold", "reference", "references", "evaluation", "evaluations", "result", "results", "submission", "submissions", "output", "outputs"]);
const NON_TASK_DIRECTORIES = new Set(["dbt_packages", "vendor", "packages", "tests", "test"]);
const PROJECT_CONFIG_NAMES = new Set(["dbt_project.yml", "profiles.yml", "profiles.yaml", "packages.yml", "requirements.txt", "pyproject.toml"]);

interface ScopeConfirmation {
  relatedModels: string[];
  scopeObservations: string;
  dependencyObservations: string;
  constraintObservations: string;
  scopeScore: 0 | 1 | 2;
  dependencyScore: 0 | 1 | 2;
  constraintScore: 0 | 1 | 2;
  estimatedRelatedFiles: number;
  visibleLongestDependencyChainEdges: number | "unknown";
  hasUnknownDependencies: boolean;
  hasBranch: boolean;
}

const SCOPE_CONFIRMATION_KEYS = new Set([
  "instance_id",
  "related_models",
  "scope_observations",
  "dependency_observations",
  "constraint_observations",
]);

function observationValue(value: string, pattern: RegExp, field: string): string {
  const match = value.match(pattern);
  if (!match) throw new Error(`${field} is missing a required observation`);
  return match[1];
}

function observationScore(value: string, field: string): 0 | 1 | 2 {
  return Number(observationValue(value, /(?:^|[;\s])score=(0|1|2)(?:;|$)/, field)) as 0 | 1 | 2;
}

function observationNumber(value: string, field: string): number {
  return Number(observationValue(value, /(?:^|[;\s])estimated_related_files=(\d+)(?:;|$)/, field));
}

function observationChain(value: string, field: string): number | "unknown" {
  const raw = observationValue(value, /(?:^|[;\s])visible_longest_chain_edges=(unknown|\d+)(?:;|$)/, field);
  return raw === "unknown" ? raw : Number(raw);
}

function observationBoolean(value: string, field: string, key: string): boolean {
  return observationValue(value, new RegExp(`(?:^|[;\\s])${key}=(true|false)(?:;|$)`), field) === "true";
}

function normalizeConfirmedModelPath(value: string, field: string): string {
  const normalized = toPosix(value.trim());
  if (!normalized || path.posix.isAbsolute(normalized) || normalized.split("/").includes("..")) {
    throw new Error(`${field} must contain relative model paths`);
  }
  return normalized;
}

async function loadScopeConfirmations(
  scopeConfirmationPath: string,
  taskIds: Set<string>,
): Promise<Map<string, ScopeConfirmation>> {
  const rows = await readJsonl<Record<string, unknown>>(scopeConfirmationPath);
  if (rows.length === 0) throw new Error("scope confirmation file must contain at least one JSONL row");

  const confirmations = new Map<string, ScopeConfirmation>();
  for (const [index, row] of rows.entries()) {
    const rowLabel = `scope confirmation row ${index + 1}`;
    const unknownKeys = Object.keys(row).filter((key) => !SCOPE_CONFIRMATION_KEYS.has(key));
    if (unknownKeys.length > 0) throw new Error(`${rowLabel} contains unsupported fields: ${unknownKeys.join(", ")}`);

    const instanceId = typeof row.instance_id === "string" ? row.instance_id.trim() : "";
    if (!instanceId || !taskIds.has(instanceId)) throw new Error(`${rowLabel} references an unknown instance_id`);
    if (confirmations.has(instanceId)) throw new Error(`duplicate scope confirmation for ${instanceId}`);

    if (!Array.isArray(row.related_models) || row.related_models.length === 0 ||
      row.related_models.some((model) => typeof model !== "string")) {
      throw new Error(`${rowLabel}.related_models must be a non-empty string array`);
    }
    const relatedModels = row.related_models.map((model, modelIndex) =>
      normalizeConfirmedModelPath(model as string, `${rowLabel}.related_models[${modelIndex}]`));
    if (new Set(relatedModels).size !== relatedModels.length) throw new Error(`${rowLabel}.related_models must not contain duplicates`);

    const observationFields = [
      "scope_observations",
      "dependency_observations",
      "constraint_observations",
    ] as const;
    const observations = Object.fromEntries(observationFields.map((field) => {
      const value = typeof row[field] === "string" ? row[field].trim() : "";
      if (!value || value.length > 4_000) {
        throw new Error(`${rowLabel}.${field} must be a non-empty string of at most 4000 characters`);
      }
      return [field, value];
    })) as Record<(typeof observationFields)[number], string>;

    const estimatedRelatedFiles = observationNumber(observations.scope_observations, `${rowLabel}.scope_observations`);
    if (estimatedRelatedFiles < relatedModels.length) throw new Error(`${rowLabel}.scope_observations undercounts related_models`);
    confirmations.set(instanceId, {
      relatedModels,
      scopeObservations: observations.scope_observations,
      dependencyObservations: observations.dependency_observations,
      constraintObservations: observations.constraint_observations,
      scopeScore: observationScore(observations.scope_observations, `${rowLabel}.scope_observations`),
      dependencyScore: observationScore(observations.dependency_observations, `${rowLabel}.dependency_observations`),
      constraintScore: observationScore(observations.constraint_observations, `${rowLabel}.constraint_observations`),
      estimatedRelatedFiles,
      visibleLongestDependencyChainEdges: observationChain(observations.dependency_observations, `${rowLabel}.dependency_observations`),
      hasUnknownDependencies: observationBoolean(observations.dependency_observations, `${rowLabel}.dependency_observations`, "unknown"),
      hasBranch: observationBoolean(observations.dependency_observations, `${rowLabel}.dependency_observations`, "branching"),
    });
  }
  return confirmations;
}

function skipRepositoryPath(relativePath: string): boolean {
  const segments = toPosix(relativePath).toLowerCase().split("/");
  return segments.includes(".git") || segments.includes("node_modules") || segments.includes("target") ||
    segments.some((segment) => FORBIDDEN_INPUT_DIRECTORIES.has(segment) || NON_TASK_DIRECTORIES.has(segment));
}

class PythonRandom {
  private readonly state: number[];
  private index = 624;

  constructor(seed: number) {
    this.state = new Array<number>(624);
    this.state[0] = 19650218;
    for (let i = 1; i < 624; i += 1) {
      this.state[i] = (Math.imul(1812433253, (this.state[i - 1] ^ (this.state[i - 1] >>> 30)) >>> 0) + i) >>> 0;
    }
    const key = [seed >>> 0];
    let i = 1;
    let j = 0;
    for (let count = Math.max(624, key.length); count > 0; count -= 1) {
      this.state[i] = (this.state[i] ^ Math.imul(this.state[i - 1] ^ (this.state[i - 1] >>> 30), 1664525)) + key[j] + j;
      this.state[i] >>>= 0;
      i += 1;
      j += 1;
      if (i >= 624) {
        this.state[0] = this.state[623];
        i = 1;
      }
      if (j >= key.length) j = 0;
    }
    for (let count = 623; count > 0; count -= 1) {
      this.state[i] = (this.state[i] ^ Math.imul(this.state[i - 1] ^ (this.state[i - 1] >>> 30), 1566083941)) - i;
      this.state[i] >>>= 0;
      i += 1;
      if (i >= 624) {
        this.state[0] = this.state[623];
        i = 1;
      }
    }
    this.state[0] = 0x80000000;
  }

  private twist(): void {
    for (let i = 0; i < 624; i += 1) {
      const y = (this.state[i] & 0x80000000) | (this.state[(i + 1) % 624] & 0x7fffffff);
      this.state[i] = this.state[(i + 397) % 624] ^ (y >>> 1);
      if (y & 1) this.state[i] ^= 0x9908b0df;
    }
    this.index = 0;
  }

  private uint32(): number {
    if (this.index >= 624) this.twist();
    let value = this.state[this.index++];
    value ^= value >>> 11;
    value ^= (value << 7) & 0x9d2c5680;
    value ^= (value << 15) & 0xefc60000;
    value ^= value >>> 18;
    return value >>> 0;
  }

  private getRandBits(bitCount: number): number {
    if (bitCount === 0) return 0;
    return this.uint32() >>> (32 - bitCount);
  }

  private randBelow(n: number): number {
    if (n <= 0) throw new Error("randBelow requires a positive bound");
    const bitCount = 32 - Math.clz32(n);
    while (true) {
      const value = this.getRandBits(bitCount);
      if (value < n) return value;
    }
  }

  sample<T>(population: T[], count: number): T[] {
    if (count < 0 || count > population.length) throw new Error("sample count is outside the population");
    const pool = [...population];
    const result: T[] = [];
    for (let i = 0; i < count; i += 1) {
      const index = this.randBelow(population.length - i);
      result.push(pool[index]);
      pool[index] = pool[population.length - i - 1];
    }
    return result;
  }
}

export function pythonSeedSample<T>(population: T[], count: number, seed = SELECTION_SEED): T[] {
  return new PythonRandom(seed).sample(population, count);
}

function hasGoldLikeKeys(task: OfficialTask): string | undefined {
  for (const key of Object.keys(task)) {
    if (GOLD_KEYS.has(key.toLowerCase())) return key;
  }
  return undefined;
}

async function locateProjectDirectory(examplesRoot: string, instanceId: string): Promise<string | undefined> {
  const candidates = [
    path.join(examplesRoot, instanceId),
    path.join(examplesRoot, "examples", instanceId),
    path.join(examplesRoot, "projects", instanceId),
  ];
  for (const candidate of candidates) {
    if (!isWithin(candidate, examplesRoot)) continue;
    if (!(await fileExists(candidate))) continue;
    const entries = await walkFiles(candidate, { includeDirectories: true, skip: skipRepositoryPath });
    if (entries.some((entry) => entry.kind === "file" && path.basename(entry.relativePath).toLowerCase() === "dbt_project.yml")) {
      return candidate;
    }
  }
  return undefined;
}

function extensionOf(relativePath: string): string {
  return path.extname(relativePath).toLowerCase();
}

function scoreScope(relatedFileCount: number, modelCount: number, hasMacrosOrConfig: boolean): 0 | 1 | 2 {
  if (relatedFileCount > 5 || modelCount > 3 || hasMacrosOrConfig) return 2;
  if (relatedFileCount > 2 || modelCount > 1) return 1;
  return 0;
}

function scoreConstraints(instruction: string, sqlText: string, modelCount: number): 0 | 1 | 2 {
  const text = `${instruction}\n${sqlText}`.toLowerCase();
  const indicators = {
    window: /\b(over|row_number|rank|dense_rank|lag|lead|rolling|partition by)\b/.test(text),
    temporal: /\b(month|daily|weekly|date|timestamp|previous|prior|year-over-year|month-over-month|running)\b/.test(text),
    joins: /\b(join|union|merge)\b/.test(text),
    conditional: /\b(case\s+when|coalesce|nullif|status|categorize)\b/.test(text),
    multipleOutputs: modelCount > 1 || /\b(two|three|multiple|reports|tables|views)\b/.test(instruction.toLowerCase()),
  };
  const count = Object.values(indicators).filter(Boolean).length;
  if (count >= 4 || (indicators.window && indicators.multipleOutputs) || (indicators.temporal && indicators.joins && indicators.conditional)) return 2;
  if (count >= 1) return 1;
  return 0;
}

interface ModelGraph {
  dependencies: Map<string, string[]>;
  hasUnknown: boolean;
}

function modelGraph(modelFiles: string[], sqlByFile: Map<string, string>): ModelGraph {
  const filesByName = new Map<string, string[]>();
  for (const file of modelFiles) {
    const name = path.basename(file, ".sql");
    filesByName.set(name, [...(filesByName.get(name) ?? []), file]);
  }
  const dependencies = new Map<string, string[]>();
  let hasUnknown = false;
  for (const file of modelFiles) {
    const text = sqlByFile.get(file) ?? "";
    const refs: string[] = [];
    for (const match of text.matchAll(/\bref\s*\(([\s\S]*?)\)/gi)) {
      const argument = match[1].trim();
      const quoted = argument.match(/^["']([^"']+)["']$/);
      if (!quoted) {
        hasUnknown = true;
        continue;
      }
      refs.push(quoted[1]);
    }
    const distinctRefs = [...new Set(refs)];
    const resolved: string[] = [];
    for (const ref of distinctRefs) {
      const candidates = filesByName.get(ref) ?? [];
      if (candidates.length !== 1) {
        hasUnknown = true;
        continue;
      }
      resolved.push(candidates[0]);
    }
    dependencies.set(file, resolved);
  }
  return { dependencies, hasUnknown };
}

function graphStats(modelFiles: string[], sqlByFile: Map<string, string>): {
  longest: number | "unknown";
  hasUnknown: boolean;
  hasBranch: boolean;
} {
  const graph = modelGraph(modelFiles, sqlByFile);
  const dependencies = graph.dependencies;
  const reverseCounts = new Map<string, number>();
  for (const refs of dependencies.values()) {
    for (const ref of refs) reverseCounts.set(ref, (reverseCounts.get(ref) ?? 0) + 1);
  }
  const hasBranch = [...dependencies.values()].some((refs) => refs.length > 1) || [...reverseCounts.values()].some((count) => count > 1);
  const visiting = new Set<string>();
  const memo = new Map<string, number>();
  let cycle = false;
  const depth = (file: string): number => {
    if (visiting.has(file)) {
      cycle = true;
      return 0;
    }
    const saved = memo.get(file);
    if (saved !== undefined) return saved;
    visiting.add(file);
    const result = Math.max(0, ...(dependencies.get(file) ?? []).map((dependency) => 1 + depth(dependency)));
    visiting.delete(file);
    memo.set(file, result);
    return result;
  };
  const longest = Math.max(0, ...modelFiles.map(depth));
  return { longest: graph.hasUnknown || cycle ? "unknown" : longest, hasUnknown: graph.hasUnknown || cycle, hasBranch };
}

function scoreDependency(longest: number | "unknown", hasBranch: boolean): 0 | 1 | 2 {
  if (longest === "unknown") return 2;
  if (longest > 3 || hasBranch) return 2;
  if (longest >= 2) return 1;
  return 0;
}

function findExternalAdapter(text: string): string | undefined {
  for (const adapter of EXTERNAL_ADAPTERS) {
    const adapterPattern = new RegExp(`(?:adapter|type|target)\\s*:\\s*["']?${adapter}\\b`, "i");
    const packagePattern = new RegExp(`dbt[-_](?:adapter[-_])?${adapter}\\b`, "i");
    if (adapterPattern.test(text) || packagePattern.test(text)) return adapter;
  }
  return undefined;
}

function buildReason(analysis: {
  scopeScore: number;
  dependencyScore: number;
  constraintScore: number;
  estimatedRelatedFiles: number;
  modelCount: number;
  longest: number | "unknown";
  hasBranch: boolean;
}): string {
  return `relative structure score ${analysis.scopeScore + analysis.dependencyScore + analysis.constraintScore}/6: ${analysis.estimatedRelatedFiles} related files, ${analysis.modelCount} models, visible dependency chain ${analysis.longest} edges${analysis.hasBranch ? ", with branching" : ""}.`;
}

function isLocalModelPath(relativePath: string): boolean {
  const segments = toPosix(relativePath).toLowerCase().split("/");
  return extensionOf(relativePath) === ".sql" && segments.includes("models") &&
    !segments.some((segment) => NON_TASK_DIRECTORIES.has(segment));
}

function modelFilesMentionedInInstruction(modelFiles: string[], instruction: string): string[] {
  return modelFiles.filter((file) => {
    const name = path.basename(file, ".sql");
    return new RegExp(`(?:^|[^A-Za-z0-9_])${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:$|[^A-Za-z0-9_])`, "i").test(instruction);
  });
}

function dependencyClosure(seedFiles: string[], graph: ModelGraph): string[] {
  const included = new Set<string>();
  const pending = [...seedFiles];
  while (pending.length > 0) {
    const file = pending.pop();
    if (!file || included.has(file)) continue;
    included.add(file);
    for (const dependency of graph.dependencies.get(file) ?? []) pending.push(dependency);
  }
  return [...included].sort();
}

function pathLooksLikeModelSchema(relativePath: string, modelDirectories: Set<string>): boolean {
  const normalized = toPosix(relativePath);
  return (extensionOf(normalized) === ".yml" || extensionOf(normalized) === ".yaml") && modelDirectories.has(path.posix.dirname(normalized));
}

async function readEntryText(entry: { absolutePath: string }): Promise<string> {
  try {
    return await readFile(entry.absolutePath, "utf8");
  } catch {
    return "";
  }
}

async function analyzeTask(task: OfficialTask, examplesRoot: string, confirmation?: ScopeConfirmation): Promise<StaticAnalysis> {
  const projectDirectory = await locateProjectDirectory(examplesRoot, task.instance_id);
  if (!projectDirectory) {
    const row: SelectionRow = {
      instance_id: task.instance_id,
      project: task.instance_id,
      scope_observations: "score=unknown; project directory or dbt_project.yml not found",
      dependency_observations: "score=unknown; not inspected",
      constraint_observations: "score=unknown; not inspected",
      tier: "excluded",
      selection_reason: "excluded: project directory or dbt_project.yml was not found under the supplied examples root",
    };
    return {
      row,
      eligible: false,
      exclusionReason: "project directory or dbt_project.yml not found",
      scopeScore: 2,
      dependencyScore: 2,
      constraintScore: 2,
      totalScore: 6,
      estimatedRelatedFiles: 0,
      modelCount: 0,
      visibleLongestDependencyChainEdges: "unknown",
      hasUnknownDependencies: true,
      databaseFiles: [],
      modelFiles: [],
    };
  }

  const entries = await walkFiles(projectDirectory, {
    includeDirectories: false,
    skip: skipRepositoryPath,
  });
  const entryByPath = new Map(entries.map((entry) => [toPosix(entry.relativePath), entry]));
  const modelEntries = entries.filter((entry) => isLocalModelPath(entry.relativePath));
  const allModelFiles = modelEntries.map((entry) => toPosix(entry.relativePath)).sort();
  const databaseFiles = entries.filter((entry) => DATABASE_EXTENSIONS.has(extensionOf(entry.relativePath))).map((entry) => entry.relativePath).sort();
  const mentionedModelFiles = modelFilesMentionedInInstruction(allModelFiles, task.instruction);
  if (confirmation) {
    const missingModels = confirmation.relatedModels.filter((file) => !entryByPath.has(file) || !isLocalModelPath(file));
    if (missingModels.length > 0) {
      throw new Error(`Manual scope for ${task.instance_id} references missing local model(s): ${missingModels.join(", ")}`);
    }
  }
  if (!confirmation && mentionedModelFiles.length === 0 && allModelFiles.length > 0) {
    const projectConfigEntries = entries.filter((entry) => PROJECT_CONFIG_NAMES.has(path.basename(toPosix(entry.relativePath))));
    const estimatedRelatedFiles = databaseFiles.length + projectConfigEntries.length;
    const constraintScore = scoreConstraints(task.instruction, "", 0);
    const relativeProject = toPosix(path.relative(examplesRoot, projectDirectory)) || task.instance_id;
    const candidateModels = allModelFiles.join(",") || "none";
    const row: SelectionRow = {
      instance_id: task.instance_id,
      project: relativeProject,
      scope_observations: `score=unknown; estimated_related_files=${estimatedRelatedFiles}; candidate_models=${candidateModels}; manual_scope_confirmation_required=true`,
      dependency_observations: "score=unknown; visible_longest_chain_edges=unknown; branching=unknown; unknown=true; manual_scope_confirmation_required=true",
      constraint_observations: `score=${constraintScore}; indicators=${constraintIndicators(task.instruction, "").join(",") || "none"}`,
      tier: "excluded",
      selection_reason: "excluded: model scope is ambiguous from the task description; manual candidate-pool confirmation is required",
    };
    return {
      row,
      eligible: false,
      exclusionReason: "model scope is ambiguous from the task description; manual candidate-pool confirmation is required",
      scopeScore: 2,
      dependencyScore: 2,
      constraintScore,
      totalScore: 4 + constraintScore,
      estimatedRelatedFiles,
      modelCount: 0,
      visibleLongestDependencyChainEdges: "unknown",
      hasUnknownDependencies: true,
      databaseFiles,
      modelFiles: [],
    };
  }
  const allSqlByFile = new Map<string, string>();
  for (const entry of modelEntries) {
    const relativePath = toPosix(entry.relativePath);
    if (!confirmation || confirmation.relatedModels.includes(relativePath)) allSqlByFile.set(relativePath, await readEntryText(entry));
  }
  const allGraph = confirmation ? undefined : modelGraph(allModelFiles, allSqlByFile);
  const seedFiles = mentionedModelFiles.length > 0 ? mentionedModelFiles : allModelFiles;
  const modelFiles = confirmation ? [...confirmation.relatedModels].sort() : dependencyClosure(seedFiles, allGraph!);
  const sqlByFile = new Map(modelFiles.map((file) => [file, allSqlByFile.get(file) ?? ""]));
  const stats = confirmation ? {
    longest: confirmation.visibleLongestDependencyChainEdges,
    hasBranch: confirmation.hasBranch,
    hasUnknown: confirmation.hasUnknownDependencies,
  } : graphStats(modelFiles, sqlByFile);
  const modelDirectories = new Set(modelFiles.map((file) => path.posix.dirname(file)));
  const relevantConfigEntries = entries.filter((entry) => {
    const normalized = toPosix(entry.relativePath);
    return PROJECT_CONFIG_NAMES.has(path.basename(normalized)) || pathLooksLikeModelSchema(normalized, modelDirectories);
  });
  const relevantSqlText = modelFiles.map((file) => sqlByFile.get(file) ?? "").join("\n");
  const macroEntries = entries.filter((entry) => {
    const segments = toPosix(entry.relativePath).toLowerCase().split("/");
    return extensionOf(entry.relativePath) === ".sql" && segments.includes("macros") && !segments.some((segment) => NON_TASK_DIRECTORIES.has(segment));
  });
  const macroNames = [...relevantSqlText.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(/g)].map((match) => match[1]);
  const macroTextByFile = new Map<string, string>();
  for (const entry of macroEntries) macroTextByFile.set(toPosix(entry.relativePath), await readEntryText(entry));
  const relevantMacroEntries = macroNames.length === 0 ? [] : macroEntries.filter((entry) => {
    const text = macroTextByFile.get(toPosix(entry.relativePath)) ?? "";
    return macroNames.some((name) => new RegExp(`\\{%-?\\s*macro\\s+${name}\\b`, "i").test(text));
  });
  const relatedPaths = new Set<string>([
    ...modelFiles,
    ...databaseFiles,
    ...relevantConfigEntries.map((entry) => toPosix(entry.relativePath)),
    ...relevantMacroEntries.map((entry) => toPosix(entry.relativePath)),
  ]);
  const relatedEntries = [...relatedPaths].map((relativePath) => entryByPath.get(relativePath)).filter((entry): entry is typeof entries[number] => Boolean(entry));
  const hasMacrosOrConfig = relevantMacroEntries.length > 0 || relevantConfigEntries.some((entry) => path.basename(entry.relativePath) === "packages.yml");
  const textByFile = new Map<string, string>();
  for (const entry of [...relevantConfigEntries, ...relevantMacroEntries]) textByFile.set(toPosix(entry.relativePath), await readEntryText(entry));
  const projectText = [...textByFile.values(), ...modelFiles.map((file) => sqlByFile.get(file) ?? "")].join("\n");
  const externalAdapter = findExternalAdapter(["dbt_project.yml", "profiles.yml", "profiles.yaml", "packages.yml", "requirements.txt", "pyproject.toml"].map((name) => {
    const entry = entries.find((candidate) => path.basename(candidate.relativePath) === name);
    return entry ? (textByFile.get(toPosix(entry.relativePath)) ?? "") : "";
  }).join("\n"));
  const scopeScore = confirmation?.scopeScore ?? scoreScope(relatedEntries.length, modelFiles.length, hasMacrosOrConfig);
  const dependencyScore = confirmation?.dependencyScore ?? scoreDependency(stats.longest, stats.hasBranch);
  const constraintScore = confirmation?.constraintScore ?? scoreConstraints(task.instruction, projectText, modelFiles.length);
  const totalScore = scopeScore + dependencyScore + constraintScore;
  const relativeProject = toPosix(path.relative(examplesRoot, projectDirectory)) || task.instance_id;
  const estimatedRelatedFiles = confirmation?.estimatedRelatedFiles ?? relatedEntries.length;
  const common = { scopeScore, dependencyScore, constraintScore, estimatedRelatedFiles, modelCount: modelFiles.length, longest: stats.longest, hasBranch: stats.hasBranch };
  let eligible = true;
  let exclusionReason: string | undefined;
  if (task.type.toUpperCase() !== "DBT") {
    eligible = false;
    exclusionReason = `task type is ${task.type}, not DBT`;
  } else if (!entries.some((entry) => path.basename(entry.relativePath).toLowerCase() === "dbt_project.yml")) {
    eligible = false;
    exclusionReason = "dbt_project.yml not found";
  } else if (databaseFiles.length === 0) {
    eligible = false;
    exclusionReason = "no local DuckDB/database file is visible";
  } else if (modelFiles.length === 0) {
    eligible = false;
    exclusionReason = "no local dbt model under a task models directory was found";
  } else if (!entries.some((entry) => ["profiles.yml", "profiles.yaml", ".dbt/profiles.yml", ".dbt/profiles.yaml"].includes(toPosix(entry.relativePath).toLowerCase()))) {
    eligible = false;
    exclusionReason = "no visible local dbt profile was found; host profiles are not allowed by the isolation contract";
  } else if (externalAdapter) {
    eligible = false;
    exclusionReason = `requires non-local adapter: ${externalAdapter}`;
  } else if (stats.hasUnknown) {
    eligible = false;
    exclusionReason = "visible dependency graph is unknown because a ref target is missing or cyclic";
  }
  const scopeObservations = confirmation?.scopeObservations ??
    `score=${scopeScore}; estimated_related_files=${estimatedRelatedFiles}; models=${modelFiles.length}; macros_or_project_config=${hasMacrosOrConfig}`;
  const dependencyObservations = confirmation?.dependencyObservations ??
    `score=${dependencyScore}; visible_longest_chain_edges=${stats.longest}; branching=${stats.hasBranch}; unknown=${stats.hasUnknown}`;
  const constraintObservations = confirmation?.constraintObservations ??
    `score=${constraintScore}; indicators=${constraintIndicators(task.instruction, projectText).join(",") || "none"}`;
  const row: SelectionRow = {
    instance_id: task.instance_id,
    project: relativeProject,
    scope_observations: scopeObservations,
    dependency_observations: dependencyObservations,
    constraint_observations: constraintObservations,
    tier: eligible ? "unassigned" : "excluded",
    selection_reason: eligible ? buildReason({ ...common }) : `excluded: ${exclusionReason}`,
  };
  return {
    row,
    eligible,
    exclusionReason,
    scopeScore,
    dependencyScore,
    constraintScore,
    totalScore,
    estimatedRelatedFiles,
    modelCount: modelFiles.length,
    visibleLongestDependencyChainEdges: stats.longest,
    hasUnknownDependencies: stats.hasUnknown,
    databaseFiles,
    modelFiles,
  };
}

function constraintIndicators(instruction: string, projectText: string): string[] {
  const text = `${instruction}\n${projectText}`.toLowerCase();
  const checks: Array<[string, RegExp]> = [
    ["window", /\b(over|row_number|rank|dense_rank|lag|lead|rolling|partition by)\b/],
    ["temporal", /\b(month|daily|weekly|date|timestamp|previous|prior|year-over-year|month-over-month|running)\b/],
    ["joins", /\b(join|union|merge)\b/],
    ["conditional", /\b(case\s+when|coalesce|nullif|status|categorize)\b/],
  ];
  return checks.filter(([, pattern]) => pattern.test(text)).map(([name]) => name);
}

function splitIntoThree<T>(items: T[]): [T[], T[], T[]] {
  const base = Math.floor(items.length / 3);
  const remainder = items.length % 3;
  const firstLength = base + (remainder > 0 ? 1 : 0);
  const secondLength = base + (remainder > 1 ? 1 : 0);
  return [items.slice(0, firstLength), items.slice(firstLength, firstLength + secondLength), items.slice(firstLength + secondLength)];
}

export async function prepareSelection(options: {
  taskFile: string;
  examplesRoot: string;
  outputDir: string;
  poolSize?: number;
  scopeConfirmationPath?: string;
}): Promise<SelectionManifest> {
  const taskFile = path.resolve(options.taskFile);
  const examplesRoot = path.resolve(options.examplesRoot);
  const outputDir = path.resolve(options.outputDir);
  const poolSize = options.poolSize ?? 18;
  if (poolSize <= 0 || poolSize > 18) throw new Error("The selection pool size must be between 1 and 18 for this protocol.");
  await ensureDir(outputDir);
  const selectionPath = path.join(outputDir, "selection.json");
  if (await fileExists(selectionPath)) throw new Error(`Selection output already exists: ${selectionPath}`);
  const tasks = (await readJsonl<OfficialTask>(taskFile)).sort((left, right) => left.instance_id.localeCompare(right.instance_id));
  if (tasks.length === 0) throw new Error("The official task JSONL is empty.");
  for (const task of tasks) {
    if (tasks.filter((candidate) => candidate.instance_id === task.instance_id).length > 1) throw new Error(`Duplicate instance_id in official task input: ${task.instance_id}`);
    const leakKey = hasGoldLikeKeys(task);
    if (leakKey) throw new Error(`Task input contains a gold/reference field (${leakKey}); refusing to use it for selection.`);
    if (!task.instance_id || !task.instruction || !task.type) throw new Error("Each task must contain instance_id, instruction, and type.");
  }
  const scopeConfirmationPath = options.scopeConfirmationPath ? path.resolve(options.scopeConfirmationPath) : undefined;
  const scopeConfirmations = scopeConfirmationPath
    ? await loadScopeConfirmations(scopeConfirmationPath, new Set(tasks.map((task) => task.instance_id)))
    : new Map<string, ScopeConfirmation>();
  const sampledTasks = pythonSeedSample(tasks, Math.min(poolSize, tasks.length));
  const analyses = [] as StaticAnalysis[];
  for (const task of sampledTasks) analyses.push(await analyzeTask(task, examplesRoot, scopeConfirmations.get(task.instance_id)));
  const eligible = analyses.filter((item) => item.eligible).sort((left, right) => left.totalScore - right.totalScore || left.row.instance_id.localeCompare(right.row.instance_id));
  const excluded = analyses.filter((item) => !item.eligible);
  const limitations: string[] = [];
  if (sampledTasks.length < poolSize) limitations.push(`official task list contains only ${sampledTasks.length} tasks; requested pool was ${poolSize}`);
  if (excluded.length > 0) limitations.push(`${excluded.length} sampled tasks were excluded by the local dbt/DuckDB eligibility checks`);
  const manualScopeCount = analyses.filter((item) => item.exclusionReason?.includes("manual candidate-pool confirmation")).length;
  if (manualScopeCount > 0) limitations.push(`${manualScopeCount} sampled tasks require one-time manual candidate-pool confirmation; automatic model-scope inference was not used`);
  if (eligible.length < 9) limitations.push(`only ${eligible.length} eligible tasks remain; fewer than the required 9 tasks`);
  const groups = splitIntoThree(eligible);
  const groupScoreRanges = groups.map((group) => (group.length > 0 ? [group[0].totalScore, group[group.length - 1].totalScore] : []));
  const distinctScores = new Set(eligible.map((item) => item.totalScore));
  if (distinctScores.size < 3 && (groupScoreRanges[0]?.[0] ?? 0) === (groupScoreRanges[2]?.[1] ?? 0)) {
    limitations.push("the eligible pool does not show three discernible structural score levels");
  }
  const status = limitations.some((item) => item.includes("manual candidate-pool confirmation") || item.includes("fewer than the required 9") || item.includes("does not show three discernible")) ? "incomplete" : "ready";
  const tiers: Record<Tier, string[]> = { low: [], medium: [], high: [] };
  let selected: Array<StaticAnalysis & { instruction: string }> = [];
  const tierNames: Tier[] = ["low", "medium", "high"];
  for (let index = 0; index < groups.length; index += 1) {
    for (const item of groups[index]) item.row.tier = tierNames[index];
  }
  if (status === "ready") {
    for (let index = 0; index < groups.length; index += 1) {
      const chosen = pythonSeedSample(groups[index], 3);
      for (const item of chosen) {
        const task = tasks.find((candidate) => candidate.instance_id === item.row.instance_id);
        if (!task) throw new Error(`Task disappeared while selecting ${item.row.instance_id}`);
        item.row.tier = tierNames[index];
        item.row.selection_reason = `${buildReason({ scopeScore: item.scopeScore, dependencyScore: item.dependencyScore, constraintScore: item.constraintScore, estimatedRelatedFiles: item.estimatedRelatedFiles, modelCount: item.modelCount, longest: item.visibleLongestDependencyChainEdges, hasBranch: item.row.dependency_observations.includes("branching=true") })} tier=${tierNames[index]}.`;
        tiers[tierNames[index]].push(item.row.instance_id);
        selected.push({ ...item, instruction: task.instruction });
      }
    }
  }
  const copiedTaskFile = path.join(outputDir, "official-task-list.jsonl");
  await copyFile(taskFile, copiedTaskFile);
  const copiedScopeConfirmation = scopeConfirmationPath ? path.join(outputDir, "scope-confirmations.jsonl") : undefined;
  if (scopeConfirmationPath && copiedScopeConfirmation && path.resolve(scopeConfirmationPath) !== copiedScopeConfirmation) {
    await copyFile(scopeConfirmationPath, copiedScopeConfirmation);
  }
  const candidateRows = analyses.map((item) => item.row);
  await writeJsonl(path.join(outputDir, "candidate_pool.jsonl"), candidateRows);
  if (status === "ready") await writeJsonl(path.join(outputDir, "selected_tasks.jsonl"), selected.map((item) => item.row));
  const manifest: SelectionManifest = {
    schema_version: "1.0",
    created_on: new Date().toISOString(),
    seed: SELECTION_SEED,
    task_source: { path: taskFile, official_url: OFFICIAL_TASK_URL, retrieved_on: RETRIEVED_ON, copied_to: copiedTaskFile },
    examples_root: examplesRoot,
    ...(copiedScopeConfirmation ? { scope_confirmation_path: copiedScopeConfirmation } : {}),
    requested_pool_size: poolSize,
    sampled_pool_size: sampledTasks.length,
    candidate_pool: analyses,
    excluded,
    tiers,
    selected,
    limitations,
    status,
  };
  await writeJson(selectionPath, manifest);
  return manifest;
}

export async function loadSelection(selectionPath: string): Promise<SelectionManifest> {
  const manifest = JSON.parse(await readFile(selectionPath, "utf8")) as SelectionManifest;
  if (manifest.schema_version !== "1.0" || manifest.seed !== SELECTION_SEED) throw new Error("Selection manifest is not compatible with the fixed protocol.");
  if (manifest.status !== "ready") throw new Error(`Selection is incomplete: ${manifest.limitations.join("; ")}`);
  if (manifest.selected.length !== 9) throw new Error(`Selection must contain exactly 9 tasks, found ${manifest.selected.length}.`);
  for (const tier of ["low", "medium", "high"] as Tier[]) {
    if (manifest.tiers[tier].length !== 3) throw new Error(`Selection tier ${tier} must contain exactly 3 tasks.`);
  }
  if (manifest.scope_confirmation_path) {
    const taskRows = await readJsonl<OfficialTask>(manifest.task_source.copied_to);
    await loadScopeConfirmations(manifest.scope_confirmation_path, new Set(taskRows.map((task) => task.instance_id)));
  }
  return manifest;
}
