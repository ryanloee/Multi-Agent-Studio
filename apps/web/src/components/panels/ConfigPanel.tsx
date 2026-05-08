"use client";

import { useCallback, type ComponentType } from "react";
import {
  Code,
  Map,
  Search,
  Terminal,
  FileCheck,
  User,
  X,
  type LucideProps,
} from "lucide-react";
import type { AgentNodeType } from "@/types/workflow";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { NODE_META } from "@/lib/constants";
import ModelSelector from "./ModelSelector";
import PromptEditor from "./PromptEditor";
import PermissionsEditor from "./PermissionsEditor";
import CommandEditor from "./CommandEditor";

const ICON_MAP: Record<string, ComponentType<LucideProps>> = {
  Code, Map, Search, Terminal, FileCheck, User,
};

const COLOR_MAP: Record<string, { bg: string; text: string }> = {
  blue: { bg: "bg-blue-50", text: "text-blue-500" },
  green: { bg: "bg-emerald-50", text: "text-emerald-500" },
  yellow: { bg: "bg-amber-50", text: "text-amber-500" },
  gray: { bg: "bg-gray-100", text: "text-gray-500" },
  purple: { bg: "bg-purple-50", text: "text-purple-500" },
  orange: { bg: "bg-orange-50", text: "text-orange-500" },
};

interface FeatureFlags {
  agentType: boolean;
  model: boolean;
  prompt: boolean;
  permissions: boolean;
  command: boolean;
  description: boolean;
}

const FEATURES: Record<AgentNodeType, FeatureFlags> = {
  coder:   { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  plan:    { agentType: true,  model: true,  prompt: true,  permissions: true,  command: false, description: false },
  explore: { agentType: true,  model: true,  prompt: true,  permissions: false, command: false, description: false },
  shell:   { agentType: false, model: false, prompt: false, permissions: false, command: true,  description: false },
  review:  { agentType: true,  model: true,  prompt: true,  permissions: false, command: false, description: false },
  human:   { agentType: false, model: false, prompt: false, permissions: false, command: false, description: true  },
};

export default function ConfigPanel() {
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const t = useLocaleStore((s) => s.t);

  const node = nodes.find((n) => n.id === selectedNodeId);

  const handleClose = useCallback(() => setSelectedNode(null), [setSelectedNode]);

  if (!selectedNodeId || !node) return null;

  const { data } = node;
  const nodeType = node.type as AgentNodeType;
  const meta = NODE_META[nodeType];
  const features = FEATURES[nodeType];
  const IconComponent = ICON_MAP[meta.icon];
  const colors = COLOR_MAP[meta.color] ?? COLOR_MAP.gray;

  return (
    <div className="w-80 bg-white border-l border-gray-200 flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100">
        <div className={`w-8 h-8 rounded-lg ${colors.bg} flex items-center justify-center`}>
          {IconComponent && <IconComponent size={16} className={colors.text} />}
        </div>
        <div className="flex-1 min-w-0">
          <span className="text-sm font-semibold text-gray-800 block truncate">
            {t(`node.${nodeType}.label`)}
          </span>
          <span className="text-xs text-gray-400 block truncate">
            {t(`node.${nodeType}.description`)}
          </span>
        </div>
        <button
          onClick={handleClose}
          className="p-1.5 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label={t("config.closePanel")}
        >
          <X size={16} />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
        {/* Label */}
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.label")}
          </label>
          <input
            type="text"
            value={data.label}
            onChange={(e) => updateNodeData(node.id, { label: e.target.value })}
            className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all"
          />
        </div>

        {/* Agent Type */}
        {features.agentType && (
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {t("config.agentType")}
            </label>
            <div className={`w-full rounded-lg border border-gray-200 ${colors.bg} px-3 py-2 text-sm text-gray-600 flex items-center gap-2`}>
              {IconComponent && <IconComponent size={14} className={colors.text} />}
              {t(`node.${nodeType}.label`)}
            </div>
          </div>
        )}

        {/* Model */}
        {features.model && (
          <ModelSelector
            value={data.modelId}
            onChange={(modelId) => updateNodeData(node.id, { modelId })}
          />
        )}

        {/* Prompt */}
        {features.prompt && (
          <PromptEditor
            value={data.prompt}
            onChange={(prompt) => updateNodeData(node.id, { prompt })}
          />
        )}

        {/* Permissions */}
        {features.permissions && (
          <PermissionsEditor
            value={data.permissions}
            onChange={(permissions) => updateNodeData(node.id, { permissions })}
          />
        )}

        {/* Command */}
        {features.command && (
          <CommandEditor
            value={data.command}
            onChange={(command) => updateNodeData(node.id, { command })}
          />
        )}

        {/* Description (human node) */}
        {features.description && (
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {t("config.description")}
            </label>
            <textarea
              value={data.description}
              onChange={(e) => updateNodeData(node.id, { description: e.target.value })}
              placeholder={t("config.descriptionPlaceholder")}
              rows={4}
              className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all"
            />
          </div>
        )}
      </div>
    </div>
  );
}
