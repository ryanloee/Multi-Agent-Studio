"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { StreamEvent, DirectorDecisionEvent, ReviewResultEvent, ReviewRetryEvent, WorkerSummaryEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const STATUS_COLORS: Record<string, string> = {
  pending: "bg-gray-200 text-gray-600",
  running: "bg-blue-100 text-blue-700",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  idle: "bg-gray-100 text-gray-500",
  cancelled: "bg-gray-200 text-gray-500",
};

const ACTION_ICONS: Record<string, string> = {
  scout: "\u{1F50D}",
  worker: "\u{1F527}",
  test: "\u{1F9EA}",
  done: "✅",
  failed: "❌",
};

const TYPE_ICONS: Record<string, string> = {
  node_started: "▶",
  node_completed: "✅",
  node_failed: "❌",
  director_decision: "\u{1F4CB}",
  worker_summary: "\u{1F4DD}",
  review_started: "\u{1F50D}",
  review_result: "\u{1F4CA}",
  review_retry: "\u{1F504}",
  llm_token: "\u{1F4AC}",
  llm_chunk: "\u{1F4AC}",
  tool_call: "\u{1F527}",
  tool_result: "✅",
  shell_stdout: "\u{1F4BB}",
  shell_stderr: "⚠️",
  error: "❌",
  run_started: "\u{1F680}",
  run_completed: "\u{1F3C1}",
  run_failed: "\u{1F4A5}",
  child_created: "\u{1F431}",
  progress_summary: "\u{1F4CA}",
  status: "\u{1F4DD}",
  agent_heartbeat: "\u{1F4E1}",
  permission_request: "\u{1F512}",
};

const TYPE_LABELS: Record<string, string> = {
  node_started: "开始",
  node_completed: "完成",
  node_failed: "失败",
  director_decision: "调度",
  worker_summary: "摘要",
  review_started: "审核中",
  review_result: "审核结果",
  review_retry: "重试",
  llm_token: "LLM",
  llm_chunk: "LLM",
  tool_call: "工具",
  tool_result: "结果",
  shell_stdout: "输出",
  shell_stderr: "错误",
  error: "错误",
  run_started: "启动",
  run_completed: "完成",
  run_failed: "失败",
  child_created: "创建",
  progress_summary: "进度",
  status: "状态",
  agent_heartbeat: "心跳",
  permission_request: "权限",
};

// Event types to show by default (noisy types filtered out)
const DEFAULT_VISIBLE_TYPES = new Set([
  "node_started", "node_completed", "node_failed",
  "director_decision",
  "worker_summary", "review_started", "review_result", "review_retry",
  "tool_call", "tool_result",
  "error",
  "run_started", "run_completed", "run_failed",
  "child_created",
  "progress_summary",
  "shell_stdout", "shell_stderr",
]);

function formatTimestamp(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function getContent(event: StreamEvent): string {
  const raw = (event as unknown as Record<string, unknown>).content;
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object") return JSON.stringify(raw);
  return "";
}

function parseToolCallContent(event: StreamEvent): { toolName: string; status: string; summary: string } {
  const ev = event as unknown as Record<string, unknown>;
  const eventName = ev.tool_name;
  let toolName = typeof eventName === "string" && eventName ? eventName : "";
  let status = "";
  let summary = "";

  // content may be a JSON string or already an object
  let parsed: Record<string, unknown> | null = null;
  const raw = ev.content;
  if (raw && typeof raw === "object") {
    parsed = raw as Record<string, unknown>;
  } else if (typeof raw === "string" && raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      // not JSON — use raw string as summary
      if (!toolName) toolName = raw.slice(0, 30);
      summary = raw;
      return { toolName, status, summary };
    }
  }

  if (parsed) {
    if (parsed.tool && !toolName) toolName = String(parsed.tool);
    const state = parsed.state as Record<string, unknown> | undefined;
    if (state?.status) status = String(state.status);
    const input = state?.input as Record<string, unknown> | undefined;
    if (input?.content && typeof input.content === "string") {
      summary = input.content;
    } else if (input?.command && typeof input.command === "string") {
      summary = input.command;
    } else if (input?.path && typeof input.path === "string") {
      summary = input.path;
    }
    if (!summary && state?.output) {
      summary = typeof state.output === "string" ? state.output : JSON.stringify(state.output);
    }
  }

  return { toolName, status, summary };
}

function truncate(text: string, max = 200): string {
  const compact = text.replace(/\s+/g, " ").trim();
  if (compact.length <= max) return compact;
  return `${compact.slice(0, max - 1)}…`;
}

// ---------------------------------------------------------------------------
// EventsTab — global timeline of ALL events
// ---------------------------------------------------------------------------
export default function EventsTab() {
  const t = useLocaleStore((s) => s.t);
  const allEvents = useRunStore((s) => s.events);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [showVerbose, setShowVerbose] = useState(false);

  const events = useMemo(
    () =>
      showVerbose
        ? allEvents.filter((e) => e.type !== "ping")
        : allEvents.filter((e) => DEFAULT_VISIBLE_TYPES.has(e.type)),
    [allEvents, showVerbose],
  );

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  if (events.length === 0 && allEvents.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-xs text-gray-400 bg-white">
        {t("events.noEvents")}
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-gray-100 shrink-0">
        <span className="text-xs text-gray-400">{events.length} events</span>
        <label className="flex items-center gap-1 ml-auto text-xs text-gray-500 cursor-pointer">
          <input
            type="checkbox"
            checked={showVerbose}
            onChange={(e) => setShowVerbose(e.target.checked)}
            className="rounded border-gray-300"
          />
          Verbose
        </label>
      </div>

      {/* Timeline */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-2 space-y-0.5">
        <div className="relative">
          <div className="absolute left-[15px] top-0 bottom-0 w-px bg-gray-200" />
          {events.map((event, idx) => {
            if (event.type === "tool_call" || event.type === "tool_result") {
              const { toolName, status, summary } = parseToolCallContent(event);
              const statusColor = status === "completed" ? "bg-green-50 text-green-600"
                : status === "failed" || status === "error" ? "bg-red-50 text-red-600"
                : "bg-amber-50 text-amber-600";
              const iconType = event.type === "tool_result" ? "tool_result" : "tool_call";
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1 group">
                  <div className="relative z-10 w-8 h-8 rounded-full bg-amber-50 border border-amber-200 flex items-center justify-center text-xs shrink-0">
                    {TYPE_ICONS[iconType]}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">
                        {toolName || "tool"}
                      </span>
                      {status && (
                        <span className={`text-[10px] font-medium px-1 py-0.5 rounded ${statusColor}`}>
                          {status}
                        </span>
                      )}
                      {event.node_id && (
                        <span className="text-[10px] text-gray-400 truncate max-w-[120px]">{event.node_id}</span>
                      )}
                    </div>
                    {summary && (
                      <p className="text-xs text-gray-600 mt-0.5 line-clamp-2 font-mono">
                        {truncate(summary, 200)}
                      </p>
                    )}
                  </div>
                </div>
              );
            }

            if (event.type === "director_decision") {
              const de = event as DirectorDecisionEvent;
              const icon = ACTION_ICONS[de.action] || "";
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1.5 group">
                  <div className="relative z-10 w-8 h-8 rounded-full bg-purple-50 border border-purple-200 flex items-center justify-center text-sm shrink-0">
                    {icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">
                        {de.action}
                      </span>
                      {de.iteration !== undefined && (
                        <span className="text-[10px] text-gray-400">#{de.iteration}</span>
                      )}
                    </div>
                    <p className="text-xs text-gray-700 mt-0.5 line-clamp-2">{de.reasoning}</p>
                    {de.target_files && de.target_files.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-0.5">
                        {de.target_files.map((f) => (
                          <span key={f} className="text-[10px] px-1 py-0.5 bg-gray-100 text-gray-600 rounded">
                            {f}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              );
            }

            if (event.type === "worker_summary") {
              const we = event as WorkerSummaryEvent;
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1.5 group">
                  <div className="relative z-10 w-8 h-8 rounded-full bg-blue-50 border border-blue-200 flex items-center justify-center text-sm shrink-0">
                    {"\u{1F4DD}"}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">
                        Worker 完成
                      </span>
                      <span className="text-[10px] text-gray-400 truncate max-w-[120px]">{we.task_id}</span>
                    </div>
                    <p className="text-xs text-gray-700 mt-0.5 line-clamp-3 font-mono whitespace-pre-wrap">
                      {truncate(we.content, 300)}
                    </p>
                  </div>
                </div>
              );
            }

            if (event.type === "review_started") {
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1.5 group">
                  <div className="relative z-10 w-8 h-8 rounded-full bg-amber-50 border border-amber-200 flex items-center justify-center text-sm shrink-0">
                    {"\u{1F50D}"}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">
                        审核中
                      </span>
                    </div>
                  </div>
                </div>
              );
            }

            if (event.type === "review_result") {
              const re = event as ReviewResultEvent;
              const isPass = re.result === "pass";
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1.5 group">
                  <div className={`relative z-10 w-8 h-8 rounded-full border flex items-center justify-center text-sm shrink-0 ${
                    isPass ? "bg-green-50 border-green-200" : "bg-red-50 border-red-200"
                  }`}>
                    {isPass ? "\u{2705}" : "\u{274C}"}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                        isPass ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"
                      }`}>
                        {isPass ? "审核通过" : "审核不通过"} (第 {re.attempt} 次)
                      </span>
                      <span className="text-[10px] text-gray-400 truncate max-w-[120px]">{re.task_id}</span>
                    </div>
                    <p className="text-xs text-gray-700 mt-0.5 line-clamp-2">{re.reason}</p>
                  </div>
                </div>
              );
            }

            if (event.type === "review_retry") {
              const rre = event as ReviewRetryEvent;
              return (
                <div key={`ev-${idx}`} className="relative flex items-start gap-2.5 py-1.5 group">
                  <div className="relative z-10 w-8 h-8 rounded-full bg-orange-50 border border-orange-200 flex items-center justify-center text-sm shrink-0">
                    {"\u{1F504}"}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                      <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-orange-100 text-orange-700">
                        重新派发 Worker (第 {rre.attempt + 1}/{rre.max_attempts} 次)
                      </span>
                      <span className="text-[10px] text-gray-400 truncate max-w-[120px]">{rre.task_id}</span>
                    </div>
                  </div>
                </div>
              );
            }

            const icon = TYPE_ICONS[event.type] || "•";
            const label = TYPE_LABELS[event.type] || event.type;
            const content = getContent(event);
            const isShell = event.type === "shell_stdout" || event.type === "shell_stderr";
            const isLlm = event.type === "llm_token" || event.type === "llm_chunk";

            return (
              <div
                key={`ev-${idx}`}
                className="relative flex items-start gap-2.5 py-1 group"
              >
                <div className="relative z-10 w-8 h-8 rounded-full bg-gray-50 border border-gray-200 flex items-center justify-center text-xs shrink-0">
                  {icon}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] font-mono text-gray-400">{formatTimestamp(event.timestamp)}</span>
                    <span className={`text-[10px] font-medium px-1 py-0.5 rounded ${
                      event.type === "error" || event.type === "node_failed"
                        ? "bg-red-50 text-red-600"
                        : event.type === "node_completed"
                          ? "bg-green-50 text-green-600"
                          : event.type === "node_started"
                            ? "bg-blue-50 text-blue-600"
                            : isLlm
                              ? "bg-indigo-50 text-indigo-600"
                              : "bg-gray-50 text-gray-600"
                    }`}>
                      {label}
                    </span>
                    {event.node_id && (
                      <span className="text-[10px] text-gray-400 truncate max-w-[120px]">{event.node_id}</span>
                    )}
                  </div>
                  {content && (
                    <p className={`text-xs mt-0.5 ${isShell ? "font-mono bg-gray-900 text-green-400 px-1.5 py-0.5 rounded" : "text-gray-600"} ${isShell ? "line-clamp-5" : "line-clamp-2"}`}>
                      {truncate(content, isShell ? 300 : 200)}
                    </p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
