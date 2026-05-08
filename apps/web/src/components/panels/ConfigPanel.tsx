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

// ---------------------------------------------------------------------------
// Icon map — matches NODE_META.icon strings to Lucide components
// ---------------------------------------------------------------------------
const ICON_MAP: Record<string, ComponentType<LucideProps>> = {
  Code,
  Map,
  Search,
  Terminal,
  FileCheck,
  User,
};

// ---------------------------------------------------------------------------
// Colour map — maps NODE_META.color to a Tailwind text-color class
// ---------------------------------------------------------------------------
const COLOR_MAP: Record<string, string> = {
  blue: "text-blue-500",
  green: "text-green-500",
  yellow: "text-yellow-500",
  gray: "text-gray-500",
  purple: "text-purple-500",
  orange: "text-orange-500",
};

// ---------------------------------------------------------------------------
// Feature flags per node type (derived from the Step 6 config matrix)
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// ConfigPanel — right-side configuration panel (320px wide)
// ---------------------------------------------------------------------------
export default function ConfigPanel() {
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);

  const t = useLocaleStore((s) => s.t);

  const node = nodes.find((n) => n.id === selectedNodeId);

  const handleClose = useCallback(() => {
    setSelectedNode(null);
  }, [setSelectedNode]);

  // Nothing selected — don't render
  if (!selectedNodeId || !node) return null;

  const { data } = node;
  const nodeType = node.type as AgentNodeType;
  const meta = NODE_META[nodeType];
  const features = FEATURES[nodeType];
  const IconComponent = ICON_MAP[meta.icon];
  const colorClass = COLOR_MAP[meta.color] ?? "text-gray-500";

  return (
    <div className="w-80 bg-white border-l border-gray-200 flex flex-col h-full overflow-hidden">
      {/* ---- Header ---- */}
      <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
        {IconComponent && (
          <IconComponent size={18} className={colorClass} />
        )}
        <span className="text-sm font-medium text-gray-700 flex-1 truncate">
          {t(`node.${nodeType}.label`)}
        </span>
        <button
          onClick={handleClose}
          className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label={t("config.closePanel")}
        >
          <X size={16} />
        </button>
      </div>

      {/* ---- Scrollable body ---- */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
        {/* Label — always shown */}
        <div className="space-y-1">
          <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
            {t("config.label")}
          </label>
          <input
            type="text"
            value={data.label}
            onChange={(e) =>
              updateNodeData(node.id, { label: e.target.value })
            }
            className="w-full rounded-md border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none"
          />
        </div>

        {/* Agent Type — conditionally shown */}
        {features.agentType && (
          <div className="space-y-1">
            <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
              {t("config.agentType")}
            </label>
            <div className="w-full rounded-md border border-gray-200 bg-gray-50 px-3 py-1.5 text-sm text-gray-500">
              {t(`node.${nodeType}.label`)}
            </div>
          </div>
        )}

        {/* Model selector — conditionally shown */}
        {features.model && (
          <ModelSelector
            value={data.modelId}
            onChange={(modelId) => updateNodeData(node.id, { modelId })}
          />
        )}

        {/* Prompt editor — conditionally shown */}
        {features.prompt && (
          <PromptEditor
            value={data.prompt}
            onChange={(prompt) => updateNodeData(node.id, { prompt })}
          />
        )}

        {/* Permissions — conditionally shown */}
        {features.permissions && (
          <PermissionsEditor
            value={data.permissions}
            onChange={(permissions) =>
              updateNodeData(node.id, { permissions })
            }
          />
        )}

        {/* Command — conditionally shown */}
        {features.command && (
          <CommandEditor
            value={data.command}
            onChange={(command) => updateNodeData(node.id, { command })}
          />
        )}

        {/* Description — conditionally shown (human node) */}
        {features.description && (
          <div className="space-y-1">
            <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide">
              {t("config.description")}
            </label>
            <textarea
              value={data.description}
              onChange={(e) =>
                updateNodeData(node.id, { description: e.target.value })
              }
              placeholder={t("config.descriptionPlaceholder")}
              rows={4}
              className="w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-1 focus:ring-blue-400 focus:outline-none"
            />
          </div>
        )}
      </div>
    </div>
  );
}
