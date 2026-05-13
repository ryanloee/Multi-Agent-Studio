"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { BookOpen, Save, Eye, Pencil, Maximize2, Minimize2, X } from "lucide-react";
import { api } from "@/lib/api";
import { useLocaleStore } from "@/stores/localeStore";
import MarkdownMessage from "@/components/common/MarkdownMessage";

export default function SharedDocTab({ workflowId }: { workflowId?: string }) {
  const t = useLocaleStore((s) => s.t);
  const [content, setContent] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [lastUpdated, setLastUpdated] = useState("");
  const [updatedBy, setUpdatedBy] = useState("");
  const loadedWorkflowIdRef = useRef<string | undefined>(undefined);

  // Load shared doc
  useEffect(() => {
    if (!workflowId || loadedWorkflowIdRef.current === workflowId) return;
    loadedWorkflowIdRef.current = workflowId;
    api.getSharedDoc(workflowId).then((doc) => {
      setContent(doc.content);
      setEditContent(doc.content);
      setLastUpdated(doc.updated_at);
      setUpdatedBy(doc.updated_by);
    }).catch(() => {
      // ignore — will be created on first save
    });
  }, [workflowId]);

  const handleSave = useCallback(async () => {
    if (!workflowId) return;
    setSaving(true);
    try {
      const doc = await api.updateSharedDoc(workflowId, editContent, "user");
      setContent(doc.content);
      setLastUpdated(doc.updated_at);
      setUpdatedBy(doc.updated_by);
      setEditing(false);
    } catch (err) {
      console.error("[SharedDocTab] save failed:", err);
    } finally {
      setSaving(false);
    }
  }, [workflowId, editContent]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        handleSave();
      }
    },
    [handleSave],
  );

  const handleBlur = useCallback(() => {
    if (editContent !== content && editContent.trim()) {
      handleSave();
    }
  }, [editContent, content, handleSave]);

  const renderDocumentContent = (fullscreen: boolean) => {
    if (editing) {
      return (
        <textarea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          onBlur={fullscreen ? undefined : handleBlur}
          onKeyDown={handleKeyDown}
          placeholder={t("sharedDoc.placeholder") || "在这里编写项目文档、架构决策、API 设计等...\n\n此文档对 Planner 和所有 Worker 可见。"}
          className={[
            "w-full resize-none focus:outline-none font-mono text-gray-700 leading-relaxed",
            fullscreen ? "h-full bg-transparent p-6 text-sm" : "flex-1 p-3 text-[11px]",
          ].join(" ")}
          autoFocus
        />
      );
    }

    if (content) {
      return (
        <div
          className={[
            "text-gray-700",
            fullscreen ? "h-full overflow-y-auto p-6 text-sm leading-7" : "text-[11px] leading-relaxed",
          ].join(" ")}
        >
          <MarkdownMessage content={content} compact={!fullscreen} />
        </div>
      );
    }

    return (
      <div className="flex h-full items-center justify-center">
        <p className={fullscreen ? "text-sm text-gray-400" : "text-[11px] text-gray-400"}>
          {t("sharedDoc.placeholder") || "点击编辑按钮添加项目文档..."}
        </p>
      </div>
    );
  };

  return (
    <>
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-gray-100 shrink-0">
        <BookOpen size={13} className="text-blue-500" />
        <span className="text-[11px] font-medium text-gray-600">
          {t("leftPanel.sharedDoc") || "项目文档"}
        </span>
        <div className="flex-1" />
        <button
          onClick={() => setExpanded(true)}
          className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-blue-50 text-blue-600 hover:bg-blue-100 transition-colors"
          title={t("sharedDoc.expand") || "展开查看"}
        >
          <Maximize2 size={10} /> {t("sharedDoc.expand") || "展开"}
        </button>
        <button
          onClick={() => {
            if (editing) {
              setEditContent(content);
              setEditing(false);
            } else {
              setEditContent(content);
              setEditing(true);
            }
          }}
          className="flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-500 hover:bg-gray-200 transition-colors"
        >
          {editing ? (
            <>
              <Eye size={10} /> {t("sharedDoc.viewMode") || "预览"}
            </>
          ) : (
            <>
              <Pencil size={10} /> {t("sharedDoc.editMode") || "编辑"}
            </>
          )}
        </button>
      </div>

      {/* Content */}
      {editing ? (
        renderDocumentContent(false)
      ) : (
        <div
          className="flex-1 overflow-y-auto p-3 cursor-zoom-in"
          onClick={() => setExpanded(true)}
        >
          {renderDocumentContent(false)}
        </div>
      )}

      {/* Footer */}
      {editing && (
        <div className="flex items-center gap-2 px-3 py-1.5 border-t border-gray-100 shrink-0">
          <button
            onClick={handleSave}
            disabled={saving || editContent === content}
            className="flex items-center gap-1 text-[10px] px-2.5 py-0.5 rounded bg-blue-500 text-white hover:bg-blue-600 disabled:opacity-40"
          >
            <Save size={10} />
            {saving ? "..." : t("settings.save") || "保存"}
          </button>
          <span className="text-[9px] text-gray-400">Ctrl+S</span>
        </div>
      )}
      {!editing && lastUpdated && (
        <div className="px-3 py-1.5 border-t border-gray-100 shrink-0">
          <span className="text-[9px] text-gray-400">
            {t("sharedDoc.lastUpdated") || "最后更新"}: {new Date(lastUpdated).toLocaleString()} ({updatedBy})
          </span>
        </div>
      )}
    </div>
    {expanded && (
      <div className="fixed inset-0 z-50 bg-slate-950/55 backdrop-blur-sm">
        <div className="absolute inset-4 rounded-3xl border border-slate-200 bg-white shadow-2xl overflow-hidden">
          <div className="flex items-center gap-3 border-b border-slate-200 px-6 py-4">
            <BookOpen size={18} className="text-blue-600" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-slate-900">
                {t("leftPanel.sharedDoc") || "项目文档"}
              </div>
              <div className="text-xs text-slate-500">
                {t("sharedDoc.lastUpdated") || "最后更新"}: {lastUpdated ? new Date(lastUpdated).toLocaleString() : "-"}
              </div>
            </div>
            <button
              onClick={() => setExpanded(false)}
              className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-200"
            >
              <Minimize2 size={12} />
              {t("sharedDoc.collapse") || "收起"}
            </button>
            <button
              onClick={() => setExpanded(false)}
              className="rounded-full p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-700"
              aria-label={t("sharedDoc.close") || "关闭"}
            >
              <X size={16} />
            </button>
          </div>

          <div className="flex h-[calc(100%-73px)] flex-col">
            <div className="flex items-center gap-2 px-6 py-3 border-b border-slate-100 bg-slate-50">
              <button
                onClick={() => {
                  if (editing) {
                    setEditContent(content);
                    setEditing(false);
                  } else {
                    setEditContent(content);
                    setEditing(true);
                  }
                }}
                className="inline-flex items-center gap-1 rounded-full bg-white px-3 py-1.5 text-xs font-medium text-slate-600 border border-slate-200 hover:bg-slate-100"
              >
                {editing ? (
                  <>
                    <Eye size={12} /> {t("sharedDoc.viewMode") || "预览"}
                  </>
                ) : (
                  <>
                    <Pencil size={12} /> {t("sharedDoc.editMode") || "编辑"}
                  </>
                )}
              </button>
              {editing && (
                <button
                  onClick={handleSave}
                  disabled={saving || editContent === content}
                  className="inline-flex items-center gap-1 rounded-full bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-40"
                >
                  <Save size={12} />
                  {saving ? "..." : t("settings.save") || "保存"}
                </button>
              )}
            </div>
            <div className="flex-1 overflow-hidden bg-white">
              {renderDocumentContent(true)}
            </div>
          </div>
        </div>
      </div>
    )}
    </>
  );
}
