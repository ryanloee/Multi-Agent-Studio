import type { WorkflowNode, WorkflowEdge, RunStatus } from "./workflow";

// ---------------------------------------------------------------------------
// Model types
// ---------------------------------------------------------------------------

/** Supported LLM providers */
export type ModelProvider =
  | "openai"
  | "anthropic"
  | "ollama"
  | "google"
  | "deepseek";

/** Model metadata returned by GET /api/models */
export interface ModelInfo {
  /** Unique model identifier, e.g. "gpt-4o" */
  id: string;
  /** Human-readable name */
  name: string;
  /** Which provider hosts this model */
  provider: ModelProvider;
  /** Maximum context window length in tokens */
  context_length: number;
  /** Maximum output tokens per request */
  max_tokens: number;
}

// ---------------------------------------------------------------------------
// Workflow API types
// ---------------------------------------------------------------------------

/** Summary item returned by GET /api/workflows */
export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
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
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  updated_at: string;
  created_at: string;
}

/** Request body for POST /api/workflows */
export interface CreateWorkflowRequest {
  name: string;
  description?: string;
}

/** Request body for PUT /api/workflows/:id */
export interface UpdateWorkflowRequest {
  name?: string;
  description?: string;
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

/** Response body for POST /api/workflows/:id/run */
export interface TriggerRunResponse {
  run_id: string;
  status: RunStatus;
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
