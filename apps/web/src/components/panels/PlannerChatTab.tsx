"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Send, Loader2, MessageCircle, Brain, Square } from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { usePlannerChatStore } from "@/stores/plannerChatStore";
import { api } from "@/lib/api";
import { authHeaders } from "@/lib/auth";
import { parsePlannerObservableContent } from "@/lib/plannerObservable";
import MarkdownMessage from "@/components/common/MarkdownMessage";
import type { ChatHistoryItem } from "@/types/settings";

function plannerChatUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_URL;
  if (configured) return `${configured}/planner/chat`;
  if (typeof window === "undefined") return "/api/planner/chat";
  const { protocol, hostname } = window.location;
  return `${protocol}//${hostname}:8000/api/planner/chat`;
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
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);
  const setLifecyclePhase = useWorkflowStore((s) => s.setLifecyclePhase);
  const setBlockers = useWorkflowStore((s) => s.setBlockers);
  const setProjectSummary = useWorkflowStore((s) => s.setProjectSummary);
  const setPlannerUiState = useWorkflowStore((s) => s.setPlannerUiState);
  const setPlannerActionState = useWorkflowStore((s) => s.setPlannerActionState);

  const selectedNodeId = usePlannerChatStore((s) => s.selectedNodeId);
  const conversationKey = usePlannerChatStore((s) => s.conversationKey);
  const setSelectedNodeId = usePlannerChatStore((s) => s.setSelectedNodeId);
  const setConversationKey = usePlannerChatStore((s) => s.setConversationKey);
  const messages = usePlannerChatStore((s) => s.messages);
  const setMessages = usePlannerChatStore((s) => s.setMessages);
  const input = usePlannerChatStore((s) => s.input);
  const setInput = usePlannerChatStore((s) => s.setInput);
  const streaming = usePlannerChatStore((s) => s.streaming);
  const streamStartedAt = usePlannerChatStore((s) => s.streamStartedAt);
  const beginStream = usePlannerChatStore((s) => s.beginStream);
  const stopStream = usePlannerChatStore((s) => s.stopStream);
  const finishStream = usePlannerChatStore((s) => s.finishStream);
  const setStreamingChars = usePlannerChatStore((s) => s.setStreamingChars);
  const setDagUpdateCount = usePlannerChatStore((s) => s.setDagUpdateCount);
  const setObservableTrace = usePlannerChatStore((s) => s.setObservableTrace);
  const appendRawText = usePlannerChatStore((s) => s.appendRawText);
  const appendThinking = usePlannerChatStore((s) => s.appendThinking);
  const recordStreamEvent = usePlannerChatStore((s) => s.recordStreamEvent);
  const recordPlannerToolCall = usePlannerChatStore((s) => s.recordPlannerToolCall);
  const streamEventCount = usePlannerChatStore((s) => s.streamEventCount);
  const lastStreamEventType = usePlannerChatStore((s) => s.lastStreamEventType);
  const lastStreamEventPreview = usePlannerChatStore((s) => s.lastStreamEventPreview);
  const plannerToolCallCount = usePlannerChatStore((s) => s.plannerToolCallCount);
  const lastPlannerToolName = usePlannerChatStore((s) => s.lastPlannerToolName);
  const lastPlannerToolInputKeys = usePlannerChatStore((s) => s.lastPlannerToolInputKeys);
  const loadingHistory = usePlannerChatStore((s) => s.loadingHistory);
  const setLoadingHistory = usePlannerChatStore((s) => s.setLoadingHistory);
  const thinkingLevel = usePlannerChatStore((s) => s.thinkingLevel);
  const setThinkingLevel = usePlannerChatStore((s) => s.setThinkingLevel);
  const [waitingSeconds, setWaitingSeconds] = useState(0);
  const streamingChars = usePlannerChatStore((s) => s.streamingChars);
  const dagUpdateCount = usePlannerChatStore((s) => s.dagUpdateCount);
  const observableTrace = usePlannerChatStore((s) => s.observableTrace);
  const thinkingContent = usePlannerChatStore((s) => s.thinkingContent);
  const thinkingEventCount = usePlannerChatStore((s) => s.thinkingEventCount);
  const rawTextContent = usePlannerChatStore((s) => s.rawTextContent);
  const scrollRef = useRef<HTMLDivElement>(null);
  const liveThinkingRef = useRef<HTMLDivElement>(null);
  const liveReplyRef = useRef<HTMLDivElement>(null);

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
    if (streaming) return;
    const wfId = currentWorkflowId;
    const key = `${wfId}:${selectedNodeId}`;
    if (conversationKey === key && messages.length > 0) return;
    let cancelled = false;

    async function loadHistory() {
      setLoadingHistory(true);
      try {
        const history = await api.getChatHistory(wfId, selectedNodeId);
        if (cancelled) return;
        setConversationKey(key);
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
  }, [
    currentWorkflowId,
    selectedNodeId,
    streaming,
    conversationKey,
    messages.length,
    setConversationKey,
    setLoadingHistory,
    setMessages,
  ]);

  // Auto-scroll
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    if (liveThinkingRef.current) {
      liveThinkingRef.current.scrollTop = liveThinkingRef.current.scrollHeight;
    }
  }, [thinkingContent]);

  useEffect(() => {
    if (liveReplyRef.current) {
      liveReplyRef.current.scrollTop = liveReplyRef.current.scrollHeight;
    }
  }, [rawTextContent]);

  useEffect(() => {
    if (!streaming) {
      setWaitingSeconds(0);
      return;
    }
    const timer = setInterval(() => {
      setWaitingSeconds(Math.floor((Date.now() - (streamStartedAt ?? Date.now())) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [streaming, streamStartedAt]);

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || !currentWorkflowId || streaming) return;

    const controller = new AbortController();
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setInput("");
    beginStream(controller);

    try {
      const response = await fetch(
        plannerChatUrl(),
        {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({
            workflow_id: currentWorkflowId,
            message: text,
            node_id: selectedNodeId,
            thinking_level: thinkingLevel,
            history: [],  // Backend loads from DB, no need to send history
          }),
          signal: controller.signal,
        }
      );

      if (!response.ok) {
        const err = await response.text();
        setMessages((prev) => [...prev, { role: "assistant", content: `请求失败: ${err}` }]);
        finishStream();
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) { finishStream(); return; }

      const decoder = new TextDecoder();
      let assistantContent = "";
      let plannerActionMessage = "";
      let sseBuffer = "";

      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        sseBuffer += decoder.decode(value, { stream: true });
        const rawEvents = sseBuffer.split(/\r?\n\r?\n/);
        sseBuffer = rawEvents.pop() ?? "";

        for (const rawEvent of rawEvents) {
          const data = rawEvent
            .split(/\r?\n/)
            .map((line) => line.trimEnd())
            .filter((line) => line.startsWith("data: "))
            .map((line) => line.slice(6).trimStart())
            .join("\n");
          if (!data) continue;
          if (data === "[DONE]") continue;

          try {
            const event = JSON.parse(data);
            recordStreamEvent(
              event.type || "unknown",
              event.content || event.message || event.name || (event.input_keys ? event.input_keys.join(", ") : "")
            );
            if (event.type === "text") {
              appendRawText(event.content || "");
              assistantContent += event.content;
              setStreamingChars(assistantContent.length);
              const parsed = parsePlannerObservableContent(assistantContent);
              setObservableTrace(parsed.traceLines);
              setMessages((prev) => {
                const updated = [...prev];
                if (updated.length > 0 && updated[updated.length - 1].role === "assistant") {
                  updated[updated.length - 1] = { ...updated[updated.length - 1], content: parsed.visibleContent };
                }
                return updated;
              });
            } else if (event.type === "thinking_delta") {
              const delta = event.content || "";
              appendThinking(delta);
            } else if (event.type === "planner_tool_call") {
              recordPlannerToolCall(
                event.name || "unknown_tool",
                Array.isArray(event.input_keys) ? event.input_keys : []
              );
            } else if (event.type === "planner_action") {
              const action = event.action;
              plannerActionMessage = action?.message || plannerActionMessage;
              if (plannerActionMessage) {
                setMessages((prev) => {
                  const updated = [...prev];
                  const last = updated[updated.length - 1];
                  if (last?.role === "assistant" && !last.content.trim()) {
                    updated[updated.length - 1] = { ...last, content: plannerActionMessage };
                  }
                  return updated;
                });
              }
              setPlannerActionState(action);
              if (action?.ui_state) setPlannerUiState(action.ui_state);
              if (action?.blockers) setBlockers(action.blockers);
              if (action?.action === "assess" && currentWorkflowId) {
                setLifecyclePhase("assessing");
                try {
                  const assessed = await api.assessWorkflow(currentWorkflowId);
                  loadWorkflow(assessed);
                  setProjectSummary(assessed.project_summary ?? {});
                } catch (err) {
                  setMessages((prev) => [
                    ...prev,
                    { role: "assistant", content: `项目评估失败: ${err instanceof Error ? err.message : String(err)}` },
                  ]);
                }
              } else if (action?.action === "set_ready") {
                setLifecyclePhase("ready");
                setBlockers([]);
              } else if (action?.action === "report_blocker") {
                setLifecyclePhase("blocked");
              } else if (action?.action === "update_dag") {
                setLifecyclePhase("planning");
                setBlockers([]);
              }
            } else if (event.type === "dag_update") {
              setDagUpdateCount((count) => count + 1);
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant" && !last.content.trim()) {
                  updated[updated.length - 1] = { ...last, content: "已更新工作流画布，请在左侧任务对象和中间画布查看规划结果。" };
                }
                return updated;
              });
              if (currentWorkflowId) {
                const updated = await api.getWorkflow(currentWorkflowId);
                loadWorkflow(updated);
              }
            } else if (event.type === "planner_ui_update") {
              if (event.ui_state) setPlannerUiState(event.ui_state);
              if (currentWorkflowId) {
                const updated = await api.getWorkflow(currentWorkflowId);
                loadWorkflow(updated);
              }
            }
          } catch { /* skip */ }
        }
      }

      if (currentWorkflowId) {
        const refreshed = await api.getWorkflow(currentWorkflowId);
        loadWorkflow(refreshed);
      }
      setMessages((prev) => {
        const updated = [...prev];
        const last = updated[updated.length - 1];
        if (last?.role === "assistant" && !last.content.trim()) {
          const parsed = parsePlannerObservableContent(assistantContent);
          updated[updated.length - 1] = {
            ...last,
            content: parsed.visibleContent || plannerActionMessage || "本轮 Planner 已完成，但没有收到可展示正文；如果画布没有变化，请让 Planner 重新生成更简洁的 DAG。",
          };
        }
        return updated;
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant" && last.content === "已停止本轮 Planner 输出。") return prev;
          return [...prev, { role: "assistant", content: "已停止本轮 Planner 输出。" }];
        });
      } else {
        setMessages((prev) => [...prev, { role: "assistant", content: `连接失败: ${err}` }]);
      }
    } finally {
      finishStream();
    }
  }, [
    input,
    currentWorkflowId,
    streaming,
    selectedNodeId,
    thinkingLevel,
    beginStream,
    finishStream,
    appendThinking,
    appendRawText,
    recordStreamEvent,
    recordPlannerToolCall,
    loadWorkflow,
    setLifecyclePhase,
    setBlockers,
    setProjectSummary,
    setPlannerUiState,
    setPlannerActionState,
  ]);

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
        <select
          value={thinkingLevel}
          onChange={(e) => setThinkingLevel(e.target.value as "off" | "low" | "medium" | "high")}
          disabled={streaming}
          className="ml-auto text-[10px] border border-gray-200 rounded px-1.5 py-0.5 bg-white text-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400"
          title="思考等级"
        >
          <option value="off">不思考</option>
          <option value="low">轻思考</option>
          <option value="medium">中度思考</option>
          <option value="high">高度思考</option>
        </select>
        {streaming && (
          <button
            onClick={stopStream}
            className="inline-flex items-center gap-1 rounded border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-600 hover:bg-red-100"
            title="停止本轮 Planner 输出"
          >
            <Square size={9} />
            停止
          </button>
        )}
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
            className={`flex gap-2 ${msg.role === "user" ? "justify-end" : "justify-start"}`}
          >
            <div
              className={`max-w-[78%] rounded-xl px-3 py-2 text-xs leading-relaxed ${
                msg.role === "user"
                  ? "bg-blue-600 text-white"
                  : "border border-gray-100 bg-gray-50 text-gray-700"
              }`}
            >
              <div className={`mb-1 font-semibold ${msg.role === "user" ? "text-blue-100" : "text-gray-500"}`}>
                {msg.role === "user" ? "You" : selectedLabel}
              </div>
              <MarkdownMessage
                content={msg.role === "assistant" ? parsePlannerObservableContent(msg.content).visibleContent : msg.content}
                compact
                inverted={msg.role === "user"}
              />
              {msg.role === "assistant" && msg.thinking?.trim() && (
                <details className="mt-2 rounded border border-slate-200 bg-slate-50 px-2 py-1.5 text-[10px] text-slate-700">
                  <summary className="cursor-pointer select-none font-medium text-slate-700">
                    本轮思考内容
                  </summary>
                  <div className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded bg-white px-2 py-1 font-mono leading-relaxed">
                    {msg.thinking.trim()}
                  </div>
                </details>
              )}
            </div>
          </div>
        ))}
        {streaming && (
          <div className="flex justify-start">
            <div className="max-w-[78%] rounded-xl border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-700">
              <div className="flex items-center gap-1.5">
                <Brain size={11} />
                <Loader2 size={10} className="animate-spin" />
                正在思考和生成回复，已等待 {waitingSeconds}s
              </div>
              <div className="mt-2 rounded-lg border border-blue-200 bg-white/70 px-2.5 py-2 text-[10px] leading-relaxed text-blue-800">
                <div className="font-medium text-blue-700">规划轨迹</div>
                <div className="mt-1 space-y-0.5">
                  {observableTrace.length > 0 ? (
                    observableTrace.map((line, index) => (
                      <div key={`${index}-${line}`}>- {line}</div>
                    ))
                  ) : (
                    <div>正在等待模型输出本轮规划轨迹。</div>
                  )}
                  <div className="mt-2 rounded border border-slate-200 bg-slate-50 px-2 py-1.5 text-slate-700">
                    <details open={thinkingContent.length > 0}>
                      <summary className="cursor-pointer select-none font-medium text-slate-700">
                        实时思考内容
                      </summary>
                      <div
                        ref={liveThinkingRef}
                        className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap rounded bg-white px-2 py-1 font-mono text-[10px] leading-relaxed text-slate-700"
                      >
                        {thinkingContent.trim() ? thinkingContent : "正在等待模型思考内容。"}
                      </div>
                    </details>
                    {rawTextContent.trim() && (
                      <div className="mt-2 rounded bg-white px-2 py-1">
                        <div className="mb-1 font-medium text-slate-700">实时回复内容</div>
                        <div
                          ref={liveReplyRef}
                          className="max-h-24 overflow-y-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-slate-700"
                        >
                          {rawTextContent}
                        </div>
                      </div>
                    )}
                  </div>
                  <div>DAG 更新事件：{dagUpdateCount} 次。</div>
                  <div>
                    工具调用：{plannerToolCallCount} 次
                    {lastPlannerToolName
                      ? `，最近调用 ${lastPlannerToolName}(${lastPlannerToolInputKeys.join(", ") || "无参数"})`
                      : "，正在等待 planner_submit_turn。"}
                  </div>
                  <div>
                    流事件：{streamEventCount} 次，最近：{lastStreamEventType || "无"}
                    {lastStreamEventPreview ? ` / ${lastStreamEventPreview}` : ""}
                  </div>
                </div>
              </div>
            </div>
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
