"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { FolderSearch, CheckCircle2, AlertTriangle, Loader2, Folder, X } from "lucide-react";
import { api } from "@/lib/api";
import { authHeaders } from "@/lib/auth";
import { useLocaleStore } from "@/stores/localeStore";
import type { PathValidateResult } from "@/types/settings";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DirectoryPickerProps {
  /** Current value */
  value: string;
  /** Callback when value changes */
  onChange: (value: string) => void;
  /** Placeholder text */
  placeholder?: string;
  /** Whether the field is disabled */
  disabled?: boolean;
  /** Debounce delay in ms for path validation (default 600) */
  validateDelay?: number;
  /** Optional label override */
  label?: string;
  /** Extra CSS class for the outer wrapper */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function DirectoryPicker({
  value,
  onChange,
  placeholder,
  disabled = false,
  validateDelay = 600,
  label,
  className = "",
}: DirectoryPickerProps) {
  const t = useLocaleStore((s) => s.t);

  // Validation state
  const [validation, setValidation] = useState<PathValidateResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserPath, setBrowserPath] = useState("");
  const [browserParent, setBrowserParent] = useState("");
  const [browserEntries, setBrowserEntries] = useState<Array<{ name: string; path: string }>>([]);
  const [browserError, setBrowserError] = useState("");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  // Debounced path validation
  const validatePath = useCallback(
    (path: string) => {
      if (timerRef.current) clearTimeout(timerRef.current);

      if (!path.trim()) {
        setValidation(null);
        setValidating(false);
        return;
      }

      setValidating(true);
      timerRef.current = setTimeout(async () => {
        try {
          const result = await api.validatePath(path);
          setValidation(result);
        } catch {
          setValidation(null);
        } finally {
          setValidating(false);
        }
      }, validateDelay);
    },
    [validateDelay]
  );

  // Handle text input change
  const handleInputChange = useCallback(
    (newValue: string) => {
      onChange(newValue);
      validatePath(newValue);
    },
    [onChange, validatePath]
  );

  const apiBase = process.env.NEXT_PUBLIC_API_URL || "/api";

  const loadServerDirectory = useCallback(async (path: string) => {
    setBrowsing(true);
    setBrowserError("");
    try {
      const resp = await fetch(`${apiBase}/settings/list-dir`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ path }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      setBrowserPath(data.path || "");
      setBrowserParent(data.parent || "");
      setBrowserEntries(Array.isArray(data.entries) ? data.entries : []);
      setBrowserError(data.error || "");
      setBrowserOpen(true);
    } catch (err) {
      setBrowserError(err instanceof Error ? err.message : "Unable to list directory");
      setBrowserOpen(true);
    } finally {
      setBrowsing(false);
    }
  }, [apiBase]);

  // Handle browse button click.
  // Native dialogs may work on desktop Linux with DISPLAY. If they do not,
  // fall back to a server-side directory browser that returns absolute paths.
  const handleBrowse = useCallback(async () => {
    await loadServerDirectory(value || "/");
  }, [apiBase, value, onChange, validatePath, loadServerDirectory]);

  const selectBrowserPath = useCallback(() => {
    if (!browserPath) return;
    onChange(browserPath);
    validatePath(browserPath);
    setBrowserOpen(false);
  }, [browserPath, onChange, validatePath]);

  // Validation status icon
  const renderValidationIcon = () => {
    if (!value.trim()) return null;
    if (validating) return <Loader2 size={14} className="animate-spin text-gray-400" />;
    if (!validation) return null;
    if (validation.valid && validation.exists && validation.is_dir) {
      return <CheckCircle2 size={14} className="text-green-500" />;
    }
    if (validation.valid && !validation.exists) {
      return <AlertTriangle size={14} className="text-amber-500" />;
    }
    return <AlertTriangle size={14} className="text-red-500" />;
  };

  // Validation message text
  const renderValidationMessage = () => {
    if (!value.trim() || !validation || validating) return null;
    if (validation.valid && validation.exists && validation.is_dir) {
      return <span className="text-green-600">{t("dirPicker.pathValid")}</span>;
    }
    if (validation.valid && !validation.exists) {
      return <span className="text-amber-600">{validation.message}</span>;
    }
    return <span className="text-red-600">{validation.message}</span>;
  };

  // Input border color based on validation state
  const inputBorderClass = (() => {
    if (!validation || !value.trim()) {
      return "border-gray-300 focus:border-blue-500 focus:ring-2 focus:ring-blue-100";
    }
    if (validation.valid && validation.exists) {
      return "border-green-300 focus:border-green-500 focus:ring-2 focus:ring-green-100";
    }
    if (validation.valid) {
      return "border-amber-300 focus:border-amber-500 focus:ring-2 focus:ring-amber-100";
    }
    return "border-red-300 focus:border-red-500 focus:ring-2 focus:ring-red-100";
  })();

  return (
    <div className={`space-y-1.5 ${className}`}>
      {label && (
        <label className="block text-xs font-semibold text-gray-500 uppercase tracking-wider">
          {label}
        </label>
      )}

      {/* Input row with browse button */}
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            type="text"
            value={value}
            onChange={(e) => handleInputChange(e.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            className={`w-full rounded-lg border bg-white px-3 py-2 pr-8 text-sm transition-all placeholder-gray-300 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none ${inputBorderClass}`}
          />
          <div className="absolute right-2 top-1/2 -translate-y-1/2">
            {renderValidationIcon()}
          </div>
        </div>

        {/* Browse button */}
        <button
          type="button"
          onClick={handleBrowse}
          disabled={disabled || browsing}
          title={t("dirPicker.browse")}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-gray-300 bg-white text-gray-600 hover:bg-gray-50 hover:border-gray-400 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {browsing ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <FolderSearch size={14} />
          )}
          <span className="hidden sm:inline">{t("dirPicker.browse")}</span>
        </button>
      </div>

      {/* Validation message */}
      {renderValidationMessage() && (
        <p className="text-xs leading-relaxed">
          {renderValidationMessage()}
        </p>
      )}

      {browserOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
          <div className="w-full max-w-lg rounded-lg bg-white shadow-xl border border-gray-200 overflow-hidden">
            <div className="flex items-center gap-2 px-4 py-3 border-b border-gray-100">
              <FolderSearch size={16} className="text-blue-500" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold text-gray-800">{t("dirPicker.title")}</div>
                <div className="text-xs text-gray-500 truncate font-mono">{browserPath || "~"}</div>
              </div>
              <button
                type="button"
                onClick={() => setBrowserOpen(false)}
                className="p-1 rounded hover:bg-gray-100 text-gray-500"
                aria-label={t("dirPicker.close")}
              >
                <X size={16} />
              </button>
            </div>

            <div className="max-h-80 overflow-y-auto p-2">
              {browserError && (
                <div className="mb-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
                  {browserError}
                </div>
              )}
              {browserParent && browserParent !== browserPath && (
                <button
                  type="button"
                  onClick={() => loadServerDirectory(browserParent)}
                  className="w-full flex items-center gap-2 rounded px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-50"
                >
                  <Folder size={14} className="text-gray-400" />
                  ..
                </button>
              )}
              {browserEntries.map((entry) => (
                <button
                  key={entry.path}
                  type="button"
                  onClick={() => loadServerDirectory(entry.path)}
                  className="w-full flex items-center gap-2 rounded px-3 py-2 text-left text-sm text-gray-700 hover:bg-blue-50 hover:text-blue-700"
                >
                  <Folder size={14} className="text-blue-400" />
                  <span className="truncate">{entry.name}</span>
                </button>
              ))}
              {browserEntries.length === 0 && !browsing && (
                <div className="px-3 py-6 text-center text-xs text-gray-400">
                  {t("dirPicker.noSubdirs")}
                </div>
              )}
            </div>

            <div className="flex items-center gap-2 px-4 py-3 border-t border-gray-100 bg-gray-50">
              <button
                type="button"
                onClick={() => loadServerDirectory(browserPath)}
                disabled={browsing || !browserPath}
                className="px-3 py-1.5 rounded border border-gray-200 bg-white text-xs text-gray-600 hover:bg-gray-100 disabled:opacity-50"
              >
                {browsing ? t("dirPicker.loading") : t("dirPicker.refresh")}
              </button>
              <div className="flex-1" />
              <button
                type="button"
                onClick={() => setBrowserOpen(false)}
                className="px-3 py-1.5 rounded border border-gray-200 bg-white text-xs text-gray-600 hover:bg-gray-100"
              >
                {t("dirPicker.cancel")}
              </button>
              <button
                type="button"
                onClick={selectBrowserPath}
                disabled={!browserPath}
                className="px-3 py-1.5 rounded bg-blue-600 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
              >
                {t("dirPicker.selectDir")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
