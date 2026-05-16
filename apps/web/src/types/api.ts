import type {
  WorkflowNode,
  WorkflowEdge,
  RunStatus,
  AutoChildModelMap,
  WorkflowLifecyclePhase,
  WorkflowBlocker,
} from "./workflow";

// ---------------------------------------------------------------------------
// Model types
// ---------------------------------------------------------------------------

/** Supported LLM providers — dynamically sourced from config */
export type ModelProvider = string;

/** Model metadata returned by GET /api/models */
export interface ModelInfo {
  /** Model ID within provider, e.g. "minimax-m2.5-free" */
  id: string;
  /** Full ID in provider/model format, e.g. "opencode/minimax-m2.5-free" */
  full_id: string;
  /** Human-readable name */
  name: string;
  /** Which provider hosts this model */
  provider: string;
  /** Display label for the provider */
  provider_label?: string;
  /** Maximum context window length in tokens */
  context_length?: number;
  /** Maximum output tokens per request */
  max_tokens?: number;
  /** Whether this model is free to use */
  free?: boolean;
}

// ---------------------------------------------------------------------------
// Workflow API types
// ---------------------------------------------------------------------------

/** Summary item returned by GET /api/workflows */
export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  /** Workflow mode: always "auto" — Planner builds the workflow */
  mode?: string;
  lifecycle_phase?: WorkflowLifecyclePhase;
  /** ISO timestamp of last modification */
  updated_at: string;
  /** ISO timestamp of creation */
  created_at: string;
}

/** Full workflow detail returned by GET /api/workflows/:id */
export interface WorkflowDetail {
  id: string;
  name: string;
  description: string;
  workspace_directory?: string;
  /** Workflow mode: always "auto" — Planner builds the workflow */
  mode?: string;
  /** Natural-language goal for auto-mode workflows */
  goal?: string;
  lifecycle_phase?: WorkflowLifecyclePhase;
  blockers?: WorkflowBlocker[];
  project_summary?: Record<string, unknown>;
  project_summary_artifact_id?: string | null;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
    planner_ui_state?: PlannerUiState;
    planner_draft_state?: PlannerDraftStructuredState;
  };
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  updated_at: string;
  created_at: string;
}

/** Request body for POST /api/workflows */
export interface CreateWorkflowRequest {
  name: string;
  description?: string;
  workspace_directory?: string;
  /** Workflow mode: always "auto" */
  mode?: "auto";
  /** Natural-language goal for auto-mode workflows */
  goal?: string;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
    planner_ui_state?: PlannerUiState;
    planner_draft_state?: PlannerDraftStructuredState;
  };
}

/** Request body for PUT /api/workflows/:id */
export interface UpdateWorkflowRequest {
  name?: string;
  description?: string;
  workspace_directory?: string;
  mode?: "auto";
  goal?: string;
  lifecycle_phase?: WorkflowLifecyclePhase;
  blockers?: WorkflowBlocker[];
  project_summary?: Record<string, unknown>;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
    planner_ui_state?: PlannerUiState;
    planner_draft_state?: PlannerDraftStructuredState;
  };
  nodes?: WorkflowNode[];
  edges?: WorkflowEdge[];
}

export interface PlannerUiTaskObject {
  title?: string;
  objective?: string;
  background?: string;
  constraints?: string[];
  success_criteria?: string[];
  assumptions?: string[];
  open_questions?: string[];
}

export interface PlannerUiTaskItem {
  id: string;
  title: string;
  description?: string;
  node_id?: string;
  status?: "planned" | "ready";
  depends_on?: string[];
}

export interface PlannerUiState {
  task_object?: PlannerUiTaskObject;
  task_board?: PlannerUiTaskItem[];
  updated_at?: string;
}

export type PlannerStage =
  | "plan_outline"
  | "fill_task_context"
  | "fill_dag"
  | "fill_task_board"
  | "finalize_ready";

export interface PlannerStageHistoryItem {
  stage: PlannerStage;
  status: "started" | "completed" | "retrying" | "fallback" | "failed";
  attempt: number;
  summary?: string;
  timestamp?: string;
}

export interface PlannerDraftStructuredState {
  current_stage?: PlannerStage;
  lifecycle_phase?: string;
  task_object?: PlannerUiTaskObject;
  project_summary?: Record<string, unknown>;
  shared_doc?: string;
  task_board?: PlannerUiTaskItem[];
  dag?: {
    nodes: Array<Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
    metadata?: Record<string, unknown>;
  };
  blockers?: WorkflowBlocker[];
  action?: PlannerStructuredAction | null;
  system_generated_dag?: boolean;
  updated_at?: string;
}

export interface PlannerStructuredAction {
  action: "clarify" | "assess" | "update_dag" | "set_ready" | "report_blocker";
  message?: string;
  ui_state?: PlannerUiState;
  dag?: {
    nodes: Array<Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
  };
  blockers?: WorkflowBlocker[];
  assess_request?: {
    scope?: "project" | "current_module" | "selected_path";
    paths?: string[];
  };
}

// ---------------------------------------------------------------------------
// Run API types
// ---------------------------------------------------------------------------

/** Run summary returned by GET /api/runs and GET /api/runs/:id */
export interface RunInfo {
  id: string;
  workflow_id: string;
  status: RunStatus;
  /** ISO timestamp when the run was created */
  created_at: string;
  /** ISO timestamp when the run finished (nullable if still running) */
  completed_at: string | null;
}

/** Response body for POST /api/runs/:id/run */
export interface TriggerRunResponse {
  id: string;
  workflow_id: string;
  status: string;
  engine_workflow_id?: string;
  created_at: string;
  completed_at?: string | null;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/** Standard error shape returned by the API on failures */
export interface ApiError {
  /** HTTP status code */
  status: number;
  /** Machine-readable error code */
  code: string;
  /** Human-readable error message */
  message: string;
  /** Additional error details */
  details?: Record<string, unknown>;
}
