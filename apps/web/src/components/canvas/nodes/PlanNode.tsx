import React, { memo } from "react";
import { type NodeProps } from "@xyflow/react";
import type { NodeData } from "@/types/workflow";
import BaseNode from "@/components/canvas/BaseNode";
import { useChildrenOfNode } from "@/stores/runStore";
import { useLocaleStore } from "@/stores/localeStore";

/**
 * PlanNode — shows planner badge + child task count when children exist.
 * Green border accent distinguishes it from regular nodes.
 */
const PlanNode = memo(function PlanNode(props: NodeProps) {
  const data = props.data as NodeData;
  const childIds = useChildrenOfNode(props.id);
  const childCount = (data.childNodeIds?.length ?? 0) + childIds.length;
  const t = useLocaleStore((s) => s.t);

  return (
    <BaseNode {...props}>
      <div className="flex flex-col gap-1">
        {data.modelId && (
          <span className="text-gray-400">
            {data.modelProvider ? `${data.modelProvider}/${data.modelId}` : data.modelId}
          </span>
        )}
        {childCount > 0 && (
          <span className="inline-block rounded bg-green-100 px-1.5 py-0.5 text-green-700 text-xs font-medium">
            {t("planNode.childTasks").replace("{n}", String(childCount))}
          </span>
        )}
      </div>
    </BaseNode>
  );
});

export default PlanNode;
