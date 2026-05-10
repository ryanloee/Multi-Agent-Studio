import type {
  AgentNodeType,
  NodeData,
  RunStatus,
  ConnectionRule,
} from "@/types/workflow";

// ---------------------------------------------------------------------------
// NODE_META — metadata for each of the 6 agent node types
// ---------------------------------------------------------------------------
export const NODE_META: Record<
  AgentNodeType,
  {
    icon: string;
    color: string;
    label: string;
    description: string;
    defaultData: Partial<NodeData>;
  }
> = {
  coder: {
    icon: "Code",
    color: "blue",
    label: "Coder",
    description: "Writes and modifies code files",
    defaultData: {
      label: "Coder",
      agentType: "coder",
      modelProvider: "mimo",
      modelId: "mimo-v2.5",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
  plan: {
    icon: "Map",
    color: "green",
    label: "Planner",
    description: "Analyses tasks and creates execution plans",
    defaultData: {
      label: "Planner",
      agentType: "plan",
      modelProvider: "mimo",
      modelId: "mimo-v2.5",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
  explore: {
    icon: "Search",
    color: "yellow",
    label: "Explorer",
    description: "Searches codebase and gathers information",
    defaultData: {
      label: "Explorer",
      agentType: "explore",
      modelProvider: "mimo",
      modelId: "mimo-v2.5",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
  shell: {
    icon: "Terminal",
    color: "gray",
    label: "Shell",
    description: "Executes shell commands",
    defaultData: {
      label: "Shell",
      agentType: "shell",
      modelProvider: "",
      modelId: "",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
  review: {
    icon: "FileCheck",
    color: "purple",
    label: "Reviewer",
    description: "Reviews code changes and provides feedback",
    defaultData: {
      label: "Reviewer",
      agentType: "review",
      modelProvider: "mimo",
      modelId: "mimo-v2.5",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
  human: {
    icon: "User",
    color: "orange",
    label: "Human",
    description: "Pauses for human approval or input",
    defaultData: {
      label: "Human",
      agentType: "human",
      modelProvider: "",
      modelId: "",
      prompt: "",
      permissions: {},
      command: "",
      description: "",
    },
  },
};

// ---------------------------------------------------------------------------
// VALID_CONNECTIONS — whitelist of allowed source→target node connections
//
// Rules:
//   - Most agent nodes can connect to each other
//   - shell cannot connect to review
//   - review cannot connect to shell
//   - human can only be a target (sink), never a source
//   - Any node can target human
// ---------------------------------------------------------------------------
export const VALID_CONNECTIONS: ConnectionRule[] = [
  // coder → *
  { source: "coder", target: "coder" },
  { source: "coder", target: "plan" },
  { source: "coder", target: "explore" },
  { source: "coder", target: "shell" },
  { source: "coder", target: "review" },
  { source: "coder", target: "human" },

  // plan → *
  { source: "plan", target: "coder" },
  { source: "plan", target: "plan" },
  { source: "plan", target: "explore" },
  { source: "plan", target: "shell" },
  { source: "plan", target: "review" },
  { source: "plan", target: "human" },

  // explore → *
  { source: "explore", target: "coder" },
  { source: "explore", target: "plan" },
  { source: "explore", target: "explore" },
  { source: "explore", target: "shell" },
  { source: "explore", target: "review" },
  { source: "explore", target: "human" },

  // shell → * (no shell→review)
  { source: "shell", target: "coder" },
  { source: "shell", target: "plan" },
  { source: "shell", target: "explore" },
  { source: "shell", target: "shell" },
  { source: "shell", target: "human" },

  // review → * (no review→shell)
  { source: "review", target: "coder" },
  { source: "review", target: "plan" },
  { source: "review", target: "explore" },
  { source: "review", target: "review" },
  { source: "review", target: "human" },

  // human is sink-only — no outgoing connections
];

// ---------------------------------------------------------------------------
// STATUS_COLORS — maps RunStatus values to Tailwind CSS class strings
// ---------------------------------------------------------------------------
export const STATUS_COLORS: Record<RunStatus, string> = {
  idle: "bg-gray-200 text-gray-700",
  pending: "bg-yellow-100 text-yellow-700",
  running: "bg-blue-100 text-blue-700 animate-pulse",
  paused: "bg-yellow-100 text-yellow-700",
  cancelling: "bg-orange-100 text-orange-700",
  cancelled: "bg-gray-100 text-gray-500",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
};
