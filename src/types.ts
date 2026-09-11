export type Tier = "low" | "medium" | "high";

export type RunStatus =
  | "pending"
  | "running"
  | "completed"
  | "agent_failed"
  | "environment_failed"
  | "timed_out"
  | "scoring_failed"
  | "not_started";

export interface OfficialTask {
  instance_id: string;
  instruction: string;
  type: string;
  [key: string]: unknown;
}

export interface SelectionRow {
  instance_id: string;
  project: string;
  scope_observations: string;
  dependency_observations: string;
  constraint_observations: string;
  tier: Tier | "excluded" | "unassigned";
  selection_reason: string;
}

export interface StaticAnalysis {
  row: SelectionRow;
  eligible: boolean;
  exclusionReason?: string;
  scopeScore: 0 | 1 | 2;
  dependencyScore: 0 | 1 | 2;
  constraintScore: 0 | 1 | 2;
  totalScore: number;
  estimatedRelatedFiles: number;
  modelCount: number;
  visibleLongestDependencyChainEdges: number | "unknown";
  hasUnknownDependencies: boolean;
  databaseFiles: string[];
  modelFiles: string[];
}

export interface SelectionManifest {
  schema_version: "1.0";
  created_on: string;
  seed: 20260911;
  task_source: {
    path: string;
    official_url: string;
    retrieved_on: string;
    copied_to: string;
  };
  examples_root: string;
  scope_confirmation_path?: string;
  requested_pool_size: number;
  sampled_pool_size: number;
  candidate_pool: StaticAnalysis[];
  excluded: StaticAnalysis[];
  tiers: Record<Tier, string[]>;
  selected: Array<StaticAnalysis & { instruction: string }>;
  limitations: string[];
  status: "ready" | "incomplete";
}

export interface RunLimits {
  wallClockMs: number;
  maxModelRequestAttempts: number;
  commandTimeoutMs: number;
  evaluatorTimeoutMs: number;
}

export interface FixedAgentConfig {
  name: string;
  model: string;
  thinking: string;
  systemPromptPath: string;
  systemPrompt: string;
  tools: string[];
  sdk: {
    package: string;
    version: string;
    compactionEnabled: boolean;
    retryEnabled: boolean;
    maxRetries: number;
  };
  parameters: {
    temperature: "N/A: not exposed by the SDK session API";
    top_p: "N/A: not exposed by the SDK session API";
  };
  limits: RunLimits;
  commands: {
    python: string;
    dbt: string;
    duckdb: string;
  };
}

export interface ToolEventRecord {
  at: string;
  event: string;
  tool?: string;
  call_id?: string;
  input?: unknown;
  is_error?: boolean;
  output?: string;
  details?: unknown;
}

export interface DbtValidationRecord {
  action: string;
  command: string[];
  started_at: string;
  ended_at: string;
  duration_ms: number;
  exit_code: number | null;
  timed_out: boolean;
  log_path: string;
  stdout_summary: string;
  stderr_summary: string;
  source: "agent_tool" | "runner_final_validation";
  sandbox_backend: "macos-sandbox-exec" | "docker" | "unavailable";
}

export interface SubmissionRecord {
  instance_id: string;
  answer_type: "answer" | "file" | "files";
  answer_or_path: string | string[];
  result_dir?: string;
  artifact_status: "present" | "missing" | "not_required";
  failure_label?: string;
}

export interface EvaluationTaskScore {
  instance_id: string;
  score: 0 | 1 | null;
  status: "passed" | "failed" | "not_evaluated" | "scoring_unavailable";
}

export interface EvaluationRecord {
  round: 1 | 2;
  result_dir: string;
  gold_dir: string;
  evaluator_script: string;
  evaluator_log: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  exit_code: number | null;
  timed_out: boolean;
  status: "completed" | "failed" | "timed_out";
  score: number | null;
  successful_runs: number | null;
  evaluated_runs: number | null;
  task_scores: EvaluationTaskScore[];
  failure_label?: string;
}

export interface RunReceipt {
  schema_version: "1.0";
  run_id: string;
  experiment_id: string;
  round: 1 | 2;
  repeat: 1 | 2;
  instance_id: string;
  tier: Tier;
  project: string;
  fixed_config_name: string;
  fixed_config: FixedAgentConfig;
  execution: {
    started_at: string;
    ended_at: string;
    duration_ms: number;
    model_request_attempts: number;
    retry_attempts: number;
    token_usage?: {
      input: number;
      output: number;
      cacheRead: number;
      cacheWrite: number;
      total: number;
    };
    stop_reason: string;
    run_status: RunStatus;
    isolation: {
      mode: "restricted-subprocess";
      sandbox_backend: "macos-sandbox-exec" | "docker" | "unavailable";
      workspace: string;
      agent_config_dir: string;
      gold_visible_to_agent: false;
      evaluator_visible_to_agent: false;
      arbitrary_shell_enabled: false;
    };
  };
  modification_and_tools: {
    diff_path: string;
    events_path: string;
    final_response_path: string;
    changed_files: string[];
  };
  validation: {
    commands: DbtValidationRecord[];
    scope: string;
    dbt_status: "passed" | "failed" | "not_run" | "timed_out";
    historical_failure_count: number;
    error_summary?: string;
  };
  submission: SubmissionRecord;
  judgment: {
    score: 0 | 1 | null;
    verdict: "pending" | "passed" | "failed" | "not_scored";
    scoring_status: "pending" | "completed" | "failed" | "timed_out";
    evaluator_log?: string;
    failure_labels: string[];
  };
  attachments: {
    workspace_repo: string;
    baseline_repo: string;
    dbt_logs_dir: string;
  };
}

export interface PlannedRun {
  run_id: string;
  round: 1 | 2;
  repeat: 1 | 2;
  instance_id: string;
  tier: Tier;
  project: string;
  status: RunStatus;
  receipt_path?: string;
  reason?: string;
}
