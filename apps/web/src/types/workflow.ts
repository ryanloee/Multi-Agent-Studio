import type { Node, Edge } from "@xyflow/react";

// ---------------------------------------------------------------------------
// Agent Node Types — the 6 supported node kinds in a workflow canvas
// ---------------------------------------------------------------------------
export type AgentNodeType =
  | "coder"
  | "plan"
  | "explore"
  | "shell"
  | "review"
  | "human";

// ---------------------------------------------------------------------------
// Node Data — the business data stored inside each workflow node
// ---------------------------------------------------------------------------
export interface NodeData {
  /** Index signature required for @xyflow/react v12 Node data compatibility (Record<string, unknown>) */
  [key: string]: unknown;
  /** Human-readable label shown on the node */
  label: string;
  /** Which agent type drives this node (mirrors AgentNodeType) */
  agentType: AgentNodeType;
  /** Model provider, e.g. "openai", "anthropic", "ollama" */
  modelProvider: string;
  /** Specific model identifier, e.g. "gpt-4o", "claude-sonnet-4-20250514" */
  modelId: string;
  /** System / user prompt fed to the agent */
  prompt: string;
  /** Tool-level permission map: tool name → "allow" | "deny" | "ask" */
  permissions: Record<string, "allow" | "deny" | "ask">;
  /** Shell command (only relevant for shell nodes) */
  command: string;
  /** Free-text description (used primarily for human nodes) */
  description: string;
}

// ---------------------------------------------------------------------------
// Workflow Node & Edge — typed wrappers around @xyflow/react primitives
// ---------------------------------------------------------------------------
export type WorkflowNode = Node<NodeData, AgentNodeType>;
export type WorkflowEdge = Edge;

// ---------------------------------------------------------------------------
// Run Status — lifecycle states of a workflow execution
// ---------------------------------------------------------------------------
export type RunStatus =
  | "idle"
  | "running"
  | "paused"
  | "completed"
  | "failed";

// ---------------------------------------------------------------------------
// Connection Rule — describes which source node types may connect to which
// target node types. Used by VALID_CONNECTIONS in constants.ts.
// ---------------------------------------------------------------------------
export interface ConnectionRule {
  source: AgentNodeType;
  target: AgentNodeType;
}
