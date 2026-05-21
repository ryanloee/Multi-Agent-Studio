"use client";

import { useState, useEffect, useRef } from "react";
import { ListTodo, ClipboardList, Target } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useLocaleStore } from "@/stores/localeStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import TaskBoard from "@/components/panels/TaskBoard";

type LeftTab = "overview" | "tasks" | "goal";

export default function LeftPanel({ workflowId }: { workflowId?: string }) {
  const [activeTab, setActiveTab] = useState<LeftTab>("overview");
  const t = useLocaleStore((s) => s.t);
  const goal = useWorkflowStore((s) => s.goal);
  const blockers = useWorkflowStore((s) => s.blockers);
  const plannerUiState = useWorkflowStore((s) => s.plannerUiState);
  const plannerDraftState = useWorkflowStore((s) => s.plannerDraftState);
  const plannerActionState = useWorkflowStore((s) => s.plannerActionState);

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
    setActiveTab("overview");
  }, []);

  useEffect(() => {
    if (plannerActionState?.action === "update_dag" || plannerActionState?.action === "set_ready") {
      setActiveTab("overview");
    }
  }, [plannerActionState]);

  const draftTaskObject = plannerDraftState?.task_object;
  const taskObject = plannerUiState.task_object ?? draftTaskObject;
  const plannedTaskItems = plannerUiState.task_board ?? plannerDraftState?.task_board ?? [];

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
        {tabBtn("overview", <ClipboardList size={14} />, t("leftPanel.overview"))}
        {tabBtn("goal", <Target size={14} />, t("leftPanel.goal"))}
        {tabBtn("tasks", <ListTodo size={14} />, t("leftPanel.tasks"), taskCount)}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === "overview" ? (
          <div className="h-full overflow-y-auto p-3 space-y-3 text-xs text-gray-700">
            {/* Goal / Problem */}
            <div className="rounded-lg border border-gray-200 bg-white p-3">
              <div className="text-[11px] font-semibold text-gray-500">{t("leftPanel.goal")}</div>
              <div className="mt-1 whitespace-pre-wrap break-words text-gray-800">
                {taskObject?.objective?.trim() ||
                  goal?.trim() ||
                  t("leftPanel.noGoal")}
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
              <CompactList title={t("leftPanel.successCriteria")} items={taskObject?.success_criteria} />
              <CompactList title={t("leftPanel.constraints")} items={taskObject?.constraints} />
              <CompactList title={t("leftPanel.openQuestions")} items={taskObject?.open_questions} />
            </div>

            {/* Blockers */}
            {blockers.length > 0 && (
              <div className="rounded-lg border border-red-200 bg-red-50 p-3">
                <div className="text-[11px] font-semibold text-red-700">{t("leftPanel.blockers")}</div>
                <div className="mt-1 space-y-1 text-red-700">
                  {blockers.map((item) => (
                    <div key={`${item.code}-${item.message}`}>- {item.message}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : activeTab === "goal" ? (
          <GoalTab />
        ) : (
          <div className="h-full overflow-y-auto">
            {plannedTaskItems.length > 0 && (
              <div className="border-b border-gray-100 p-3">
                <div className="mb-2 text-[11px] font-semibold text-gray-500">{t("leftPanel.plannerTasks")}</div>
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
                        <div className="mt-1 text-[10px] text-blue-500">{t("leftPanel.node")}{item.node_id}</div>
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

function GoalTab() {
  const t = useLocaleStore((s) => s.t);
  const goal = useWorkflowStore((s) => s.goal);
  const updateGoal = useWorkflowStore((s) => s.updateGoal);

  return (
    <div className="h-full overflow-y-auto p-3 space-y-3">
      <div className="space-y-1.5">
        <label className="block text-[11px] font-semibold text-gray-500 uppercase tracking-wider">
          {t("workflow.goalLabel")}
        </label>
        <textarea
          value={goal}
          onChange={(e) => updateGoal(e.target.value)}
          placeholder={t("workflow.goalPlaceholder")}
          rows={12}
          className="w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-xs text-gray-800 placeholder-gray-300 resize-y focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all leading-relaxed"
        />
      </div>
    </div>
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
