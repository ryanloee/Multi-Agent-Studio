import { create } from "zustand";
import { useMemo } from "react";
import type { RunStatus } from "@/types/workflow";
import type { StreamEvent, StreamEventType, StatusEvent, ChildCreatedEvent, ChildCompletedEvent } from "@/types/events";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface RunState {
  runId: string | null;
  status: RunStatus;
  events: StreamEvent[];
  /** Per-node run status, keyed by node_id */
  nodeStatuses: Record<string, RunStatus>;
  /** Currently selected node for run detail view in bottom panel */
  selectedRunNodeId: string | null;
  /** Parent-to-children mapping: parentId → childNodeIds */
  parentChildMap: Record<string, string[]>;
  /** Progress summary from DAG execution */
  progressSummary: { total: number; completed: number; failed: number } | null;

  // Actions
  setRunId: (id: string | null) => void;
  setStatus: (status: RunStatus) => void;
  addEvent: (event: StreamEvent) => void;
  hydrateEvents: (events: StreamEvent[]) => void;
  mergeEvents: (events: StreamEvent[]) => void;
  clearEvents: () => void;
  setNodeStatus: (nodeId: string, status: RunStatus) => void;
  setSelectedRunNode: (id: string | null) => void;
  /** Register a child node under a parent planner */
  registerChild: (parentId: string, childId: string) => void;
}

// ---------------------------------------------------------------------------
// Helper: derive RunStatus from event content string
// ---------------------------------------------------------------------------
const VALID_RUN_STATUSES: RunStatus[] = [
  "idle", "pending", "running", "paused", "cancelling", "cancelled", "completed", "failed",
];

function parseRunStatus(raw: string): RunStatus | null {
  return VALID_RUN_STATUSES.includes(raw as RunStatus) ? (raw as RunStatus) : null;
}

// ---------------------------------------------------------------------------
// Event batching — coalesce rapid events into single state updates
// ---------------------------------------------------------------------------
const _eventIds = new Set<string>();
let _pendingEvents: StreamEvent[] = [];
let _flushRaf: number | null = null;
const MAX_PENDING = 100;

/** Apply a batch of events to the store in a single setState call */
function _applyEvents(events: StreamEvent[]) {
  if (events.length === 0) return;
  const state = useRunStore.getState();

  const newNodeStatuses = { ...state.nodeStatuses };
  const newParentChildMap = { ...state.parentChildMap };
  let newStatus = state.status;
  let newProgressSummary = state.progressSummary;
  let mutatedStatuses = false;
  let mutatedParentChild = false;

  for (const event of events) {
    // -- status --
    if (event.type === "status" && event.node_id) {
      const parsed = parseRunStatus((event as StatusEvent).content);
      if (parsed !== null) {
        newNodeStatuses[event.node_id] = parsed;
        mutatedStatuses = true;
      }
    }

    // -- node lifecycle --
    if (
      event.node_id &&
      (event.type === "node_started" ||
        event.type === "node_completed" ||
        event.type === "node_failed")
    ) {
      const map: Record<string, RunStatus> = {
        node_started: "running",
        node_completed: "completed",
        node_failed: "failed",
      };
      newNodeStatuses[event.node_id] = map[event.type];
      mutatedStatuses = true;
    }

    // -- run lifecycle --
    if (event.type === "run_started") newStatus = "running";
    if (event.type === "run_completed") newStatus = "completed";
    if (event.type === "run_failed") newStatus = "failed";

    // -- child_created --
    if (event.type === "child_created") {
      const ce = event as ChildCreatedEvent;
      const parentId = event.node_id;
      const childId = ce.child_node_id;
      if (parentId && childId) {
        const existing = newParentChildMap[parentId] ?? [];
        newParentChildMap[parentId] = [...existing, childId];
        mutatedParentChild = true;
        if (!newNodeStatuses[childId]) {
          newNodeStatuses[childId] = "pending";
          mutatedStatuses = true;
        }
      }
    }

    // -- child_completed --
    if (event.type === "child_completed") {
      const ce = event as ChildCompletedEvent;
      if (ce.child_node_id) {
        newNodeStatuses[ce.child_node_id] = "completed";
        mutatedStatuses = true;
      }
    }

    // -- progress_summary --
    if (event.type === "progress_summary") {
      const ps = event as import("@/types/events").ProgressSummaryEvent;
      newProgressSummary = { total: ps.total, completed: ps.completed, failed: ps.failed };
    }

    // -- infer running from activity events --
    if (event.node_id) {
      const cur = newNodeStatuses[event.node_id];
      if (!cur || cur === "idle") {
        newNodeStatuses[event.node_id] = "running";
        mutatedStatuses = true;
      }
    }
  }

  useRunStore.setState({
    events: [...state.events, ...events],
    status: newStatus,
    nodeStatuses: mutatedStatuses ? newNodeStatuses : state.nodeStatuses,
    parentChildMap: mutatedParentChild ? newParentChildMap : state.parentChildMap,
    progressSummary: newProgressSummary,
  });
}

/** Flush the pending event buffer into the store */
function _flushPending() {
  if (_pendingEvents.length === 0) return;
  const batch = _pendingEvents;
  _pendingEvents = [];
  _applyEvents(batch);
}

/** Schedule a batched flush on the next animation frame */
function _scheduleFlush() {
  if (_flushRaf === null) {
    _flushRaf = requestAnimationFrame(() => {
      _flushRaf = null;
      _flushPending();
    });
  }
  // Safety valve: if buffer grows too large, flush synchronously
  if (_pendingEvents.length >= MAX_PENDING) {
    if (_flushRaf !== null) {
      cancelAnimationFrame(_flushRaf);
      _flushRaf = null;
    }
    _flushPending();
  }
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useRunStore = create<RunState>((set, get) => ({
  runId: null,
  status: "idle",
  events: [],
  nodeStatuses: {},
  selectedRunNodeId: null,
  parentChildMap: {},
  progressSummary: null,

  setRunId: (id) => set({ runId: id }),

  setStatus: (status) => set({ status }),

  addEvent: (event: StreamEvent) => {
    // O(1) dedup via Set
    if (event.event_id) {
      if (_eventIds.has(event.event_id)) return;
      _eventIds.add(event.event_id);
    }
    _pendingEvents.push(event);
    _scheduleFlush();
  },

  hydrateEvents: (events: StreamEvent[]) => {
    // Reset batch state
    _eventIds.clear();
    _pendingEvents = [];
    if (_flushRaf !== null) {
      cancelAnimationFrame(_flushRaf);
      _flushRaf = null;
    }
    set({ events: [], nodeStatuses: {}, parentChildMap: {}, progressSummary: null });
    // Register IDs and process immediately
    const newEvents = events.filter((e) => {
      if (e.event_id && _eventIds.has(e.event_id)) return false;
      if (e.event_id) _eventIds.add(e.event_id);
      return true;
    });
    _applyEvents(newEvents);
  },

  mergeEvents: (events: StreamEvent[]) => {
    // Dedup + register IDs, then process immediately (REST backfill)
    const newEvents = events.filter((e) => {
      if (e.event_id && _eventIds.has(e.event_id)) return false;
      if (e.event_id) _eventIds.add(e.event_id);
      return true;
    });
    _applyEvents(newEvents);
  },

  clearEvents: () => {
    _eventIds.clear();
    _pendingEvents = [];
    if (_flushRaf !== null) {
      cancelAnimationFrame(_flushRaf);
      _flushRaf = null;
    }
    set({ events: [], nodeStatuses: {}, selectedRunNodeId: null, parentChildMap: {}, progressSummary: null });
  },

  setNodeStatus: (nodeId: string, status: RunStatus) => {
    set({ nodeStatuses: { ...get().nodeStatuses, [nodeId]: status } });
  },

  setSelectedRunNode: (id) => set({ selectedRunNodeId: id }),

  registerChild: (parentId: string, childId: string) => {
    const existing = get().parentChildMap[parentId] ?? [];
    set({
      parentChildMap: {
        ...get().parentChildMap,
        [parentId]: [...existing, childId],
      },
    });
  },
}));

// ---------------------------------------------------------------------------
// Selector helpers — use with useRunStore(selector)
// ---------------------------------------------------------------------------

/** Select events filtered by type */
export function useEventsByType(type: StreamEventType): StreamEvent[] {
  const all = useRunStore((s) => s.events);
  return useMemo(() => all.filter((e) => e.type === type), [all, type]);
}

/** Select events filtered by node_id */
export function useEventsByNode(nodeId: string): StreamEvent[] {
  const all = useRunStore((s) => s.events);
  return useMemo(() => all.filter((e) => e.node_id === nodeId), [all, nodeId]);
}

/** Select the current RunStatus for a specific node */
export function useNodeStatus(nodeId: string): RunStatus {
  return useRunStore(
    (state) => state.nodeStatuses[nodeId] ?? "idle"
  );
}

/** Select child node IDs for a given parent planner node */
export function useChildrenOfNode(parentId: string): string[] {
  const children = useRunStore((state) => state.parentChildMap[parentId]);
  return useMemo(() => children ?? [], [children]);
}
