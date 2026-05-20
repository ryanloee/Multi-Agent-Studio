import { create } from "zustand";
import { useMemo } from "react";
import type { RunStatus, WorkflowLifecyclePhase } from "@/types/workflow";
import type { StreamEvent, StreamEventType, StatusEvent, ChildCreatedEvent, ChildCompletedEvent, DirectorDecisionEvent } from "@/types/events";
import { useWorkflowStore } from "@/stores/workflowStore";

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
  /** Director dispatch decisions for timeline display */
  directorDecisions: DirectorDecisionEvent[];

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
const MAX_EVENT_IDS = 5000;

let _lastSyncedPhase: WorkflowLifecyclePhase | null = null;

/** Update workflowStore.lifecyclePhase when run reaches a terminal state. */
function _syncWorkflowPhase(phase: WorkflowLifecyclePhase) {
  if (_lastSyncedPhase === phase) return;
  const ws = useWorkflowStore.getState();
  if (ws.lifecyclePhase !== phase) {
    ws.setLifecyclePhase(phase);
  }
  _lastSyncedPhase = phase;
}

/** Trim _eventIds if it exceeds the cap. */
function _trimEventIds() {
  if (_eventIds.size <= MAX_EVENT_IDS) return;
  const state = useRunStore.getState();
  const keep = new Set<string>();
  for (let i = state.events.length - MAX_EVENT_IDS; i < state.events.length; i++) {
    const eid = state.events[i]?.event_id;
    if (eid) keep.add(eid);
  }
  _eventIds.clear();
  for (const id of keep) _eventIds.add(id);
}

/** Apply a batch of events to the store in a single setState call */
function _applyEvents(events: StreamEvent[]) {
  if (events.length === 0) return;
  const state = useRunStore.getState();

  let newNodeStatuses: Record<string, RunStatus> | null = null;
  let newParentChildMap: Record<string, string[]> | null = null;
  let newStatus = state.status;
  let newProgressSummary = state.progressSummary;

  function statuses(): Record<string, RunStatus> {
    if (!newNodeStatuses) newNodeStatuses = { ...state.nodeStatuses };
    return newNodeStatuses;
  }
  function childMap(): Record<string, string[]> {
    if (!newParentChildMap) newParentChildMap = { ...state.parentChildMap };
    return newParentChildMap;
  }

  for (const event of events) {
    // -- status --
    if (event.type === "status" && event.node_id) {
      const parsed = parseRunStatus((event as StatusEvent).content);
      if (parsed !== null) {
        statuses()[event.node_id] = parsed;
      }
    }

    // -- node lifecycle --
    if (
      event.node_id &&
      (event.type === "node_started" ||
        event.type === "node_completed" ||
        event.type === "node_failed" ||
        event.type === "node_retried")
    ) {
      const map: Record<string, RunStatus> = {
        node_started: "running",
        node_completed: "completed",
        node_failed: "failed",
        node_retried: "running",
      };
      statuses()[event.node_id] = map[event.type];
    }

    // -- run lifecycle --
    if (event.type === "run_started") newStatus = "running";
    if (event.type === "run_resumed") newStatus = "running";
    if (event.type === "run_completed") newStatus = "completed";
    if (event.type === "run_failed") newStatus = "failed";

    // -- child_created --
    if (event.type === "child_created") {
      const ce = event as ChildCreatedEvent;
      const parentId = event.node_id;
      const childId = ce.child_node_id;
      if (parentId && childId) {
        const existing = childMap()[parentId] ?? [];
        childMap()[parentId] = [...existing, childId];
        if (!statuses()[childId]) {
          statuses()[childId] = "pending";
        }
      }
    }

    // -- child_completed --
    if (event.type === "child_completed") {
      const ce = event as ChildCompletedEvent;
      if (ce.child_node_id) {
        statuses()[ce.child_node_id] = "completed";
      }
    }

    // -- progress_summary --
    if (event.type === "progress_summary") {
      const ps = event as import("@/types/events").ProgressSummaryEvent;
      newProgressSummary = { total: ps.total, completed: ps.completed, failed: ps.failed };
    }

    // -- infer running from activity events --
    if (event.node_id) {
      const ns = newNodeStatuses ?? state.nodeStatuses;
      const cur = ns[event.node_id];
      if (!cur || cur === "idle") {
        statuses()[event.node_id] = "running";
      }
    }
  }

  const directorDecisions = events.filter(
    (e): e is DirectorDecisionEvent => e.type === "director_decision",
  );

  // Sync workflow lifecycle phase once after processing the batch
  if (newStatus === "completed" || newStatus === "failed") {
    _syncWorkflowPhase("review");
  }

  useRunStore.setState({
    events: [...state.events, ...events],
    status: newStatus,
    nodeStatuses: newNodeStatuses ?? state.nodeStatuses,
    parentChildMap: newParentChildMap ?? state.parentChildMap,
    progressSummary: newProgressSummary,
    directorDecisions: directorDecisions.length > 0
      ? [...state.directorDecisions, ...directorDecisions]
      : state.directorDecisions,
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
  directorDecisions: [],

  setRunId: (id) => set({ runId: id }),

  setStatus: (status) => set({ status }),

  addEvent: (event: StreamEvent) => {
    if (event.event_id) {
      if (_eventIds.has(event.event_id)) return;
      _eventIds.add(event.event_id);
      _trimEventIds();
    }
    _pendingEvents.push(event);
    _scheduleFlush();
  },

  hydrateEvents: (events: StreamEvent[]) => {
    _eventIds.clear();
    _lastSyncedPhase = null;
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
    _lastSyncedPhase = null;
    _pendingEvents = [];
    if (_flushRaf !== null) {
      cancelAnimationFrame(_flushRaf);
      _flushRaf = null;
    }
    set({ events: [], nodeStatuses: {}, selectedRunNodeId: null, parentChildMap: {}, progressSummary: null, directorDecisions: [] });
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
