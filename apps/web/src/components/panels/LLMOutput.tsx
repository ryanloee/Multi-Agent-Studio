"use client";

import { useEffect, useRef, useMemo } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { LLMTokenEvent, LLMChunkEvent, ToolCallEvent, ToolResultEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface LLMOutputProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

// ---------------------------------------------------------------------------
// Simple Markdown-ish renderer
// ---------------------------------------------------------------------------
function renderSimpleMarkdown(text: string): string {
  let html = "";
  const parts = text.split(/(```[\s\S]*?```)/g);

  for (const part of parts) {
    if (part.startsWith("```") && part.endsWith("```")) {
      const inner = part.slice(3, -3);
      const firstNewline = inner.indexOf("\n");
      const body = firstNewline >= 0 ? inner.slice(firstNewline + 1) : inner;
      html += `<pre class="bg-gray-800 text-gray-100 rounded px-3 py-2 my-2 text-xs overflow-x-auto font-mono whitespace-pre">${escapeHtml(body)}</pre>`;
    } else {
      const lines = part.split("\n");
      for (const line of lines) {
        if (line.trim() === "") {
          html += '<div class="h-2"></div>';
          continue;
        }

        // Thinking block quote lines
        if (line.startsWith("> ")) {
          const thinkingContent = escapeHtml(line.slice(2));
          const rendered = thinkingContent
            .replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold">$1</strong>');
          html += `<div class="text-sm leading-6 text-purple-700 bg-purple-50 border-l-2 border-purple-300 pl-3 italic">${rendered}</div>`;
          continue;
        }

        // Tool action lines (wrench prefix)
        if (line.includes("\u{1f527}")) {
          html += `<div class="text-sm leading-6 text-blue-600 font-medium">${escapeHtml(line)}</div>`;
          continue;
        }

        // Thinking/step completion lines
        if (line.startsWith("思考完成") || line.startsWith("Step complete")) {
          html += `<div class="text-xs leading-5 text-gray-400 italic">${escapeHtml(line)}</div>`;
          continue;
        }

        let rendered = escapeHtml(line);
        rendered = rendered.replace(
          /`([^`]+)`/g,
          '<code class="bg-gray-100 text-pink-600 px-1 py-0.5 rounded text-xs font-mono">$1</code>'
        );
        rendered = rendered.replace(/\*\*([^*]+)\*\*/g, '<strong class="font-semibold">$1</strong>');
        rendered = rendered.replace(/\*([^*]+)\*/g, '<em>$1</em>');
        html += `<div class="text-sm leading-6 text-gray-700">${rendered}</div>`;
      }
    }
  }

  return html;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// LLMOutput — typewriter-effect display of llm_token / llm_chunk / tool events
// ---------------------------------------------------------------------------
export default function LLMOutput({ nodeId = "" }: LLMOutputProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const rafIdRef = useRef<number>(0);

  const t = useLocaleStore((s) => s.t);

  const allEvents = useRunStore((s) => s.events);

  // Include llm_token, llm_chunk, tool_call, and tool_result events
  // so the thinking panel shows the agent's full reasoning + tool activity
  const events = useMemo(
    () =>
      allEvents.filter(
        (e): e is LLMTokenEvent | LLMChunkEvent | ToolCallEvent | ToolResultEvent =>
          (e.type === "llm_token" || e.type === "llm_chunk" ||
           e.type === "tool_call" || e.type === "tool_result") &&
          (nodeId === "" || e.node_id === nodeId)
      ),
    [allEvents, nodeId]
  );

  // Build full text from all events whenever the list changes
  const fullText = useMemo(() => {
    let text = "";
    let inThinking = false;

    for (const ev of events) {
      if (ev.type === "llm_token") {
        if (inThinking) {
          text += "\n```\n";
          inThinking = false;
        }
        text += ev.content;
      } else if (ev.type === "llm_chunk") {
        const isThinking = (ev as LLMChunkEvent).metadata?.thinking === true;
        if (isThinking) {
          if (!inThinking) {
            text += "\n> \u{1F4AD} **Thinking...**\n```\n";
            inThinking = true;
          }
          text += ev.content;
        } else {
          if (inThinking) {
            text += "\n```\n";
            inThinking = false;
          }
          text += ev.content;
        }
      } else if (ev.type === "tool_call") {
        const tc = ev as ToolCallEvent;
        let label = tc.tool_name || "tool";
        try {
          const parsed = JSON.parse(tc.content);
          if (parsed.name) label = parsed.name;
        } catch {}
        text += `\n\u{1F527} ${label}\n`;
      } else if (ev.type === "tool_result") {
        const tr = ev as ToolResultEvent;
        const content = tr.content || "";
        const preview = content.length > 200 ? content.slice(0, 200) + "..." : content;
        text += `${preview}\n`;
      }
    }

    return text;
  }, [events]);

  // Render markdown into the container whenever fullText changes
  useEffect(() => {
    if (fullText === "") return;

    cancelAnimationFrame(rafIdRef.current);
    rafIdRef.current = requestAnimationFrame(() => {
      const el = containerRef.current;
      if (el) {
        el.innerHTML = renderSimpleMarkdown(fullText);
        el.scrollTop = el.scrollHeight;
      }
    });

    return () => cancelAnimationFrame(rafIdRef.current);
  }, [fullText]);

  if (events.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-sm text-gray-400">
        {t("llm.waiting")}
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="w-full h-full overflow-y-auto px-4 py-3 bg-white font-mono"
    />
  );
}
