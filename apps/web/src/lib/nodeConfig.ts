import type { ComponentType } from "react";
import {
  Code,
  Map,
  Search,
  Terminal,
  FileCheck,
  GitMerge,
  User,
  type LucideProps,
} from "lucide-react";
import type { AgentNodeType, WorkerAgentType } from "@/types/workflow";

export const ICON_MAP: Record<string, ComponentType<LucideProps>> = {
  Code, Map, Search, Terminal, FileCheck, GitMerge, User,
};

export const COLOR_MAP: Record<string, { bg: string; text: string }> = {
  blue: { bg: "bg-blue-50", text: "text-blue-500" },
  green: { bg: "bg-emerald-50", text: "text-emerald-500" },
  yellow: { bg: "bg-amber-50", text: "text-amber-500" },
  gray: { bg: "bg-gray-100", text: "text-gray-500" },
  purple: { bg: "bg-purple-50", text: "text-purple-500" },
  orange: { bg: "bg-orange-50", text: "text-orange-500" },
  teal: { bg: "bg-teal-50", text: "text-teal-500" },
};

export interface FeatureFlags {
  agentType: boolean;
  model: boolean;
  prompt: boolean;
  permissions: boolean;
  command: boolean;
  description: boolean;
}

export const FEATURES: Record<AgentNodeType, FeatureFlags> = {
  coder:   { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  plan:    { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  design:  { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  explore: { agentType: true,  model: true,  prompt: true,  permissions: false, command: false, description: false },
  merge:   { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  shell:   { agentType: false, model: false, prompt: false, permissions: false, command: true,  description: false },
  review:  { agentType: true,  model: true,  prompt: true,  permissions: false, command: false, description: false },
  human:   { agentType: false, model: false, prompt: false, permissions: false, command: false, description: true  },
};

export const CHILD_MODEL_TYPES: WorkerAgentType[] = [
  "design", "explore", "coder", "merge", "review", "shell",
];
