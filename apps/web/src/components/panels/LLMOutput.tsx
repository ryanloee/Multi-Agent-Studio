"use client";

import { useEffect, useMemo, useRef } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import MarkdownMessage from "@/components/common/MarkdownMessage";
import type { AgentHeartbeatEvent, LLMTokenEvent, LLMChunkEvent, PermissionRequestEvent, ToolCallEvent, ToolResultEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface LLMOutputProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

function normalizeToolPayload(content: string): string {
  const trimmed = content.trim();
  if (!trimmed) return "(empty)";
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return trimmed;
  }
}

function escapeFence(content: string): string {
  return content.replace(/```/g, "``\\`");
}

// ---------------------------------------------------------------------------
// LLMOutput — typewriter-effect display of llm_token / llm_chunk / tool events
// ---------------------------------------------------------------------------
export default function LLMOutput({ nodeId = "" }: LLMOutputProps) {
  const t = useLocaleStore((s) => s.t);
  const scrollRef = useRef<HTMLDivElement>(null);

  const allEvents = useRunStore((s) => s.events);

  // Include LLM, tool, permission, and heartbeat events so the panel keeps
  // showing observable progress even when the provider is temporarily silent.
  // so the thinking panel shows the agent's full reasoning + tool activity
  const events = useMemo(
    () =>
      allEvents.filter(
        (e): e is LLMTokenEvent | LLMChunkEvent | ToolCallEvent | ToolResultEvent | AgentHeartbeatEvent | PermissionRequestEvent =>
          (e.type === "llm_token" || e.type === "llm_chunk" ||
           e.type === "tool_call" || e.type === "tool_result" ||
           e.type === "agent_heartbeat" || e.type === "permission_request") &&
          (nodeId === "" || e.node_id === nodeId)
      ),
    [allEvents, nodeId]
  );

  // Convert event stream into normalized Markdown for readable rendering.
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
      blocks.push(`> 思考片段\n\n\`\`\`text\n${escapeFence(trimmed)}\n\`\`\``);
      thinkingBuffer = "";
    };

    for (const ev of events) {
      if (ev.type === "llm_token") {
        if (inThinking) {
          flushThinking();
          inThinking = false;
        }
        assistantBuffer += ev.content;
      } else if (ev.type === "llm_chunk") {
        const isThinking = (ev as LLMChunkEvent).metadata?.thinking === true;
        if (isThinking) {
          flushAssistant();
          if (!inThinking) {
            inThinking = true;
          }
          thinkingBuffer += ev.content;
        } else {
          if (inThinking) {
            flushThinking();
            inThinking = false;
          }
          assistantBuffer += ev.content;
        }
      } else if (ev.type === "tool_call") {
        flushAssistant();
        if (inThinking) {
          flushThinking();
          inThinking = false;
        }
        const tc = ev as ToolCallEvent;
        const label = tc.tool_name || "tool";
        const payload = normalizeToolPayload(tc.content);
        blocks.push(`### 工具调用 \`${label}\`\n\n\`\`\`json\n${escapeFence(payload)}\n\`\`\``);
      } else if (ev.type === "tool_result") {
        flushAssistant();
        if (inThinking) {
          flushThinking();
          inThinking = false;
        }
        const tr = ev as ToolResultEvent;
        const label = tr.tool_name || "tool";
        const payload = normalizeToolPayload(tr.content || "");
        blocks.push(`### 工具结果 \`${label}\`\n\n\`\`\`text\n${escapeFence(payload)}\n\`\`\``);
      } else if (ev.type === "agent_heartbeat") {
        flushAssistant();
        if (inThinking) {
          flushThinking();
          inThinking = false;
        }
        const heartbeat = ev as AgentHeartbeatEvent;
        blocks.push(`> 系统进度：${heartbeat.content}`);
      } else if (ev.type === "permission_request") {
        flushAssistant();
        if (inThinking) {
          flushThinking();
          inThinking = false;
        }
        const request = ev as PermissionRequestEvent;
        blocks.push(`> 权限请求：\`${request.tool_name}\` -> \`${request.target}\``);
      }
    }

    flushAssistant();
    if (inThinking) {
      flushThinking();
    }

    return blocks.join("\n\n");
  }, [events]);

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
      <div className="mx-auto max-w-5xl px-4 py-3">
        <MarkdownMessage content={markdown} />
      </div>
    </div>
  );
}
