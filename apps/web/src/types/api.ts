import type { WorkflowNode, WorkflowEdge, RunStatus, AutoChildModelMap } from "./workflow";

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
  /** Workflow mode: "auto" lets a Planner build the workflow, "manual" is user-designed */
  mode?: string;
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
  /** Workflow mode: "auto" lets a Planner build the workflow, "manual" is user-designed */
  mode?: string;
  /** Natural-language goal for auto-mode workflows */
  goal?: string;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
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
  /** Workflow mode: "auto" lets a Planner build the workflow, "manual" is user-designed */
  mode?: "auto" | "manual";
  /** Natural-language goal for auto-mode workflows */
  goal?: string;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
  };
}

/** Request body for PUT /api/workflows/:id */
export interface UpdateWorkflowRequest {
  name?: string;
  description?: string;
  workspace_directory?: string;
  mode?: "auto" | "manual";
  goal?: string;
  metadata?: {
    auto_child_model_map?: AutoChildModelMap;
  };
  nodes?: WorkflowNode[];
  edges?: WorkflowEdge[];
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
