import { copyFile, lstat, readdir } from "node:fs/promises";
import path from "node:path";
import { assertWithin, copyTree, ensureDir, existingPathWithin, fileExists, readJsonl, toPosix, writeJsonl } from "./files.js";
import type { RunReceipt, SubmissionRecord } from "./types.js";

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

  const candidates = responseCandidates(response);
  const existingCandidates: string[] = [];
  for (const candidate of candidates) {
    if (!candidate || candidate === "NO_ARTIFACT") continue;
    try {
      const absolutePath = await existingPathWithin(options.workspaceRepo, path.join(options.workspaceRepo, candidate));
      if (!(await lstat(absolutePath)).isFile()) continue;
      const relativePath = toPosix(path.relative(options.workspaceRepo, absolutePath));
      if (relativePath && !relativePath.startsWith("../") && relativePath !== "..") existingCandidates.push(relativePath);
    } catch {
      // Keep the original response in the receipt, but do not treat it as a submitted artifact.
    }
  }
  const uniqueCandidates = [...new Set(existingCandidates)];
  if (uniqueCandidates.length > 0 && uniqueCandidates.length === candidates.length) {
    await ensureDir(resultInstanceDir);
    for (const candidate of uniqueCandidates) await copyArtifact(options.workspaceRepo, resultInstanceDir, candidate);
    const answerType = uniqueCandidates.length === 1 ? "file" : "files";
    const answerOrPath = uniqueCandidates.length === 1 ? uniqueCandidates[0] : uniqueCandidates;
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
      failure_label: "missing_artifact",
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

export async function validateRoundResultDirectory(options: { resultDir: string; expectedInstanceIds?: string[] }): Promise<{ ok: boolean; metadataIds: string[]; problems: string[] }> {
  const problems: string[] = [];
  const resultDir = path.resolve(options.resultDir);
  const metadataPath = path.join(resultDir, "results_metadata.jsonl");
  let metadataIds: string[] = [];
  try {
    metadataIds = await metadataIdsForDirectory(resultDir);
  } catch (error) {
    problems.push(error instanceof Error ? error.message : String(error));
  }
  if (options.expectedInstanceIds) {
    const expected = new Set(options.expectedInstanceIds);
    for (const instanceId of metadataIds) if (!expected.has(instanceId)) problems.push(`submission contains an unexpected instance_id: ${instanceId}`);
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
  return { ok: problems.length === 0, metadataIds, problems };
}

async function metadataIdsForDirectory(resultDir: string): Promise<string[]> {
  const metadataPath = path.join(resultDir, "results_metadata.jsonl");
  if (!(await fileExists(metadataPath))) throw new Error(`Official submission metadata is missing: ${metadataPath}`);
  const rows = await readJsonl<Record<string, unknown>>(metadataPath);
  const ids: string[] = [];
  for (const row of rows) {
    if (typeof row.instance_id !== "string" || !row.instance_id) throw new Error("Every submission row needs a non-empty instance_id.");
    if (row.answer_type !== "answer" && row.answer_type !== "file" && row.answer_type !== "files") throw new Error(`Unsupported answer_type for ${row.instance_id}.`);
    if (row.answer_type === "files" ? !Array.isArray(row.answer_or_path) : typeof row.answer_or_path !== "string") throw new Error(`answer_or_path has the wrong type for ${row.instance_id}.`);
    if (ids.includes(row.instance_id)) throw new Error(`Duplicate instance_id in one submission directory: ${row.instance_id}`);
    ids.push(row.instance_id);
  }
  return ids;
}

export async function submissionFromReceipt(receipt: RunReceipt, roundResultDir: string): Promise<SubmissionBuildResult> {
  if (receipt.submission.artifact_status === "missing") return { record: receipt.submission };
  const answerOrPath = receipt.submission.answer_or_path;
  const metadataEntry = { instance_id: receipt.instance_id, answer_type: receipt.submission.answer_type, answer_or_path: answerOrPath };
  return { record: receipt.submission, metadataEntry };
}
