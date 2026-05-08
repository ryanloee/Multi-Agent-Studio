import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import BaseNode from "@/components/canvas/BaseNode";

/**
 * ExploreNode — basic node with no extra content beyond the base shell.
 */
const ExploreNode = memo(function ExploreNode(props: NodeProps) {
  return <BaseNode {...props} />;
});

export default ExploreNode;
