import dynamic from "next/dynamic";

// ---------------------------------------------------------------------------
// page.tsx — Server Component wrapper
//
// The WorkflowEditor uses @xyflow/react (React Flow) which accesses
// window / document at module-load time and crashes during SSR.
// Using next/dynamic with { ssr: false } ensures the entire editor
// sub-tree is only loaded and rendered in the browser.
// ---------------------------------------------------------------------------

const WorkflowEditor = dynamic(
  () => import("./WorkflowEditor"),
  { ssr: false }
);

export default function WorkflowEditorPage() {
  return <WorkflowEditor />;
}
