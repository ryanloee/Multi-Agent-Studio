import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * CoderNode — displays the model identifier as a small subtitle.
 */
const CoderNode = memo(function CoderNode(props: NodeProps) {
  return <BaseNode {...props} />;
});

export default CoderNode;
