import React, { memo, useMemo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { AgentNodeType, NodeData, RunStatus } from "@/types/workflow";
import { NODE_META, STATUS_COLORS } from "@/lib/constants";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useSettingsStore } from "@/stores/settingsStore";
import type { StreamEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Color mapping — NODE_META stores Tailwind color names, we map to actual CSS
// ---------------------------------------------------------------------------
const COLOR_MAP: Record<string, string> = {
  blue: "#3b82f6",
  green: "#22c55e",
  yellow: "#eab308",
  gray: "#6b7280",
  teal: "#0d9488",
  purple: "#a855f7",
  orange: "#f97316",
};

const EMPTY_TASK_MESSAGES: import("@/types/task").TaskMessage[] = [];

// ---------------------------------------------------------------------------
// Icon mapping — maps icon name strings to simple SVG glyphs
// ---------------------------------------------------------------------------
const ICON_MAP: Record<string, JSX.Element> = {
  Code: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <polyline points="16 18 22 12 16 6" />
      <polyline points="8 6 2 12 8 18" />
    </svg>
  ),
  Map: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" />
      <line x1="8" y1="2" x2="8" y2="18" />
      <line x1="16" y1="6" x2="16" y2="22" />
    </svg>
  ),
  Search: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  ),
  Terminal: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  ),
  GitMerge: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <circle cx="6" cy="18" r="3" />
      <circle cx="6" cy="6" r="3" />
      <circle cx="18" cy="18" r="3" />
      <path d="M6 9v6" />
      <path d="M9 6c6 0 3 12 6 12" />
    </svg>
  ),
  FileCheck: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
      <polyline points="10 9 9 9 8 9" />
    </svg>
  ),
  User: (
    <svg viewBox="0 0 24 24" className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  ),
};

// ---------------------------------------------------------------------------
// Derive current action label from the most recent event for a node
// ---------------------------------------------------------------------------
function eventContent(event: StreamEvent): string {
  const raw = (event as unknown as Record<string, unknown>).content;
  return typeof raw === "string" ? raw.trim() : "";
}

function truncate(text: string, max = 120): string {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= max) return compact;
  return `${compact.slice(0, max - 1)}...`;
}

function getCurrentAction(events: StreamEvent[]): string {
  if (events.length === 0) return "运行中...";
  const last = events[events.length - 1];
  const t = last.type as string;
  if (t === "llm_token" || t === "llm_chunk" || t === "text" || t === "step_start") {
    return "正在思考...";
  }
  if (t === "tool_call" || t === "tool_use") {
    return "正在使用工具...";
  }
  if (t === "shell_stdout" || t === "shell_stderr") {
    return "正在执行...";
  }
  if (t === "step_finish") {
    return "步骤完成";
  }
  return "运行中...";
}

function getActivitySummary(events: StreamEvent[]): string {
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    const type = event.type as string;
    const content = eventContent(event);
    if (!content) continue;
    if (type === "tool_call") {
      const toolName = (event as unknown as Record<string, unknown>).tool_name;
      return `工具: ${typeof toolName === "string" && toolName ? toolName : content}`;
    }
    if (type === "shell_stdout" || type === "shell_stderr") {
      return truncate(content.startsWith("$") ? content : `命令输出: ${content}`);
    }
    if (type === "llm_token" || type === "llm_chunk") {
      return truncate(content);
    }
    if (type === "node_failed" || type === "error") {
      return truncate(content);
    }
  }
  return "";
}

// ---------------------------------------------------------------------------
// Props for BaseNode
// ---------------------------------------------------------------------------
export interface BaseNodeProps extends NodeProps {
  /** Extra content rendered below the header area */
  children?: React.ReactNode;
}

// ---------------------------------------------------------------------------
// BaseNode — shared outer shell for all custom workflow nodes
// ---------------------------------------------------------------------------
const BaseNode = memo(function BaseNode({ id, data, selected, children }: BaseNodeProps) {
  const nodeData = data as NodeData;
  const meta = NODE_META[nodeData.agentType];
  const colorHex = COLOR_MAP[meta.color] ?? "#6b7280";
  const icon = ICON_MAP[meta.icon] ?? ICON_MAP.Code;
  const t = useLocaleStore((s) => s.t);
  const defaultModel = useSettingsStore((s) => {
    const models = s.settings.models;
    return Array.isArray(models) && models.length > 0 ? models[0] : null;
  });
  const effectiveModel =
    nodeData.modelId
      ? (nodeData.modelProvider ? `${nodeData.modelProvider}/${nodeData.modelId}` : nodeData.modelId)
      : defaultModel && nodeData.agentType !== "shell" && nodeData.agentType !== "human"
        ? `${defaultModel.format}/${defaultModel.default_model || defaultModel.name}`
        : "";
  const modelIsDefault = Boolean(effectiveModel && !nodeData.modelId);

  // Read node run status from runStore
  const nodeStatus: RunStatus = useRunStore(
    (state) => state.nodeStatuses[id] ?? "idle"
  );
  const statusClasses = STATUS_COLORS[nodeStatus];
  const nodeStatusClass =
    nodeStatus === "running"
      ? "mas-node-running"
      : nodeStatus === "completed"
        ? "mas-node-completed"
        : nodeStatus === "failed"
          ? "mas-node-failed"
          : "";

  // Get node events to derive current action
  const allEvents = useRunStore((s) => s.events);
  const nodeEvents = useMemo(
    () => allEvents.filter((e) => e.node_id === id),
    [allEvents, id]
  );
  const currentAction = nodeStatus === "running" ? getCurrentAction(nodeEvents) : "";
  const activitySummary = getActivitySummary(nodeEvents);

  // Task status from taskStore (for dynamic worker nodes)
  const taskForNode = useTaskStore((s) =>
    s.tasks.find((t) => t.assigned_node_id === id)
  );
  const taskMessages = useTaskStore((s) =>
    taskForNode ? (s.taskMessages[taskForNode.id] ?? EMPTY_TASK_MESSAGES) : EMPTY_TASK_MESSAGES
  );
  const artifactCount = useTaskStore((s) =>
    taskForNode ? s.artifacts.filter((artifact) => artifact.task_id === taskForNode.id).length : 0
  );
  const collaborationCount = taskMessages.filter((message) =>
    message.message_type === "worker_question" ||
    message.message_type === "planner_question" ||
    message.message_type === "planner_answer"
  ).length;
  const lastTaskMessage = taskMessages[taskMessages.length - 1];
  const detailText =
    (lastTaskMessage?.content && truncate(lastTaskMessage.content)) ||
    activitySummary ||
    (taskForNode?.result_summary && truncate(taskForNode.result_summary)) ||
    (taskForNode?.description && nodeStatus !== "completed" ? truncate(taskForNode.description) : "");

  return (
    <div
      className={[
        "mas-node-shell relative rounded-md border bg-white shadow-sm transition-shadow overflow-hidden",
        nodeStatusClass,
        selected ? "ring-2 ring-blue-500" : "",
      ].join(" ")}
      style={{ width: 260 }}
    >
      {/* Handles */}
      <Handle
        type="target"
        position={Position.Top}
        className="!w-3 !h-3 !bg-gray-400 !border-2 !border-white"
      />

      {/* Top color bar */}
      <div className="h-1.5" style={{ backgroundColor: colorHex }} />

      {/* Header: icon + label */}
      <div className="flex items-start gap-2 px-3 py-2">
        <span style={{ color: colorHex }}>{icon}</span>
        <div className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-gray-800 truncate">
            {nodeData.label || t(`node.${nodeData.agentType}.label`)}
          </span>
          <span className="block text-[10px] uppercase tracking-wide text-gray-400">
            {nodeData.agentType}
          </span>
          {effectiveModel && (
            <span className="block text-[10px] text-gray-500 truncate">
              模型: {effectiveModel}{modelIsDefault ? " (默认)" : ""}
            </span>
          )}
        </div>
      </div>

      {/* Status indicator bar */}
      {nodeStatus !== "idle" && (
        <div className="mx-3 mb-1 flex items-center gap-1.5">
          {nodeStatus === "running" && (
            <>
              {/* Blue pulsing dot */}
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
              </span>
              {/* Current action text */}
              <span className="text-xs text-blue-600 font-medium truncate">
                {currentAction}
              </span>
            </>
          )}
          {nodeStatus === "completed" && (
            <>
              {/* Green checkmark */}
              <svg className="w-3.5 h-3.5 text-green-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
                <polyline points="20 6 9 17 4 12" />
              </svg>
              <span className="text-xs text-green-600 font-medium">完成</span>
            </>
          )}
          {nodeStatus === "failed" && (
            <>
              {/* Red X */}
              <svg className="w-3.5 h-3.5 text-red-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
              <span className="text-xs text-red-600 font-medium">失败</span>
            </>
          )}
          {nodeStatus === "paused" && (
            <span
              className={[
                "inline-block rounded-full px-2 py-0.5 text-xs font-medium",
                statusClasses,
              ].join(" ")}
            >
              {nodeStatus}
            </span>
          )}
          {/* Task-specific status: blocked (escalation) or progress */}
          {taskForNode && taskForNode.status === "blocked" && (
            <>
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500" />
              </span>
              <span className="text-xs text-amber-600 font-medium">等待协助...</span>
            </>
          )}
          {taskForNode && taskForNode.status === "running" && taskForNode.progress > 0 && (
            <span className="text-[10px] font-mono text-blue-500 ml-auto">
              {taskForNode.progress}%
            </span>
          )}
        </div>
      )}

      {(taskForNode || detailText) && (
        <div className="mx-3 mb-2 rounded bg-gray-50 border border-gray-100 px-2 py-1.5">
          {taskForNode && (
            <div className="mb-1 flex items-center justify-between gap-2 text-[10px]">
              <span className="font-medium text-gray-500 truncate">
                {taskForNode.title}
              </span>
              <span className="shrink-0 font-mono text-gray-400">
                {taskForNode.progress ?? 0}%
              </span>
            </div>
          )}
          {taskForNode && (artifactCount > 0 || collaborationCount > 0) && (
            <div className="mb-1 flex items-center gap-2 text-[10px] text-gray-500">
              {artifactCount > 0 && <span>{artifactCount} artifacts</span>}
              {collaborationCount > 0 && <span>{collaborationCount} messages</span>}
            </div>
          )}
          {detailText && (
            <div className="text-[11px] leading-snug text-gray-600 line-clamp-3">
              {detailText}
            </div>
          )}
          {taskForNode && taskForNode.status === "running" && (
            <div className="mt-1 h-1 overflow-hidden rounded-full bg-gray-200">
              <div
                className="h-full rounded-full bg-blue-500 transition-all"
                style={{ width: `${Math.max(5, Math.min(taskForNode.progress || 5, 100))}%` }}
              />
            </div>
          )}
        </div>
      )}

      {/* Extra content area — each specific node provides this */}
      {children && (
        <div className="px-3 pb-2 text-xs text-gray-500">
          {children}
        </div>
      )}

      <Handle
        type="source"
        position={Position.Bottom}
        className="!w-3 !h-3 !bg-gray-400 !border-2 !border-white"
      />
    </div>
  );
});

export default BaseNode;
