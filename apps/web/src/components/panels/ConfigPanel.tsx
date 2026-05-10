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
  ArrowRight,
  Target,
  PenLine,
  type LucideProps,
} from "lucide-react";
import type { AgentNodeType, EdgeData, WorkflowNode, WorkflowEdge } from "@/types/workflow";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { NODE_META } from "@/lib/constants";
import ModelSelector from "./ModelSelector";
import PromptEditor from "./PromptEditor";
import PermissionsEditor from "./PermissionsEditor";
import CommandEditor from "./CommandEditor";
import DirectoryPicker from "@/components/common/DirectoryPicker";

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
  const selectedEdgeId = useWorkflowStore((s) => s.selectedEdgeId);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);
  const updateEdgeData = useWorkflowStore((s) => s.updateEdgeData);
  const removeNode = useWorkflowStore((s) => s.removeNode);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const setSelectedEdge = useWorkflowStore((s) => s.setSelectedEdge);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);
  const updateWorkspaceDirectory = useWorkflowStore((s) => s.updateWorkspaceDirectory);
  const mode = useWorkflowStore((s) => s.mode);
  const goal = useWorkflowStore((s) => s.goal);
  const updateGoal = useWorkflowStore((s) => s.updateGoal);
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

  const handleClose = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdge(null);
  }, [setSelectedNode, setSelectedEdge]);
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
      {selectedEdgeId ? (
        /* ---- Edge Configuration ---- */
        <EdgeConfigSection
          edgeId={selectedEdgeId}
          edges={edges}
          nodes={nodes}
          updateEdgeData={updateEdgeData}
          t={t}
          mode={mode}
          onClose={handleClose}
        />
      ) : selectedNodeId && node && data ? (
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
                  // The value from ModelSelector is "format/base_url/model_name"
                  // We only store format as modelProvider and model_name as modelId
                  // The base_url and api_key are resolved from settings at runtime
                  const parts = fullId.split("/");
                  if (parts.length >= 3) {
                    updateNodeData(node.id, {
                      modelProvider: parts[0],
                      modelId: parts.slice(2).join("/"),
                    });
                  } else if (parts.length === 2) {
                    updateNodeData(node.id, {
                      modelProvider: parts[0],
                      modelId: parts[1],
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
          <DirectoryPicker
            value={localDir}
            onChange={handleDirChange}
            placeholder={t("config.workspaceDirectoryPlaceholder")}
            disabled={mode === "auto"}
            label={t("config.workspaceDirectory")}
          />

          {/* Workflow Mode (read-only badge, set at creation time) */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {t("config.workflowMode") || "工作流模式"}
            </label>
            <div className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium ${
              mode === "auto"
                ? "bg-blue-100 text-blue-700"
                : "bg-gray-100 text-gray-600"
            }`}>
              {mode === "auto" ? <Target size={12} /> : <PenLine size={12} />}
              {mode === "auto" ? t("workflow.modeAuto") : t("workflow.modeManual")}
            </div>
            <p className="text-xs text-gray-400 mt-1">
              {mode === "auto"
                ? (t("config.modeAutoHint") || "自动模式：输入目标，Planner 自动规划并构建工作流 DAG")
                : (t("config.modeManualHint") || "手动模式：在画布上拖拽节点、连线，自定义工作流")
              }
            </p>
          </div>

          {/* Goal input (auto mode only) */}
          {mode === "auto" && (
            <div className="space-y-1.5">
              <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
                {t("workflow.goalLabel")}
              </label>
              <textarea
                value={goal}
                onChange={(e) => updateGoal(e.target.value)}
                placeholder={t("workflow.goalPlaceholder")}
                rows={4}
                className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all"
              />
            </div>
          )}

          <p className="text-xs text-gray-400 text-center mt-4">
            {t("config.selectNodeHint") || "Select a node on the canvas to edit its configuration"}
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EdgeConfigSection — configuration panel for workflow edges
// ---------------------------------------------------------------------------
interface EdgeConfigSectionProps {
  edgeId: string;
  edges: WorkflowEdge[];
  nodes: WorkflowNode[];
  updateEdgeData: (id: string, data: Partial<EdgeData>) => void;
  t: (key: string) => string;
  mode: "auto" | "manual";
  onClose: () => void;
}

function EdgeConfigSection({
  edgeId,
  edges,
  nodes,
  updateEdgeData,
  t,
  mode,
  onClose,
}: EdgeConfigSectionProps) {
  const edge = edges.find((e) => e.id === edgeId);
  if (!edge) return null;

  const edgeData = (edge.data ?? {}) as EdgeData;
  const sourceNode = nodes.find((n) => n.id === edge.source);
  const targetNode = nodes.find((n) => n.id === edge.target);

  const sourceLabel = sourceNode?.data?.label || edge.source;
  const targetLabel = targetNode?.data?.label || edge.target;

  return (
    <>
      {/* Edge header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100">
        <div className="w-8 h-8 rounded-lg bg-indigo-50 flex items-center justify-center">
          <ArrowRight size={16} className="text-indigo-500" />
        </div>
        <div className="flex-1 min-w-0">
          <span className="text-sm font-semibold text-gray-800 block truncate">
            {t("config.edgeTitle") || "连线配置"}
          </span>
          <span className="text-xs text-gray-400 block truncate">
            {sourceLabel} → {targetLabel}
          </span>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">
        {/* Transfer Files */}
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferFiles") || "文件传递"}
          </label>
          <p className="text-xs text-gray-400">
            {t("config.transferFilesDesc") || "下游节点是否复用上游的 sandbox（能看到上游的文件改动）"}
          </p>
          <label className="flex items-center gap-2 cursor-pointer mt-1">
            <input
              type="checkbox"
              checked={edgeData.transfer_files !== false}
              onChange={(e) =>
                updateEdgeData(edgeId, { transfer_files: e.target.checked })
              }
              disabled={mode === "auto"}
              className="w-4 h-4 text-blue-500 border-gray-300 rounded focus:ring-blue-400 disabled:opacity-50"
            />
            <span className="text-sm text-gray-700">
              {t("config.enableFileTransfer") || "启用文件传递"}
            </span>
          </label>
        </div>

        {/* Transfer Summary */}
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferSummary") || "摘要注入"}
          </label>
          <p className="text-xs text-gray-400">
            {t("config.transferSummaryDesc") || "将上游节点的输出摘要注入下游节点的 prompt 中"}
          </p>
          <label className="flex items-center gap-2 cursor-pointer mt-1">
            <input
              type="checkbox"
              checked={edgeData.transfer_summary !== false}
              onChange={(e) =>
                updateEdgeData(edgeId, { transfer_summary: e.target.checked })
              }
              disabled={mode === "auto"}
              className="w-4 h-4 text-blue-500 border-gray-300 rounded focus:ring-blue-400 disabled:opacity-50"
            />
            <span className="text-sm text-gray-700">
              {t("config.enableSummaryInjection") || "启用摘要注入"}
            </span>
          </label>
        </div>

        {/* Transfer Format */}
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferFormat") || "传递格式"}
          </label>
          <p className="text-xs text-gray-400">
            {t("config.transferFormatDesc") || "选择上游数据传递到下游的方式"}
          </p>
          <select
            value={edgeData.transfer_format || "summary"}
            onChange={(e) =>
              updateEdgeData(edgeId, {
                transfer_format: e.target.value as "summary" | "full" | "diff",
              })
            }
            disabled={mode === "auto"}
            className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50 disabled:cursor-not-allowed mt-1"
          >
            <option value="summary">
              {t("config.formatSummary") || "摘要 (Summary)"}
            </option>
            <option value="full">
              {t("config.formatFull") || "完整输出 (Full)"}
            </option>
            <option value="diff">
              {t("config.formatDiff") || "文件差异 (Diff)"}
            </option>
          </select>
        </div>

        {/* Info */}
        <div className="bg-blue-50 border border-blue-100 rounded-lg px-3 py-2.5 mt-2">
          <p className="text-xs text-blue-600 leading-relaxed">
            {t("config.edgeInfo") || "连线定义了节点间的数据通道：执行顺序、文件继承和上下文传递。禁用文件传递时，下游节点将获得独立 sandbox。禁用摘要注入时，下游节点不会收到上游的输出信息。"}
          </p>
        </div>
      </div>
    </>
  );
}
