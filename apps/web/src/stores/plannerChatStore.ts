import { create } from "zustand";
import type { PlannerDraftStructuredState, PlannerStage, PlannerStageHistoryItem } from "@/types/api";

export interface PlannerChatMessage {
  role: "user" | "assistant";
  content: string;
  thinking?: string;
}

interface PlannerChatState {
  selectedNodeId: string;
  conversationKey: string;
  messages: PlannerChatMessage[];
  input: string;
  streaming: boolean;
  streamStartedAt: number | null;
  streamingChars: number;
  dagUpdateCount: number;
  observableTrace: string[];
  thinkingContent: string;
  thinkingEventCount: number;
  rawTextContent: string;
  streamEventCount: number;
  lastStreamEventType: string;
  lastStreamEventPreview: string;
  plannerToolCallCount: number;
  lastPlannerToolName: string;
  lastPlannerToolInputKeys: string[];
  loadingHistory: boolean;
  thinkingLevel: "off" | "low" | "medium" | "high";
  alignmentMaxAttempts: number;
  abortController: AbortController | null;
  currentStage: PlannerStage | null;
  stageAttempt: number;
  stageProgressItems: string[];
  stageHistory: PlannerStageHistoryItem[];
  draftStructuredState: PlannerDraftStructuredState | null;
  setSelectedNodeId: (nodeId: string) => void;
  setConversationKey: (key: string) => void;
  setMessages: (messages: PlannerChatMessage[] | ((prev: PlannerChatMessage[]) => PlannerChatMessage[])) => void;
  setInput: (input: string) => void;
  beginStream: (controller: AbortController) => void;
  stopStream: () => void;
  finishStream: () => void;
  setStreamingChars: (count: number) => void;
  setDagUpdateCount: (count: number | ((prev: number) => number)) => void;
  setObservableTrace: (lines: string[]) => void;
  appendRawText: (delta: string) => void;
  appendThinking: (delta: string) => void;
  recordStreamEvent: (eventType: string, preview?: string) => void;
  recordPlannerToolCall: (toolName: string, inputKeys: string[]) => void;
  setLoadingHistory: (loading: boolean) => void;
  setThinkingLevel: (level: "off" | "low" | "medium" | "high") => void;
  setAlignmentMaxAttempts: (attempts: number) => void;
  setCurrentStage: (stage: PlannerStage | null) => void;
  setStageAttempt: (attempt: number) => void;
  setStageProgressItems: (items: string[]) => void;
  pushStageHistory: (item: PlannerStageHistoryItem) => void;
  setDraftStructuredState: (state: PlannerDraftStructuredState | null) => void;
}

export const usePlannerChatStore = create<PlannerChatState>((set, get) => ({
  selectedNodeId: "planner",
  conversationKey: "",
  messages: [],
  input: "",
  streaming: false,
  streamStartedAt: null,
  streamingChars: 0,
  dagUpdateCount: 0,
  observableTrace: [],
  thinkingContent: "",
  thinkingEventCount: 0,
  rawTextContent: "",
  streamEventCount: 0,
  lastStreamEventType: "",
  lastStreamEventPreview: "",
  plannerToolCallCount: 0,
  lastPlannerToolName: "",
  lastPlannerToolInputKeys: [],
  loadingHistory: false,
  thinkingLevel: "medium",
  alignmentMaxAttempts: 3,
  abortController: null,
  currentStage: null,
  stageAttempt: 0,
  stageProgressItems: [],
  stageHistory: [],
  draftStructuredState: null,

  setSelectedNodeId: (selectedNodeId) => set({ selectedNodeId }),
  setConversationKey: (conversationKey) => set({ conversationKey }),
  setMessages: (messages) => set((state) => ({
    messages: typeof messages === "function" ? messages(state.messages) : messages,
  })),
  setInput: (input) => set({ input }),
  beginStream: (abortController) => set({
    streaming: true,
    streamStartedAt: Date.now(),
    streamingChars: 0,
    dagUpdateCount: 0,
    observableTrace: [],
    thinkingContent: "",
    thinkingEventCount: 0,
    rawTextContent: "",
    streamEventCount: 0,
    lastStreamEventType: "",
    lastStreamEventPreview: "",
    plannerToolCallCount: 0,
    lastPlannerToolName: "",
    lastPlannerToolInputKeys: [],
    abortController,
    currentStage: null,
    stageAttempt: 0,
    stageProgressItems: [],
    stageHistory: [],
    draftStructuredState: null,
  }),
  stopStream: () => {
    get().abortController?.abort();
    set((state) => ({
      streaming: false,
      streamStartedAt: null,
      abortController: null,
      messages: state.messages.some((msg) => msg.role === "assistant")
        ? state.messages
        : [...state.messages, { role: "assistant", content: "已停止本轮 Planner 输出。" }],
    }));
  },
  finishStream: () => set({ streaming: false, streamStartedAt: null, abortController: null }),
  setStreamingChars: (streamingChars) => set({ streamingChars }),
  setDagUpdateCount: (count) => set((state) => ({
    dagUpdateCount: typeof count === "function" ? count(state.dagUpdateCount) : count,
  })),
  setObservableTrace: (observableTrace) => set({ observableTrace }),
  appendRawText: (delta) => set((state) => ({
    rawTextContent: `${state.rawTextContent}${delta || ""}`,
  })),
  appendThinking: (delta) => set((state) => {
    if (!delta) return {};
    const messages = [...state.messages];
    const last = messages[messages.length - 1];
    if (last?.role === "assistant") {
      messages[messages.length - 1] = {
        ...last,
        thinking: `${last.thinking || ""}${delta}`,
      };
    }
    return {
      thinkingContent: `${state.thinkingContent}${delta}`,
      thinkingEventCount: state.thinkingEventCount + 1,
      messages,
    };
  }),
  recordStreamEvent: (eventType, preview = "") => set((state) => ({
    streamEventCount: state.streamEventCount + 1,
    lastStreamEventType: eventType,
    lastStreamEventPreview: preview.slice(0, 500),
  })),
  recordPlannerToolCall: (toolName, inputKeys) => set((state) => ({
    plannerToolCallCount: state.plannerToolCallCount + 1,
    lastPlannerToolName: toolName,
    lastPlannerToolInputKeys: inputKeys,
  })),
  setLoadingHistory: (loadingHistory) => set({ loadingHistory }),
  setThinkingLevel: (thinkingLevel) => set({ thinkingLevel }),
  setAlignmentMaxAttempts: (alignmentMaxAttempts) => set({
    alignmentMaxAttempts: Math.max(1, Math.min(Math.trunc(alignmentMaxAttempts || 3), 10)),
  }),
  setCurrentStage: (currentStage) => set({ currentStage }),
  setStageAttempt: (stageAttempt) => set({ stageAttempt }),
  setStageProgressItems: (stageProgressItems) => set({ stageProgressItems }),
  pushStageHistory: (item) => set((state) => ({
    stageHistory: [...state.stageHistory, item],
  })),
  setDraftStructuredState: (draftStructuredState) => set({ draftStructuredState }),
}));
