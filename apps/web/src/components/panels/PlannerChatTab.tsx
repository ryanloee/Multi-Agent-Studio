"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Send, Loader2, MessageCircle } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useTaskStore } from "@/stores/taskStore";
import { useLocaleStore } from "@/stores/localeStore";
import { api } from "@/lib/api";
import { authHeaders } from "@/lib/auth";
import MarkdownMessage from "@/components/common/MarkdownMessage";
import type { ChatHistoryItem } from "@/types/settings";

// ---------------------------------------------------------------------------
// Chat message type (local state)
// ---------------------------------------------------------------------------

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

function isExecutionRequest(text: string): boolean {
  return /(^|\s|，|。|,)(开始执行|开始工作|开始做|执行工作流|开始|执行|运行|开跑|run|start)(\s|，|。|,|$)/i.test(text);
}

// ---------------------------------------------------------------------------
// PlannerChatTab — embedded in the OutputPanel when in auto mode
// Features:
//   - Node selector to choose which node to talk to (planner by default)
//   - Loads persisted chat history from backend
//   - Sends node_id with each message for context isolation
// ---------------------------------------------------------------------------

export default function PlannerChatTab() {
  const t = useLocaleStore((s) => s.t);
  const currentWorkflowId = useWorkflowStore((s) => s.currentWorkflowId);
  const nodes = useWorkflowStore((s) => s.nodes);

  // Node selector — default to "planner"
  const [selectedNodeId, setSelectedNodeId] = useState("planner");

  // Messages loaded from server + new local ones
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const triggerRun = useCallback(async () => {
    if (!currentWorkflowId) return;

    const runStore = useRunStore.getState();
    const taskStore = useTaskStore.getState();

    runStore.clearEvents();
    taskStore.clearTasks();

    const store = useWorkflowStore.getState();
    const result = await api.triggerRun(currentWorkflowId, {
      nodes: store.nodes,
      edges: store.edges,
    });
    runStore.setRunId(result.id);
    runStore.setStatus("running");
    taskStore.setCurrentRunId(result.id);

    const updated = await api.getWorkflow(currentWorkflowId);
    useWorkflowStore.getState().loadWorkflow(updated);
  }, [currentWorkflowId]);

  // Build node options for the selector
  const nodeOptions = (() => {
    // Always include planner
    const opts = [{ id: "planner", label: "Planner" }];
    // Add plan-type nodes from canvas
    for (const n of nodes) {
      const data = n.data as Record<string, unknown>;
      const agentType = data?.agentType as string | undefined;
      const label = (data?.label as string) || n.id;
      if (agentType === "plan" && n.id !== "planner") {
        opts.push({ id: n.id, label });
      } else if (agentType && agentType !== "plan") {
        // Allow chatting with any node
        opts.push({ id: n.id, label });
      }
    }
    return opts;
  })();

  // Load chat history when workflow/node changes
  useEffect(() => {
    if (!currentWorkflowId) return;
    const wfId = currentWorkflowId;
    let cancelled = false;

    async function loadHistory() {
      setLoadingHistory(true);
      try {
        const history = await api.getChatHistory(wfId, selectedNodeId);
        if (cancelled) return;
        setMessages(
          history.map((h: ChatHistoryItem) => ({
            role: h.role as "user" | "assistant",
            content: h.content,
          }))
        );
      } catch {
        if (!cancelled) setMessages([]);
      } finally {
        if (!cancelled) setLoadingHistory(false);
      }
    }

    loadHistory();
    return () => { cancelled = true; };
  }, [currentWorkflowId, selectedNodeId]);

  // Auto-scroll
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || !currentWorkflowId || streaming) return;

    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    setStreaming(true);

    try {
      const response = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "/api"}/planner/chat`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({
            workflow_id: currentWorkflowId,
            message: text,
            node_id: selectedNodeId,
            history: [],  // Backend loads from DB, no need to send history
          }),
        }
      );

      if (!response.ok) {
        const err = await response.text();
        setMessages((prev) => [...prev, { role: "assistant", content: `请求失败: ${err}` }]);
        setStreaming(false);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) { setStreaming(false); return; }

      const decoder = new TextDecoder();
      let assistantContent = "";

      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        const lines = chunk.split("\n");

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          if (data === "[DONE]") continue;

          try {
            const event = JSON.parse(data);
            if (event.type === "text") {
              assistantContent += event.content;
              setMessages((prev) => {
                const updated = [...prev];
                if (updated.length > 0 && updated[updated.length - 1].role === "assistant") {
                  updated[updated.length - 1] = { ...updated[updated.length - 1], content: assistantContent };
                }
                return updated;
              });
            } else if (event.type === "dag_update") {
              const updated = await api.getWorkflow(currentWorkflowId);
              useWorkflowStore.getState().loadWorkflow(updated);
            }
          } catch { /* skip */ }
        }
      }

      if (isExecutionRequest(text)) {
        try {
          await triggerRun();
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: "已开始执行工作流，任务面板会显示各节点任务。" },
          ]);
        } catch (err) {
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: `启动工作流失败: ${err}` },
          ]);
        }
      }
    } catch (err) {
      setMessages((prev) => [...prev, { role: "assistant", content: `连接失败: ${err}` }]);
    } finally {
      setStreaming(false);
    }
  }, [input, currentWorkflowId, streaming, selectedNodeId, triggerRun]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  // Find the display name for the selected node
  const selectedLabel = nodeOptions.find((o) => o.id === selectedNodeId)?.label || selectedNodeId;

  return (
    <div className="flex flex-col h-full">
      {/* Node selector + header */}
      <div className="flex items-center gap-2 px-2 py-1.5 border-b border-gray-100 shrink-0">
        <MessageCircle size={12} className="text-blue-500" />
        <select
          value={selectedNodeId}
          onChange={(e) => setSelectedNodeId(e.target.value)}
          className="text-xs border border-gray-200 rounded px-1.5 py-0.5 bg-white text-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400 max-w-[160px]"
        >
          {nodeOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>
              {opt.label}
            </option>
          ))}
        </select>
        <span className="text-[10px] text-gray-400 truncate">
          {messages.length > 0 ? `${messages.length} messages` : t("planner.inputHint")}
        </span>
        {loadingHistory && <Loader2 size={10} className="animate-spin text-gray-400" />}
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2 space-y-2">
        {messages.length === 0 && !loadingHistory && (
          <div className="text-center text-xs text-gray-400 pt-4">
            {t("planner.inputHint")}
          </div>
        )}
        {messages.map((msg, idx) => (
          <div
            key={idx}
            className={`text-xs leading-relaxed ${
              msg.role === "user" ? "text-blue-700" : "text-gray-700"
            }`}
          >
            <span className={`font-semibold ${msg.role === "user" ? "text-blue-500" : "text-gray-500"}`}>
              {msg.role === "user" ? "You: " : `${selectedLabel}: `}
            </span>
            <div className="mt-1">
              <MarkdownMessage
                content={msg.content}
                compact
                inverted={false}
              />
            </div>
          </div>
        ))}
        {streaming && messages[messages.length - 1]?.content === "" && (
          <div className="text-xs text-gray-400">
            <Loader2 size={10} className="animate-spin inline mr-1" />
            thinking...
          </div>
        )}
      </div>

      {/* Input */}
      <div className="flex items-center gap-2 px-2 py-1.5 border-t border-gray-100">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={t("planner.inputPlaceholder")}
          disabled={streaming}
          className="flex-1 text-xs border border-gray-200 rounded px-2 py-1.5 focus:outline-none focus:border-blue-400 disabled:opacity-50"
        />
        <button
          onClick={handleSend}
          disabled={streaming || !input.trim()}
          className="p-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {streaming ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />}
        </button>
      </div>
    </div>
  );
}
