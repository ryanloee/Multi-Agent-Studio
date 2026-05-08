"use client";

import { useEffect, useRef } from "react";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import type { LLMTokenEvent, LLMChunkEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
interface LLMOutputProps {
  /** Filter events to a specific node. Empty string = all nodes. */
  nodeId?: string;
}

// ---------------------------------------------------------------------------
// Simple Markdown-ish renderer (very lightweight — handles code blocks, bold,
// italic, inline code, and line breaks).  Avoids pulling in a heavy dep.
// ---------------------------------------------------------------------------
function renderSimpleMarkdown(text: string): string {
  let html = "";

  // Split into code-block and non-code-block sections
  const parts = text.split(/(```[\s\S]*?```)/g);

  for (const part of parts) {
    if (part.startsWith("```") && part.endsWith("```")) {
      // Code block
      const inner = part.slice(3, -3);
      const firstNewline = inner.indexOf("\n");
      const body = firstNewline >= 0 ? inner.slice(firstNewline + 1) : inner;
      html += `<pre class="bg-gray-800 text-gray-100 rounded px-3 py-2 my-2 text-xs overflow-x-auto font-mono whitespace-pre">${escapeHtml(body)}</pre>`;
    } else {
      // Regular text
      const lines = part.split("\n");
      for (const line of lines) {
        if (line.trim() === "") {
          html += '<div class="h-2"></div>';
          continue;
        }

        let rendered = escapeHtml(line);

        // Inline code
        rendered = rendered.replace(
          /`([^`]+)`/g,
          '<code class="bg-gray-100 text-pink-600 px-1 py-0.5 rounded text-xs font-mono">$1</code>'
        );

        // Bold
        rendered = rendered.replace(
          /\*\*([^*]+)\*\*/g,
          '<strong class="font-semibold">$1</strong>'
        );

        // Italic
        rendered = rendered.replace(
          /\*([^*]+)\*/g,
          '<em>$1</em>'
        );

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
// LLMOutput — typewriter-effect display of llm_token / llm_chunk events
// ---------------------------------------------------------------------------
export default function LLMOutput({ nodeId = "" }: LLMOutputProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const textRef = useRef("");
  const lastRenderedLength = useRef(0);
  const rafIdRef = useRef<number>(0);

  const t = useLocaleStore((s) => s.t);

  // Select token events from the store
  const events = useRunStore((s) =>
    s.events.filter(
      (e): e is LLMTokenEvent | LLMChunkEvent =>
        (e.type === "llm_token" || e.type === "llm_chunk") &&
        (nodeId === "" || e.node_id === nodeId)
    )
  );

  // Accumulate text from events (only new ones)
  useEffect(() => {
    const newEvents = events.slice(lastRenderedLength.current);
    if (newEvents.length === 0) return;
    lastRenderedLength.current = events.length;

    for (const ev of newEvents) {
      textRef.current += ev.content;
    }

    // Batch DOM update via requestAnimationFrame
    cancelAnimationFrame(rafIdRef.current);
    rafIdRef.current = requestAnimationFrame(() => {
      const el = containerRef.current;
      if (el) {
        el.innerHTML = renderSimpleMarkdown(textRef.current);
        el.scrollTop = el.scrollHeight;
      }
    });

    return () => cancelAnimationFrame(rafIdRef.current);
  }, [events]);

  // Clear accumulated text when store events are cleared (new run)
  const eventsLength = events.length;
  useEffect(() => {
    if (eventsLength === 0) {
      textRef.current = "";
      lastRenderedLength.current = 0;
    }
  }, [eventsLength]);

  if (events.length === 0 && textRef.current === "") {
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
