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
  NodeData,
} from "@/types/workflow";
import type { WorkflowDetail } from "@/types/api";
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

  // Current workflow identity + workspace directory
  currentWorkflowId: string | null;
  workspaceDirectory: string;

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

  // Bulk operations
  loadWorkflow: (workflow: WorkflowDetail) => void;
  clearCanvas: () => void;

  // Selection
  setSelectedNode: (id: string | null) => void;

  // Focus + highlight a node on the canvas (triggers fitView animation)
  focusNodeId: string | null;
  setFocusNode: (id: string | null) => void;

  // Workspace directory
  updateWorkspaceDirectory: (dir: string) => Promise<void>;
}

// ---------------------------------------------------------------------------
// Helper: generate a simple unique id
// ---------------------------------------------------------------------------
let idCounter = 0;
function nextNodeId(): string {
  idCounter += 1;
  return `node_${Date.now()}_${idCounter}`;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------
export const useWorkflowStore = create<WorkflowState>((set, get) => ({
  nodes: [],
  edges: [],
  selectedNodeId: null,
  focusNodeId: null,
  currentWorkflowId: null,
  workspaceDirectory: "",

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

    // Calculate existing children count for positioning
    const existingChildren = nodes.filter(
      (n) => (n.data as NodeData).parentNodeId === parentId
    );
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
        parentNodeId: parentId,
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
      if (node.id === parentId) {
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

    // Skip if node or edge already exists (prevents duplicates on re-render)
    const nodeExists = nodes.some((n) => n.id === childDef.id);
    const edgeExists = get().edges.some((e) => e.id === newEdge.id);

    set({
      nodes: nodeExists ? updatedNodes : [...updatedNodes, newNode],
      edges: edgeExists ? get().edges : [...get().edges, newEdge],
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
      ...(connection.sourceHandle && { sourceHandle: connection.sourceHandle }),
      ...(connection.targetHandle && { targetHandle: connection.targetHandle }),
    };

    set({ edges: [...edges, newEdge] });
  },

  // ---- Bulk operations ----
  loadWorkflow: (workflow: WorkflowDetail) => {
    set({
      nodes: workflow.nodes as WorkflowNode[],
      edges: workflow.edges as WorkflowEdge[],
      selectedNodeId: null,
      currentWorkflowId: workflow.id,
      workspaceDirectory: workflow.workspace_directory ?? "",
    });
  },

  clearCanvas: () => {
    set({
      nodes: [],
      edges: [],
      selectedNodeId: null,
    });
  },

  // ---- Selection ----
  setSelectedNode: (id: string | null) => {
    set({ selectedNodeId: id });
  },

  // ---- Focus node on canvas ----
  setFocusNode: (id: string | null) => {
    set({ focusNodeId: id, selectedNodeId: id });
  },

  // ---- Workspace directory ----
  updateWorkspaceDirectory: async (dir: string) => {
    const { currentWorkflowId } = get();
    if (!currentWorkflowId) return;
    set({ workspaceDirectory: dir });
    try {
      await api.updateWorkflow(currentWorkflowId, { workspace_directory: dir });
    } catch (err) {
      console.error("Failed to update workspace directory:", err);
    }
  },
}));
