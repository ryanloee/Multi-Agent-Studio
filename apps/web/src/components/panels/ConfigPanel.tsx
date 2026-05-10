"use client";

import { useState, useCallback, useRef, useEffect, type ComponentType } from "react";
import {
  Code,
  Map,
  Search,
  Terminal,
  FileCheck,
  User,
  X,
  Trash2,
  Settings,
  FolderOpen,
  type LucideProps,
} from "lucide-react";
import type { AgentNodeType } from "@/types/workflow";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
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
  const removeNode = useWorkflowStore((s) => s.removeNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);
  const updateWorkspaceDirectory = useWorkflowStore((s) => s.updateWorkspaceDirectory);
  const mode = useWorkflowStore((s) => s.mode);
  const t = useLocaleStore((s) => s.t);

  // Local state for workspace directory input (allows debounced saving)
  const [localDir, setLocalDir] = useState(workspaceDirectory);

  // Sync local state when store value changes (e.g. on workflow load)
  useEffect(() => {
    setLocalDir(workspaceDirectory);
  }, [workspaceDirectory]);

  // Debounced save for workspace directory
  const dirTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleDirChange = useCallback((value: string) => {
    setLocalDir(value);
    if (dirTimerRef.current) clearTimeout(dirTimerRef.current);
    dirTimerRef.current = setTimeout(() => {
      updateWorkspaceDirectory(value);
    }, 800);
  }, [updateWorkspaceDirectory]);

  const node = nodes.find((n) => n.id === selectedNodeId);

  const handleClose = useCallback(() => setSelectedNode(null), [setSelectedNode]);
  const handleDelete = useCallback(() => {
    if (selectedNodeId) {
      removeNode(selectedNodeId);
    }
  }, [selectedNodeId, removeNode]);

  // Derived node data
  const data = node?.data;
  const nodeType = (node?.type ?? "coder") as AgentNodeType;
  const meta = NODE_META[nodeType];
  const features = FEATURES[nodeType];
  const IconComponent = ICON_MAP[meta.icon];
  const colors = COLOR_MAP[meta.color] ?? COLOR_MAP.gray;

  return (
    <div className="w-80 bg-white border-l border-gray-200 flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 shrink-0">
        <Settings size={14} className="text-gray-500" />
        <span className="text-xs font-semibold text-gray-700">{t("config.title") || "配置"}</span>
        <div className="flex-1" />
        <button
          onClick={handleClose}
          className="p-1 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label={t("config.closePanel")}
        >
          <X size={16} />
        </button>
      </div>

      {/* Content */}
      {selectedNodeId && node && data ? (
        <>
          {/* Node header */}
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
            {mode !== "auto" && (
              <button
                onClick={handleDelete}
                className="p-1.5 rounded-lg hover:bg-red-50 text-gray-400 hover:text-red-500 transition-colors"
                aria-label={t("config.deleteNode")}
              >
                <Trash2 size={16} />
              </button>
            )}
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
                disabled={mode === "auto"}
                className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50 disabled:cursor-not-allowed"
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
                value={data.modelProvider && data.modelId ? `${data.modelProvider}/${data.modelId}` : ""}
                onChange={(fullId) => {
                  const slash = fullId.indexOf("/");
                  if (slash >= 0) {
                    updateNodeData(node.id, {
                      modelProvider: fullId.slice(0, slash),
                      modelId: fullId.slice(slash + 1),
                    });
                  } else {
                    updateNodeData(node.id, { modelProvider: "", modelId: fullId });
                  }
                }}
                disabled={mode === "auto"}
              />
            )}

            {/* Prompt */}
            {features.prompt && (
              <PromptEditor
                value={data.prompt}
                onChange={(prompt) => updateNodeData(node.id, { prompt })}
                disabled={mode === "auto"}
              />
            )}

            {/* Permissions */}
            {features.permissions && (
              <PermissionsEditor
                value={data.permissions}
                onChange={(permissions) => updateNodeData(node.id, { permissions })}
                disabled={mode === "auto"}
              />
            )}

            {/* Command */}
            {features.command && (
              <CommandEditor
                value={data.command}
                onChange={(command) => updateNodeData(node.id, { command })}
                disabled={mode === "auto"}
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
                  disabled={mode === "auto"}
                  className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                />
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
          {/* Workflow Settings header */}
          <div className="flex items-center gap-3 px-0 py-2">
            <div className="w-8 h-8 rounded-lg bg-gray-50 flex items-center justify-center">
              <FolderOpen size={16} className="text-gray-500" />
            </div>
            <div className="flex-1 min-w-0">
              <span className="text-sm font-semibold text-gray-800 block">
                {t("config.workflowSettings")}
              </span>
            </div>
          </div>

          {/* Workspace Directory input */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {t("config.workspaceDirectory")}
            </label>
            <input
              type="text"
              value={localDir}
              onChange={(e) => handleDirChange(e.target.value)}
              placeholder={t("config.workspaceDirectoryPlaceholder")}
              disabled={mode === "auto"}
              className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            />
          </div>

          <p className="text-xs text-gray-400 text-center mt-4">
            {t("config.selectNodeHint") || "Select a node on the canvas to edit its configuration"}
          </p>
        </div>
      )}
    </div>
  );
}
