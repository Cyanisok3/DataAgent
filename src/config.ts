import path from "node:path";
import { readFile } from "node:fs/promises";
import type { FixedAgentConfig, RunLimits } from "./types.js";

export const SELECTION_SEED = 20260911 as const;
export const PI_SDK_VERSION = "0.85.1" as const;
export const FIXED_MODEL_ID = "deepseek/deepseek-v4-flash" as const;
export const FIXED_THINKING = "off" as const;
export const DEFAULT_LIMITS: RunLimits = {
  wallClockMs: 15 * 60 * 1000,
  maxModelRequestAttempts: 30,
  commandTimeoutMs: 5 * 60 * 1000,
  evaluatorTimeoutMs: 10 * 60 * 1000,
};

export const DEFAULT_TOOLS = ["read_file", "edit_file", "write_file", "list_files", "search_files", "dbt_build"];

export async function loadSystemPrompt(projectRoot: string): Promise<{ path: string; content: string }> {
  const promptPath = path.join(projectRoot, "config", "system-prompt.md");
  return { path: promptPath, content: await readFile(promptPath, "utf8") };
}

export function createFixedConfig(options: {
  projectRoot: string;
  model: string;
  thinking: string;
  systemPromptPath: string;
  systemPrompt: string;
  pythonCommand?: string;
  dbtCommand?: string;
  duckdbCommand?: string;
  limits?: Partial<RunLimits>;
}): FixedAgentConfig {
  const limits: RunLimits = { ...DEFAULT_LIMITS, ...options.limits };
  if (options.model !== FIXED_MODEL_ID) throw new Error(`The fixed protocol requires model ${FIXED_MODEL_ID}.`);
  if (options.thinking !== FIXED_THINKING) throw new Error(`The fixed protocol requires thinking ${FIXED_THINKING}.`);
  return {
    name: "pi-dbt-diagnostic-v1",
    model: options.model,
    thinking: options.thinking,
    systemPromptPath: path.relative(options.projectRoot, options.systemPromptPath).split(path.sep).join("/"),
    systemPrompt: options.systemPrompt,
    tools: [...DEFAULT_TOOLS],
    sdk: {
      package: "@earendil-works/pi-coding-agent",
      version: PI_SDK_VERSION,
      compactionEnabled: false,
      retryEnabled: false,
      maxRetries: 0,
    },
    parameters: {
      temperature: "N/A: not exposed by the SDK session API",
      top_p: "N/A: not exposed by the SDK session API",
    },
    limits,
    commands: {
      python: options.pythonCommand ?? "python3",
      dbt: options.dbtCommand ?? "dbt",
      duckdb: options.duckdbCommand ?? "duckdb",
    },
  };
}
