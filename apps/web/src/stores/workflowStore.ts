import { create } from "zustand";
import {
  applyNodeChanges,
  applyEdgeChanges,
  type OnNodesChange,
  type OnEdgesChange,
  type OnConnect,
  type NodeChange,
  type EdgeChange,
  type Connection,
} from "@xyflow/react";
// Connection is used in onConnect parameter type
import type {
  WorkflowNode,
  WorkflowEdge,
  AgentNodeType,
  WorkerAgentType,
  NodeData,
  AutoChildModelMap,
  WorkflowLifecyclePhase,
  WorkflowBlocker,
} from "@/types/workflow";
import type {
  PlannerDraftStructuredState,
  PlannerStructuredAction,
  PlannerUiState,
  PlannerStage,
  WorkflowDetail,
} from "@/types/api";
import { NODE_META, VALID_CONNECTIONS } from "@/lib/constants";
import { translations } from "@/lib/i18n";
import { useLocaleStore } from "./localeStore";
import { api } from "@/lib/api";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------
interface WorkflowState {
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  selectedNodeId: string | null;
  selectedEdgeId: string | null;

  // Current workflow identity + workspace directory
  currentWorkflowId: string | null;
  workspaceDirectory: string;

  // Workflow dual mode (auto vs manual)
  mode: "auto" | "manual";
  goal: string;
  autoChildModelMap: AutoChildModelMap;
  lifecyclePhase: WorkflowLifecyclePhase;
  blockers: WorkflowBlocker[];
  projectSummary: Record<string, unknown>;
  plannerUiState: PlannerUiState;
  plannerDraftState: PlannerDraftStructuredState | null;
  plannerActionState: PlannerStructuredAction | null;
  plannerSubStage: PlannerStage | null;

  // React Flow change handlers
  onNodesChange: OnNodesChange<WorkflowNode>;
  onEdgesChange: OnEdgesChange;

  // Node CRUD
  addNode: (type: AgentNodeType, position: { x: number; y: number }) => void;
  updateNodeData: (id: string, data: Partial<NodeData>) => void;
  removeNode: (id: string) => void;

  // Dynamic node creation (planner children at runtime)
  addDynamicNode: (
    parentId: string,
    childDef: {
      id: string;
      type: AgentNodeType;
      prompt: string;
      model: string;
    }
  ) => void;

  // Connection handler with validation
  onConnect: OnConnect;

  // Edge configuration
  updateEdgeData: (id: string, data: Partial<import("@/types/workflow").EdgeData>) => void;

  // Bulk operations
  loadWorkflow: (workflow: WorkflowDetail) => void;
  clearCanvas: () => void;

  // Selection
  setSelectedNode: (id: string | null) => void;
  setSelectedEdge: (id: string | null) => void;

  // Focus + highlight a node on the canvas (triggers fitView animation)
  focusNodeId: string | null;
  setFocusNode: (id: string | null) => void;

  // Workspace directory
  updateWorkspaceDirectory: (dir: string) => Promise<void>;
  setLifecyclePhase: (phase: WorkflowLifecyclePhase) => void;
  setBlockers: (blockers: WorkflowBlocker[]) => void;
  setProjectSummary: (summary: Record<string, unknown>) => void;
  setPlannerUiState: (state: PlannerUiState) => void;
  setPlannerDraftState: (state: PlannerDraftStructuredState | null) => void;
  setPlannerActionState: (action: PlannerStructuredAction | null) => void;
  setPlannerSubStage: (stage: PlannerStage | null) => void;
  applyPlannerDagPreview: (dag: { nodes: Array<Record<string, unknown>>; edges: Array<Record<string, unknown>> }) => void;
  clearPlannerDraftState: () => void;

  // Mode & goal
  updateMode: (mode: "auto" | "manual") => Promise<void>;
  updateGoal: (goal: string) => Promise<void>;
  updateAutoChildModelMap: (agentType: WorkerAgentType, model: string) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Helper: generate a simple unique id
// ---------------------------------------------------------------------------
let idCounter = 0;
function nextNodeId(): string {
  idCounter += 1;
  return `node_${Date.now()}_${idCounter}`;
}

function isAgentNodeType(value: unknown): value is AgentNodeType {
  return typeof value === "string" && value in NODE_META;
}

function normalizeWorkflowNode(node: unknown, index: number): WorkflowNode {
  const raw = (node ?? {}) as Record<string, unknown>;
  const data = (typeof raw.data === "object" && raw.data !== null ? raw.data : {}) as Partial<NodeData>;
  let nodeType = isAgentNodeType(raw.type)
    ? raw.type
    : isAgentNodeType(data.agentType)
      ? data.agentType
      : "coder";
  if (nodeType === "plan" && String(raw.id || "") !== "planner") {
    nodeType = "design";
  }
  const meta = NODE_META[nodeType];
  const rawPosition = (typeof raw.position === "object" && raw.position !== null ? raw.position : {}) as {
    x?: unknown;
    y?: unknown;
  };
  const position = {
    x: typeof rawPosition.x === "number" ? rawPosition.x : 100 + (index % 3) * 280,
    y: typeof rawPosition.y === "number" ? rawPosition.y : 100 + Math.floor(index / 3) * 140,
  };

  return {
    id: String(raw.id || nextNodeId()),
    type: nodeType,
    position,
    data: {
      label: String(data.label || raw.label || meta.label),
      agentType: nodeType,
      modelProvider: String(data.modelProvider || raw.model_provider || ""),
      modelId: String(data.modelId || raw.model_id || ""),
      prompt: String(data.prompt || raw.prompt || ""),
      permissions: data.permissions ?? meta.defaultData.permissions ?? {},
      command: String(data.command || raw.command || ""),
      description: String(data.description || raw.description || ""),
      childNodeIds: data.childNodeIds,
      parentNodeId: data.parentNodeId,
      isDynamic: data.isDynamic,
    },
  };
}

function normalizeWorkflowEdge(edge: unknown): WorkflowEdge | null {
  const raw = (edge ?? {}) as Record<string, unknown>;
  const source = typeof raw.source === "string" ? raw.source : "";
  const target = typeof raw.target === "string" ? raw.target : "";
  if (!source || !target) return null;
  return {
    ...(raw as Partial<WorkflowEdge>),
    id: String(raw.id || `e_${source}-${target}`),
    source,
    target,
    data: {
      transfer_files: true,
      transfer_summary: true,
      transfer_format: "summary",
      ...((typeof raw.data === "object" && raw.data !== null) ? raw.data : {}),
    },
  } as WorkflowEdge;
}

function stripInternalPlannerNodes(nodes: WorkflowNode[], edges: WorkflowEdge[]) {
  const visibleNodes = nodes.filter((node) => node.id !== "planner");
  const visibleNodeIds = new Set(visibleNodes.map((node) => node.id));
  const visibleEdges = edges.filter(
    (edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target)
  );
  return { nodes: visibleNodes, edges: visibleEdges };
}

function layoutDagNodes(nodes: WorkflowNode[], edges: WorkflowEdge[]): WorkflowNode[] {
  if (nodes.length === 0) return nodes;

  const nodeIds = new Set(nodes.map((node) => node.id));
  const incoming = new Map<string, string[]>();
  const outgoing = new Map<string, string[]>();
  for (const node of nodes) {
    incoming.set(node.id, []);
    outgoing.set(node.id, []);
  }

  for (const edge of edges) {
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    incoming.get(edge.target)?.push(edge.source);
    outgoing.get(edge.source)?.push(edge.target);
  }

  const roots = nodes
    .filter((node) => (incoming.get(node.id) ?? []).length === 0)
    .map((node) => node.id);
  const queue = roots.length > 0 ? [...roots] : [nodes[0].id];
  const layerById = new Map<string, number>();
  for (const root of queue) layerById.set(root, 0);

  while (queue.length > 0) {
    const current = queue.shift()!;
    const currentLayer = layerById.get(current) ?? 0;
    for (const child of outgoing.get(current) ?? []) {
      const nextLayer = Math.max(layerById.get(child) ?? 0, currentLayer + 1);
      if ((layerById.get(child) ?? -1) < nextLayer) {
        layerById.set(child, nextLayer);
        queue.push(child);
      }
    }
  }

  for (const node of nodes) {
    if (!layerById.has(node.id)) {
      layerById.set(node.id, Math.max(0, layerById.size));
    }
  }

  const layers = new Map<number, WorkflowNode[]>();
  for (const node of nodes) {
    const layer = layerById.get(node.id) ?? 0;
    layers.set(layer, [...(layers.get(layer) ?? []), node]);
  }

  const xCenter = 520;
  const xGap = 300;
  const yGap = 170;
  const yStart = 80;

  return nodes.map((node) => {
    const layer = layerById.get(node.id) ?? 0;
    const layerNodes = layers.get(layer) ?? [];
    const index = layerNodes.findIndex((item) => item.id === node.id);
    const count = Math.max(layerNodes.length, 1);
    return {
      ...node,
      position: {
        x: xCenter + (index - (count - 1) / 2) * xGap,
        y: yStart + layer * yGap,
      },
    };
  });
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  selectedEdgeId: null,
  focusNodeId: null,
  currentWorkflowId: null,
  workspaceDirectory: "",
  mode: "auto" as const,
  goal: "",
  autoChildModelMap: {},
  lifecyclePhase: "draft",
  blockers: [],
  projectSummary: {},
  plannerUiState: {},
  plannerDraftState: null,
  plannerActionState: null,
  plannerSubStage: null,

  // ---- React Flow change handlers ----
  onNodesChange: (changes: NodeChange<WorkflowNode>[]) => {
    set({ nodes: applyNodeChanges(changes, get().nodes) });
  },

  onEdgesChange: (changes: EdgeChange<WorkflowEdge>[]) => {
    set({ edges: applyEdgeChanges(changes, get().edges) });
  },

  // ---- Node CRUD ----
  addNode: (type: AgentNodeType, position: { x: number; y: number }) => {
    const meta = NODE_META[type];
    const id = nextNodeId();
    // Read current locale for translated default label
    const { locale } = useLocaleStore.getState();
    const t = (key: string) => translations[locale][key] || key;
    const newNode: WorkflowNode = {
      id,
      type,
      position,
      data: {
        label: t(`node.${type}.label`),
        agentType: type,
        modelProvider: meta.defaultData.modelProvider ?? "",
        modelId: meta.defaultData.modelId ?? "",
        prompt: meta.defaultData.prompt ?? "",
        permissions: meta.defaultData.permissions ?? {},
        command: meta.defaultData.command ?? "",
        description: meta.defaultData.description ?? "",
      },
    };
    set({ nodes: [...get().nodes, newNode] });
  },

  updateNodeData: (id: string, data: Partial<NodeData>) => {
    set({
      nodes: get().nodes.map((node) =>
        node.id === id ? { ...node, data: { ...node.data, ...data } } : node
      ),
    });
  },

  removeNode: (id: string) => {
    set({
      nodes: get().nodes.filter((node) => node.id !== id),
      edges: get().edges.filter((edge) => edge.source !== id && edge.target !== id),
      selectedNodeId: get().selectedNodeId === id ? null : get().selectedNodeId,
      selectedEdgeId: null,
    });
  },

  // ---- Dynamic node creation (planner children) ----
  addDynamicNode: (
    parentId: string,
    childDef: {
      id: string;
      type: AgentNodeType;
      prompt: string;
      model: string;
    }
  ) => {
    const { nodes } = get();
    const parentNode = nodes.find((n) => n.id === parentId);
    const attachToParent = parentId !== "planner" && Boolean(parentNode);

    // Calculate existing children count for positioning
    const existingChildren = attachToParent
      ? nodes.filter((n) => (n.data as NodeData).parentNodeId === parentId)
      : [];
    const offset = existingChildren.length;

    // Position child below parent; fall back to origin if parent not found
    const parentX = parentNode?.position.x ?? 400;
    const parentY = parentNode?.position.y ?? 200;
    const childX = parentX + 220;
    const childY = parentY + offset * 120;

    // Parse model string into provider/modelId
    const modelParts = childDef.model.split("/");
    const modelProvider = modelParts.length > 1 ? modelParts[0] : "";
    const modelId = modelParts.length > 1 ? modelParts.slice(1).join("/") : childDef.model;

    const meta = NODE_META[childDef.type];
    const { locale } = useLocaleStore.getState();
    const t = (key: string) => translations[locale][key] || key;

    const newNode: WorkflowNode = {
      id: childDef.id,
      type: childDef.type,
      position: { x: childX, y: childY },
      data: {
        label: `${t(`node.${childDef.type}.label`)} #${offset + 1}`,
        agentType: childDef.type,
        modelProvider,
        modelId,
        prompt: childDef.prompt,
        permissions: meta.defaultData.permissions ?? {},
        command: "",
        description: "",
        parentNodeId: attachToParent ? parentId : undefined,
        isDynamic: true,
      },
    };

    // Create a dashed "dynamic" edge from parent to child
    const newEdge: WorkflowEdge = {
      id: `e_dynamic_${parentId}-${childDef.id}`,
      source: parentId,
      target: childDef.id,
      style: { strokeDasharray: "5 5", stroke: "#22c55e" },
      animated: true,
    };

    // Update parent node's childNodeIds
    const updatedNodes = nodes.map((node) => {
      if (attachToParent && node.id === parentId) {
        const currentChildIds = (node.data as NodeData).childNodeIds ?? [];
        if (currentChildIds.includes(childDef.id)) return node;
        return {
          ...node,
          data: {
            ...node.data,
            childNodeIds: [...currentChildIds, childDef.id],
          },
        };
      }
      return node;
    });

    // Skip duplicate node creation, but still refresh runtime metadata and add
    // a missing edge. Events can arrive as task_created first, then
    // child_created with the real agent type/model.
    const nodeExists = nodes.some((n) => n.id === childDef.id);
    const edgeExists = get().edges.some((e) => e.id === newEdge.id);
    const nodesWithUpdatedChild = nodeExists
      ? updatedNodes.map((node) =>
          node.id === childDef.id
            ? {
                ...node,
                type: childDef.type,
                data: {
                  ...node.data,
                  agentType: childDef.type,
                  modelProvider,
                  modelId,
                  prompt: childDef.prompt || node.data.prompt,
                  parentNodeId: (node.data as NodeData).parentNodeId ?? (attachToParent ? parentId : undefined),
                  isDynamic: true,
                },
              }
            : node
        )
      : updatedNodes;

    set({
      nodes: nodeExists ? nodesWithUpdatedChild : [...updatedNodes, newNode],
      edges: attachToParent && !edgeExists ? [...get().edges, newEdge] : get().edges,
    });
  },

  // ---- Connection with validation ----
  onConnect: (connection: Connection) => {
    const { nodes, edges } = get();

    // Look up source and target node types
    const sourceNode = nodes.find((n) => n.id === connection.source);
    const targetNode = nodes.find((n) => n.id === connection.target);

    if (!sourceNode || !targetNode) return;

    // Check against VALID_CONNECTIONS whitelist
    const allowed = VALID_CONNECTIONS.some(
      (rule) =>
        rule.source === sourceNode.type &&
        rule.target === targetNode.type
    );

    if (!allowed) return;

    // Prevent duplicate edges
    const duplicate = edges.some(
      (edge) =>
        edge.source === connection.source &&
        edge.target === connection.target
    );
    if (duplicate) return;

    const newEdge: WorkflowEdge = {
      id: `e_${connection.source}-${connection.target}`,
      source: connection.source,
      target: connection.target,
      data: {
        transfer_files: true,
        transfer_summary: true,
        transfer_format: "summary",
      },
      ...(connection.sourceHandle && { sourceHandle: connection.sourceHandle }),
      ...(connection.targetHandle && { targetHandle: connection.targetHandle }),
    };

    set({ edges: [...edges, newEdge] });
  },

  // ---- Edge configuration ----
  updateEdgeData: (id: string, data: Partial<import("@/types/workflow").EdgeData>) => {
    set({
      edges: get().edges.map((edge) =>
        edge.id === id
          ? { ...edge, data: { ...(edge.data ?? {}), ...data } }
          : edge
      ),
    });
  },

  // ---- Bulk operations ----
  loadWorkflow: (workflow: WorkflowDetail) => {
    const normalizedNodes = (workflow.nodes ?? []).map(normalizeWorkflowNode);
    const normalizedEdges = (workflow.edges ?? [])
      .map(normalizeWorkflowEdge)
      .filter((edge): edge is WorkflowEdge => edge !== null);
    const visibleDag = stripInternalPlannerNodes(normalizedNodes, normalizedEdges);
    const shouldAutoLayout = workflow.mode === "auto" || visibleDag.nodes.some((node) => {
      const raw = (workflow.nodes ?? []).find((item) => (item as WorkflowNode).id === node.id) as Partial<WorkflowNode> | undefined;
      return !raw?.position || typeof raw.position.x !== "number" || typeof raw.position.y !== "number";
    });

    set({
      nodes: shouldAutoLayout ? layoutDagNodes(visibleDag.nodes, visibleDag.edges) : visibleDag.nodes,
      edges: visibleDag.edges,
      selectedNodeId: null,
      selectedEdgeId: null,
      currentWorkflowId: workflow.id,
      workspaceDirectory: workflow.workspace_directory ?? "",
      mode: "auto",
      goal: workflow.goal ?? "",
      autoChildModelMap: workflow.metadata?.auto_child_model_map ?? {},
      lifecyclePhase: workflow.lifecycle_phase ?? "draft",
      blockers: workflow.blockers ?? [],
      projectSummary: workflow.project_summary ?? {},
      plannerUiState: workflow.metadata?.planner_ui_state ?? {},
      plannerDraftState: workflow.metadata?.planner_draft_state ?? null,
      plannerActionState: null,
      plannerSubStage: workflow.metadata?.planner_draft_state?.current_stage ?? null,
    });
  },

  clearCanvas: () => {
    set({
      nodes: [],
      edges: [],
      selectedNodeId: null,
      selectedEdgeId: null,
      mode: "auto",
      goal: "",
      autoChildModelMap: {},
      lifecyclePhase: "draft",
      blockers: [],
      projectSummary: {},
      plannerUiState: {},
      plannerDraftState: null,
      plannerActionState: null,
      plannerSubStage: null,
    });
  },

  // ---- Selection ----
  setSelectedNode: (id: string | null) => {
    set({ selectedNodeId: id, selectedEdgeId: id ? null : get().selectedEdgeId });
  },

  setSelectedEdge: (id: string | null) => {
    set({ selectedEdgeId: id, selectedNodeId: id ? null : get().selectedNodeId });
  },

  // ---- Focus node on canvas ----
  setFocusNode: (id: string | null) => {
    set({ focusNodeId: id, selectedNodeId: id });
  },

  // ---- Workspace directory ----
  updateWorkspaceDirectory: async (dir: string) => {
    const { currentWorkflowId, workspaceDirectory, lifecyclePhase } = get();
    if (!currentWorkflowId) return;
    set({ workspaceDirectory: dir });
    try {
      await api.updateWorkflow(currentWorkflowId, { workspace_directory: dir });
      if (!workspaceDirectory.trim() && dir.trim() && (lifecyclePhase === "draft" || lifecyclePhase === "blocked")) {
        const assessed = await api.assessWorkflow(currentWorkflowId);
        get().loadWorkflow(assessed);
      }
    } catch (err) {
      console.error("Failed to update workspace directory:", err);
    }
  },

  setLifecyclePhase: (phase) => set({ lifecyclePhase: phase }),
  setBlockers: (blockers) => set({ blockers }),
  setProjectSummary: (projectSummary) => set({ projectSummary }),
  setPlannerUiState: (plannerUiState) => set({ plannerUiState }),
  setPlannerDraftState: (plannerDraftState) => set({ plannerDraftState }),
  setPlannerActionState: (plannerActionState) => set({ plannerActionState }),
  setPlannerSubStage: (plannerSubStage) => set({ plannerSubStage }),
  applyPlannerDagPreview: (dag) => {
    const normalizedNodes = (dag.nodes ?? []).map(normalizeWorkflowNode);
    const normalizedEdges = (dag.edges ?? [])
      .map(normalizeWorkflowEdge)
      .filter((edge): edge is WorkflowEdge => edge !== null);
    const visibleDag = stripInternalPlannerNodes(normalizedNodes, normalizedEdges);
    set({
      nodes: layoutDagNodes(visibleDag.nodes, visibleDag.edges),
      edges: visibleDag.edges,
    });
  },
  clearPlannerDraftState: () => set({
    plannerDraftState: null,
    plannerSubStage: null,
  }),

  // ---- Mode & goal ----
  updateMode: async (_mode: "auto" | "manual") => {
    const { currentWorkflowId } = get();
    // Always update local state immediately (optimistic)
    set({ mode: "auto" });
    if (!currentWorkflowId) return;
    try {
      await api.updateWorkflow(currentWorkflowId, { mode: "auto" });
    } catch (err) {
      console.error("Failed to update workflow mode:", err);
    }
  },

  updateGoal: async (goal: string) => {
    const { currentWorkflowId } = get();
    // Always update local state immediately (optimistic)
    set({ goal });
    if (!currentWorkflowId) return;
    try {
      await api.updateWorkflow(currentWorkflowId, { goal });
    } catch (err) {
      console.error("Failed to update workflow goal:", err);
    }
  },

  updateAutoChildModelMap: async (agentType, model) => {
    const { currentWorkflowId, autoChildModelMap, plannerUiState, plannerDraftState } = get();
    const nextMap = {
      ...autoChildModelMap,
      [agentType]: model,
    };
    set({ autoChildModelMap: nextMap });
    if (!currentWorkflowId) return;
    try {
      await api.updateWorkflow(currentWorkflowId, {
        metadata: {
          auto_child_model_map: nextMap,
          planner_ui_state: plannerUiState,
          planner_draft_state: plannerDraftState ?? undefined,
        },
      });
    } catch (err) {
      console.error("Failed to update auto child model map:", err);
    }
  },
}));
