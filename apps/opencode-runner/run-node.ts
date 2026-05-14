#!/usr/bin/env bun

import path from "node:path"
import { appendFile } from "node:fs/promises"
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

async function appendEvent(streamFile: string, event: Record<string, unknown>) {
  await appendFile(streamFile, JSON.stringify({ timestamp: now(), ...event }) + "\n", "utf8")
}

function npmForProvider(provider: string, baseURL: string) {
  if (provider === "anthropic") return "@ai-sdk/anthropic"
  if (provider === "openai" && !baseURL) return "@ai-sdk/openai"
  return "@ai-sdk/openai-compatible"
}

function resolveProviderConfig(provider: string, baseURL: string) {
  if (provider === "anthropic" && baseURL.includes("open.bigmodel.cn/api/anthropic")) {
    return {
      provider: "bigmodel",
      baseURL: "https://open.bigmodel.cn/api/paas/v4",
      npm: "@ai-sdk/openai-compatible",
      note: "converted_bigmodel_anthropic_to_openai_compatible",
    }
  }
  return {
    provider,
    baseURL,
    npm: npmForProvider(provider, baseURL),
    note: "",
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

function mapOpencodeEvent(raw: any): Record<string, unknown> | undefined {
  const part = raw?.part
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

async function appendResponseParts(streamFile: string, response: any) {
  const parts = Array.isArray(response?.parts) ? response.parts : []
  for (const part of parts) {
    if (part?.type === "reasoning" && typeof part.text === "string" && part.text.trim()) {
      await appendEvent(streamFile, {
        type: "llm_chunk",
        content: part.text,
        metadata: { source: "opencode", kind: "reasoning", part_id: part.id },
      })
      continue
    }
    if (part?.type === "text" && typeof part.text === "string" && part.text.trim()) {
      await appendEvent(streamFile, {
        type: "llm_chunk",
        content: part.text,
        metadata: { source: "opencode", part_id: part.id },
      })
      continue
    }
    if (part?.type === "tool") {
      await appendEvent(streamFile, {
        type: part.state?.status === "error" ? "tool_result" : "tool_call",
        tool_name: String(part.tool || "tool"),
        content: clip(JSON.stringify(part, null, 2), 8000),
        metadata: { source: "opencode", part_id: part.id, status: part.state?.status },
      })
      continue
    }
    if (part?.type === "step-finish") {
      await appendEvent(streamFile, {
        type: "status",
        content: "step_finish",
        metadata: { source: "opencode", part },
      })
    }
  }
}

function responseHasOutput(response: any) {
  const parts = Array.isArray(response?.parts) ? response.parts : []
  const hasContent = parts.some((part) => {
    if (part?.type === "text" || part?.type === "reasoning") return Boolean(String(part.text || "").trim())
    if (part?.type === "tool") return true
    return false
  })
  const tokens = response?.info?.tokens ?? {}
  const total =
    Number(tokens.total || 0) +
    Number(tokens.input || 0) +
    Number(tokens.output || 0) +
    Number(tokens.reasoning || 0) +
    Number(tokens.cache?.write || 0) +
    Number(tokens.cache?.read || 0)
  return hasContent || total > 0
}

async function streamOpencodeEvents(input: {
  client: any
  app: any
  workspace: string
  streamFile: string
  sessionID: string
}) {
  const eventURL = `http://opencode.internal/event?directory=${encodeURIComponent(input.workspace)}`
  const response = await input.app.fetch(new Request(eventURL, { headers: { accept: "text/event-stream" } }))
  if (!response.ok || !response.body) {
    throw new Error(`opencode event stream failed: ${response.status} ${response.statusText}`)
  }
  const partTypes = new Map<string, string>()
  const partLengths = new Map<string, number>()
  let hasOutput = false
  let errorText = ""

  const decoder = new TextDecoder()
  let buffer = ""
  async function handleEventPayload(payload: string) {
    if (!payload.trim()) return false
    let event: any
    try {
      event = JSON.parse(payload)
    } catch {
      return false
    }
    const type = event?.type
    const properties = event?.properties ?? {}

    if (type === "message.part.delta" && properties.sessionID === input.sessionID) {
      const delta = String(properties.delta || "")
      if (!delta) return false
      const partID = String(properties.partID || "")
      const partType = partTypes.get(partID) || ""
      await appendEvent(input.streamFile, {
        type: "llm_chunk",
        content: delta,
        metadata: {
          source: "opencode",
          kind: partType === "reasoning" ? "reasoning" : undefined,
          part_id: partID,
          field: properties.field,
          streaming: true,
        },
      })
      hasOutput = true
      partLengths.set(partID, (partLengths.get(partID) || 0) + delta.length)
      return false
    }

    if (type === "message.part.updated") {
      const part = properties.part
      if (!part || part.sessionID !== input.sessionID) return false
      if (part.id && part.type) partTypes.set(String(part.id), String(part.type))

      if ((part.type === "text" || part.type === "reasoning") && typeof part.text === "string") {
        const already = partLengths.get(String(part.id)) || 0
        const next = part.text.slice(already)
        if (next.trim()) {
          await appendEvent(input.streamFile, {
            type: "llm_chunk",
            content: next,
            metadata: {
              source: "opencode",
              kind: part.type === "reasoning" ? "reasoning" : undefined,
              part_id: part.id,
              streaming: true,
            },
          })
          hasOutput = true
          partLengths.set(String(part.id), part.text.length)
        }
        return false
      }

      if (part.type === "tool") {
        await appendEvent(input.streamFile, {
          type: part.state?.status === "error" ? "tool_result" : "tool_call",
          tool_name: String(part.tool || "tool"),
          content: clip(JSON.stringify(part, null, 2), 8000),
          metadata: { source: "opencode", part_id: part.id, status: part.state?.status, streaming: true },
        })
        hasOutput = true
        return false
      }

      if (part.type === "step-start" || part.type === "step-finish") {
        await appendEvent(input.streamFile, {
          type: "status",
          content: part.type === "step-start" ? "step_start" : "step_finish",
          metadata: { source: "opencode", part },
        })
      }
      return false
    }

    if (type === "permission.asked" && properties.sessionID === input.sessionID) {
      await appendEvent(input.streamFile, {
        type: "status",
        content: `permission requested: ${properties.permission || "unknown"}`,
        metadata: { source: "opencode", permission_id: properties.id, patterns: properties.patterns || [] },
      })
      await input.client.postSessionIdPermissionsPermissionId({
        path: { id: input.sessionID, permissionID: properties.id },
        body: { response: "once" },
      })
      return false
    }

    if (type === "session.error" && properties.sessionID === input.sessionID) {
      const err = properties.error
      errorText = err?.data?.message ? String(err.data.message) : clip(JSON.stringify(err ?? properties, null, 2), 8000)
      await appendEvent(input.streamFile, {
        type: "error",
        content: errorText,
        metadata: { source: "opencode" },
      })
      return false
    }

    if (type === "session.status" && properties.sessionID === input.sessionID && properties.status?.type === "idle") {
      return true
    }
    if (type === "session.idle" && properties.sessionID === input.sessionID) {
      return true
    }
    return false
  }

  for await (const chunk of response.body) {
    buffer += decoder.decode(chunk, { stream: true })
    while (true) {
      const split = buffer.indexOf("\n\n")
      if (split < 0) break
      const rawEvent = buffer.slice(0, split)
      buffer = buffer.slice(split + 2)
      const dataLines = rawEvent
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
      if (!dataLines.length) continue
      if (await handleEventPayload(dataLines.join("\n"))) {
        return { hasOutput, errorText }
      }
    }
  }

  return { hasOutput, errorText }
}

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
  await appendEvent(streamFile, { type: "node_started", run_id: runID, node_id: nodeID, content: "opencode runner started" })

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
    process.exit(124)
  }

  if (exitCode !== 0 && !sawFinalStepFinish) {
    await appendEvent(streamFile, {
      type: "node_failed",
      content: `opencode source runner failed with exit_code=${exitCode}`,
      metadata: { source: "opencode-runner", exit_code: exitCode },
    })
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
    process.exit(1)
  }
  await appendEvent(streamFile, {
    type: "node_completed",
    content: "opencode source runner completed",
    metadata: { source: "opencode" },
  })
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
