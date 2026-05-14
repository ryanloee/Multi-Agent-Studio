import type { NodeTypes } from "@xyflow/react";

import CoderNode from "@/components/canvas/nodes/CoderNode";
import PlanNode from "@/components/canvas/nodes/PlanNode";
import ShellNode from "@/components/canvas/nodes/ShellNode";
import ReviewNode from "@/components/canvas/nodes/ReviewNode";
import ExploreNode from "@/components/canvas/nodes/ExploreNode";
import MergeNode from "@/components/canvas/nodes/MergeNode";
import HumanNode from "@/components/canvas/nodes/HumanNode";
import ChildNode from "@/components/canvas/nodes/ChildNode";

/**
 * nodeTypes mapping — registered with React Flow's <ReactFlow> component.
 *
 * IMPORTANT: This object must be defined OUTSIDE of any React component
 * so that its reference remains stable across re-renders (React Flow requirement).
 */
export const nodeTypes: NodeTypes = {
  coder: CoderNode,
  plan: PlanNode,
  design: PlanNode,
  shell: ShellNode,
  review: ReviewNode,
  explore: ExploreNode,
  merge: MergeNode,
  human: HumanNode,
  child: ChildNode,
};
