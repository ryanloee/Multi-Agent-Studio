"use client";

import { useState, useCallback, useEffect } from "react";
import {
  FolderOpen,
  File,
  ChevronRight,
  ChevronDown,
  RefreshCw,
  Loader2,
  Folder,
  X,
} from "lucide-react";
import { useWorkflowStore } from "@/stores/workflowStore";
import { useLocaleStore } from "@/stores/localeStore";
import { api } from "@/lib/api";

interface FileEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  children: FileEntry[];
}

interface TreeNodeProps {
  entry: FileEntry;
  depth: number;
  selectedPath: string | null;
  onSelect: (path: string) => void;
  onToggle: (path: string) => void;
  expanded: Set<string>;
}

function TreeNode({ entry, depth, selectedPath, onSelect, onToggle, expanded }: TreeNodeProps) {
  const isExpanded = expanded.has(entry.path);
  const isSelected = selectedPath === entry.path;

  const handleClick = useCallback(() => {
    if (entry.is_dir) {
      onToggle(entry.path);
    } else {
      onSelect(entry.path);
    }
  }, [entry.is_dir, entry.path, onToggle, onSelect]);

  return (
    <div>
      <button
        onClick={handleClick}
        className={`w-full flex items-center gap-1.5 py-1 px-2 text-left text-xs rounded-md transition-colors ${
          isSelected
            ? "bg-blue-50 text-blue-700"
            : "text-gray-600 hover:bg-gray-50 hover:text-gray-800"
        }`}
        style={{ paddingLeft: `${depth * 12 + 8}px` }}
      >
        {entry.is_dir ? (
          <>
            {isExpanded ? (
              <ChevronDown size={12} className="shrink-0 text-gray-400" />
            ) : (
              <ChevronRight size={12} className="shrink-0 text-gray-400" />
            )}
            <Folder size={13} className={`shrink-0 ${isExpanded ? "text-blue-400" : "text-gray-400"}`} />
          </>
        ) : (
          <>
            <span className="w-3" />
            <File size={13} className="shrink-0 text-gray-400" />
          </>
        )}
        <span className="truncate">{entry.name}</span>
      </button>
      {entry.is_dir && isExpanded && entry.children.length > 0 && (
        <div>
          {entry.children.map((child) => (
            <TreeNode
              key={child.path}
              entry={child}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onToggle={onToggle}
              expanded={expanded}
            />
          ))}
        </div>
      )}
    </div>
  );
}

interface OpenTab {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
  mime_type: string;
}

function getLanguageFromPath(path: string): string {
  const fileName = path.split("/").pop()?.toLowerCase() ?? "";
  const ext = fileName.split(".").pop() ?? "";

  // Match by full filename first
  const FILE_MAP: Record<string, string> = {
    dockerfile: "dockerfile",
    makefile: "makefile",
    ".gitignore": "plaintext",
  };
  if (FILE_MAP[fileName]) return FILE_MAP[fileName];

  const EXT_MAP: Record<string, string> = {
    ts: "typescript", tsx: "typescriptreact", js: "javascript", jsx: "javascriptreact",
    py: "python", rs: "rust", go: "go", java: "java", c: "c", h: "c",
    cpp: "cpp", hpp: "cpp", cs: "csharp", rb: "ruby", php: "php",
    sql: "sql", sh: "shell", bash: "shell", yml: "yaml", yaml: "yaml",
    json: "json", xml: "xml", html: "html", css: "css", scss: "scss",
    md: "markdown", txt: "plaintext", toml: "ini", ini: "ini", cfg: "ini",
    env: "plaintext", lock: "json",
  };
  return EXT_MAP[ext] ?? "plaintext";
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function FileBrowserPanel() {
  const t = useLocaleStore((s) => s.t);
  const workspaceDirectory = useWorkflowStore((s) => s.workspaceDirectory);

  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  // Modal state
  const [modalOpen, setModalOpen] = useState(false);
  const [tabs, setTabs] = useState<OpenTab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [loadingFile, setLoadingFile] = useState(false);
  const [fileError, setFileError] = useState("");

  const loadTree = useCallback(async () => {
    if (!workspaceDirectory?.trim()) return;
    setLoading(true);
    try {
      const result = await api.listWorkspaceTree({
        workspace: workspaceDirectory,
        depth: 2,
      });
      setEntries(result.entries as FileEntry[]);
    } catch (err) {
      console.error("Failed to load file tree:", err);
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [workspaceDirectory]);

  useEffect(() => {
    loadTree();
  }, [loadTree]);

  const handleToggle = useCallback(async (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

    if (!expanded.has(path) && workspaceDirectory) {
      try {
        const result = await api.listWorkspaceTree({
          workspace: workspaceDirectory,
          subpath: path,
          depth: 1,
        });
        if (result.entries.length > 0) {
          setEntries((prev) => injectChildren(prev, path, result.entries as FileEntry[]));
        }
      } catch { /* ignore */ }
    }
  }, [expanded, workspaceDirectory]);

  const handleSelectFile = useCallback(async (path: string) => {
    setSelectedPath(path);

    const existing = tabs.find((t) => t.path === path);
    if (existing) {
      setActiveTab(path);
      setModalOpen(true);
      return;
    }

    setLoadingFile(true);
    setFileError("");
    setModalOpen(true);

    if (!workspaceDirectory) return;

    try {
      const result = await api.readWorkspaceFile({ workspace: workspaceDirectory, path });
      const tab: OpenTab = {
        path,
        content: result.content,
        size: result.size,
        truncated: result.truncated,
        mime_type: result.mime_type,
      };
      setTabs((prev) => [...prev, tab]);
      setActiveTab(path);
    } catch (err) {
      setFileError(err instanceof Error ? err.message : t("fileBrowser.readFileError"));
    } finally {
      setLoadingFile(false);
    }
  }, [workspaceDirectory, tabs, t]);

  const closeTab = useCallback((path: string) => {
    setTabs((prev) => {
      const next = prev.filter((tab) => tab.path !== path);
      return next;
    });
    setActiveTab((prevActive) => {
      if (prevActive === path) {
        // Find the last remaining tab
        const remaining = tabs.filter((tab) => tab.path !== path);
        return remaining.length > 0 ? remaining[remaining.length - 1].path : null;
      }
      return prevActive;
    });
  }, [tabs]);

  const closeModal = useCallback(() => {
    setModalOpen(false);
  }, []);

  if (!workspaceDirectory?.trim()) {
    return (
      <div className="w-80 bg-white border-l border-gray-200 flex flex-col h-full overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 shrink-0">
          <FolderOpen size={14} className="text-gray-500" />
          <span className="text-xs font-semibold text-gray-700">{t("fileBrowser.title")}</span>
        </div>
        <div className="flex-1 flex items-center justify-center p-4">
          <p className="text-xs text-gray-400 text-center">{t("fileBrowser.noWorkspace")}</p>
        </div>
      </div>
    );
  }

  const currentTab = tabs.find((t) => t.path === activeTab);
  const fileName = activeTab?.split("/").pop() ?? activeTab ?? "";

  return (
    <>
      <div className="w-80 bg-white border-l border-gray-200 flex flex-col h-full overflow-hidden">
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-gray-100 shrink-0">
          <FolderOpen size={14} className="text-gray-500" />
          <span className="text-xs font-semibold text-gray-700">{t("fileBrowser.title")}</span>
          <div className="flex-1" />
          <button
            onClick={loadTree}
            disabled={loading}
            className="p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
            title={t("fileBrowser.refresh")}
          >
            <RefreshCw size={13} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto overflow-x-hidden min-h-0">
          {loading && entries.length === 0 ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 size={16} className="animate-spin text-gray-400" />
              <span className="ml-2 text-xs text-gray-400">{t("fileBrowser.loading")}</span>
            </div>
          ) : entries.length === 0 ? (
            <div className="flex items-center justify-center py-8">
              <span className="text-xs text-gray-400">{t("fileBrowser.empty")}</span>
            </div>
          ) : (
            <div className="py-1">
              {entries.map((entry) => (
                <TreeNode
                  key={entry.path}
                  entry={entry}
                  depth={0}
                  selectedPath={selectedPath}
                  onSelect={handleSelectFile}
                  onToggle={handleToggle}
                  expanded={expanded}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* VS Code style preview modal */}
      {modalOpen && (
        <FilePreviewModal
          tabs={tabs}
          activeTab={activeTab}
          currentTab={currentTab ?? null}
          fileName={fileName}
          loading={loadingFile}
          error={fileError}
          onSelectTab={setActiveTab}
          onCloseTab={closeTab}
          onClose={closeModal}
          t={t}
        />
      )}
    </>
  );
}

function FilePreviewModal({
  tabs,
  activeTab,
  currentTab,
  fileName,
  loading,
  error,
  onSelectTab,
  onCloseTab,
  onClose,
  t,
}: {
  tabs: OpenTab[];
  activeTab: string | null;
  currentTab: OpenTab | null;
  fileName: string;
  loading: boolean;
  error: string;
  onSelectTab: (path: string) => void;
  onCloseTab: (path: string) => void;
  onClose: () => void;
  t: (key: string) => string;
}) {
  const lineCount = currentTab?.content ? currentTab.content.split("\n").length : 0;
  const lang = currentTab ? getLanguageFromPath(currentTab.path) : "plaintext";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="relative bg-[#1e1e1e] rounded-lg shadow-2xl w-[90vw] max-w-[1200px] h-[85vh] flex flex-col overflow-hidden border border-[#3c3c3c]">
        {/* Title bar */}
        <div className="flex items-center h-9 bg-[#323233] border-b border-[#3c3c3c] shrink-0 px-2 select-none">
          <div className="flex items-center gap-1.5 text-[#cccccc] text-xs">
            <File size={13} className="text-[#cccccc]" />
            <span className="font-medium">{fileName}</span>
            {currentTab && (
              <span className="text-[#888] ml-2">
                {formatSize(currentTab.size)} · {lineCount} {t("fileBrowser.lines")} · {lang}
              </span>
            )}
          </div>
          <div className="flex-1" />
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-[#505050] text-[#cccccc] hover:text-white transition-colors"
          >
            <X size={14} />
          </button>
        </div>

        {/* Tab bar */}
        {tabs.length > 0 && (
          <div className="flex items-center bg-[#252526] border-b border-[#3c3c3c] shrink-0 overflow-x-auto">
            {tabs.map((tab) => {
              const isActive = tab.path === activeTab;
              const name = tab.path.split("/").pop() ?? tab.path;
              return (
                <div
                  key={tab.path}
                  className={`flex items-center gap-1.5 px-3 h-[35px] text-xs cursor-pointer border-r border-[#3c3c3c] shrink-0 ${
                    isActive
                      ? "bg-[#1e1e1e] text-white border-t border-t-[#007acc]"
                      : "bg-[#2d2d2d] text-[#969696] hover:bg-[#2d2d2d]"
                  }`}
                  onClick={() => onSelectTab(tab.path)}
                >
                  <File size={13} className={isActive ? "text-[#519aba]" : "text-[#6a6a6a]"} />
                  <span>{name}</span>
                  <button
                    onClick={(e) => { e.stopPropagation(); onCloseTab(tab.path); }}
                    className="ml-1 p-0.5 rounded hover:bg-[#505050] text-[#888] hover:text-white transition-colors"
                  >
                    <X size={10} />
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {/* Editor area */}
        <div className="flex-1 min-h-0 overflow-hidden">
          {loading ? (
            <div className="flex items-center justify-center h-full">
              <Loader2 size={20} className="animate-spin text-[#007acc]" />
              <span className="ml-2 text-sm text-[#888]">{t("fileBrowser.loading")}</span>
            </div>
          ) : error ? (
            <div className="flex items-center justify-center h-full">
              <span className="text-sm text-red-400">{error}</span>
            </div>
          ) : currentTab ? (
            <MonacoViewer
              value={currentTab.content}
              language={lang}
              truncated={currentTab.truncated}
              t={t}
            />
          ) : null}
        </div>

        {/* Status bar */}
        <div className="flex items-center h-[22px] bg-[#007acc] shrink-0 px-3 text-[11px] text-white/90 select-none">
          <span>{currentTab?.path ?? ""}</span>
          <div className="flex-1" />
          {currentTab && (
            <>
              <span className="mr-4">{lang}</span>
              <span>UTF-8</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function MonacoViewer({
  value,
  language,
  truncated,
  t,
}: {
  value: string;
  language: string;
  truncated: boolean;
  t: (key: string) => string;
}) {
  const [Editor, setEditor] = useState<React.ComponentType<{
    value: string;
    language: string;
    theme: string;
    options: Record<string, unknown>;
    loading?: string;
  }> | null>(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    import("@monaco-editor/react")
      .then((mod) => setEditor(() => mod.default))
      .catch(() => setLoadError(true));
  }, []);

  if (loadError) {
    return (
      <pre className="h-full p-4 text-sm font-mono text-[#d4d4d4] bg-[#1e1e1e] overflow-auto whitespace-pre">
        {value}
      </pre>
    );
  }

  if (!Editor) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 size={16} className="animate-spin text-[#007acc]" />
      </div>
    );
  }

  return (
    <div className="h-full relative">
      {truncated && (
        <div className="absolute top-0 left-0 right-0 z-10 px-3 py-1 text-[11px] text-amber-200 bg-amber-900/80">
          {t("fileBrowser.fileTooLarge")}
        </div>
      )}
      <Editor
        value={value}
        language={language}
        theme="vs-dark"
        loading={t("fileBrowser.loading")}
        options={{
          readOnly: true,
          minimap: { enabled: false },
          scrollBeyondLastLine: false,
          fontSize: 13,
          lineHeight: 20,
          renderLineHighlight: "line",
          wordWrap: "off",
          automaticLayout: true,
          scrollbar: {
            verticalScrollbarSize: 10,
            horizontalScrollbarSize: 10,
          },
          padding: { top: 8 },
          lineNumbers: "on",
          folding: true,
          glyphMargin: false,
          overviewRulerBorder: false,
          hideCursorInOverviewRuler: true,
          renderWhitespace: "none",
          contextmenu: false,
        }}
      />
    </div>
  );
}

function injectChildren(entries: FileEntry[], targetPath: string, children: FileEntry[]): FileEntry[] {
  return entries.map((entry) => {
    if (entry.path === targetPath) {
      return { ...entry, children };
    }
    if (entry.is_dir && entry.children.length > 0) {
      return { ...entry, children: injectChildren(entry.children, targetPath, children) };
    }
    return entry;
  });
}
