"use client";

import { useState, useCallback, useMemo, useEffect, useRef } from "react";
import {
  PanelBottomOpen,
  PanelBottomClose,
  Maximize2,
  Minimize2,
  Brain,
  Terminal,
  Wrench,
  Filter,
  X,
  MessageSquare,
  MessageCircle,
  ListTree,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { NODE_META, STATUS_COLORS } from "@/lib/constants";
import type { RunStatus, NodeData } from "@/types/workflow";
import LLMOutput from "./LLMOutput";
import XtermStream from "./XtermStream";
import ToolCallList from "./ToolCallList";
import CommunicationPanel from "./CommunicationPanel";
import PlannerChatTab from "./PlannerChatTab";
import TimelinePanel from "./TimelinePanel";

// ---------------------------------------------------------------------------
// Tab definitions — Chat tab is added dynamically when in auto mode
// ---------------------------------------------------------------------------
const BASE_TABS = [
  { key: "llm", labelKey: "output.tab.llm", icon: Brain },
  { key: "shell", labelKey: "output.tab.shell", icon: Terminal },
  { key: "tools", labelKey: "output.tab.tools", icon: Wrench },
  { key: "comm", labelKey: "output.tab.comm", icon: MessageSquare },
  { key: "timeline", labelKey: "output.tab.timeline", icon: ListTree },
] as const;

const CHAT_TAB = { key: "chat", labelKey: "output.tab.chat", icon: MessageCircle } as const;

type TabKey = "llm" | "shell" | "tools" | "comm" | "timeline" | "chat";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const COLLAPSED_HEIGHT = 36; // px — just enough for the header bar
const EXPANDED_HEIGHT = 300; // px — comfortable reading area

// ---------------------------------------------------------------------------
// Status badge for the node detail header
// ---------------------------------------------------------------------------
function StatusDot({ status }: { status: RunStatus }) {
  if (status === "running") {
    return (
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
      </span>
    );
  }
  if (status === "completed") {
    return (
      <svg className="w-3.5 h-3.5 text-green-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
        <polyline points="20 6 9 17 4 12" />
      </svg>
    );
  }
  if (status === "failed") {
    return (
      <svg className="w-3.5 h-3.5 text-red-500 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// OutputPanel — collapsible bottom panel with LLM / Shell / Tools tabs
// ---------------------------------------------------------------------------
export default function OutputPanel() {
  const [expanded, setExpanded] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [activeTab, setActiveTab] = useState<TabKey>("shell");
  const autoExpandedRef = useRef(false);

  // Node list from workflow store for the filter dropdown
  const nodes = useWorkflowStore((s) => s.nodes) ?? [];
  const mode = useWorkflowStore((s) => s.mode);
  const t = useLocaleStore((s) => s.t);

  // Build tab list: add Chat tab in auto mode
  const TABS = mode === "auto"
    ? [...BASE_TABS, CHAT_TAB]
    : BASE_TABS;

  // Run state
  const runStatus = useRunStore((s) => s.status);
  const selectedRunNodeId = useRunStore((s) => s.selectedRunNodeId);
  const setSelectedRunNode = useRunStore((s) => s.setSelectedRunNode);
  const nodeStatuses = useRunStore((s) => s.nodeStatuses);
  const events = useRunStore((s) => s.events);

  const isRunActive = runStatus === "running" || runStatus === "paused";
  const hasRunData = runStatus !== "idle";

  // Single source of truth for the active filter node.
  // Syncs from selectedRunNodeId (canvas clicks) but can also be changed
  // directly via the dropdown.
  const [filterNodeId, setFilterNodeId] = useState<string>("");
  useEffect(() => {
    if (selectedRunNodeId) {
      setFilterNodeId(selectedRunNodeId);
    }
  }, [selectedRunNodeId]);

  // Auto-expand when run starts and first events arrive
  useEffect(() => {
    if (isRunActive && events.length > 0 && !autoExpandedRef.current) {
      autoExpandedRef.current = true;
      setExpanded(true);
    }
    if (!isRunActive) {
      autoExpandedRef.current = false;
    }
  }, [isRunActive, events.length]);

  // Auto-switch to LLM tab when LLM events first appear during a run
  const hasLLMRef = useRef(false);
  useEffect(() => {
    if (!hasLLMRef.current && activeTab !== "llm") {
      const hasLLM = events.some(
        (e) => e.type === "llm_token" || e.type === "llm_chunk"
      );
      if (hasLLM) {
        hasLLMRef.current = true;
        setActiveTab("llm");
      }
    }
  }, [events, activeTab]);

  // Reset the LLM-auto-switch flag when a new run starts
  useEffect(() => {
    if (runStatus === "idle") {
      hasLLMRef.current = false;
    }
  }, [runStatus]);

  // Effective nodeId: prefer selectedRunNodeId during/after a run,
  // otherwise fall back to the manual dropdown filter
  // Build node filter options
  const nodeOptions = useMemo(
    () => [
      { id: "", label: t("output.filter.allNodes") },
      ...nodes.map((n) => ({ id: n.id, label: n.data.label || n.id })),
    ],
    [nodes, t]
  );

  const toggleExpanded = useCallback(() => setExpanded((v) => !v), []);
  const toggleFullscreen = useCallback(() => {
    setFullscreen((value) => {
      if (!value) {
        setExpanded(true);
      }
      return !value;
    });
  }, []);

  const handleTabChange = useCallback((tab: TabKey) => {
    setActiveTab(tab);
    // Auto-expand when switching tabs
    setExpanded(true);
  }, []);

  useEffect(() => {
    if (!fullscreen) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setFullscreen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fullscreen]);

  const handleNodeFilter = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setFilterNodeId(e.target.value);
    },
    []
  );

  const panelHeight = fullscreen ? undefined : expanded ? EXPANDED_HEIGHT : COLLAPSED_HEIGHT;

  // Derive info for the selected run node header
  const runNode = useMemo(() => {
    if (!filterNodeId) return null;
    return nodes.find((n) => n.id === filterNodeId) ?? null;
  }, [nodes, filterNodeId]);
  const runNodeStatus: RunStatus = filterNodeId
    ? (nodeStatuses[filterNodeId] ?? "idle")
    : "idle";
  const runNodeLabel = runNode
    ? (runNode.data as NodeData).label || t(`node.${(runNode.data as NodeData).agentType}.label`)
    : filterNodeId ?? "";

  return (
    <div
      className={`w-full border-t border-gray-200 flex flex-col overflow-hidden ${
        fullscreen
          ? "fixed inset-0 z-[80] border-0 shadow-2xl"
          : "transition-[height] duration-200 ease-in-out"
      } ${activeTab === "shell" && expanded ? "bg-[#1e1e1e]" : "bg-white"}`}
      style={panelHeight ? { height: panelHeight } : undefined}
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
            value={filterNodeId}
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

        <button
          onClick={toggleFullscreen}
          className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
          aria-label={fullscreen ? t("output.exitFullscreen") : t("output.fullscreen")}
          title={fullscreen ? t("output.exitFullscreen") : t("output.fullscreen")}
        >
          {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
        </button>

        {/* Collapse / expand toggle */}
        <button
          onClick={() => {
            if (fullscreen && expanded) {
              setFullscreen(false);
              setExpanded(false);
              return;
            }
            toggleExpanded();
          }}
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
        <>
          {/* Node detail header (when a node is selected) */}
          {filterNodeId && (
            <div className="flex items-center gap-2 px-3 py-1.5 bg-gray-50 border-b border-gray-100 shrink-0">
              <StatusDot status={runNodeStatus} />
              <span className="text-xs font-medium text-gray-700 truncate">
                {runNodeLabel}
              </span>
              <span
                className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full ${
                  runNodeStatus === "running"
                    ? "bg-blue-100 text-blue-600"
                    : runNodeStatus === "completed"
                    ? "bg-green-100 text-green-600"
                    : runNodeStatus === "failed"
                    ? "bg-red-100 text-red-600"
                    : "bg-gray-100 text-gray-500"
                }`}
              >
                {runNodeStatus}
              </span>
              <button
                onClick={() => { setFilterNodeId(""); setSelectedRunNode(null); }}
                className="ml-auto p-0.5 rounded hover:bg-gray-200 text-gray-400 hover:text-gray-600 transition-colors"
                title={t("output.clearNodeFilter") || "Clear node filter"}
              >
                <X size={12} />
              </button>
            </div>
          )}
          {/* Tab content */}
          <div className={`flex-1 overflow-hidden ${activeTab === "shell" ? "bg-[#1e1e1e]" : ""}`}>
            {activeTab === "llm" && <LLMOutput nodeId={filterNodeId} />}
            {activeTab === "shell" && <XtermStream nodeId={filterNodeId} />}
            {activeTab === "tools" && <ToolCallList nodeId={filterNodeId} />}
            {activeTab === "comm" && <CommunicationPanel nodeId={filterNodeId} />}
            {activeTab === "timeline" && <TimelinePanel nodeId={filterNodeId} />}
            {activeTab === "chat" && <PlannerChatTab />}
          </div>
        </>
      )}
    </div>
  );
}
