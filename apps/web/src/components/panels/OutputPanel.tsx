"use client";

import { useState, useCallback, useMemo } from "react";
import {
  PanelBottomOpen,
  PanelBottomClose,
  Brain,
  Terminal,
  Wrench,
  Filter,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import LLMOutput from "./LLMOutput";
import XtermStream from "./XtermStream";
import ToolCallList from "./ToolCallList";

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------
const TABS = [
  { key: "llm", labelKey: "output.tab.llm", icon: Brain },
  { key: "shell", labelKey: "output.tab.shell", icon: Terminal },
  { key: "tools", labelKey: "output.tab.tools", icon: Wrench },
] as const;

type TabKey = (typeof TABS)[number]["key"];

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const COLLAPSED_HEIGHT = 36; // px — just enough for the header bar
const EXPANDED_HEIGHT = 300; // px — comfortable reading area

// ---------------------------------------------------------------------------
// OutputPanel — collapsible bottom panel with LLM / Shell / Tools tabs
// ---------------------------------------------------------------------------
export default function OutputPanel() {
  const [expanded, setExpanded] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>("shell");
  const [selectedNodeId, setSelectedNodeId] = useState<string>("");

  // Node list from workflow store for the filter dropdown
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const t = useLocaleStore((s) => s.t);

  // Build node filter options
  const nodeOptions = useMemo(
    () => [
      { id: "", label: t("output.filter.allNodes") },
      ...nodes.map((n) => ({ id: n.id, label: n.data.label || n.id })),
    ],
    [nodes, t]
  );

  const toggleExpanded = useCallback(() => setExpanded((v) => !v), []);

  const handleTabChange = useCallback((tab: TabKey) => {
    setActiveTab(tab);
    // Auto-expand when switching tabs
    setExpanded(true);
  }, []);

  const handleNodeFilter = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setSelectedNodeId(e.target.value);
    },
    []
  );

  const panelHeight = expanded ? EXPANDED_HEIGHT : COLLAPSED_HEIGHT;

  return (
    <div
      className="w-full bg-white border-t border-gray-200 flex flex-col transition-[height] duration-200 ease-in-out overflow-hidden"
      style={{ height: panelHeight }}
    >
      {/* ---- Header bar (always visible) ---- */}
      <div className="flex items-center h-9 shrink-0 border-b border-gray-100 px-2 gap-1">
        {/* Tab buttons */}
        <div className="flex items-center gap-0.5">
          {TABS.map((tab) => {
            const Icon = tab.icon;
            const isActive = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                onClick={() => handleTabChange(tab.key)}
                className={`
                  flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium transition-colors
                  ${
                    isActive
                      ? "bg-gray-900 text-white"
                      : "text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                  }
                `}
              >
                <Icon size={13} />
                {t(tab.labelKey)}
              </button>
            );
          })}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* Node filter dropdown */}
        <div className="flex items-center gap-1 mr-1">
          <Filter size={12} className="text-gray-400" />
          <select
            value={selectedNodeId}
            onChange={handleNodeFilter}
            className="text-xs border border-gray-200 rounded px-1.5 py-0.5 bg-white text-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400 max-w-[160px]"
          >
            {nodeOptions.map((opt) => (
              <option key={opt.id} value={opt.id}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        {/* Collapse / expand toggle */}
        <button
          onClick={toggleExpanded}
          className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label={expanded ? t("output.collapse") : t("output.expand")}
        >
          {expanded ? (
            <PanelBottomClose size={16} />
          ) : (
            <PanelBottomOpen size={16} />
          )}
        </button>
      </div>

      {/* ---- Content area (only when expanded) ---- */}
      {expanded && (
        <div className="flex-1 overflow-hidden">
          {activeTab === "llm" && <LLMOutput nodeId={selectedNodeId} />}
          {activeTab === "shell" && <XtermStream nodeId={selectedNodeId} />}
          {activeTab === "tools" && <ToolCallList nodeId={selectedNodeId} />}
        </div>
      )}
    </div>
  );
}
