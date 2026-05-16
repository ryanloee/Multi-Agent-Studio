"use client";

import { useState, useEffect, useRef, useMemo } from "react";
import { ListTodo, BookOpen, ClipboardList, ArrowDown } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import TaskBoard from "@/components/panels/TaskBoard";
import SharedDocTab from "@/components/panels/SharedDocTab";
import type { WorkflowEdge, WorkflowNode } from "@/types/workflow";

type LeftTab = "task_object" | "tasks" | "doc";

export default function LeftPanel({ workflowId }: { workflowId?: string }) {
  const [activeTab, setActiveTab] = useState<LeftTab>("task_object");
  const t = useLocaleStore((s) => s.t);
  const goal = useWorkflowStore((s) => s.goal);
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);
  const lifecyclePhase = useWorkflowStore((s) => s.lifecyclePhase);
  const blockers = useWorkflowStore((s) => s.blockers);
  const plannerUiState = useWorkflowStore((s) => s.plannerUiState);
  const plannerDraftState = useWorkflowStore((s) => s.plannerDraftState);
  const plannerActionState = useWorkflowStore((s) => s.plannerActionState);
  const plannerSubStage = useWorkflowStore((s) => s.plannerSubStage);

  const runStatus = useRunStore((s) => s.status);
  const taskCount = useTaskStore((s) => s.tasks.length);

  // Auto-switch to tasks tab when run starts
  const prevRunStatusRef = useRef(runStatus);
  useEffect(() => {
    if (
      (runStatus === "running" || runStatus === "paused") &&
      prevRunStatusRef.current !== "running" &&
      prevRunStatusRef.current !== "paused"
    ) {
      setActiveTab("tasks");
    }
    prevRunStatusRef.current = runStatus;
  }, [runStatus]);

  useEffect(() => {
    setActiveTab("task_object");
  }, []);

  useEffect(() => {
    if (plannerActionState?.action === "update_dag" || plannerActionState?.action === "set_ready") {
      setActiveTab("task_object");
    }
  }, [plannerActionState]);

  const plannedNodes = nodes.filter((node) => node.id !== "planner");
  const draftTaskObject = plannerDraftState?.task_object;
  const taskObject = plannerUiState.task_object ?? draftTaskObject;
  const plannedTaskItems = plannerUiState.task_board ?? plannerDraftState?.task_board ?? [];
  const draftDag = plannerDraftState?.dag;
  const plannedNodeCount = plannedNodes.length > 0
    ? plannedNodes.length
    : Array.isArray(draftDag?.nodes) ? draftDag.nodes.length : 0;
  const nodeTypeCounts = plannedNodes.reduce<Record<string, number>>((acc, node) => {
    acc[node.type] = (acc[node.type] || 0) + 1;
    return acc;
  }, {});
  const flowLayers = useMemo(() => buildFlowLayers(plannedNodes, edges), [plannedNodes, edges]);

  // Tab button helper
  const tabBtn = (tab: LeftTab, icon: React.ReactNode, label: string, badge?: number) => (
    <button
      onClick={() => setActiveTab(tab)}
      className={`flex items-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors border-b-2 relative ${
        activeTab === tab
          ? "border-blue-500 text-blue-600"
          : "border-transparent text-gray-400 hover:text-gray-600"
      }`}
    >
      {icon}
      {label}
      {badge !== undefined && badge > 0 && (
        <span className="ml-0.5 text-[10px] bg-blue-100 text-blue-600 rounded-full px-1.5 py-px leading-none font-semibold">
          {badge}
        </span>
      )}
    </button>
  );

  return (
    <aside className="w-[248px] h-full bg-white border-r border-gray-200 flex flex-col overflow-hidden shrink-0">
      {/* Tab header */}
      <div className="flex items-center border-b border-gray-100 shrink-0">
        {tabBtn("task_object", <ClipboardList size={14} />, "任务对象")}
        {tabBtn("tasks", <ListTodo size={14} />, t("leftPanel.tasks") || "任务", taskCount)}
        {tabBtn("doc", <BookOpen size={14} />, t("leftPanel.sharedDoc") || "文档")}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === "task_object" ? (
          <div className="h-full overflow-y-auto p-3 space-y-3 text-xs text-gray-700">
            <div className="rounded-lg border border-gray-200 bg-gray-50 p-3">
              <div className="text-[11px] font-semibold text-gray-500">当前阶段</div>
              <div className="mt-1 text-sm font-medium text-gray-800">
                {lifecyclePhase}
                {plannerSubStage ? ` · ${plannerSubStage}` : ""}
              </div>
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-[11px] font-semibold text-gray-500">目标 / 问题</div>
              <div className="mt-1 whitespace-pre-wrap break-words text-gray-800">
                {taskObject?.objective?.trim() ||
                  goal?.trim() ||
                  "还没有明确的任务目标。可以先在 Planner 中描述要处理的项目、问题或需求。"}
              </div>
              {taskObject?.title && (
                <div className="mt-2 rounded-md bg-blue-50 px-2 py-1.5 text-[11px] font-semibold text-blue-700">
                  {taskObject.title}
                </div>
              )}
              {taskObject?.background && (
                <div className="mt-2 text-[11px] leading-relaxed text-gray-600">
                  {taskObject.background}
                </div>
              )}
              <CompactList title="验收标准" items={taskObject?.success_criteria} />
              <CompactList title="约束条件" items={taskObject?.constraints} />
              <CompactList title="待确认问题" items={taskObject?.open_questions} />
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-[11px] font-semibold text-gray-500">当前规划</div>
              <div className="mt-1 text-gray-800">
                {plannedNodeCount > 0 ? `已生成 ${plannedNodeCount} 个执行节点` : "还没有形成可执行节点"}
              </div>
              {plannerDraftState?.system_generated_dag && (
                <div className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-700">
                  当前 DAG 草案由系统补全，建议继续让 Planner 精化。
                </div>
              )}
              {plannedNodes.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(nodeTypeCounts).map(([type, count]) => (
                    <span
                      key={`${type}-${count}`}
                      className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700"
                    >
                      {type} × {count}
                    </span>
                  ))}
                </div>
              )}
              {plannerActionState?.message && (
                <div className="mt-2 rounded-md bg-gray-50 px-2 py-1.5 text-[11px] leading-relaxed text-gray-600">
                  {plannerActionState.message}
                </div>
              )}
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-[11px] font-semibold text-gray-500">可视化流程</div>
              {flowLayers.length === 0 ? (
                <div className="mt-2 text-[11px] leading-relaxed text-gray-500">
                  Planner 一旦形成规划，这里会自动按阶段展示任务流和依赖顺序。
                </div>
              ) : (
                <div className="mt-2 space-y-2">
                  {flowLayers.map((layer, layerIndex) => (
                    <div key={`layer-${layerIndex}`}>
                      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">
                        第 {layerIndex + 1} 阶段
                      </div>
                      <div className="space-y-1.5">
                        {layer.map((node) => (
                          <div
                            key={node.id}
                            className="rounded-lg border border-blue-100 bg-blue-50/70 px-2.5 py-2"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="truncate text-[11px] font-semibold text-blue-800">
                                {String(node.data.label || node.id)}
                              </div>
                              <span className="shrink-0 rounded-full bg-white px-1.5 py-0.5 text-[9px] font-medium text-blue-600">
                                {node.type}
                              </span>
                            </div>
                            {node.data.prompt ? (
                              <div className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-blue-700/80">
                                {String(node.data.prompt)}
                              </div>
                            ) : null}
                          </div>
                        ))}
                      </div>
                      {layerIndex < flowLayers.length - 1 && (
                        <div className="flex justify-center py-1 text-gray-300">
                          <ArrowDown size={13} />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-[11px] font-semibold text-gray-500">工作目录</div>
              <div className="mt-1 break-all text-gray-800">
                {workspaceDirectory?.trim() || "未设置"}
              </div>
            </div>
            {blockers.length > 0 && (
              <div className="rounded-lg border border-red-200 bg-red-50 p-3">
                <div className="text-[11px] font-semibold text-red-700">阻塞项</div>
                <div className="mt-1 space-y-1 text-red-700">
                  {blockers.map((item) => (
                    <div key={`${item.code}-${item.message}`}>- {item.message}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : activeTab === "doc" ? (
          <SharedDocTab workflowId={workflowId} />
        ) : (
          <div className="h-full overflow-y-auto">
            {plannedTaskItems.length > 0 && (
              <div className="border-b border-gray-100 p-3">
                <div className="mb-2 text-[11px] font-semibold text-gray-500">Planner 规划任务</div>
                <div className="space-y-2">
                  {plannedTaskItems.map((item) => (
                    <div key={item.id} className="rounded-lg border border-blue-100 bg-blue-50/60 p-2.5 text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <div className="font-semibold text-blue-800">{item.title}</div>
                        <span className="shrink-0 rounded-full bg-white px-1.5 py-0.5 text-[9px] font-medium text-blue-600">
                          {item.status || "planned"}
                        </span>
                      </div>
                      {item.description && (
                        <div className="mt-1 text-[11px] leading-relaxed text-blue-700/80">
                          {item.description}
                        </div>
                      )}
                      {item.node_id && (
                        <div className="mt-1 text-[10px] text-blue-500">节点：{item.node_id}</div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
            <TaskBoard workflowId={workflowId} />
          </div>
        )}
      </div>
    </aside>
  );
}

function CompactList({ title, items }: { title: string; items?: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-2">
      <div className="text-[10px] font-semibold text-gray-500">{title}</div>
      <div className="mt-1 space-y-1 text-[11px] leading-relaxed text-gray-600">
        {items.map((item) => (
          <div key={item}>- {item}</div>
        ))}
      </div>
    </div>
  );
}

function buildFlowLayers(nodes: WorkflowNode[], edges: WorkflowEdge[]): WorkflowNode[][] {
  if (nodes.length === 0) return [];

  const nodeMap = new Map(nodes.map((node) => [node.id, node]));
  const indegree = new Map<string, number>();
  const outgoing = new Map<string, string[]>();

  for (const node of nodes) {
    indegree.set(node.id, 0);
    outgoing.set(node.id, []);
  }

  for (const edge of edges) {
    if (!nodeMap.has(edge.source) || !nodeMap.has(edge.target)) continue;
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
    outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge.target]);
  }

  const currentLayer = nodes
    .filter((node) => (indegree.get(node.id) ?? 0) === 0)
    .map((node) => node.id);
  const layers: WorkflowNode[][] = [];
  const visited = new Set<string>();

  while (currentLayer.length > 0) {
    const layerIds = [...currentLayer];
    currentLayer.length = 0;
    const layerNodes = layerIds
      .map((id) => nodeMap.get(id))
      .filter((node): node is WorkflowNode => Boolean(node));
    for (const id of layerIds) {
      if (visited.has(id)) continue;
      visited.add(id);
      for (const next of outgoing.get(id) ?? []) {
        indegree.set(next, (indegree.get(next) ?? 1) - 1);
        if ((indegree.get(next) ?? 0) <= 0) {
          currentLayer.push(next);
        }
      }
    }
    if (layerNodes.length > 0) {
      layers.push(layerNodes);
    }
  }

  const remaining = nodes.filter((node) => !visited.has(node.id));
  if (remaining.length > 0) {
    layers.push(remaining);
  }

  return layers;
}
