"use client";

import { useMemo, memo } from "react";
import {
  Ban,
  CheckCircle2,
  Circle,
  Clock,
  FileText,
  MessageSquare,
  XCircle,
} from "lucide-react";
import type { Artifact, Task, TaskMessage, TaskStatus } from "@/types/task";

const STATUS_ICON_MAP: Record<TaskStatus, { icon: typeof Circle; color: string; label: string }> = {
  pending:   { icon: Circle,       color: "text-gray-400",  label: "等待" },
  assigned:  { icon: Clock,        color: "text-blue-500",  label: "已分配" },
  running:   { icon: Clock,        color: "text-blue-600",  label: "运行中" },
  blocked:   { icon: Ban,          color: "text-amber-600", label: "咨询中" },
  completed: { icon: CheckCircle2, color: "text-green-600", label: "完成" },
  failed:    { icon: XCircle,      color: "text-red-600",   label: "失败" },
};

function parseDeps(task: Task): string[] {
  if (!task.dependencies) return [];
  try {
    const parsed = JSON.parse(task.dependencies);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

function buildLayers(tasks: Task[]): Task[][] {
  const byTaskId = new Map(tasks.map((task) => [task.id, task]));
  const byNodeId = new Map(tasks.flatMap((task) => task.assigned_node_id ? [[task.assigned_node_id, task] as const] : []));
  const levelCache = new Map<string, number>();

  function levelOf(task: Task, visiting = new Set<string>()): number {
    const cached = levelCache.get(task.id);
    if (cached !== undefined) return cached;
    if (visiting.has(task.id)) return 0;
    visiting.add(task.id);

    const deps = parseDeps(task)
      .map((dep) => byNodeId.get(dep) || byTaskId.get(dep))
      .filter((dep): dep is Task => Boolean(dep));
    const level = deps.length === 0
      ? 0
      : Math.max(...deps.map((dep) => levelOf(dep, visiting))) + 1;
    levelCache.set(task.id, level);
    visiting.delete(task.id);
    return level;
  }

  const layers: Task[][] = [];
  for (const task of tasks) {
    const level = levelOf(task);
    if (!layers[level]) layers[level] = [];
    layers[level].push(task);
  }

  return layers
    .filter(Boolean)
    .map((layer) => [...layer].sort((a, b) =>
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
    ));
}

function compact(text: string, max = 260): string {
  const value = text.replace(/\s+/g, " ").trim();
  return value.length > max ? `${value.slice(0, max - 1)}...` : value;
}

const TopologyTask = memo(function TopologyTask({
  task,
  artifacts,
  messages,
  selectedTaskId,
  onSelect,
}: {
  task: Task;
  artifacts: Artifact[];
  messages: TaskMessage[];
  selectedTaskId: string | null;
  onSelect: (taskId: string) => void;
}) {
  const statusCfg = STATUS_ICON_MAP[task.status];
  const StatusIcon = statusCfg.icon;
  const isSelected = selectedTaskId === task.id;
  const importantMessages = messages.filter((message) =>
    message.message_type === "worker_question" ||
    message.message_type === "worker_answer" ||
    message.message_type === "planner_question" ||
    message.message_type === "planner_answer" ||
    message.message_type === "artifact_created"
  );
  const deps = parseDeps(task);

  return (
    <div className={`border-b border-gray-100 ${isSelected ? "bg-blue-50/60" : "bg-white"}`}>
      <button
        onClick={() => onSelect(task.id)}
        className="w-full px-3 py-2 text-left hover:bg-gray-50"
      >
        <div className="flex items-center gap-1.5">
          <StatusIcon size={12} className={`${statusCfg.color} shrink-0`} />
          <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-gray-700">
            {task.title}
          </span>
          {task.progress > 0 && task.progress < 100 && (
            <span className="shrink-0 font-mono text-[9px] text-blue-500">{task.progress}%</span>
          )}
          {artifacts.length > 0 && (
            <span className="flex shrink-0 items-center gap-0.5 text-[9px] text-emerald-600">
              <FileText size={9} />
              {artifacts.length}
            </span>
          )}
          {importantMessages.length > 0 && (
            <span className="flex shrink-0 items-center gap-0.5 text-[9px] text-purple-600">
              <MessageSquare size={9} />
              {importantMessages.length}
            </span>
          )}
        </div>
        <div className="mt-1 flex items-center gap-2 text-[9px] text-gray-400">
          <span>{statusCfg.label}</span>
          {task.assigned_worker_label && <span className="truncate">{task.assigned_worker_label}</span>}
        </div>
      </button>

      {isSelected && (
        <div className="space-y-2 px-3 pb-3 text-[10px] text-gray-600">
          {deps.length > 0 && (
            <div>
              <div className="mb-0.5 font-medium text-gray-500">依赖</div>
              <div className="flex flex-wrap gap-1">
                {deps.map((dep) => (
                  <span key={dep} className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-500">{dep}</span>
                ))}
              </div>
            </div>
          )}
          {task.description && (
            <div>
              <div className="mb-0.5 font-medium text-gray-500">Prompt</div>
              <div className="max-h-24 overflow-y-auto whitespace-pre-wrap rounded border border-gray-100 bg-white p-1.5">
                {compact(task.description, 900)}
              </div>
            </div>
          )}
          {importantMessages.length > 0 && (
            <div>
              <div className="mb-0.5 font-medium text-gray-500">Messages</div>
              <div className="max-h-24 space-y-1 overflow-y-auto">
                {importantMessages.map((message) => (
                  <div key={message.id} className="rounded bg-purple-50 px-1.5 py-1">
                    <span className="font-medium text-purple-700">{message.sender_id}</span>
                    {message.target_node_id && <span className="text-purple-500"> {"->"} {message.target_node_id}</span>}
                    <span className="text-gray-500">: {message.content}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {artifacts.length > 0 && (
            <div>
              <div className="mb-0.5 font-medium text-gray-500">Artifacts</div>
              <div className="max-h-28 space-y-1 overflow-y-auto">
                {artifacts.map((artifact) => (
                  <div key={artifact.id} className="rounded bg-emerald-50 px-1.5 py-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium text-emerald-700">{artifact.title}</span>
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
            </div>
          )}
        </div>
      )}
    </div>
  );
});

export default function TaskTopology({
  tasks,
  artifacts,
  messages,
  selectedTaskId,
  onSelect,
}: {
  tasks: Task[];
  artifacts: Artifact[];
  messages: Record<string, TaskMessage[]>;
  selectedTaskId: string | null;
  onSelect: (taskId: string) => void;
}) {
  const layers = useMemo(() => buildLayers(tasks), [tasks]);
  const artifactsByTask = useMemo(() => {
    const map: Record<string, Artifact[]> = {};
    for (const artifact of artifacts) {
      if (!artifact.task_id) continue;
      if (!map[artifact.task_id]) map[artifact.task_id] = [];
      map[artifact.task_id].push(artifact);
    }
    return map;
  }, [artifacts]);

  if (tasks.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center p-6">
        <p className="text-xs text-gray-400">No tasks</p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {layers.map((layer, index) => (
        <div key={index}>
          <div className="sticky top-0 z-10 border-b border-gray-100 bg-gray-50 px-3 py-1 text-[10px] font-medium text-gray-500">
            Layer {index + 1}
            {layer.length > 1 && <span className="ml-1 text-gray-400">并行 x{layer.length}</span>}
          </div>
          {layer.map((task) => (
            <TopologyTask
              key={task.id}
              task={task}
              artifacts={artifactsByTask[task.id] ?? []}
              messages={messages[task.id] ?? []}
              selectedTaskId={selectedTaskId}
              onSelect={onSelect}
            />
          ))}
        </div>
      ))}
    </div>
  );
}
