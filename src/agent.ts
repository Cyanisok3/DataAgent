import { lstat, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  createAgentSession,
  DefaultResourceLoader,
  ModelRuntime,
  resolveCliModel,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { createSafeTools } from "./safe-tools.js";
import { ensureDir, fileExists, isoNow, toPosix, truncateText, writeJson, writeJsonl } from "./files.js";
import { hasValidSubmissionArtifact } from "./submission.js";
import type { DbtValidationRecord, FixedAgentConfig, ToolEventRecord } from "./types.js";
import type { RunWorkspace } from "./workspace.js";

/**
 * Runtime-only model registry for this diagnostic runner. The SDK ships a
 * built-in `deepseek` provider (openai-completions API) with a
 * `deepseek-v4-flash` model; this config only pins the official base URL so
 * requests are unaffected by any local defaults. The API key is injected into
 * the SDK runtime from the DEEPSEEK_API_KEY environment variable and is never
 * written into the per-run workspace.
 */
const DEEPSEEK_MODELS_JSON = {
  providers: {
    deepseek: {
      baseUrl: "https://api.deepseek.com",
    },
  },
} as const;

async function injectModelRuntimeFiles(agentConfigDir: string): Promise<string> {
  await ensureDir(agentConfigDir);
  await writeJson(path.join(agentConfigDir, "models.json"), DEEPSEEK_MODELS_JSON);
  const apiKey = process.env.DEEPSEEK_API_KEY?.trim();
  if (!apiKey) throw new Error("DEEPSEEK_API_KEY is required for a model-backed run.");
  return apiKey;
}

type ThinkingLevel = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

export interface AgentExecutionResult {
  startedAt: string;
  endedAt: string;
  durationMs: number;
  modelRequestAttempts: number;
  retryAttempts: number;
  stopReason: string;
  finalResponse: string;
  error?: string;
  events: ToolEventRecord[];
  dbtValidations: DbtValidationRecord[];
  tokenUsage?: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    total: number;
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? value as Record<string, unknown> : {};
}

function textFromContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value.filter((block) => asRecord(block).type === "text").map((block) => String(asRecord(block).text ?? "")).join("");
}

export function assistantMessageError(value: unknown): string | undefined {
  const message = asRecord(value);
  if (message.role !== "assistant") return undefined;
  const stopReason = String(message.stopReason ?? message.stop_reason ?? "").toLowerCase();
  if (stopReason !== "error" && stopReason !== "aborted") return undefined;
  const errorMessage = String(message.errorMessage ?? message.error_message ?? "").trim();
  return errorMessage || `assistant message stopped with ${stopReason}`;
}

export function lastAssistantText(messages: unknown[]): string {
  const message = [...messages].reverse().map(asRecord).find((candidate) => candidate.role === "assistant");
  const stopReason = String(message?.stopReason ?? message?.stop_reason ?? "").toLowerCase();
  if (!message || stopReason !== "stop" || assistantMessageError(message)) return "";
  return textFromContent(message.content).trim();
}

export function modelRequestLimitReached(completedRequests: number, maximumRequests: number): boolean {
  return completedRequests >= maximumRequests;
}

async function findUniqueRootDuckdb(repo: string): Promise<string | undefined> {
  const entries = await readdir(repo, { withFileTypes: true });
  const candidates: string[] = [];
  for (const entry of entries) {
    if (!entry.name.toLowerCase().endsWith(".duckdb")) continue;
    const info = await lstat(path.join(repo, entry.name));
    if (info.isFile()) candidates.push(entry.name);
  }
  return candidates.length === 1 ? toPosix(candidates[0]) : undefined;
}

type AgentStreamFunction = Awaited<ReturnType<typeof createAgentSession>>["session"]["agent"]["streamFunction"];

interface RequestBudget {
  maximumRequests: number;
  requestCount: () => number;
  onRequest: () => void;
  onBudgetExhausted: () => void;
}

function abortError(): DOMException {
  return new DOMException("The operation was aborted", "AbortError");
}

export function wrapModelStreamFunction(streamFunction: AgentStreamFunction, budget: RequestBudget): AgentStreamFunction {
  return (model, context, streamOptions) => {
    const transport = streamOptions?.fetch ?? globalThis.fetch;
    const fetchWithBudget: typeof globalThis.fetch = async (input, init) => {
      if (streamOptions?.signal?.aborted || init?.signal?.aborted) throw abortError();
      if (budget.requestCount() >= budget.maximumRequests) {
        budget.onBudgetExhausted();
        throw abortError();
      }
      budget.onRequest();
      return transport(input, init);
    };
    return streamFunction(model, context, { ...streamOptions, fetch: fetchWithBudget });
  };
}

export async function selectFinalArtifactResponse(options: {
  workspaceRepo: string;
  stopReason: string;
  explicitResponse: string;
}): Promise<string> {
  const response = options.explicitResponse.trim();
  if (response === "NO_ARTIFACT") return options.explicitResponse;
  if (response && await hasValidSubmissionArtifact(options.workspaceRepo, response)) return options.explicitResponse;
  if (options.stopReason === "agent_end" || options.stopReason === "model_request_limit" || options.stopReason === "wall_clock_timeout") {
    return await findUniqueRootDuckdb(options.workspaceRepo) ?? options.explicitResponse;
  }
  return options.explicitResponse;
}

function collectTokenUsage(messages: unknown[]): AgentExecutionResult["tokenUsage"] {
  const total = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 };
  let found = false;
  for (const message of messages) {
    const usage = asRecord(asRecord(message).usage);
    const input = Number(usage.input ?? 0);
    const output = Number(usage.output ?? 0);
    const cacheRead = Number(usage.cacheRead ?? usage.cache_read ?? 0);
    const cacheWrite = Number(usage.cacheWrite ?? usage.cache_write ?? 0);
    if (![input, output, cacheRead, cacheWrite].every(Number.isFinite)) continue;
    if (input || output || cacheRead || cacheWrite) found = true;
    total.input += input;
    total.output += output;
    total.cacheRead += cacheRead;
    total.cacheWrite += cacheWrite;
  }
  total.total = total.input + total.output;
  return found ? total : undefined;
}

function boundedInput(value: unknown): unknown {
  if (value === undefined) return undefined;
  try {
    const serialized = JSON.stringify(value);
    if (serialized && serialized.length > 32_000) return { truncated_input: truncateText(serialized, 32_000) };
  } catch {
    return { input: "[unserializable tool input]" };
  }
  return value;
}

function eventInput(event: Record<string, unknown>): unknown {
  return boundedInput(event.args ?? event.input ?? event.parameters);
}

function eventOutput(event: Record<string, unknown>): string | undefined {
  const result = event.result ?? event.output;
  if (typeof result === "string") return truncateText(result, 32_000);
  const record = asRecord(result);
  const content = textFromContent(record.content);
  return content ? truncateText(content, 32_000) : undefined;
}

function eventDetails(event: Record<string, unknown>): Record<string, unknown> | undefined {
  const result = asRecord(event.result ?? event.output);
  const details = asRecord(result.details ?? event.details);
  const selected: Record<string, unknown> = {};
  for (const key of ["command", "exit_code", "timed_out", "sandbox_backend", "log_path", "duration_ms", "path", "action"]) {
    if (details[key] !== undefined) selected[key] = details[key];
  }
  return Object.keys(selected).length > 0 ? selected : undefined;
}

function recordEvent(events: ToolEventRecord[], type: string, event: Record<string, unknown>): void {
  const record: ToolEventRecord = { at: isoNow(), event: type };
  if (typeof event.toolName === "string") record.tool = event.toolName;
  if (typeof event.toolCallId === "string") record.call_id = event.toolCallId;
  if (type === "tool_execution_start") record.input = eventInput(event);
  if (type === "tool_execution_end" || type === "tool_execution_update") {
    record.is_error = Boolean(event.isError);
    record.output = eventOutput(event);
    record.details = eventDetails(event);
  }
  if (type === "auto_retry_start" || type === "auto_retry_end" || type === "compaction_start" || type === "compaction_end") {
    const details: Record<string, unknown> = {};
    for (const key of ["attempt", "maxAttempts", "delayMs", "success", "reason", "aborted", "willRetry"]) {
      if (event[key] !== undefined) details[key] = event[key];
    }
    record.details = details;
  }
  if (type === "model_error") record.details = event.details;
  events.push(record);
}

export async function runAgent(options: {
  workspace: RunWorkspace;
  config: FixedAgentConfig;
  taskInstruction: string;
  instanceId: string;
}): Promise<AgentExecutionResult> {
  const startedMs = Date.now();
  const startedAt = isoNow();
  const events: ToolEventRecord[] = [];
  const dbtValidations: DbtValidationRecord[] = [];
  let modelRequestAttempts = 0;
  let retryAttempts = 0;
  let stopReason = "agent_end";
  let errorMessage: string | undefined;
  let modelErrorObserved = false;
  const deadline = startedMs + options.config.limits.wallClockMs;
  const abortController = new AbortController();
  let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | undefined;
  const timeout = setTimeout(() => {
    stopReason = "wall_clock_timeout";
    abortController.abort();
    if (session) void session.abort().catch(() => undefined);
  }, options.config.limits.wallClockMs);

  try {
    const apiKey = await injectModelRuntimeFiles(options.workspace.agentConfigDir);
    const modelRuntime = await ModelRuntime.create({
      authPath: path.join(options.workspace.agentConfigDir, "auth.json"),
      modelsPath: path.join(options.workspace.agentConfigDir, "models.json"),
      modelsStorePath: path.join(options.workspace.agentConfigDir, "models-store.json"),
      refreshOnCreate: false,
      allowModelNetwork: false,
    });
    await modelRuntime.setRuntimeApiKey("deepseek", apiKey);
    const thinking = options.config.thinking as ThinkingLevel;
    const resolution = resolveCliModel({ cliModel: options.config.model, cliThinking: thinking, modelRuntime });
    if (resolution.error || !resolution.model) throw new Error(resolution.error ?? `Model not found: ${options.config.model}`);
    if (resolution.warning) recordEvent(events, "model_warning", { details: { warning: resolution.warning } });
    const settingsManager = SettingsManager.inMemory({
      compaction: { enabled: options.config.sdk.compactionEnabled },
      retry: { enabled: options.config.sdk.retryEnabled, maxRetries: options.config.sdk.maxRetries },
    });
    const resourceLoader = new DefaultResourceLoader({
      cwd: options.workspace.repo,
      agentDir: options.workspace.agentConfigDir,
      settingsManager,
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: true,
      systemPrompt: options.config.systemPrompt,
    });
    await resourceLoader.reload();
    const safeTools = createSafeTools({
      root: options.workspace.repo,
      runtimeDir: options.workspace.dbtEvidenceDir,
      config: options.config,
      remainingMs: () => Math.max(0, deadline - Date.now()),
      onDbtValidation: async (record) => {
        dbtValidations.push(record);
      },
    });
    const result = await createAgentSession({
      cwd: options.workspace.repo,
      agentDir: options.workspace.agentConfigDir,
      model: resolution.model,
      thinkingLevel: resolution.thinkingLevel ?? thinking,
      modelRuntime,
      noTools: "builtin",
      tools: options.config.tools,
      customTools: safeTools,
      resourceLoader,
      sessionManager: SessionManager.inMemory(options.workspace.repo),
      settingsManager,
    });
    session = result.session;
    session.agent.streamFunction = wrapModelStreamFunction(session.agent.streamFunction, {
      maximumRequests: options.config.limits.maxModelRequestAttempts,
      requestCount: () => modelRequestAttempts,
      onRequest: () => {
        modelRequestAttempts += 1;
      },
      onBudgetExhausted: () => {
        if (stopReason === "agent_end") stopReason = "model_request_limit";
        abortController.abort();
        void session?.abort().catch(() => undefined);
      },
    });
    session.subscribe((event) => {
      const eventRecord = event as unknown as Record<string, unknown>;
      recordEvent(events, String(eventRecord.type ?? "unknown"), eventRecord);
      const eventType = String(eventRecord.type ?? "");
      const message = asRecord(eventRecord.message);
      if ((eventType === "message_start" || eventType === "message_end") && !modelErrorObserved) {
        const messageError = assistantMessageError(message);
        const messageStopReason = String(message.stopReason ?? message.stop_reason ?? "").toLowerCase();
        const expectedAbort = (stopReason === "model_request_limit" || stopReason === "wall_clock_timeout") && messageStopReason === "aborted";
        if (messageError && !expectedAbort) {
          modelErrorObserved = true;
          errorMessage = truncateText(messageError, 4_000);
          recordEvent(events, "model_error", {
            details: {
              stop_reason: String(message.stopReason ?? message.stop_reason ?? ""),
              error_message: errorMessage,
            },
          });
          if (stopReason === "agent_end") stopReason = String(message.stopReason ?? "error") === "aborted" ? "agent_aborted" : "model_error";
        }
      }
      if (eventType === "turn_start") {
        if (modelRequestLimitReached(modelRequestAttempts, options.config.limits.maxModelRequestAttempts)) {
          stopReason = "model_request_limit";
          abortController.abort();
          void session?.abort().catch(() => undefined);
        }
      }
      if (eventType === "auto_retry_start") retryAttempts += 1;
      if (eventType === "agent_end" && stopReason === "agent_end") stopReason = "agent_end";
    });
    const prompt = `Task instance_id: ${options.instanceId}\n\nInstruction:\n${options.taskInstruction}\n\nFollow the fixed system prompt. Inspect the visible repository, implement the requested dbt changes, validate with dbt_build, and finish with exactly one relative .duckdb artifact path or NO_ARTIFACT.`;
    await session.prompt(prompt, { preflightResult: (accepted) => {
      if (!accepted && stopReason === "agent_end") stopReason = "agent_error";
    } });
    await session.waitForIdle();
  } catch (error) {
    const expectedStop = stopReason === "model_request_limit" || stopReason === "wall_clock_timeout";
    if (!expectedStop && stopReason === "agent_end") stopReason = "agent_error";
    if (!expectedStop && !errorMessage) errorMessage = error instanceof Error ? error.message : String(error);
    if (!expectedStop) recordEvent(events, "runner_error", { details: { message: errorMessage ?? String(error) } });
  }

  const submittableStop = stopReason === "agent_end" || stopReason === "model_request_limit" || stopReason === "wall_clock_timeout";
  let finalResponse = "";
  if (submittableStop) {
    const explicitResponse = session && !errorMessage && !modelErrorObserved ? lastAssistantText(session.messages as unknown[]) : "";
    finalResponse = await selectFinalArtifactResponse({ workspaceRepo: options.workspace.repo, stopReason, explicitResponse });
  }
  const tokenUsage = session ? collectTokenUsage(session.messages as unknown[]) : undefined;
  if (session) {
    session.dispose();
  }

  clearTimeout(timeout);

  const endedAt = isoNow();
  await writeFile(options.workspace.finalResponsePath, `${finalResponse}\n`, "utf8");
  await writeJsonl(options.workspace.eventsPath, events);
  return {
    startedAt,
    endedAt,
    durationMs: Date.now() - startedMs,
    modelRequestAttempts,
    retryAttempts,
    stopReason,
    finalResponse,
    error: errorMessage,
    events,
    dbtValidations,
    tokenUsage,
  };
}
