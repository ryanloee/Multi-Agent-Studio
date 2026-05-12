"use client";

import { useState, useEffect, useCallback, useMemo, memo } from "react";
import {
  ChevronDown,
  ChevronRight,
  Send,
  AlertTriangle,
  CheckCircle2,
  Circle,
  Clock,
  Ban,
  XCircle,
  RotateCw,
  Pencil,
  X,
  Check,
  ExternalLink,
  Plus,
  Play,
  ArrowRightLeft,
} from "lucide-react";
import { useTaskStore } from "@/stores/taskStore";
import { useRunStore } from "@/stores/runStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { Artifact, Task, TaskStatus, TaskMessage } from "@/types/task";
import { TASK_STATUS_CONFIG } from "@/types/task";
import type { NodeData } from "@/types/workflow";
import type { LucideIcon } from "lucide-react";
import TaskTopology from "./TaskTopology";

// ---------------------------------------------------------------------------
// Status icon mapping
// ---------------------------------------------------------------------------
const STATUS_ICONS: Record<TaskStatus, LucideIcon> = {
  pending: Circle,
  assigned: Clock,
  running: Clock,
  blocked: Ban,
  completed: CheckCircle2,
  failed: XCircle,
};

const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: "未开始",
  assigned: "已分配",
  running: "进行中",
  blocked: "阻塞",
  completed: "成功",
  failed: "失败",
};

// ---------------------------------------------------------------------------
// TaskLeaf — single task with expand, edit, node reassignment, agent link
// ---------------------------------------------------------------------------
const TaskLeaf = memo(function TaskLeaf({
  task,
  isSelected,
  onSelect,
  messages,
  artifacts,
  onSendMessage,
  onRestart,
  onUpdate,
  onAssign,
  workflowNodes,
}: {
  task: Task;
  isSelected: boolean;
  onSelect: () => void;
  messages: TaskMessage[];
  artifacts: Artifact[];
  onSendMessage: (text: string) => void;
  onRestart: () => void;
  onUpdate: (patch: Partial<Task>) => void;
  onAssign: (nodeId: string, nodeLabel: string, agentType: string, modelProvider: string, modelId: string, prompt: string) => void;
  workflowNodes: { id: string; label: string; agentType: string; modelProvider: string; modelId: string; prompt: string }[];
}) {
  const [expanded, setExpanded] = useState(isSelected);
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(task.title);
  const [editDesc, setEditDesc] = useState(task.description);
  const [input, setInput] = useState("");
  const [assigning, setAssigning] = useState(false);
  const [selectedAssignNode, setSelectedAssignNode] = useState("");

  const setFocusNode = useWorkflowStore((s) => s.setFocusNode);

  useEffect(() => {
    setExpanded(isSelected);
  }, [isSelected]);

  useEffect(() => {
    setEditTitle(task.title);
    setEditDesc(task.description);
  }, [task.title, task.description]);

  const config = TASK_STATUS_CONFIG[task.status];
  const StatusIcon = STATUS_ICONS[task.status];

  const handleSend = useCallback(() => {
    const text = input.trim();
    if (!text) return;
    onSendMessage(text);
    setInput("");
  }, [input, onSendMessage]);

  const handleSaveEdit = useCallback(() => {
    onUpdate({ title: editTitle, description: editDesc });
    setEditing(false);
  }, [editTitle, editDesc, onUpdate]);

  const handleAgentClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      if (task.assigned_node_id) {
        setFocusNode(task.assigned_node_id);
      }
    },
    [task.assigned_node_id, setFocusNode]
  );

  const handleAssignSubmit = useCallback(() => {
    if (!selectedAssignNode) return;
    const node = workflowNodes.find((n) => n.id === selectedAssignNode);
    if (node) {
      onAssign(node.id, node.label, node.agentType, node.modelProvider, node.modelId, task.description || task.title);
      setAssigning(false);
      setSelectedAssignNode("");
    }
  }, [selectedAssignNode, workflowNodes, onAssign, task.description, task.title]);

  // Shorten title for the collapsed view
  const shortTitle = task.title.length > 80 ? task.title.slice(0, 80) + "..." : task.title;

  // Can assign/reassign if task is pending, blocked, failed, or completed
  const canAssign = ["pending", "blocked", "failed", "completed"].includes(task.status);

  return (
    <div
      className={`transition-colors ${
        isSelected ? "bg-blue-50/50" : "hover:bg-gray-50"
      }`}
    >
      {/* Header row — task title is the main display */}
      <button
        className="w-full flex items-center gap-2 pl-3 pr-3 py-2 text-left"
        onClick={() => {
          if (!editing && !assigning) {
            onSelect();
            setExpanded(!expanded);
          }
        }}
      >
        {expanded ? (
          <ChevronDown size={12} className="text-gray-300 shrink-0" />
        ) : (
          <ChevronRight size={12} className="text-gray-300 shrink-0" />
        )}

        <StatusIcon size={12} className={`${config.color} shrink-0`} />

        {/* Task title — main display */}
        <span className="text-[11px] text-gray-700 flex-1 truncate font-medium">
          {editing ? (
            <input
              type="text"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              className="w-full text-[11px] border border-blue-300 rounded px-1 py-0.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            shortTitle
          )}
        </span>

        {task.progress > 0 && task.progress < 100 && (
          <span className="text-[9px] font-mono text-blue-500 shrink-0">
            {task.progress}%
          </span>
        )}

        <span
          className={`text-[9px] font-medium px-1.5 py-0.5 rounded-full ${config.bgColor} ${config.color} shrink-0`}
        >
          {STATUS_LABELS[task.status]}
        </span>
      </button>

      {/* Agent link / reassignment — shown below the title */}
      {task.assigned_worker_label && !assigning && (
        <div className="pl-7 pr-3 pb-1 flex items-center gap-2">
          <button
            onClick={handleAgentClick}
            className="flex items-center gap-1 text-[10px] text-blue-500 hover:text-blue-700 hover:underline"
          >
            <ExternalLink size={9} />
            {task.assigned_worker_label}
          </button>
          {canAssign && (
            <button
              onClick={(e) => { e.stopPropagation(); setAssigning(true); }}
              className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-blue-500"
              title="重新分配节点"
            >
              <ArrowRightLeft size={9} />
            </button>
          )}
        </div>
      )}

      {/* Node assignment dropdown */}
      {assigning && (
        <div className="pl-7 pr-3 pb-2 space-y-1.5">
          <select
            value={selectedAssignNode}
            onChange={(e) => setSelectedAssignNode(e.target.value)}
            className="w-full text-[10px] border border-gray-200 rounded px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
          >
            <option value="">选择执行节点...</option>
            {workflowNodes.map((node) => (
              <option key={node.id} value={node.id}>
                {node.label} ({node.agentType})
              </option>
            ))}
          </select>
          <div className="flex items-center gap-1">
            <button
              onClick={handleAssignSubmit}
              disabled={!selectedAssignNode}
              className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40"
            >
              <Play size={9} /> 分配并执行
            </button>
            <button
              onClick={() => { setAssigning(false); setSelectedAssignNode(""); }}
              className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-gray-100 text-gray-500 hover:bg-gray-200"
            >
              <X size={9} /> 取消
            </button>
          </div>
        </div>
      )}

      {/* No node assigned yet — show assign button for pending tasks */}
      {!task.assigned_worker_label && !assigning && task.status === "pending" && (
        <div className="pl-7 pr-3 pb-1">
          <button
            onClick={(e) => { e.stopPropagation(); setAssigning(true); }}
            className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-blue-500"
          >
            <ArrowRightLeft size={9} /> 分配节点
          </button>
        </div>
      )}

      {/* Progress bar */}
      {(task.status === "running" || task.status === "assigned") && task.progress > 0 && (
        <div className="pl-7 pr-3 pb-1">
          <div className="h-0.5 bg-gray-200 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 transition-all duration-300"
              style={{ width: `${task.progress}%` }}
            />
          </div>
        </div>
      )}

      {/* Expanded detail */}
      {expanded && (
        <div className="pl-7 pr-3 pb-2 space-y-2">
          {/* Description */}
          {editing ? (
            <textarea
              value={editDesc}
              onChange={(e) => setEditDesc(e.target.value)}
              rows={3}
              className="w-full text-[10px] border border-blue-300 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-y"
            />
          ) : task.description ? (
            <p className="text-[10px] text-gray-500 leading-relaxed">
              {task.description}
            </p>
          ) : null}

          {/* Result summary */}
          {task.result_summary && !editing && (
            <div className="text-[10px] text-gray-500 bg-gray-50 rounded p-1.5 max-h-24 overflow-y-auto whitespace-pre-wrap">
              {task.result_summary}
            </div>
          )}

          {/* Messages */}
          {messages.length > 0 && !editing && (
            <div className="space-y-1 max-h-28 overflow-y-auto">
              {messages.map((msg) => (
                <div key={msg.id} className="text-[10px]">
                  <span
                    className={`font-medium ${
                      msg.sender_type === "planner"
                        ? "text-purple-600"
                        : msg.sender_type === "worker"
                        ? "text-blue-600"
                        : "text-gray-600"
                    }`}
                  >
                    {msg.sender_type === "planner"
                      ? "Planner"
                      : msg.sender_type === "user"
                      ? "You"
                      : msg.sender_id}
                    :
                  </span>{" "}
                  <span className="text-gray-600">{msg.content}</span>
                </div>
              ))}
            </div>
          )}

          {artifacts.length > 0 && !editing && (
            <div className="space-y-1 max-h-28 overflow-y-auto">
              {artifacts.map((artifact) => (
                <div key={artifact.id} className="rounded border border-emerald-100 bg-emerald-50/60 p-1.5 text-[10px]">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium text-emerald-700 truncate">{artifact.title}</span>
                    <span className="shrink-0 text-[9px] text-emerald-600">{artifact.type}</span>
                  </div>
                  {artifact.content && (
                    <div className="mt-0.5 line-clamp-3 whitespace-pre-wrap text-gray-600">
                      {artifact.content}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Action bar */}
          <div className="flex items-center gap-1.5 pt-0.5">
            {editing ? (
              <>
                <button
                  onClick={handleSaveEdit}
                  className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-blue-500 text-white hover:bg-blue-600"
                >
                  <Check size={10} /> Save
                </button>
                <button
                  onClick={() => {
                    setEditing(false);
                    setEditTitle(task.title);
                    setEditDesc(task.description);
                  }}
                  className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-gray-100 text-gray-600 hover:bg-gray-200"
                >
                  <X size={10} /> Cancel
                </button>
              </>
            ) : (
              <>
                {(task.status === "failed" || task.status === "completed") && (
                  <button
                    onClick={onRestart}
                    className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-amber-50 text-amber-600 hover:bg-amber-100 border border-amber-200"
                  >
                    <RotateCw size={10} /> Restart
                  </button>
                )}
                <button
                  onClick={() => setEditing(true)}
                  className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded bg-gray-50 text-gray-500 hover:bg-gray-100 border border-gray-200"
                >
                  <Pencil size={10} /> Edit
                </button>
              </>
            )}

            {/* Message input (only for active tasks) */}
            {!editing && task.status !== "completed" && task.status !== "failed" && (
              <div className="flex gap-1 ml-auto flex-1">
                <input
                  type="text"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleSend()}
                  placeholder="Send instruction..."
                  className="flex-1 text-[10px] border border-gray-200 rounded px-2 py-0.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                <button
                  onClick={handleSend}
                  disabled={!input.trim()}
                  className="p-0.5 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Send size={10} />
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

// ---------------------------------------------------------------------------
// NewTaskForm — inline form for creating tasks manually
// ---------------------------------------------------------------------------
function NewTaskForm({
  onCreate,
  workflowNodes,
}: {
  onCreate: (title: string, description: string, nodeId?: string, nodeLabel?: string) => void;
  workflowNodes: { id: string; label: string; agentType: string; modelProvider: string; modelId: string; prompt: string }[];
}) {
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [nodeId, setNodeId] = useState("");

  const handleSubmit = useCallback(() => {
    const trimmed = title.trim();
    if (!trimmed) return;
    const node = workflowNodes.find((n) => n.id === nodeId);
    onCreate(trimmed, desc.trim(), nodeId || undefined, node?.label);
    setTitle("");
    setDesc("");
    setNodeId("");
  }, [title, desc, nodeId, workflowNodes, onCreate]);

  return (
    <div className="px-3 py-2 border-b border-gray-100 space-y-1.5 bg-gray-50/50">
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="任务标题..."
        className="w-full text-[11px] border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-400"
        onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
      />
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        placeholder="任务描述（可选）..."
        rows={2}
        className="w-full text-[10px] border border-gray-200 rounded px-2 py-1 focus:outline-none focus:ring-1 focus:ring-blue-400 resize-y"
      />
      <div className="flex items-center gap-1.5">
        <select
          value={nodeId}
          onChange={(e) => setNodeId(e.target.value)}
          className="flex-1 text-[10px] border border-gray-200 rounded px-2 py-1 bg-white focus:outline-none focus:ring-1 focus:ring-blue-400"
        >
          <option value="">自动分配节点</option>
          {workflowNodes.map((node) => (
            <option key={node.id} value={node.id}>
              {node.label} ({node.agentType})
            </option>
          ))}
        </select>
        <button
          onClick={handleSubmit}
          disabled={!title.trim()}
          className="flex items-center gap-1 text-[10px] px-2.5 py-1 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40 shrink-0"
        >
          <Plus size={10} /> 创建
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// TaskBoard — main component (flat task list, task-title-centric)
// ---------------------------------------------------------------------------
export default function TaskBoard({ workflowId }: { workflowId?: string }) {
  const tasks = useTaskStore((s) => s.tasks);
  const selectedTaskId = useTaskStore((s) => s.selectedTaskId);
  const selectTask = useTaskStore((s) => s.selectTask);
  const taskMessages = useTaskStore((s) => s.taskMessages);
  const artifacts = useTaskStore((s) => s.artifacts);
  const upsertTask = useTaskStore((s) => s.upsertTask);
  const optimisticUpdateTask = useTaskStore((s) => s.optimisticUpdateTask);
  const currentRunId = useTaskStore((s) => s.currentRunId);
  const runStatus = useRunStore((s) => s.status);
  const runId = useRunStore((s) => s.runId);
  const progressSummary = useRunStore((s) => s.progressSummary);

  // Workflow nodes for assignment — use stable selector + useMemo to avoid
  // creating new array references on every render (which causes infinite
  // re-render loops via Zustand's useSyncExternalStore).
  const rawNodes = useWorkflowStore((s) => s.nodes);
  const workflowNodes = useMemo(() => (rawNodes ?? []).map((n) => {
    const data = n.data as NodeData;
    return {
      id: n.id,
      label: data?.label || n.id,
      agentType: data?.agentType || "coder",
      modelProvider: data?.modelProvider || "",
      modelId: data?.modelId || "",
      prompt: data?.prompt || "",
    };
  }), [rawNodes]);

  const [filterStatus, setFilterStatus] = useState<TaskStatus | "all">("all");
  const [showNewTask, setShowNewTask] = useState(false);
  const [viewMode, setViewMode] = useState<"topology" | "flat">("topology");
  const t = useLocaleStore((s) => s.t);

  // On mount: find latest run with tasks
  useEffect(() => {
    if (!workflowId) return;
    const activeRunId = currentRunId || runId;
    if (activeRunId) return;

    let cancelled = false;
    const loadLatestRunTasks = async () => {
      try {
        const { api } = await import("@/lib/api");
        const runs = await api.listRuns();
        if (cancelled || runs.length === 0) return;
        const sorted = runs.sort((a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        );
        for (const run of sorted.slice(0, 10)) {
          if (cancelled) return;
          const fetched = await api.listTasks(run.id);
          if (fetched.length > 0) {
            useTaskStore.getState().setTasks(fetched);
            useTaskStore.getState().setCurrentRunId(run.id);
            const fetchedArtifacts = await api.listArtifacts(run.id);
            useTaskStore.getState().setArtifacts(fetchedArtifacts);
            const messagePairs = await Promise.all(
              fetched.map(async (task) => [task.id, await api.listTaskMessages(run.id, task.id)] as const)
            );
            for (const [taskId, messages] of messagePairs) {
              useTaskStore.getState().setTaskMessages(taskId, messages);
            }
            return;
          }
        }
      } catch {
        // ignore
      }
    };

    loadLatestRunTasks();
    return () => { cancelled = true; };
  }, [workflowId, currentRunId, runId]);

  // REST polling for active runs
  useEffect(() => {
    const activeRunId = currentRunId || runId;
    if (!activeRunId) return;

    let cancelled = false;
    const fetchTasks = async () => {
      try {
        const { api } = await import("@/lib/api");
        const fetched = await api.listTasks(activeRunId);
        if (!cancelled && fetched.length > 0) {
          useTaskStore.getState().setTasks(fetched);
          const fetchedArtifacts = await api.listArtifacts(activeRunId);
          if (!cancelled) useTaskStore.getState().setArtifacts(fetchedArtifacts);
          const messagePairs = await Promise.all(
            fetched.map(async (task) => [task.id, await api.listTaskMessages(activeRunId, task.id)] as const)
          );
          if (!cancelled) {
            for (const [taskId, messages] of messagePairs) {
              useTaskStore.getState().setTaskMessages(taskId, messages);
            }
          }
          if (!useTaskStore.getState().currentRunId) {
            useTaskStore.getState().setCurrentRunId(activeRunId);
          }
        }
      } catch {
        // ignore
      }
    };

    fetchTasks();

    if (runStatus === "running" || runStatus === "paused") {
      const interval = setInterval(fetchTasks, 3000);
      return () => { cancelled = true; clearInterval(interval); };
    }

    return () => { cancelled = true; };
  }, [currentRunId, runId, runStatus]);

  const filteredTasks = useMemo(() => {
    if (filterStatus === "all") return tasks;
    return tasks.filter((t) => t.status === filterStatus);
  }, [tasks, filterStatus]);

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = { all: tasks.length };
    for (const t of tasks) {
      counts[t.status] = (counts[t.status] || 0) + 1;
    }
    return counts;
  }, [tasks]);

  const handleSendMessage = useCallback(
    async (taskId: string, text: string) => {
      const sendRunId = currentRunId || runId;
      if (!sendRunId) return;
      try {
        const { api } = await import("@/lib/api");
        await api.sendTaskMessage(sendRunId, taskId, {
          sender_type: "user",
          sender_id: "user",
          message_type: "user_edit",
          content: text,
        });
      } catch {
        useTaskStore.getState().appendMessage(taskId, {
          id: `local_${Date.now()}`,
          task_id: taskId,
          sender_type: "user",
          sender_id: "user",
          message_type: "user_edit",
          content: text,
          created_at: new Date().toISOString(),
        });
      }
    },
    [currentRunId, runId]
  );

  const handleRestart = useCallback(
    async (taskId: string) => {
      const sendRunId = currentRunId || runId;
      if (!sendRunId) return;
      // Find the task to check if it has an assigned node
      const task = useTaskStore.getState().tasks.find((t) => t.id === taskId);
      const nextStatus = task?.assigned_node_id ? "assigned" : "pending";
      optimisticUpdateTask(taskId, {
        status: nextStatus,
        progress: 0,
        result_summary: "",
      });
      try {
        const { api } = await import("@/lib/api");
        const updated = await api.restartTask(sendRunId, taskId);
        optimisticUpdateTask(taskId, updated);
      } catch (err) {
        console.error("[TaskBoard] restart failed:", err);
      }
    },
    [currentRunId, runId, optimisticUpdateTask]
  );

  const handleUpdateTask = useCallback(
    async (taskId: string, patch: Partial<Task>) => {
      const sendRunId = currentRunId || runId;
      if (!sendRunId) return;
      optimisticUpdateTask(taskId, patch);
      try {
        const { api } = await import("@/lib/api");
        const updated = await api.updateTask(sendRunId, taskId, patch);
        optimisticUpdateTask(taskId, updated);
      } catch {
        // ignore
      }
    },
    [currentRunId, runId, optimisticUpdateTask]
  );

  const handleAssignTask = useCallback(
    async (taskId: string, nodeId: string, nodeLabel: string, agentType: string, modelProvider: string, modelId: string, prompt: string) => {
      const sendRunId = currentRunId || runId;
      if (!sendRunId) return;
      optimisticUpdateTask(taskId, {
        status: "assigned",
        assigned_node_id: nodeId,
        assigned_worker_label: nodeLabel,
      });
      try {
        const { api } = await import("@/lib/api");
        const updated = await api.assignTask(sendRunId, taskId, {
          node_id: nodeId,
          node_label: nodeLabel,
          agent_type: agentType,
          model_provider: modelProvider,
          model_id: modelId,
          prompt,
        });
        optimisticUpdateTask(taskId, updated);
      } catch {
        // ignore
      }
    },
    [currentRunId, runId, optimisticUpdateTask]
  );

  const handleCreateTask = useCallback(
    async (title: string, description: string, nodeId?: string, nodeLabel?: string) => {
      const sendRunId = currentRunId || runId;
      if (!sendRunId) return;
      try {
        const { api } = await import("@/lib/api");
        const newTask = await api.createTask(sendRunId, {
          title,
          description,
          assigned_node_id: nodeId,
          assigned_worker_label: nodeLabel,
        });
        upsertTask(newTask);
        setShowNewTask(false);
      } catch {
        // ignore
      }
    },
    [currentRunId, runId, upsertTask]
  );

  // Empty state
  const activeRunId = currentRunId || runId;
  if (!activeRunId || (runStatus === "idle" && tasks.length === 0)) {
    return (
      <div className="flex-1 flex items-center justify-center p-6">
        <div className="text-center space-y-2">
          <div className="w-10 h-10 rounded-lg bg-gray-100 flex items-center justify-center mx-auto">
            <AlertTriangle size={20} className="text-gray-400" />
          </div>
          <p className="text-xs text-gray-400">
            Run a workflow to see tasks here
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Progress summary bar */}
      {progressSummary && progressSummary.total > 0 && (
        <div className="px-3 py-2 border-b border-gray-100 shrink-0">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] text-gray-500">
              {progressSummary.completed}/{progressSummary.total} 任务完成
            </span>
            <span className="text-[10px] text-gray-400">
              {progressSummary.failed > 0 && `${progressSummary.failed} 失败`}
            </span>
          </div>
          <div className="h-1.5 bg-gray-200 rounded-full overflow-hidden flex">
            {progressSummary.completed > 0 && (
              <div
                className="h-full bg-green-500 transition-all duration-300"
                style={{ width: `${(progressSummary.completed / progressSummary.total) * 100}%` }}
              />
            )}
            {progressSummary.failed > 0 && (
              <div
                className="h-full bg-red-400 transition-all duration-300"
                style={{ width: `${(progressSummary.failed / progressSummary.total) * 100}%` }}
              />
            )}
          </div>
        </div>
      )}

      {/* Filter bar + new task button */}
      <div className="flex items-center gap-1 px-3 py-2 border-b border-gray-100 flex-wrap">
        {/* View toggle */}
        <div className="flex items-center bg-gray-100 rounded-full p-0.5 mr-1">
          <button
            onClick={() => setViewMode("topology")}
            className={`text-[10px] font-medium px-2 py-0.5 rounded-full transition-colors ${
              viewMode === "topology" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {t("taskBoard.viewTopology") || "拓扑"}
          </button>
          <button
            onClick={() => setViewMode("flat")}
            className={`text-[10px] font-medium px-2 py-0.5 rounded-full transition-colors ${
              viewMode === "flat" ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {t("taskBoard.viewFlat") || "列表"}
          </button>
        </div>

        {(["all", "running", "pending", "blocked", "completed", "failed"] as const).map(
          (status) => (
            <button
              key={status}
              onClick={() => setFilterStatus(status)}
              className={`text-[10px] font-medium px-2 py-0.5 rounded-full transition-colors ${
                filterStatus === status
                  ? "bg-gray-900 text-white"
                  : "bg-gray-100 text-gray-500 hover:bg-gray-200"
              }`}
            >
              {status === "all" ? "All" : STATUS_LABELS[status]}
              {statusCounts[status] ? ` (${statusCounts[status]})` : ""}
            </button>
          )
        )}
        <div className="flex-1" />
        <button
          onClick={() => setShowNewTask(!showNewTask)}
          className="flex items-center gap-1 text-[10px] font-medium px-2 py-0.5 rounded-full bg-blue-50 text-blue-600 hover:bg-blue-100 transition-colors"
        >
          <Plus size={10} />
          新任务
        </button>
      </div>

      {/* New task form */}
      {showNewTask && (
        <NewTaskForm
          onCreate={handleCreateTask}
          workflowNodes={workflowNodes}
        />
      )}

      {/* Task list — topology or flat */}
      <div className="flex-1 overflow-y-auto divide-y divide-gray-100">
        {filteredTasks.length === 0 ? (
          <div className="text-center py-8">
            <p className="text-xs text-gray-400">
              {filterStatus === "all" ? "No tasks yet" : `No ${STATUS_LABELS[filterStatus]} tasks`}
            </p>
          </div>
        ) : viewMode === "topology" ? (
          <TaskTopology
            tasks={filteredTasks}
            artifacts={artifacts}
            messages={taskMessages}
            selectedTaskId={selectedTaskId}
            onSelect={(taskId) => selectTask(selectedTaskId === taskId ? null : taskId)}
          />
        ) : (
          filteredTasks.map((task) => (
            <TaskLeaf
              key={task.id}
              task={task}
              isSelected={selectedTaskId === task.id}
              onSelect={() => selectTask(selectedTaskId === task.id ? null : task.id)}
              messages={taskMessages[task.id] ?? []}
              artifacts={artifacts.filter((artifact) => artifact.task_id === task.id)}
              onSendMessage={(text) => handleSendMessage(task.id, text)}
              onRestart={() => handleRestart(task.id)}
              onUpdate={(patch) => handleUpdateTask(task.id, patch)}
              onAssign={(nodeId, nodeLabel, agentType, modelProvider, modelId, prompt) =>
                handleAssignTask(task.id, nodeId, nodeLabel, agentType, modelProvider, modelId, prompt)
              }
              workflowNodes={workflowNodes}
            />
          ))
        )}
      </div>
    </div>
  );
}
