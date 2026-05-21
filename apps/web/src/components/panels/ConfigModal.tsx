"use client";

import { useCallback } from "react";
import {
  X,
  Settings,
  FolderOpen,
  ArrowRight,
  AlertTriangle,
  CheckCircle2,
} from "lucide-react";
import type { AgentNodeType, EdgeData, WorkflowNode } from "@/types/workflow";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useSettingsStore } from "@/stores/settingsStore";
import { NODE_META } from "@/lib/constants";
import { ICON_MAP, COLOR_MAP, FEATURES } from "@/lib/nodeConfig";
import ModelSelector from "./ModelSelector";
import PromptEditor from "./PromptEditor";
import PermissionsEditor from "./PermissionsEditor";
import CommandEditor from "./CommandEditor";
import DirectoryPicker from "@/components/common/DirectoryPicker";

interface ConfigModalProps {
  open: boolean;
  onClose: () => void;
}

export default function ConfigModal({ open, onClose }: ConfigModalProps) {
  const t = useLocaleStore((s) => s.t);
  const setSelectedNode = useWorkflowStore((s) => s.setSelectedNode);
  const setSelectedEdge = useWorkflowStore((s) => s.setSelectedEdge);

  const handleClose = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdge(null);
    onClose();
  }, [setSelectedNode, setSelectedEdge, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-sm" onClick={handleClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2">
            <Settings size={18} className="text-gray-500" />
            <h3 className="text-base font-semibold text-gray-800">
              {t("configModal.title")}
            </h3>
          </div>
          <button
            onClick={handleClose}
            className="p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          >
            <X size={18} />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          <ConfigModalBody />
        </div>
      </div>
    </div>
  );
}

function ConfigModalBody() {
  const selectedNodeId = useWorkflowStore((s) => s.selectedNodeId);
  const selectedEdgeId = useWorkflowStore((s) => s.selectedEdgeId);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const node = nodes.find((n) => n.id === selectedNodeId);

  if (selectedEdgeId) {
    return <EdgeConfigSectionWrapper edgeId={selectedEdgeId} />;
  }

  if (selectedNodeId && node && node.data) {
    return <NodeConfigSection node={node} />;
  }

  return <WorkflowSettingsSection />;
}

function NodeConfigSection({ node }: { node: WorkflowNode }) {
  const t = useLocaleStore((s) => s.t);
  const updateNodeData = useWorkflowStore((s) => s.updateNodeData);
  const mode = useWorkflowStore((s) => s.mode);
  const defaultModel = useSettingsStore((s) => {
    const models = s.settings.models;
    return Array.isArray(models) && models.length > 0 ? models[0] : null;
  });

  const data = node.data;
  const nodeType = (node.type ?? "coder") as AgentNodeType;
  const meta = NODE_META[nodeType];
  const features = FEATURES[nodeType];
  const IconComponent = ICON_MAP[meta.icon];
  const colors = COLOR_MAP[meta.color] ?? COLOR_MAP.gray;

  return (
    <>
      <div className="flex items-center gap-3 px-6 py-3 border-b border-gray-100">
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
      </div>

      <div className="p-6 space-y-5">
        {features.agentType && (
          <>
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

            <div className="space-y-1.5">
              <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
                {t("config.agentType")}
              </label>
              <div className={`w-full rounded-lg border border-gray-200 ${colors.bg} px-3 py-2 text-sm text-gray-600 flex items-center gap-2`}>
                {IconComponent && <IconComponent size={14} className={colors.text} />}
                {t(`node.${nodeType}.label`)}
              </div>
            </div>
          </>
        )}

        {features.model && (
          <div className="space-y-1">
            <ModelSelector
              value={
                data.modelProvider && data.modelId
                  ? `${data.modelProvider}/${data.modelId}`
                  : defaultModel
                    ? `${defaultModel.format}/${defaultModel.default_model || defaultModel.name}`
                    : ""
              }
              onChange={(fullId) => {
                const parts = fullId.split("/");
                if (parts.length >= 2) {
                  updateNodeData(node.id, {
                    modelProvider: parts[0],
                    modelId: parts.slice(1).join("/"),
                  });
                } else {
                  updateNodeData(node.id, { modelProvider: "", modelId: fullId });
                }
              }}
              disabled={false}
            />
          </div>
        )}

        {features.prompt && (
          <PromptEditor
            value={data.prompt}
            onChange={(prompt) => updateNodeData(node.id, { prompt })}
            disabled={mode === "auto"}
          />
        )}

        {features.permissions && (
          <PermissionsEditor
            value={data.permissions}
            onChange={(permissions) => updateNodeData(node.id, { permissions })}
            disabled={mode === "auto"}
          />
        )}

        {features.command && (
          <CommandEditor
            value={data.command}
            onChange={(command) => updateNodeData(node.id, { command })}
            disabled={mode === "auto"}
          />
        )}

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
  );
}

function EdgeConfigSectionWrapper({ edgeId }: { edgeId: string }) {
  const t = useLocaleStore((s) => s.t);
  const edges = useWorkflowStore((s) => s.edges) ?? [];
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const updateEdgeData = useWorkflowStore((s) => s.updateEdgeData);
  const mode = useWorkflowStore((s) => s.mode);

  const edge = edges.find((e) => e.id === edgeId);
  if (!edge) return null;

  const edgeData = (edge.data ?? {}) as EdgeData;
  const sourceNode = nodes.find((n) => n.id === edge.source);
  const targetNode = nodes.find((n) => n.id === edge.target);
  const sourceLabel = sourceNode?.data?.label || edge.source;
  const targetLabel = targetNode?.data?.label || edge.target;

  return (
    <>
      <div className="flex items-center gap-3 px-6 py-3 border-b border-gray-100">
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

      <div className="p-6 space-y-5">
        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferFiles") || "文件传递"}
          </label>
          <label className="flex items-center gap-2 cursor-pointer mt-1">
            <input
              type="checkbox"
              checked={edgeData.transfer_files !== false}
              onChange={(e) => updateEdgeData(edgeId, { transfer_files: e.target.checked })}
              disabled={mode === "auto"}
              className="w-4 h-4 text-blue-500 border-gray-300 rounded focus:ring-blue-400 disabled:opacity-50"
            />
            <span className="text-sm text-gray-700">{t("config.enableFileTransfer") || "启用文件传递"}</span>
          </label>
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferSummary") || "摘要注入"}
          </label>
          <label className="flex items-center gap-2 cursor-pointer mt-1">
            <input
              type="checkbox"
              checked={edgeData.transfer_summary !== false}
              onChange={(e) => updateEdgeData(edgeId, { transfer_summary: e.target.checked })}
              disabled={mode === "auto"}
              className="w-4 h-4 text-blue-500 border-gray-300 rounded focus:ring-blue-400 disabled:opacity-50"
            />
            <span className="text-sm text-gray-700">{t("config.enableSummaryInjection") || "启用摘要注入"}</span>
          </label>
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            {t("config.transferFormat") || "传递格式"}
          </label>
          <select
            value={edgeData.transfer_format || "summary"}
            onChange={(e) => updateEdgeData(edgeId, { transfer_format: e.target.value as "summary" | "full" | "diff" })}
            disabled={mode === "auto"}
            className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50 disabled:cursor-not-allowed mt-1"
          >
            <option value="summary">{t("config.formatSummary") || "摘要"}</option>
            <option value="full">{t("config.formatFull") || "完整输出"}</option>
            <option value="diff">{t("config.formatDiff") || "文件差异"}</option>
          </select>
        </div>
      </div>
    </>
  );
}

function WorkflowSettingsSection() {
  const t = useLocaleStore((s) => s.t);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);
  const updateWorkspaceDirectory = useWorkflowStore((s) => s.updateWorkspaceDirectory);
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const lifecyclePhase = useWorkflowStore((s) => s.lifecyclePhase);
  const blockers = useWorkflowStore((s) => s.blockers);

  return (
    <div className="p-6 space-y-5">
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

      <DirectoryPicker
        value={workspaceDirectory ?? ""}
        onChange={(v) => void updateWorkspaceDirectory(v)}
        placeholder={t("config.workspaceDirectoryPlaceholder")}
        label={t("config.workspaceDirectory")}
      />

      <div className="space-y-3 rounded-xl border border-gray-200 bg-gray-50/70 p-3">
        <div>
          <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
            Execution Readiness
          </label>
        </div>
        <div className="flex items-center gap-2 text-sm font-medium text-gray-700">
          <CheckCircle2 size={14} className="text-emerald-500" />
          <span>{t("config.phase")}{lifecyclePhase}</span>
        </div>
        <div className="text-[11px] text-gray-500">
          {t("config.dagStatus")}{nodes.length > 1 ? t("config.dagReady") : t("config.dagIncomplete")}
        </div>
        <div className="text-[11px] text-gray-500">
          {t("config.workDirLabel")}{workspaceDirectory?.trim() ? t("config.dirSet") : t("config.dirNotSet")}
        </div>
        {blockers.length > 0 ? (
          <div className="rounded-lg border border-red-200 bg-red-50 p-2 text-[11px] text-red-700">
            <div className="mb-1 flex items-center gap-1 font-medium">
              <AlertTriangle size={12} />
              {t("config.blockers")}
            </div>
            <div className="space-y-1">
              {blockers.map((item) => (
                <div key={`${item.code}-${item.message}`}>- {item.message}</div>
              ))}
            </div>
          </div>
        ) : (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-2 text-[11px] text-emerald-700">
            {t("config.noBlockers")}
          </div>
        )}
      </div>
    </div>
  );
}
