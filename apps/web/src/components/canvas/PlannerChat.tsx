"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Send, Bot, User, Play, Loader2, Target } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useRunStore } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";
import { api } from "@/lib/api";
import type { EdgeData } from "@/types/workflow";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  dag?: DagData | null;
}

interface DagData {
  nodes: DagNode[];
  edges: DagEdge[];
}

interface DagNode {
  id: string;
  type: string;
  label?: string;
  prompt?: string;
  depends_on?: string[];
}

interface DagEdge {
  source: string;
  target: string;
}

// ---------------------------------------------------------------------------
// Node type color/icon mapping
// ---------------------------------------------------------------------------

const NODE_STYLES: Record<string, { bg: string; border: string; text: string; label: string }> = {
  plan:    { bg: "bg-purple-50",  border: "border-purple-300", text: "text-purple-700", label: "规划器" },
  coder:   { bg: "bg-blue-50",    border: "border-blue-300",   text: "text-blue-700",   label: "编码器" },
  explore: { bg: "bg-green-50",   border: "border-green-300", text: "text-green-700",  label: "探索器" },
  review:  { bg: "bg-amber-50",   border: "border-amber-300",  text: "text-amber-700", label: "审查器" },
  shell:   { bg: "bg-gray-50",    border: "border-gray-300",   text: "text-gray-700",  label: "执行器" },
  human:   { bg: "bg-rose-50",    border: "border-rose-300",  text: "text-rose-700",   label: "人工" },
};

// ---------------------------------------------------------------------------
// PlannerChat component
// ---------------------------------------------------------------------------

export default function PlannerChat() {
  const currentWorkflowId = useWorkflowStore((s) => s.currentWorkflowId);
  const goal = useWorkflowStore((s) => s.goal);
  const addDynamicNode = useWorkflowStore((s) => s.addDynamicNode);
  const nodes = useWorkflowStore((s) => s.nodes);
  const edges = useWorkflowStore((s) => s.edges);
  const t = useLocaleStore((s) => s.t);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [currentDag, setCurrentDag] = useState<DagData | null>(null);
  const [plannerReady, setPlannerReady] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  // Send initial goal as first message if goal is set
  const handleStartConversation = useCallback(() => {
    if (!goal.trim()) return;
    setInput(goal.trim());
    setPlannerReady(true);
  }, [goal]);

  // Send message to Planner
  const handleSend = useCallback(async (messageText?: string) => {
    const text = (messageText ?? input).trim();
    if (!text || !currentWorkflowId || streaming) return;

    const userMsg: ChatMessage = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setStreaming(true);

    // Build history for the API
    const history = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    try {
      const response = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || "/api"}/planner/chat`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            workflow_id: currentWorkflowId,
            message: text,
            history,
          }),
          signal: abortRef.current?.signal,
        }
      );

      if (!response.ok) {
        const err = await response.text();
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `请求失败: ${err}` },
        ]);
        setStreaming(false);
        return;
      }

      // Read SSE stream
      const reader = response.body?.getReader();
      if (!reader) {
        setStreaming(false);
        return;
      }

      const decoder = new TextDecoder();
      let assistantContent = "";
      let latestDag: DagData | null = null;

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
              // Update the last assistant message
              setMessages((prev) => {
                const updated = [...prev];
                if (updated.length > 0 && updated[updated.length - 1].role === "assistant") {
                  updated[updated.length - 1] = {
                    ...updated[updated.length - 1],
                    content: assistantContent,
                  };
                }
                return updated;
              });
            } else if (event.type === "dag_update") {
              latestDag = event.dag;
              setCurrentDag(latestDag);
              // Also sync to workflow store
              syncDagToStore(latestDag);
            }
          } catch {
            // Skip non-JSON lines
          }
        }
      }

      // Finalize: attach DAG to the last assistant message
      if (latestDag) {
        setMessages((prev) => {
          const updated = [...prev];
          if (updated.length > 0 && updated[updated.length - 1].role === "assistant") {
            updated[updated.length - 1] = {
              ...updated[updated.length - 1],
              dag: latestDag,
            };
          }
          return updated;
        });
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `连接失败: ${err}` },
      ]);
    } finally {
      setStreaming(false);
    }
  }, [input, currentWorkflowId, streaming, messages]);

  // Sync the DAG from Planner to the workflow store so it can be executed
  const syncDagToStore = useCallback((dag: DagData) => {
    const store = useWorkflowStore.getState();
    // Clear existing dynamic nodes (keep only user-created ones)
    // For auto mode, all nodes are planner-created
    const workflowNodes = dag.nodes.map((n, idx) => {
      const style = NODE_STYLES[n.type] || NODE_STYLES.coder;
      return {
        id: n.id,
        type: n.type,
        position: { x: 100 + (idx % 3) * 280, y: 100 + Math.floor(idx / 3) * 140 },
        data: {
          label: n.label || style.label,
          agentType: n.type,
          modelProvider: "",
          modelId: "",
          prompt: n.prompt || "",
          permissions: {},
          command: "",
          description: "",
          parentNodeId: "planner",
          isDynamic: true,
        },
      };
    });

    const workflowEdges = dag.edges.map((e) => ({
      id: `e_${e.source}-${e.target}`,
      source: e.source,
      target: e.target,
      data: {
        transfer_files: true,
        transfer_summary: true,
        transfer_format: "summary",
      } as EdgeData,
    }));

    // Update store
    store.onNodesChange({ type: "__planner_sync", nodes: workflowNodes } as any);
    store.onEdgesChange({ type: "__planner_sync", edges: workflowEdges } as any);

    // Direct set is cleaner for full replacement
    useWorkflowStore.setState({ nodes: workflowNodes as any, edges: workflowEdges as any });
  }, []);

  // Handle "Run" button
  const handleRun = useCallback(async () => {
    if (!currentWorkflowId) return;
    try {
      const result = await api.triggerRun(currentWorkflowId);
      useRunStore.getState().setRunId(result.id);
      useRunStore.getState().setStatus("running");
      // Reload workflow so auto-mode planner node (saved to dag_json by backend) appears on canvas
      const updated = await api.getWorkflow(currentWorkflowId);
      useWorkflowStore.getState().loadWorkflow(updated);
    } catch (err) {
      console.error("Failed to trigger run:", err);
    }
  }, [currentWorkflowId]);

  // Handle Enter key
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend]
  );

  // Initial state: show goal prompt
  if (!plannerReady && messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-gradient-to-br from-slate-50 to-blue-50 p-8">
        <div className="w-full max-w-2xl space-y-6">
          <div className="text-center space-y-2">
            <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-100 text-blue-700 text-xs font-medium">
              <Target size={14} />
              {t("workflow.modeAuto")}
            </div>
            <h2 className="text-2xl font-bold text-gray-800">
              {t("workflow.goalLabel")}
            </h2>
            <p className="text-sm text-gray-500">
              {t("workflow.modeAutoDesc")}
            </p>
          </div>
          <textarea
            value={goal}
            onChange={(e) => useWorkflowStore.getState().updateGoal(e.target.value)}
            placeholder={t("workflow.goalPlaceholder")}
            rows={6}
            className="w-full rounded-xl border border-gray-200 bg-white px-5 py-4 text-base text-gray-800 placeholder-gray-300 shadow-sm resize-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all"
          />
          <div className="flex justify-center">
            <button
              onClick={handleStartConversation}
              disabled={!goal.trim()}
              className="inline-flex items-center gap-2 px-6 py-3 text-sm font-semibold rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 text-white hover:from-blue-700 hover:to-indigo-700 transition-all shadow-sm disabled:opacity-50"
            >
              <Bot size={18} />
              {t("planner.startConversation") || "开始对话"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Chat interface
  return (
    <div className="flex-1 flex flex-col h-full bg-white">
      {/* Chat messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {messages.map((msg, idx) => (
          <div key={idx} className={`flex gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            {msg.role === "assistant" && (
              <div className="w-8 h-8 rounded-lg bg-blue-100 flex items-center justify-center shrink-0 mt-1">
                <Bot size={16} className="text-blue-600" />
              </div>
            )}
            <div
              className={`max-w-[70%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                msg.role === "user"
                  ? "bg-blue-600 text-white"
                  : "bg-gray-50 text-gray-800 border border-gray-100"
              }`}
            >
              {/* Render text content */}
              <div className="whitespace-pre-wrap">{msg.content}</div>

              {/* Render DAG preview */}
              {msg.dag && (
                <DagPreview dag={msg.dag} />
              )}
            </div>
            {msg.role === "user" && (
              <div className="w-8 h-8 rounded-lg bg-gray-200 flex items-center justify-center shrink-0 mt-1">
                <User size={16} className="text-gray-600" />
              </div>
            )}
          </div>
        ))}

        {/* Streaming indicator */}
        {streaming && (
          <div className="flex gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-100 flex items-center justify-center shrink-0">
              <Bot size={16} className="text-blue-600" />
            </div>
            <div className="bg-gray-50 rounded-2xl px-4 py-3 border border-gray-100">
              <Loader2 size={16} className="animate-spin text-blue-500" />
            </div>
          </div>
        )}
      </div>

      {/* DAG status bar */}
      {currentDag && (
        <div className="px-6 py-2 bg-blue-50 border-t border-blue-100 flex items-center gap-4 text-xs">
          <span className="text-blue-700 font-medium">
            {t("planner.currentPlan") || "当前方案"}:
          </span>
          <span className="text-blue-600">
            {currentDag.nodes.length} {t("planner.nodes") || "个节点"} · {currentDag.edges.length} {t("planner.edges") || "条连线"}
          </span>
          <div className="flex-1" />
          <button
            onClick={handleRun}
            disabled={streaming || currentDag.nodes.length === 0}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 text-xs font-semibold rounded-lg bg-gradient-to-r from-green-500 to-emerald-600 text-white hover:from-green-600 hover:to-emerald-700 transition-all shadow-sm disabled:opacity-50"
          >
            <Play size={12} />
            {t("toolbar.run")}
          </button>
        </div>
      )}

      {/* Input area */}
      <div className="px-6 py-4 border-t border-gray-100 bg-white">
        <div className="flex items-end gap-3">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t("planner.inputPlaceholder") || "描述你想要的修改，或说「运行」开始执行..."}
            rows={2}
            disabled={streaming}
            className="flex-1 rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-800 placeholder-gray-400 resize-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 focus:outline-none transition-all disabled:opacity-50"
          />
          <button
            onClick={() => handleSend()}
            disabled={streaming || !input.trim()}
            className="inline-flex items-center justify-center w-10 h-10 rounded-xl bg-blue-600 text-white hover:bg-blue-700 transition-colors shadow-sm disabled:opacity-50 shrink-0"
          >
            {streaming ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
          </button>
        </div>
        <p className="text-xs text-gray-400 mt-2">
          {t("planner.inputHint") || "按 Enter 发送，Shift+Enter 换行。可以说「加一个审查步骤」或「让编码和探索并行执行」来修改工作流。"}
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DagPreview — mini visual preview of the current plan
// ---------------------------------------------------------------------------

function DagPreview({ dag }: { dag: DagData }) {
  return (
    <div className="mt-3 p-3 bg-white rounded-lg border border-gray-200">
      <div className="text-xs font-semibold text-gray-500 mb-2">
        {useLocaleStore.getState().t("planner.planPreview") || "工作流预览"}
      </div>
      <div className="space-y-2">
        {dag.nodes.map((node) => {
          const style = NODE_STYLES[node.type] || NODE_STYLES.coder;
          // Find incoming edges
          const incoming = dag.edges.filter((e) => e.target === node.id);
          return (
            <div key={node.id} className="flex items-center gap-2">
              {/* Connection indicator */}
              {incoming.length > 0 && (
                <div className="flex items-center gap-1 text-gray-400">
                  <span className="text-[10px]">←</span>
                  {incoming.map((e, i) => (
                    <span key={i} className="text-[10px] bg-gray-100 rounded px-1">
                      {e.source}
                    </span>
                  ))}
                </div>
              )}
              {/* Node card */}
              <div
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium ${style.bg} ${style.border} border ${style.text}`}
              >
                <span>{style.label}</span>
                <span className="font-normal text-gray-500 truncate max-w-[160px]">
                  {node.label || node.id}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
