import type { ClientRequest } from "../.local/app-server-types/ClientRequest";
import type { ClientNotification } from "../.local/app-server-types/ClientNotification";
import type { ServerNotification } from "../.local/app-server-types/ServerNotification";
import type { ServerRequest } from "../.local/app-server-types/ServerRequest";
import type { InitializeResponse } from "../.local/app-server-types/InitializeResponse";
import type {
  CommandExecutionRequestApprovalResponse,
  ConfigReadResponse,
  FileChangeRequestApprovalResponse,
  GetAccountResponse,
  LoginAccountResponse,
  ModelListResponse,
  PermissionsRequestApprovalResponse,
  ThreadCompactStartResponse,
  ThreadForkResponse,
  ThreadResumeResponse,
  ThreadStartResponse,
  TurnInterruptResponse,
  TurnStartResponse,
  TurnSteerResponse,
} from "../.local/app-server-types/v2/index";

type ClientResponses = {
  initialize: InitializeResponse;
  "account/read": GetAccountResponse;
  "account/login/start": LoginAccountResponse;
  "config/read": ConfigReadResponse;
  "model/list": ModelListResponse;
  "thread/start": ThreadStartResponse;
  "thread/resume": ThreadResumeResponse;
  "thread/fork": ThreadForkResponse;
  "thread/compact/start": ThreadCompactStartResponse;
  "turn/start": TurnStartResponse;
  "turn/interrupt": TurnInterruptResponse;
  "turn/steer": TurnSteerResponse;
};

type ApprovalResponses = {
  "item/commandExecution/requestApproval": CommandExecutionRequestApprovalResponse;
  "item/fileChange/requestApproval": FileChangeRequestApprovalResponse;
  "item/permissions/requestApproval": PermissionsRequestApprovalResponse;
};

type ClientCall<M extends keyof ClientResponses> = Extract<ClientRequest, { method: M }>;
type ApprovalCall<M extends keyof ApprovalResponses> = Extract<ServerRequest, { method: M }>;
type HarnessPush = { kind: "notification"; notification: ServerNotification }
  | { kind: "server-request"; request: ServerRequest };

export interface AppServerConnection {
  call<M extends keyof ClientResponses>(message: ClientCall<M>): Promise<ClientResponses[M]>;
  notify(message: ClientNotification): Promise<void>;
  reply<M extends keyof ApprovalResponses>(request: ApprovalCall<M>, result: ApprovalResponses[M]): Promise<void>;
  events(signal: AbortSignal): AsyncIterable<HarnessPush>;
  close(): Promise<void>;
}

export interface HarnessRoute {
  domainId: number;
  runId: string;
  role: "orchestrator" | "worker";
  modelProfileId: string;
  workdir: string;
  sandbox: "read-only" | "workspace-write" | "danger-full-access";
  approval: "user" | "guardian" | "never";
}

export interface HarnessGateway {
  connect(route: HarnessRoute, signal: AbortSignal): Promise<AppServerConnection>;
}

export type ExecutionMode = "codex_subscription" | "api_gateway";
export type RuntimeSelection =
  | { execution_mode: "codex_subscription"; subscription_model_id?: string; reasoning_effort?: string }
  | { execution_mode: "api_gateway"; model_profile_id: string; reasoning_effort?: string };

export interface SubscriptionModel {
  id: string;
  model: string;
  name: string;
  reasoning_efforts: string[];
  default_effort?: string;
  is_default: boolean;
}

export interface CodexLocalStatus {
  status: "unchecked" | "ready_subscription" | "conflict_apikey" | "unauthenticated" | "cli_missing" | "probe_failed";
  message: string;
  version: string;
  binary_path: string;
  catalog_source: string;
  version_notice: string;
  email?: string;
  plan?: string;
  models: SubscriptionModel[];
  catalog_error: string;
  login_pending: boolean;
}

export type WorkflowStage = "STAGE_1_DOMAIN" | "STAGE_2_TOPOLOGY" | "STAGE_3_ACQUISITION"
  | "STAGE_4_TASK_DESIGN" | "STAGE_5_VERIFIER" | "STAGE_6_EVAL_EXPORT";
export type ExecutionPace = "PAUSE_EACH_STAGE" | "AUTO_CONTINUE_QUEUE";

export interface ExecutionScopeContract {
  contract_id: string;
  stage_queue: readonly WorkflowStage[];
  execution_pace: ExecutionPace;
  stage_index: number;
  target_stage: WorkflowStage;
  next_stage: WorkflowStage | null;
  domain_id: string | null;
  granularity: "STAGE_WIDE" | "PROCESS_NODE_ONLY" | "TASK_ONLY" | "VERIFIER_ONLY";
  allowed_process_node_ids: readonly string[];
  allowed_task_ids: readonly string[];
  operation_mode: "APPEND_OR_PATCH" | "REBUILD_TARGET" | "READ_ONLY";
  router_confidence: number;
  rationale: string;
  ambiguity_prompt: string;
}

export type StageKey = "routing" | "stage_1" | "stage_2" | "stage_3" | "stage_4" | "stage_5" | "stage_6";
export type StageStatus = "running" | "completed" | "failed" | "needs_review"
  | "awaiting_scope" | "paused" | "cancelled" | "interrupted";
export interface StageEvent {
  kind: "thread_started" | "turn_started" | "progress" | "tool" | "reasoning"
    | "subagent" | "commandExecution" | "fileChange" | "compaction" | "stage_error";
  stage_key: StageKey;
  stage_label: string;
  agent_kind: "main" | "subagent";
  parent_turn_id: string | null;
  subagent_id: string | null;
  thread?: string;
  turn?: string;
  item?: string;
  label?: string;
  preview?: string;
  status?: string;
  duration_ms?: number;
  at?: string;
}

export interface StageError extends StageEvent {
  kind: "stage_error";
  message: string;
  code?: string | number;
  method?: string;
  http_status?: number;
  exit_code?: number | null;
  turn_status?: string;
  detail?: string;
  stderr?: string;
  retryable: boolean;
  suggested_stages?: readonly WorkflowStage[];
}

export interface StageTimelineEntry {
  id: string;
  stage_key: StageKey;
  stage_label: string;
  status: StageStatus;
  started_at: string | null;
  finished_at?: string;
  contract_id?: string;
  outcome?: string;
  summary?: string;
  events: readonly StageEvent[];
  activity?: StageEvent & { since: string; state: string; detail?: string };
  error?: StageError;
}

export type Channel = "C1_STANDARD_ORG" | "C2_OEM_FAB_SPEC" | "C3_TOOL_FORMAT_DOC" | "C4_EQUIP_MAT_VENDOR"
  | "C5_ASSOC_WHITEPAPER" | "C6_OPEN_ENG_REPO" | "C7_FAILURE_CASE";
export type ProcessScope = { domainId: number; nodeId: string };

export interface ProcessTopology {
  nodes: readonly {
    id: string;
    title: string;
    position: number;
    flow_group: string;
    lifecycle_stages: readonly LifecycleStage[];
    occupations: readonly string[];
    coverage: number;
    sources: number;
    verified: number;
    missing_t1_standard: boolean;
  }[];
  edges: readonly { upstream_id: string; downstream_id: string }[];
  flow_groups: readonly { id: string; title: string; node_ids: readonly string[] }[];
  node_links: readonly { node_id: string; canonical_id: string; reason: string; created_at: string }[];
  node_suggestions: Record<string, readonly string[]>;
}

export type FrontierTarget = { kind: "query"; value: string; expansion: "seed" | "synonym" | "long_tail" }
  | { kind: "url"; value: string };

export interface FrontierEntry {
  scope: ProcessScope;
  id: string;
  round: number;
  channel: Channel;
  target: FrontierTarget;
}

export type FrontierLease = FrontierEntry & { token: string; expiresAt: string; attempt: number };
export type FrontierOutcome = { kind: "done"; discoveries: readonly FrontierEntry[] }
  | { kind: "retry"; nextAttemptAt: string; error: string }
  | { kind: "blocked"; error: string };
export type CandidateAssessment = { kind: "eligible"; fingerprint: string } | { kind: "ineligible" };

export interface RoundMetrics {
  round: number;
  assessed: number;
  novel: number;
  noveltyRate: number;
  channels: readonly Channel[];
}

export type FrontierDecision = { kind: "continue"; reason: "pending" | "expansion" | "novelty" }
  | { kind: "blocked"; reason: "authorization" | "budget" | "provider" }
  | { kind: "exhausted"; evidence: readonly [RoundMetrics, RoundMetrics, RoundMetrics] };

export interface FrontierScheduler {
  openRound(scope: ProcessScope, seeds: readonly FrontierEntry[]): Promise<number>;
  lease(scope: ProcessScope, workerId: string): Promise<FrontierLease | null>;
  settle(lease: FrontierLease, outcome: FrontierOutcome): Promise<void>;
  assess(scope: ProcessScope, round: number, sourceId: string, result: CandidateAssessment): Promise<void>;
  closeRound(scope: ProcessScope, round: number): Promise<RoundMetrics>;
  decide(scope: ProcessScope): Promise<FrontierDecision>;
  recoverExpiredLeases(): Promise<number>;
}

export type ReferenceAuthority = "acceptance" | "standard" | "manufacturer" | "open_record" | "expert" | "research";
export type ReferenceClassification = (
  | { authority_tier: "T1_STANDARD"; source_channel: "C1_STANDARD_ORG" }
  | { authority_tier: "T2_MANUFACTURER"; source_channel: "C2_OEM_FAB_SPEC" | "C4_EQUIP_MAT_VENDOR" }
  | { authority_tier: "T3_TOOLCHAIN"; source_channel: "C3_TOOL_FORMAT_DOC" }
  | { authority_tier: "T4_CASE_SAMPLE"; source_channel: "C5_ASSOC_WHITEPAPER" | "C6_OPEN_ENG_REPO" | "C7_FAILURE_CASE" }
) & {
  process_node_ids: [string, ...string[]];
  object_cluster: string;
  lifecycle_stages: [LifecycleStage, ...LifecycleStage[]];
};
export type AuthorityTier = ReferenceClassification["authority_tier"];
export type LifecycleStage = "S0" | "S1" | "S2" | "S3" | "S4" | "S5" | "S6" | "S7" | "S8";
export type ProcessIdentity = { id: string; title: string; position: number };

export interface ReferenceIndexRecord {
  id: string;
  title: string;
  summary_zh: string;
  publisher: string;
  url: string;
  authority_tier: AuthorityTier;
  source_channel: Channel;
  process_node_ids: [string, ...string[]];
  process_names: string[];
  object_cluster: string | null;
  lifecycle_stages: LifecycleStage[];
  sha256: string | null;
  version: string | null;
  citations: number;
}

export interface ReferenceLibraryPage {
  items: ReferenceIndexRecord[];
  more: boolean;
  total: number;
  node: string;
  node_title: string;
  authority_tiers: { id: AuthorityTier; title: string; short_title: string; verified: number }[];
  channels: { id: Channel; title: string; verified: number }[];
  processes: ((ProcessIdentity | { id: "unassigned"; title: string; position?: never }) & {
    count: number;
    authority_counts: Partial<Record<AuthorityTier, number>>;
    missing_t1_standard: boolean;
  })[];
}
export type ClauseKind = "format" | "threshold" | "command" | "process" | "acceptance" | "failure_case";

export interface NormativeClause {
  id: string;
  kind: ClauseKind;
  locator: string;
  quote: string;
  requirement: string;
  check: string;
}

export interface ClauseKey {
  reference_id: string;
  clause_id: string;
}

export interface ClauseCitation extends ClauseKey {
  applies_to: string;
}

export interface VerifiedReferenceProfile {
  title: string;
  summary_zh: string;
  publisher_info: { name: string; authority: ReferenceAuthority; qualification: string };
  version_or_date: { version: string; published_on?: string } | { version?: string; published_on: string };
  applicable_scope: { applies_to: string; preconditions: string[] };
  limitations: string[];
  extracted_clauses: NormativeClause[];
}

export type ReferenceAdmission = VerifiedReferenceProfile & ReferenceClassification;

export interface ReferenceDetail extends Partial<VerifiedReferenceProfile> {
  id: string;
  quality_approved: boolean;
  summary_zh?: string;
  url: string;
  node_id?: string;
  authority_tier?: AuthorityTier | null;
  authority_tier_title?: string;
  source_channel?: Channel | null;
  source_channel_title?: string;
  process_node_ids?: string[];
  processes?: ProcessIdentity[];
  object_cluster?: string | null;
  lifecycle_stages?: LifecycleStage[];
  publisher?: string;
  stage?: string;
  sha256?: string;
  retrieved_at?: string;
  verified_at?: string;
  attachment_uri?: string;
  snapshot_uri?: string;
  quality_note?: string;
  task_usage: { id: string; title: string; citations: ClauseCitation[] }[];
  verifier_usage: { task_id: string; task_title: string; version: number; is_stale: boolean;
    layer: "L1" | "L2" | "L3" | "L4"; title: string; clause_id: string; applies_to: string }[];
}

export type BoundaryCondition = {
  parameter: string;
  limit: string;
  unit: string;
  precision: string;
  exceptions: string;
} & ({ basis: "case" } | ({ basis: "normative" } & ClauseKey));

export type EngineeringArtifact = {
  path: string;
  role: string;
  provenance: string;
  format: string;
  sha256: string;
  bytes: number;
} & ({ origin: "sandbox_fixture" | "supporting" } | { origin: "public_source"; source_id: string; url: string });

export interface QuantifiedEstimate {
  min: number;
  max: number;
  basis: string;
  source_status: "documented" | "estimate";
}

export interface IndustrialTaskDefinition {
  description: string;
  industrial_context: { upstream_input: string; responsibility: string; pain_point: string };
  applicable_standards: (ClauseCitation & { name: string; version: string })[];
  boundary_conditions: BoundaryCondition[];
  edge_cases_and_rejection: { condition: string; detection: string; disposition: string }[];
  downstream_handoff: { recipient: string; process: string; interface: string; acceptance: string };
  inputs?: Record<string, string>;
  input_artifacts: EngineeringArtifact[];
  deliverables: { path: string; format: string; interface: string; acceptance: string }[];
  value_assessment: {
    frequency: QuantifiedEstimate & { period: "shift" | "day" | "week" | "month" | "year" | "batch" };
    expert_minutes: QuantifiedEstimate;
    error_loss: QuantifiedEstimate & { currency: string; scenario: string };
    automation_value: { benefit: string; basis: string };
  };
  reference_citations: ClauseCitation[];
}

export interface TaskAdmission {
  id: string;
  title: string;
  ready: boolean;
  clause_count: number;
  issues: { stage: 2 | 3; message: string }[];
}
