import { cp, lstat, mkdir, readFile, readdir, realpath, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export function isoNow(): string {
  return new Date().toISOString();
}

export function isWithin(candidate: string, parent: string): boolean {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

export function assertWithin(candidate: string, parent: string, label = "path"): string {
  const resolvedCandidate = path.resolve(candidate);
  const resolvedParent = path.resolve(parent);
  if (!isWithin(resolvedCandidate, resolvedParent)) {
    throw new Error(`${label} escapes its allowed root: ${candidate}`);
  }
  return resolvedCandidate;
}

export function toPosix(value: string): string {
  return value.split(path.sep).join("/");
}

export async function ensureDir(directory: string): Promise<void> {
  await mkdir(directory, { recursive: true });
}

export async function writeJson(filePath: string, value: unknown): Promise<void> {
  await ensureDir(path.dirname(filePath));
  await writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export async function writeJsonl(filePath: string, rows: unknown[]): Promise<void> {
  await ensureDir(path.dirname(filePath));
  const content = rows.map((row) => JSON.stringify(row)).join("\n");
  await writeFile(filePath, content.length > 0 ? `${content}\n` : "", "utf8");
}

export async function readJson<T>(filePath: string): Promise<T> {
  return JSON.parse(await readFile(filePath, "utf8")) as T;
}

export async function readJsonl<T>(filePath: string): Promise<T[]> {
  const content = await readFile(filePath, "utf8");
  const rows: T[] = [];
  for (const [index, line] of content.split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    try {
      rows.push(JSON.parse(line) as T);
    } catch (error) {
      throw new Error(`Invalid JSONL at ${filePath}:${index + 1}: ${String(error)}`);
    }
  }
  return rows;
}

export interface FileEntry {
  absolutePath: string;
  relativePath: string;
  kind: "file" | "directory";
  size: number;
}

export async function walkFiles(root: string, options: { includeDirectories?: boolean; skip?: (relativePath: string) => boolean } = {}): Promise<FileEntry[]> {
  const resolvedRoot = path.resolve(root);
  const entries: FileEntry[] = [];

  async function visit(current: string): Promise<void> {
    const children = await readdir(current, { withFileTypes: true });
    children.sort((left, right) => left.name.localeCompare(right.name));
    for (const child of children) {
      const absolutePath = path.join(current, child.name);
      const relativePath = toPosix(path.relative(resolvedRoot, absolutePath));
      if (options.skip?.(relativePath)) continue;
      const info = await lstat(absolutePath);
      if (info.isSymbolicLink()) {
        throw new Error(`Symbolic links are not allowed in task inputs: ${absolutePath}`);
      }
      if (info.isDirectory()) {
        if (options.includeDirectories) {
          entries.push({ absolutePath, relativePath, kind: "directory", size: 0 });
        }
        await visit(absolutePath);
      } else if (info.isFile()) {
        entries.push({ absolutePath, relativePath, kind: "file", size: info.size });
      }
    }
  }

  await visit(resolvedRoot);
  return entries;
}

export async function copyTree(source: string, destination: string, options: { skip?: (relativePath: string) => boolean } = {}): Promise<void> {
  const sourceRoot = path.resolve(source);
  const destinationRoot = path.resolve(destination);
  const sourceInfo = await lstat(sourceRoot);
  if (!sourceInfo.isDirectory()) throw new Error(`Expected a directory: ${source}`);
  await ensureDir(destinationRoot);

  async function copyDirectory(currentSource: string, currentDestination: string): Promise<void> {
    const children = await readdir(currentSource, { withFileTypes: true });
    children.sort((left, right) => left.name.localeCompare(right.name));
    for (const child of children) {
      const sourcePath = path.join(currentSource, child.name);
      const relativePath = toPosix(path.relative(sourceRoot, sourcePath));
      if (options.skip?.(relativePath)) continue;
      const destinationPath = path.join(currentDestination, child.name);
      const info = await lstat(sourcePath);
      if (info.isSymbolicLink()) {
        throw new Error(`Symbolic links are not allowed in task inputs: ${sourcePath}`);
      }
      if (info.isDirectory()) {
        await ensureDir(destinationPath);
        await copyDirectory(sourcePath, destinationPath);
      } else if (info.isFile()) {
        await cp(sourcePath, destinationPath, { force: true });
      }
    }
  }

  await copyDirectory(sourceRoot, destinationRoot);
}

export async function removeIfExists(target: string): Promise<void> {
  await rm(target, { recursive: true, force: true });
}

export async function existingPathWithin(root: string, candidate: string): Promise<string> {
  const lexicalRoot = path.resolve(root);
  const canonicalRoot = await realpath(lexicalRoot);
  const lexical = assertWithin(candidate, lexicalRoot);
  const resolved = await realpath(lexical);
  assertWithin(resolved, canonicalRoot, "resolved path");
  return lexical;
}

export async function writablePathWithin(root: string, candidate: string): Promise<string> {
  const lexicalRoot = path.resolve(root);
  const canonicalRoot = await realpath(lexicalRoot);
  const lexical = assertWithin(candidate, lexicalRoot);
  let current = lexical;
  while (true) {
    try {
      const resolvedCurrent = await realpath(current);
      assertWithin(resolvedCurrent, canonicalRoot, "resolved path");
      break;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      const parent = path.dirname(current);
      if (parent === current) throw error;
      current = parent;
    }
  }
  return lexical;
}

export function truncateText(value: string, maxChars = 16000): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}\n[truncated; full output is in the referenced attachment]`;
}

export async function fileExists(filePath: string): Promise<boolean> {
  try {
    await stat(filePath);
    return true;
  } catch {
    return false;
  }
}
