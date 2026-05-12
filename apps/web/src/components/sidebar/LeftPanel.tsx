"use client";

import { useState, useEffect, useRef } from "react";
import { ListTodo, BookOpen } from "lucide-react";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useLocaleStore } from "@/stores/localeStore";
import TaskBoard from "@/components/panels/TaskBoard";
import SharedDocTab from "@/components/panels/SharedDocTab";

type LeftTab = "nodes" | "tasks" | "doc";

export default function LeftPanel({ workflowId }: { workflowId?: string }) {
  const [activeTab, setActiveTab] = useState<LeftTab>("nodes");
  const t = useLocaleStore((s) => s.t);

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
    setActiveTab("tasks");
  }, []);

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
        {tabBtn("tasks", <ListTodo size={14} />, t("leftPanel.tasks") || "任务", taskCount)}
        {tabBtn("doc", <BookOpen size={14} />, t("leftPanel.sharedDoc") || "文档")}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {activeTab === "doc" ? (
          <SharedDocTab workflowId={workflowId} />
        ) : (
          <TaskBoard workflowId={workflowId} />
        )}
      </div>
    </aside>
  );
}
