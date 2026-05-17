// ---------------------------------------------------------------------------
// Stream Event Types — aligned with shared-types/schemas/events.json
// ---------------------------------------------------------------------------

/** All possible stream event type discriminators */
export type StreamEventType =
  | "llm_token"
  | "llm_chunk"
  | "tool_call"
  | "tool_result"
  | "shell_stdout"
  | "shell_stderr"
  | "status"
  | "error"
  | "run_started"
  | "run_completed"
  | "run_failed"
  | "node_started"
  | "node_completed"
  | "node_failed"
  | "child_created"
  | "child_completed"
  | "task_created"
  | "task_updated"
  | "task_message"
  | "worker_message"
  | "artifact_created"
  | "progress_summary"
  | "agent_heartbeat"
  | "agent_status"
  | "idle_warning"
  | "permission_request"
  | "director_decision"
  | "node_paused"
  | "node_approved"
  | "node_rejected"
  | "ping";

// ---------------------------------------------------------------------------
// Base event shape — every event carries these fields
// ---------------------------------------------------------------------------
export interface StreamEvent {
  /** Stable persisted/delivered event ID, when provided by the backend */
  event_id?: string;
  /** Event type discriminator */
  type: StreamEventType;
  /** Which workflow run this event belongs to */
  run_id: string;
  /** Which node emitted this event (empty string for run-level events) */
  node_id: string;
  /** Unix timestamp (seconds or milliseconds, server-defined) */
  timestamp: number;
}

// ---------------------------------------------------------------------------
// Concrete event types
// ---------------------------------------------------------------------------

/** A single LLM token (incremental streaming) */
export interface LLMTokenEvent extends StreamEvent {
  type: "llm_token";
  /** The token text fragment */
  content: string;
}

/** A complete LLM chunk (batched output) */
export interface LLMChunkEvent extends StreamEvent {
  type: "llm_chunk";
  /** Full text chunk */
  content: string;
  metadata?: Record<string, unknown>;
}

/** Agent is invoking a tool */
export interface ToolCallEvent extends StreamEvent {
  type: "tool_call";
  /** Name of the tool being called */
  tool_name: string;
  /** Serialized arguments or description */
  content: string;
  metadata?: Record<string, unknown>;
}

/** Tool execution result */
export interface ToolResultEvent extends StreamEvent {
  type: "tool_result";
  /** Name of the tool that was called */
  tool_name: string;
  /** Tool output */
  content: string;
  metadata?: Record<string, unknown>;
}

/** Shell stdout output line */
export interface ShellStdoutEvent extends StreamEvent {
  type: "shell_stdout";
  /** stdout text */
  content: string;
}

/** Shell stderr output line */
export interface ShellStderrEvent extends StreamEvent {
  type: "shell_stderr";
  /** stderr text */
  content: string;
}

/** Status change event (run-level or node-level) */
export interface StatusEvent extends StreamEvent {
  type: "status";
  /** New status value, e.g. "running", "completed", "failed", "paused" */
  content: string;
}

/** Error event */
export interface ErrorEvent extends StreamEvent {
  type: "error";
  /** Error message */
  content: string;
  metadata?: Record<string, unknown>;
}

/** Backend heartbeat emitted while a node is still alive but silent */
export interface AgentHeartbeatEvent extends StreamEvent {
  type: "agent_heartbeat";
  content: string;
  idle_seconds?: number;
  poll_count?: number;
}

/** Real-time agent status from opencode (busy/idle/retry) */
export interface AgentStatusEvent extends StreamEvent {
  type: "agent_status";
  content: string;
  status_type: "busy" | "idle" | "retry" | string;
}

/** Progressive idle warning before timeout */
export interface IdleWarningEvent extends StreamEvent {
  type: "idle_warning";
  content: string;
  idle_seconds?: number;
  timeout_seconds?: number;
  threshold_pct?: number;
}

/** Tool permission request. Usually avoided in autonomous runs. */
export interface PermissionRequestEvent extends StreamEvent {
  type: "permission_request";
  request_id: string;
  permission: string;
  target: string;
  tool_name: string;
  arguments?: Record<string, unknown>;
}

/** Run has started */
export interface RunStartedEvent extends StreamEvent {
  type: "run_started";
  node_id: "";
}

/** Run has completed successfully */
export interface RunCompletedEvent extends StreamEvent {
  type: "run_completed";
  node_id: "";
  content?: string;
}

/** Run has failed */
export interface RunFailedEvent extends StreamEvent {
  type: "run_failed";
  node_id: "";
  content: string;
}

/** A specific node has started executing */
export interface NodeStartedEvent extends StreamEvent {
  type: "node_started";
}

/** A specific node has finished executing */
export interface NodeCompletedEvent extends StreamEvent {
  type: "node_completed";
  content?: string;
}

/** A specific node has failed */
export interface NodeFailedEvent extends StreamEvent {
  type: "node_failed";
  content: string;
}

/** Planner dynamically created a child node */
export interface ChildCreatedEvent extends StreamEvent {
  type: "child_created";
  /** The newly created child node's ID */
  child_node_id: string;
  /** The agent type of the child node */
  child_type: string;
  /** The prompt assigned to the child */
  child_prompt: string;
  /** The model assigned to the child */
  child_model: string;
}

/** A dynamically created child node has completed */
export interface ChildCompletedEvent extends StreamEvent {
  type: "child_completed";
  /** The child node that completed */
  child_node_id: string;
  /** Summary of the child's output */
  content?: string;
}

/** Progress summary after DAG layer execution */
export interface ProgressSummaryEvent extends StreamEvent {
  type: "progress_summary";
  total: number;
  completed: number;
  failed: number;
}

export interface ArtifactCreatedEvent extends StreamEvent {
  type: "artifact_created";
  artifact_id: string;
  task_id?: string;
  artifact_type: string;
  title: string;
}

/** Director agent made a dispatch decision */
export interface DirectorDecisionEvent extends StreamEvent {
  type: "director_decision";
  node_id: "director";
  action: "scout" | "worker" | "test" | "done" | "failed";
  reasoning: string;
  task_id: string;
  target_files?: string[];
  iteration?: number;
}

// ---------------------------------------------------------------------------
// Event map — maps type discriminator to the concrete event interface
// ---------------------------------------------------------------------------
export interface StreamEventMap {
  llm_token: LLMTokenEvent;
  llm_chunk: LLMChunkEvent;
  tool_call: ToolCallEvent;
  tool_result: ToolResultEvent;
  shell_stdout: ShellStdoutEvent;
  shell_stderr: ShellStderrEvent;
  status: StatusEvent;
  error: ErrorEvent;
  run_started: RunStartedEvent;
  run_completed: RunCompletedEvent;
  run_failed: RunFailedEvent;
  node_started: NodeStartedEvent;
  node_completed: NodeCompletedEvent;
  node_failed: NodeFailedEvent;
  child_created: ChildCreatedEvent;
  child_completed: ChildCompletedEvent;
  task_created: StreamEvent;
  task_updated: StreamEvent;
  task_message: StreamEvent;
  worker_message: StreamEvent;
  artifact_created: ArtifactCreatedEvent;
  progress_summary: ProgressSummaryEvent;
  agent_heartbeat: AgentHeartbeatEvent;
  agent_status: AgentStatusEvent;
  idle_warning: IdleWarningEvent;
  permission_request: PermissionRequestEvent;
  director_decision: DirectorDecisionEvent;
}
