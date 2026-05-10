"use client";

import { useState, useEffect, useRef } from "react";
import { Blocks, ListTodo } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import Sidebar from "./Sidebar";
import TaskBoard from "@/components/panels/TaskBoard";

type LeftTab = "nodes" | "tasks";

export default function LeftPanel({ workflowId }: { workflowId?: string }) {
  const [activeTab, setActiveTab] = useState<LeftTab>("nodes");
  const t = useLocaleStore((s) => s.t);
  const mode = useWorkflowStore((s) => s.mode);

  const runStatus = useRunStore((s) => s.status);
  const taskCount = useTaskStore((s) => s.tasks.length);

  // In auto mode, always show tasks tab and hide node palette
  const showNodePalette = mode === "manual";

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

  // Force tasks tab in auto mode
  useEffect(() => {
    if (mode === "auto") setActiveTab("tasks");
  }, [mode]);

  return (
    <aside className="w-[248px] h-full bg-white border-r border-gray-200 flex flex-col overflow-hidden shrink-0">
      {/* Tab header — hide node palette tab in auto mode */}
      {showNodePalette && (
        <div className="flex items-center border-b border-gray-100 shrink-0">
          <button
            onClick={() => setActiveTab("nodes")}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium transition-colors border-b-2 ${
              activeTab === "nodes"
                ? "border-blue-500 text-blue-600"
                : "border-transparent text-gray-400 hover:text-gray-600"
            }`}
          >
            <Blocks size={14} />
            {t("sidebar.title")}
          </button>
          <button
            onClick={() => setActiveTab("tasks")}
            className={`flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium transition-colors border-b-2 relative ${
              activeTab === "tasks"
                ? "border-blue-500 text-blue-600"
                : "border-transparent text-gray-400 hover:text-gray-600"
            }`}
          >
            <ListTodo size={14} />
            {t("leftPanel.tasks") || "任务"}
            {taskCount > 0 && (
              <span className="ml-0.5 text-[10px] bg-blue-100 text-blue-600 rounded-full px-1.5 py-px leading-none font-semibold">
                {taskCount}
              </span>
            )}
          </button>
        </div>
      )}

      {/* Auto mode: show tasks-only header */}
      {!showNodePalette && (
        <div className="flex items-center border-b border-gray-100 shrink-0 px-4 py-2.5">
          <div className="flex items-center gap-1.5 text-xs font-medium text-blue-600">
            <ListTodo size={14} />
            {t("leftPanel.tasks") || "任务"}
            {taskCount > 0 && (
              <span className="ml-0.5 text-[10px] bg-blue-100 text-blue-600 rounded-full px-1.5 py-px leading-none font-semibold">
                {taskCount}
              </span>
            )}
          </div>
        </div>
      )}

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === "nodes" && showNodePalette ? (
          <Sidebar />
        ) : (
          <TaskBoard workflowId={workflowId} />
        )}
      </div>
    </aside>
  );
}
