import { copyFile, lstat, mkdtemp, readdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { assertWithin, copyTree, ensureDir, existingPathWithin, fileExists, readJsonl, toPosix, walkFiles, writeJsonl } from "./files.js";
import type { SubmissionExclusion, SubmissionRecord } from "./types.js";

function isDuckdbArtifact(value: string): boolean {
  return value.toLowerCase().endsWith(".duckdb");
}

function hasExactlyOneValidCandidate(candidates: string[], existingCandidates: string[]): boolean {
  return candidates.length === 1 && existingCandidates.length === 1 && isDuckdbArtifact(existingCandidates[0]);
}

export interface SubmissionBuildResult {
  record: SubmissionRecord;
  metadataEntry?: {
    instance_id: string;
    answer_type: "answer" | "file" | "files";
    answer_or_path: string | string[];
  };
}

function cleanCandidate(value: string): string {
  let candidate = value.trim().replace(/^`+|`+$/g, "");
  candidate = candidate.replace(/^\/?workspace\//, "");
  candidate = candidate.replace(/^(?:FINAL_ARTIFACT|ARTIFACT|result|answer)\s*[:=]\s*/i, "").trim();
  return candidate;
}

function responseCandidates(response: string): string[] {
  const lines = response.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !/^```/.test(line));
  if (lines.length === 0) return [];
  const last = cleanCandidate(lines[lines.length - 1]);
  if (last.startsWith("[") && last.endsWith("]")) {
    try {
      const parsed = JSON.parse(last) as unknown;
      if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string")) return parsed.map((item) => cleanCandidate(item));
    } catch {
      // Treat an invalid JSON-looking response as a direct answer below.
    }
  }
  if (last.includes(",") && !last.includes(" ")) return last.split(",").map(cleanCandidate).filter(Boolean);
  return [last];
}

async function copyArtifact(sourceRepo: string, resultInstanceDir: string, relativePath: string): Promise<void> {
  const sourcePath = await existingPathWithin(sourceRepo, path.join(sourceRepo, relativePath));
  const targetPath = path.join(resultInstanceDir, relativePath);
  assertWithin(targetPath, resultInstanceDir, "submission artifact");
  const info = await lstat(sourcePath);
  if (info.isDirectory()) {
    await copyTree(sourcePath, targetPath);
  } else if (info.isFile()) {
    await ensureDir(path.dirname(targetPath));
    await copyFile(sourcePath, targetPath);
  } else {
    throw new Error(`Unsupported submission artifact: ${relativePath}`);
  }
}

async function existingArtifactCandidates(workspaceRepo: string, response: string): Promise<{ candidates: string[]; existingCandidates: string[] }> {
  const candidates = responseCandidates(response);
  const existingCandidates: string[] = [];
  for (const candidate of candidates) {
    if (!candidate || candidate === "NO_ARTIFACT") continue;
    try {
      const absolutePath = await existingPathWithin(workspaceRepo, path.join(workspaceRepo, candidate));
      if (!(await lstat(absolutePath)).isFile()) continue;
      const relativePath = toPosix(path.relative(workspaceRepo, absolutePath));
      if (relativePath && !relativePath.startsWith("../") && relativePath !== "..") existingCandidates.push(relativePath);
    } catch {
      // Keep the original response in the receipt, but do not treat it as a submitted artifact.
    }
  }
  return { candidates, existingCandidates: [...new Set(existingCandidates)] };
}

export async function hasValidSubmissionArtifact(workspaceRepo: string, response: string): Promise<boolean> {
  const { candidates, existingCandidates } = await existingArtifactCandidates(workspaceRepo, response.trim());
  return hasExactlyOneValidCandidate(candidates, existingCandidates);
}

export async function buildSubmission(options: {
  instanceId: string;
  finalResponse: string;
  workspaceRepo: string;
  roundResultDir: string;
}): Promise<SubmissionBuildResult> {
  const resultInstanceDir = path.join(options.roundResultDir, options.instanceId);
  const response = options.finalResponse.trim();
  if (response === "NO_ARTIFACT") {
    return {
      record: {
        instance_id: options.instanceId,
        answer_type: "answer",
        answer_or_path: "",
        artifact_status: "missing",
        failure_label: "missing_artifact",
      },
    };
  }
  if (!response) {
    return {
      record: {
        instance_id: options.instanceId,
        answer_type: "answer",
        answer_or_path: "",
        artifact_status: "missing",
        failure_label: "missing_artifact",
      },
    };
  }

  const { candidates, existingCandidates: uniqueCandidates } = await existingArtifactCandidates(options.workspaceRepo, response);
  if (hasExactlyOneValidCandidate(candidates, uniqueCandidates)) {
    await ensureDir(resultInstanceDir);
    for (const candidate of uniqueCandidates) await copyArtifact(options.workspaceRepo, resultInstanceDir, candidate);
    const answerType = "file" as const;
    const answerOrPath = uniqueCandidates[0];
    return {
      record: {
        instance_id: options.instanceId,
        answer_type: answerType,
        answer_or_path: answerOrPath,
        result_dir: resultInstanceDir,
        artifact_status: "present",
      },
      metadataEntry: { instance_id: options.instanceId, answer_type: answerType, answer_or_path: answerOrPath },
    };
  }

  return {
    record: {
      instance_id: options.instanceId,
      answer_type: "answer",
      answer_or_path: response,
      artifact_status: "missing",
      failure_label: "invalid_submission_artifact",
    },
  };
}

export async function writeRoundMetadata(roundResultDir: string, submissions: SubmissionBuildResult[]): Promise<string> {
  await ensureDir(roundResultDir);
  const seen = new Set<string>();
  const entries = [] as Array<{ instance_id: string; answer_type: "answer" | "file" | "files"; answer_or_path: string | string[] }>;
  for (const submission of submissions) {
    const entry = submission.metadataEntry;
    if (!entry || seen.has(entry.instance_id)) continue;
    seen.add(entry.instance_id);
    entries.push(entry);
  }
  entries.sort((left, right) => left.instance_id.localeCompare(right.instance_id));
  const metadataPath = path.join(roundResultDir, "results_metadata.jsonl");
  await writeJsonl(metadataPath, entries);
  return metadataPath;
}

export interface SubmissionValidationResult {
  ok: boolean;
  metadataIds: string[];
  problems: string[];
  excluded: SubmissionExclusion[];
  validEntries: Array<Record<string, unknown>>;
}

export async function validateRoundResultDirectory(options: { resultDir: string; expectedInstanceIds?: string[] }): Promise<SubmissionValidationResult> {
  const problems: string[] = [];
  const resultDir = path.resolve(options.resultDir);
  let metadataIds: string[] = [];
  let excluded: SubmissionExclusion[] = [];
  let validEntries: Array<Record<string, unknown>> = [];
  try {
    const metadata = await metadataForDirectory(resultDir);
    metadataIds = metadata.ids;
    excluded = metadata.excluded;
    validEntries = metadata.entries;
  } catch (error) {
    problems.push(error instanceof Error ? error.message : String(error));
  }
  if (options.expectedInstanceIds) {
    const expected = new Set(options.expectedInstanceIds);
    const expectedMetadataIds: string[] = [];
    const expectedEntries: Array<Record<string, unknown>> = [];
    for (const [index, instanceId] of metadataIds.entries()) {
      if (expected.has(instanceId)) {
        expectedMetadataIds.push(instanceId);
        expectedEntries.push(validEntries[index]);
      } else {
        excluded.push({ instance_id: instanceId, reason: `unexpected instance_id: ${instanceId}` });
      }
    }
    metadataIds = expectedMetadataIds;
    validEntries = expectedEntries;
  }
  if (await fileExists(resultDir)) {
    const entries = await readdir(resultDir, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.name === "results_metadata.jsonl") continue;
      if (!entry.isDirectory()) problems.push(`submission root contains an unexpected file: ${entry.name}`);
    }
  } else {
    problems.push(`submission directory is missing: ${resultDir}`);
  }
  return { ok: problems.length === 0 && excluded.length === 0, metadataIds, problems, excluded, validEntries };
}

async function metadataForDirectory(resultDir: string): Promise<{ ids: string[]; excluded: SubmissionExclusion[]; entries: Array<Record<string, unknown>> }> {
  const metadataPath = path.join(resultDir, "results_metadata.jsonl");
  if (!(await fileExists(metadataPath))) throw new Error(`Official submission metadata is missing: ${metadataPath}`);
  const rows = await readJsonl<unknown>(metadataPath);
  const ids: string[] = [];
  const excluded: SubmissionExclusion[] = [];
  const entries: Array<Record<string, unknown>> = [];
  for (const row of rows) {
    if (row === null || typeof row !== "object" || Array.isArray(row)) {
      excluded.push({ reason: "Submission metadata row must be an object." });
      continue;
    }
    const metadata = row as Record<string, unknown>;
    const instanceId = typeof metadata.instance_id === "string" && metadata.instance_id ? metadata.instance_id : undefined;
    if (!instanceId) {
      excluded.push({ reason: "Every submission row needs a non-empty instance_id." });
      continue;
    }
    if (ids.includes(instanceId)) {
      excluded.push({ instance_id: instanceId, reason: `Duplicate instance_id in one submission directory: ${instanceId}` });
      continue;
    }
    if (metadata.answer_type !== "file" || typeof metadata.answer_or_path !== "string" || !isDuckdbArtifact(metadata.answer_or_path) || path.isAbsolute(metadata.answer_or_path)) {
      excluded.push({ instance_id: instanceId, reason: `DBT submission for ${instanceId} must reference exactly one relative .duckdb file.` });
      continue;
    }
    if (instanceId === "." || instanceId === ".." || path.basename(instanceId) !== instanceId) {
      excluded.push({ instance_id: instanceId, reason: `Submission instance_id must be a single directory name: ${instanceId}` });
      continue;
    }
    try {
      const instanceDir = await existingPathWithin(resultDir, path.join(resultDir, instanceId));
      const artifactPath = await existingPathWithin(instanceDir, path.join(instanceDir, metadata.answer_or_path));
      const artifactFiles = (await walkFiles(instanceDir)).filter((entry) => entry.kind === "file");
      const artifactRelativePath = toPosix(path.relative(instanceDir, artifactPath));
      if (artifactFiles.length !== 1 || artifactFiles[0].relativePath !== artifactRelativePath || !isDuckdbArtifact(artifactFiles[0].relativePath)) {
        excluded.push({ instance_id: instanceId, reason: `DBT submission for ${instanceId} must contain exactly one .duckdb file.` });
        continue;
      }
    } catch (error) {
      excluded.push({ instance_id: instanceId, reason: error instanceof Error ? error.message : String(error) });
      continue;
    }
    ids.push(instanceId);
    entries.push(metadata);
  }
  return { ids, excluded, entries };
}

export async function createFilteredSubmissionDirectory(options: { resultDir: string; entries: Array<Record<string, unknown>> }): Promise<string> {
  const resultDir = path.resolve(options.resultDir);
  const filteredDir = await mkdtemp(path.join(os.tmpdir(), "dataagent-evaluator-"));
  await writeJsonl(path.join(filteredDir, "results_metadata.jsonl"), options.entries);
  for (const entry of options.entries) {
    const instanceId = entry.instance_id;
    if (typeof instanceId !== "string" || !instanceId) throw new Error("Filtered submission metadata contains an invalid instance_id.");
    await copyTree(path.join(resultDir, instanceId), path.join(filteredDir, instanceId));
  }
  return filteredDir;
}
