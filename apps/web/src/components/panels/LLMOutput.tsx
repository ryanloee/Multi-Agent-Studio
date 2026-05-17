"use client";

import { useEffect, useMemo, useRef } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import MarkdownMessage from "@/components/common/MarkdownMessage";
import type { AgentHeartbeatEvent, AgentStatusEvent, LLMTokenEvent, LLMChunkEvent, ToolCallEvent, ToolResultEvent, PermissionRequestEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface LLMOutputProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

function escapeFence(content: string): string {
  return content.replace(/```/g, "``\\`");
}

// ---------------------------------------------------------------------------
// LLMOutput — Agent tab: reasoning + thinking, tool calls as summary lines
// ---------------------------------------------------------------------------
export default function LLMOutput({ nodeId = "" }: LLMOutputProps) {
  const t = useLocaleStore((s) => s.t);
  const scrollRef = useRef<HTMLDivElement>(null);

  const allEvents = useRunStore((s) => s.events);

  const events = useMemo(
    () =>
      allEvents.filter(
        (e): e is LLMTokenEvent | LLMChunkEvent | ToolCallEvent | ToolResultEvent | AgentHeartbeatEvent | AgentStatusEvent | PermissionRequestEvent =>
          (e.type === "llm_token" || e.type === "llm_chunk" ||
           e.type === "tool_call" || e.type === "tool_result" ||
           e.type === "agent_heartbeat" || e.type === "agent_status" ||
           e.type === "permission_request") &&
          (nodeId === "" || e.node_id === nodeId)
      ),
    [allEvents, nodeId]
  );

  // Build markdown: LLM tokens/thinking in full, tool events as one-line summaries
  const markdown = useMemo(() => {
    const blocks: string[] = [];
    let assistantBuffer = "";
    let thinkingBuffer = "";
    let inThinking = false;

    const flushAssistant = () => {
      const trimmed = assistantBuffer.trim();
      if (!trimmed) return;
      blocks.push(trimmed);
      assistantBuffer = "";
    };

    const flushThinking = () => {
      const trimmed = thinkingBuffer.trim();
      if (!trimmed) return;
      blocks.push(`> ${t("llm.thinking")}\n\n\`\`\`text\n${escapeFence(trimmed)}\n\`\`\``);
      thinkingBuffer = "";
    };

    for (const ev of events) {
      if (ev.type === "llm_token") {
        if (inThinking) { flushThinking(); inThinking = false; }
        assistantBuffer += ev.content;
      } else if (ev.type === "llm_chunk") {
        const isThinking = (ev as LLMChunkEvent).metadata?.thinking === true;
        if (isThinking) {
          flushAssistant();
          if (!inThinking) inThinking = true;
          thinkingBuffer += ev.content;
        } else {
          if (inThinking) { flushThinking(); inThinking = false; }
          assistantBuffer += ev.content;
        }
      } else if (ev.type === "tool_call") {
        flushAssistant();
        if (inThinking) { flushThinking(); inThinking = false; }
        // Summary line only — no payload detail
        const tc = ev as ToolCallEvent;
        blocks.push(`> ${t("llm.toolCall")} \`${tc.tool_name || "tool"}\``);
      } else if (ev.type === "tool_result") {
        flushAssistant();
        if (inThinking) { flushThinking(); inThinking = false; }
        // Summary line only
        const tr = ev as ToolResultEvent;
        const short = (tr.content || "").slice(0, 120);
        blocks.push(`> ✅ \`${tr.tool_name || "tool"}\`${short ? ` — ${short}` : ""}`);
      } else if (ev.type === "agent_heartbeat") {
        flushAssistant();
        if (inThinking) { flushThinking(); inThinking = false; }
        const heartbeat = ev as AgentHeartbeatEvent;
        blocks.push(`> 📡 ${heartbeat.content}`);
      } else if (ev.type === "agent_status") {
        flushAssistant();
        if (inThinking) { flushThinking(); inThinking = false; }
        const status = ev as AgentStatusEvent;
        const label = status.status_type === "busy" ? t("llm.statusBusy")
                    : status.status_type === "retry" ? t("llm.statusRetry")
                    : status.status_type === "idle"   ? t("llm.statusIdle")
                    : status.status_type;
        blocks.push(`> ${label}`);
      } else if (ev.type === "permission_request") {
        flushAssistant();
        if (inThinking) { flushThinking(); inThinking = false; }
        const req = ev as PermissionRequestEvent;
        blocks.push(`> ${t("llm.permissionRequest")}\`${req.tool_name}\` → \`${req.target}\``);
      }
    }

    flushAssistant();
    if (inThinking) flushThinking();

    return blocks.join("\n\n");
  }, [events, t]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [markdown, events.length]);

  if (events.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">
        {t("llm.waiting")}
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="w-full h-full overflow-y-auto bg-white">
      <div className="max-w-5xl px-4 py-3">
        <MarkdownMessage content={markdown} />
      </div>
    </div>
  );
}
