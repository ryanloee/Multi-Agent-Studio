"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { FolderSearch, CheckCircle2, AlertTriangle, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
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

  // Handle browse button click
  // Strategy: try backend native dialog first (works on Windows with tkinter),
  // fall back to HTML <input webkitdirectory> if backend is unavailable.
  const handleBrowse = useCallback(async () => {
    setBrowsing(true);

    // ---- Attempt 1: Backend native dialog (gives full path) ----
    try {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || "/api";
      const resp = await fetch(`${apiBase}/settings/browse-dir`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_path: value || "" }),
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data.path) {
          onChange(data.path);
          validatePath(data.path);
          setBrowsing(false);
          return;
        }
      }
    } catch {
      // Backend browse not available — fall through
    }

    // ---- Attempt 2: File System Access API (Chrome/Edge only, name only) ----
    if ("showDirectoryPicker" in window) {
      try {
        const dirHandle = await (window as any).showDirectoryPicker({ mode: "read" });
        if (dirHandle?.name) {
          // On the web we can only get the folder name, not the full path.
          // We set it and let the user adjust the prefix if needed.
          onChange(dirHandle.name);
          validatePath(dirHandle.name);
          setBrowsing(false);
          return;
        }
      } catch (err) {
        if ((err as DOMException)?.name === "AbortError") {
          setBrowsing(false);
          return;
        }
      }
    }

    // ---- Attempt 3: <input webkitdirectory> fallback ----
    const input = document.createElement("input");
    input.type = "file";
    // @ts-expect-error — webkitdirectory is non-standard but widely supported
    input.webkitdirectory = true;
    // @ts-expect-error — directory is the standard equivalent
    input.directory = true;
    input.style.display = "none";

    input.onchange = () => {
      const files = input.files;
      if (files && files.length > 0) {
        // In Electron, File.path gives the absolute path
        const filePath = (files[0] as any).path;
        if (filePath) {
          // Electron: derive directory from file path
          const dirPath = filePath.substring(0, filePath.length - files[0].name.length - 1);
          onChange(dirPath);
          validatePath(dirPath);
        } else {
          // Web browser: only relative path available
          const relativePath = files[0].webkitRelativePath;
          const rootFolder = relativePath.split("/")[0];
          onChange(rootFolder);
          validatePath(rootFolder);
        }
      }
      document.body.removeChild(input);
      setBrowsing(false);
    };

    input.oncancel = () => {
      document.body.removeChild(input);
      setBrowsing(false);
    };

    document.body.appendChild(input);
    input.click();
  }, [value, onChange, validatePath]);

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
    </div>
  );
}
