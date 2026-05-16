#!/usr/bin/env bun

import path from "node:path"
import { appendFile, writeFile } from "node:fs/promises"
import { pathToFileURL } from "node:url"

type Args = Record<string, string | boolean>

function parseArgs(argv: string[]): Args {
  const args: Args = {}
  for (let i = 0; i < argv.length; i++) {
    const item = argv[i]
    if (!item.startsWith("--")) continue
    const key = item.slice(2)
    const next = argv[i + 1]
    if (!next || next.startsWith("--")) {
      args[key] = true
      continue
    }
    args[key] = next
    i++
  }
  return args
}

function value(args: Args, key: string, fallback = ""): string {
  const item = args[key]
  if (typeof item === "string") return item
  return fallback
}

function now() {
  return Date.now()
}

function clip(text: string, max = 12000) {
  if (text.length <= max) return text
  return text.slice(0, max) + "\n...[truncated]"
}

function terminateChild(child: any) {
  try {
    child.kill()
  } catch {
    // Already exited.
  }
  const pid = Number(child?.pid || 0)
  if (pid > 0) {
    try {
      process.kill(pid, "SIGTERM")
    } catch {
      // Already exited.
    }
    setTimeout(() => {
      try {
        process.kill(pid, "SIGKILL")
      } catch {
        // Already exited.
      }
    }, 2000)
  }
}

// ---------------------------------------------------------------------------
// SSE broadcaster — multiple subscribers, push events in real-time
// ---------------------------------------------------------------------------

type SseSubscriber = {
  controller: ReadableStreamDefaultController
  closed: boolean
}

const sseSubscribers = new Set<SseSubscriber>()

function broadcastSse(event: Record<string, unknown>) {
  const payload = `data: ${JSON.stringify(event)}\n\n`
  for (const sub of sseSubscribers) {
    if (sub.closed) continue
    try {
      sub.controller.enqueue(new TextEncoder().encode(payload))
    } catch {
      sub.closed = true
      sseSubscribers.delete(sub)
    }
  }
}

function startSseServer(): { port: number; stop: () => void } {
  const server = Bun.serve({
    port: 0,
    fetch(req) {
      const url = new URL(req.url)
      if (url.pathname === "/events") {
        const stream = new ReadableStream({
          start(controller) {
            const sub: SseSubscriber = { controller, closed: false }
            sseSubscribers.add(sub)
            // Send initial connected event
            try {
              controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({ type: "connected" })}\n\n`))
            } catch { /* */ }
          },
          cancel() {
            // sub is captured in start closure; find and remove
            for (const sub of sseSubscribers) {
              if (sub.closed) {
                sseSubscribers.delete(sub)
              }
            }
          },
        })
        return new Response(stream, {
          headers: {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
          },
        })
      }
      return new Response("not found", { status: 404 })
    },
  })
  return { port: server.port, stop: () => server.stop() }
}

// ---------------------------------------------------------------------------
// Dual event writer: stream.jsonl file + SSE broadcast
// ---------------------------------------------------------------------------

async function appendEvent(streamFile: string, event: Record<string, unknown>) {
  const full = { timestamp: now(), ...event }
  // 1. Write to stream.jsonl (backup/replay)
  await appendFile(streamFile, JSON.stringify(full) + "\n", "utf8")
  // 2. Broadcast to SSE subscribers (real-time)
  broadcastSse(full)
}

// ---------------------------------------------------------------------------
// Provider / config helpers
// ---------------------------------------------------------------------------

function npmForProvider(provider: string, baseURL: string) {
  if (provider === "anthropic") return "@ai-sdk/anthropic"
  if (provider === "openai" && !baseURL) return "@ai-sdk/openai"
  return "@ai-sdk/openai-compatible"
}

function resolveProviderConfig(provider: string, baseURL: string) {
  // The @ai-sdk/anthropic SDK appends /messages to baseURL.
  // For GLM's Anthropic endpoint to work correctly, baseURL must end with /v1.
  let resolvedURL = baseURL
  if (provider === "anthropic" && resolvedURL && !resolvedURL.endsWith("/v1")) {
    resolvedURL = resolvedURL.replace(/\/+$/, "") + "/v1"
  }
  return {
    provider,
    baseURL: resolvedURL,
    npm: npmForProvider(provider, baseURL),
    note: provider === "anthropic" && resolvedURL !== baseURL ? "appended_v1_for_anthropic_sdk" : "",
  }
}

function agentPrompt(agentType: string) {
  const shared = [
    "You are running as a MAS workflow node powered by opencode source.",
    "Work autonomously until the node task is complete.",
    "Use tools to inspect and edit files instead of only explaining.",
    "Write durable artifacts into the workspace when appropriate.",
    "If a requirement is impossible or unsafe, state the blocker clearly and stop.",
    "Never create a new top-level workflow plan; this node only completes its assigned task.",
  ].join("\n")

  const byType: Record<string, string> = {
    explore: "Focus on repository inspection, requirements discovery, and concise findings. Avoid code edits unless necessary.",
    design: "Produce concrete local design decisions for downstream implementation. Do not spawn another planner.",
    coder: "Implement the requested code changes directly and keep the workspace buildable.",
    merge: "Merge upstream work products, resolve conflicts, and keep final files coherent.",
    review: "Review code for defects, regressions, missing tests, and safety issues. Apply small fixes when clearly correct.",
    shell: "Run validation commands, collect results, and fix straightforward failures if possible.",
  }

  return `${shared}\n\nNode role: ${agentType}\n${byType[agentType] ?? byType.coder}`
}

function buildConfig(input: {
  provider: string
  configuredProvider?: string
  model: string
  baseURL: string
  apiKey: string
  contextWindow: number
  maxOutputTokens: number
  agentType: string
}) {
  const provider = input.provider || "openai"
  const model = input.model || "gpt-4.1"
  const agentName = `mas-${input.agentType || "coder"}`
  return {
    $schema: "https://opencode.ai/config.json",
    model: `${provider}/${model}`,
    enabled_providers: [provider],
    provider: {
      [provider]: {
        name: `MAS ${provider}`,
        npm: npmForProvider(provider, input.baseURL),
        env: [],
        models: {
          [model]: {
            name: model,
            tool_call: true,
            limit: {
              context: input.contextWindow,
              output: input.maxOutputTokens,
            },
          },
        },
        options: {
          ...(input.apiKey ? { apiKey: input.apiKey } : {}),
          ...(input.baseURL ? { baseURL: input.baseURL } : {}),
        },
      },
    },
    agent: {
      [agentName]: {
        mode: "primary",
        model: `${provider}/${model}`,
        prompt: agentPrompt(input.agentType),
        steps: 80,
        permission: {
          bash: "allow",
          edit: "allow",
          webfetch: "allow",
        },
      },
    },
  }
}

// ---------------------------------------------------------------------------
// Map opencode stdout JSON events → MAS event format
// Now includes session.status (busy/idle/retry) for accurate state detection
// ---------------------------------------------------------------------------

function mapOpencodeEvent(raw: any): Record<string, unknown> | undefined {
  const part = raw?.part

  // session.status — the key event for knowing model state
  if (raw?.type === "session.status" || raw?.type === "session_status") {
    const status = raw?.status ?? raw?.properties?.status
    if (status) {
      return {
        type: "agent_status",
        content: `session status: ${status.type}`,
        status_type: status.type, // "busy" | "idle" | "retry"
        metadata: { source: "opencode", status, sessionID: raw?.sessionID },
      }
    }
  }

  if (raw?.type === "text" && typeof part?.text === "string") {
    return {
      type: "llm_chunk",
      content: part.text,
      metadata: { source: "opencode", part_id: part.id },
    }
  }
  if (raw?.type === "reasoning" && typeof part?.text === "string") {
    return {
      type: "llm_chunk",
      content: part.text,
      metadata: { source: "opencode", kind: "reasoning", part_id: part.id },
    }
  }
  if (raw?.type === "tool_use") {
    const tool = String(part?.tool || "tool")
    const state = part?.state ?? {}
    return {
      type: state?.status === "error" ? "tool_result" : "tool_call",
      tool_name: tool,
      content: clip(JSON.stringify({ tool, state }, null, 2), 8000),
      metadata: { source: "opencode", part_id: part?.id, status: state?.status },
    }
  }
  if (raw?.type === "step_start" || raw?.type === "step_finish") {
    return {
      type: "status",
      content: raw.type,
      metadata: { source: "opencode", part },
    }
  }
  if (raw?.type === "error") {
    return {
      type: "error",
      content: clip(JSON.stringify(raw.error ?? raw, null, 2), 8000),
      metadata: { source: "opencode" },
    }
  }
  return undefined
}

// ---------------------------------------------------------------------------
// Utility: consume line stream
// ---------------------------------------------------------------------------

async function consumeLines(
  stream: ReadableStream<Uint8Array> | null,
  onLine: (line: string) => Promise<void>,
) {
  if (!stream) return
  const decoder = new TextDecoder()
  let buffer = ""
  for await (const chunk of stream) {
    buffer += decoder.decode(chunk, { stream: true })
    while (true) {
      const idx = buffer.indexOf("\n")
      if (idx < 0) break
      const line = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 1)
      await onLine(line)
    }
  }
  buffer += decoder.decode()
  if (buffer.trim()) await onLine(buffer)
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
  const args = parseArgs(Bun.argv.slice(2))
  const runID = value(args, "run-id")
  const nodeID = value(args, "node-id")
  const workspace = value(args, "workspace", process.cwd())
  const promptFile = value(args, "prompt-file")
  const streamDir = value(args, "stream-dir", path.join(workspace, ".agent"))
  const streamFile = path.join(streamDir, "stream.jsonl")
  const provider = value(args, "provider", "openai")
  const model = value(args, "model", "gpt-4.1")
  const agentType = value(args, "agent-type", "coder")
  const providerURL = value(args, "provider-url")
  const providerKey = value(args, "provider-key") || process.env.MAS_OPENCODE_PROVIDER_KEY || ""
  const contextWindow = Number(value(args, "context-window", "128000")) || 128000
  const maxOutputTokens = Number(value(args, "max-tokens", "4096")) || 4096
  const timeoutMs = Number(value(args, "timeout-ms", process.env.MAS_OPENCODE_TIMEOUT_MS || "900000")) || 900000
  const effective = resolveProviderConfig(provider, providerURL)
  const runtimeModel =
    effective.provider === "bigmodel" && model === "glm-5.1"
      ? "glm-4.7"
      : model

  await Bun.$`mkdir -p ${streamDir}`.quiet()
  await Bun.write(streamFile, "")

  // Start SSE server for real-time event streaming to Python backend
  const sse = startSseServer()
  const portFile = path.join(streamDir, "runner.port")
  await Bun.write(portFile, String(sse.port))

  await appendEvent(streamFile, { type: "node_started", run_id: runID, node_id: nodeID, content: "opencode runner started", sse_port: sse.port })

  const vendoredRoot = path.resolve(import.meta.dir, "vendor/opencode")
  const opencodeEntry =
    process.env.OPENCODE_SOURCE_ENTRY ||
    path.join(vendoredRoot, "packages/opencode/src/index.ts")
  const serverEntry =
    process.env.OPENCODE_SERVER_ENTRY ||
    path.join(vendoredRoot, "packages/opencode/src/server/server.ts")
  const opencodeCwd = process.env.OPENCODE_PACKAGE_DIR || path.dirname(path.dirname(opencodeEntry))
  const prompt = await Bun.file(promptFile).text()
  const config = buildConfig({
    provider: effective.provider,
    configuredProvider: provider,
    model: runtimeModel,
    baseURL: effective.baseURL,
    apiKey: providerKey,
    contextWindow,
    maxOutputTokens,
    agentType,
  })
  const agentName = `mas-${agentType || "coder"}`

  await appendEvent(streamFile, {
    type: "status",
    content: `running opencode source agent ${agentName}`,
    metadata: {
      provider: effective.provider,
      configured_provider: provider,
      model: runtimeModel,
      requested_model: model,
      agent_type: agentType,
      workspace,
      opencodeEntry,
      serverEntry,
      opencodeCwd,
      prompt_length: prompt.length,
      provider_mapping: effective.note,
    },
  })

  const runnerEnv = {
    ...process.env,
    OPENCODE_CONFIG_CONTENT: JSON.stringify(config),
    OPENCODE_DISABLE_MODELS_FETCH: process.env.OPENCODE_DISABLE_MODELS_FETCH || "1",
    PWD: workspace,
  }
  let timedOut = false
  let sawFinalStepFinish = false
  const child = Bun.spawn({
    cmd: [
      "bun",
      opencodeEntry,
      "run",
      "--format",
      "json",
      "--agent",
      agentName,
      "--model",
      `${effective.provider}/${runtimeModel}`,
      prompt,
    ],
    cwd: workspace,
    env: runnerEnv,
    stdin: "ignore",
    stdout: "pipe",
    stderr: "pipe",
  })
  const timeout = setTimeout(() => {
    timedOut = true
    appendEvent(streamFile, {
      type: "error",
      content: `opencode source runner timed out after ${timeoutMs}ms`,
      metadata: { source: "opencode-runner", timeout_ms: timeoutMs },
    }).catch(() => undefined)
    terminateChild(child)
  }, timeoutMs)

  let hasOutput = false
  const stdoutTask = consumeLines(child.stdout, async (line) => {
    if (!line.trim()) return
    try {
      const raw = JSON.parse(line)
      const mapped = mapOpencodeEvent(raw)
      if (mapped) {
        if (mapped.type === "llm_chunk" || mapped.type === "tool_call" || mapped.type === "tool_result") {
          hasOutput = true
        }
        await appendEvent(streamFile, mapped)
      }
      // Also forward session.status events that mapOpencodeEvent may not catch
      // opencode --format json outputs: { type: "session.status", sessionID, status: {type} }
      if (raw?.type === "session.status" && raw?.status) {
        await appendEvent(streamFile, {
          type: "agent_status",
          content: `session status: ${raw.status.type}`,
          status_type: raw.status.type,
          metadata: { source: "opencode", status: raw.status, sessionID: raw.sessionID },
        })
      }
      if (raw?.type === "step_finish") {
        const reason = String(raw?.part?.reason || "")
        if (reason && reason !== "tool-calls") {
          sawFinalStepFinish = true
          setTimeout(() => terminateChild(child), 50)
        }
      }
    } catch {
      hasOutput = true
      await appendEvent(streamFile, { type: "shell_stdout", content: line, metadata: { source: "opencode" } })
    }
  })
  const stderrTask = consumeLines(child.stderr, async (line) => {
    if (!line.trim()) return
    await appendEvent(streamFile, { type: "shell_stderr", content: line, metadata: { source: "opencode" } })
  })
  const exitCode = await child.exited
  clearTimeout(timeout)
  await Promise.allSettled([stdoutTask, stderrTask])

  if (timedOut) {
    await appendEvent(streamFile, {
      type: "node_failed",
      content: `opencode source runner timed out after ${timeoutMs}ms`,
      metadata: { source: "opencode-runner", exit_code: exitCode, timeout_ms: timeoutMs },
    })
    sse.stop()
    process.exit(124)
  }

  if (exitCode !== 0 && !sawFinalStepFinish) {
    await appendEvent(streamFile, {
      type: "node_failed",
      content: `opencode source runner failed with exit_code=${exitCode}`,
      metadata: { source: "opencode-runner", exit_code: exitCode },
    })
    sse.stop()
    process.exit(exitCode || 1)
  }

  if (!hasOutput) {
    await appendEvent(streamFile, {
      type: "error",
      content: "opencode exited successfully but produced no text/tool output and reported zero model tokens; treating this as a failed node execution.",
      metadata: { source: "opencode-runner", prompt_length: prompt.length, provider: effective.provider, configured_provider: provider, model: runtimeModel, requested_model: model, agent_type: agentType },
    })
    await appendEvent(streamFile, {
      type: "node_failed",
      content: "opencode source runner produced no output",
      metadata: { source: "opencode-runner" },
    })
    sse.stop()
    process.exit(1)
  }
  await appendEvent(streamFile, {
    type: "node_completed",
    content: "opencode source runner completed",
    metadata: { source: "opencode" },
  })
  // Give SSE subscribers a moment to receive the final events
  await new Promise((r) => setTimeout(r, 200))
  sse.stop()
  process.exit(0)
}

main().catch(async (error) => {
  const args = parseArgs(Bun.argv.slice(2))
  const workspace = value(args, "workspace", process.cwd())
  const streamDir = value(args, "stream-dir", path.join(workspace, ".agent"))
  const streamFile = path.join(streamDir, "stream.jsonl")
  await Bun.$`mkdir -p ${streamDir}`.quiet().catch(() => undefined)
  await appendEvent(streamFile, {
    type: "error",
    content: error instanceof Error ? `${error.message}\n${error.stack ?? ""}` : String(error),
    metadata: { source: "opencode-runner" },
  }).catch(() => undefined)
  console.error(error)
  process.exit(1)
})
