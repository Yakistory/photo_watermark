/**
 * dsh-camera-watermark — Host half.
 *
 * Wraps the vendored "相机水印" engine (Flask + Pillow + piexif) as DSH tools:
 * presets, single-image render, folder batch, EXIF read/write, and the
 * interactive studio process. Every render goes through the engine's own
 * `batch_core.render_one`, the same function the studio's "下载图片" uses, so
 * agent output matches the UI byte for byte.
 */

import { spawn } from 'node:child_process'
import {
  closeSync,
  mkdtempSync,
  openSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ENGINE = path.join(HERE, 'engine')
const BRIDGE = path.join(HERE, 'bridge.py')
const APP = path.join(ENGINE, 'app.py')

const NAME = 'camera-watermark'

/** The tool service is the only required Host service. */
export const inject = ['tools']

const DEFAULT_CONFIG = Object.freeze({
  pythonPath: '',
  studioPort: 5000,
  timeoutSeconds: 300,
})

function clampNumber(value, fallback, min, max) {
  const number = Number(value)
  if (!Number.isFinite(number)) return fallback
  return Math.min(max, Math.max(min, number))
}

/**
 * Normalize the profile patch config without a Schemastery dependency: this
 * bundle is installed from a workspace directory, where only Node built-ins and
 * DSH-resolved modules are guaranteed to resolve.
 */
function normalizeConfig(raw) {
  const config = raw && typeof raw === 'object' ? raw : {}
  return {
    pythonPath: typeof config.pythonPath === 'string' ? config.pythonPath.trim() : DEFAULT_CONFIG.pythonPath,
    studioPort: Math.trunc(clampNumber(config.studioPort, DEFAULT_CONFIG.studioPort, 1, 65535)),
    timeoutSeconds: clampNumber(config.timeoutSeconds, DEFAULT_CONFIG.timeoutSeconds, 10, 3600),
  }
}

/**
 * Compile the small author schema used by this file into raw JSON Schema.
 * Mirrors the subset of `defineTool` this plugin needs, without importing a DSH
 * package that a linked workspace bundle cannot resolve.
 */
function toJsonSchema(node) {
  const source = node && typeof node === 'object' ? node : {}
  const schema = {}
  for (const key of ['type', 'description', 'title', 'enum', 'const', 'default', 'examples']) {
    if (source[key] !== undefined) schema[key] = source[key]
  }
  if (source.type === 'array') {
    schema.items = toJsonSchema(source.items ?? { type: 'string' })
  }
  if (source.type === 'object') {
    const properties = source.properties && typeof source.properties === 'object' ? source.properties : {}
    schema.properties = {}
    for (const [key, value] of Object.entries(properties)) schema.properties[key] = toJsonSchema(value)
    schema.additionalProperties = source.additionalProperties === true
  }
  return schema
}

function defineTool(options) {
  const properties = {}
  const required = []
  for (const [key, value] of Object.entries(options.parameters ?? {})) {
    if (value?.required === true) required.push(key)
    properties[key] = toJsonSchema(value)
  }
  const parameters = {
    type: 'object',
    properties,
    additionalProperties: false,
  }
  if (required.length) parameters.required = required
  const outputSchema = options.output?.schema?.type === 'json'
    ? { type: 'object', additionalProperties: true }
    : (options.output?.schema ?? { type: 'object', additionalProperties: true })
  return {
    name: options.name,
    description: options.description,
    parameters,
    output: {
      schema: outputSchema,
      render: options.output.render,
      ...(options.output.presentationMeta ? { presentationMeta: options.output.presentationMeta } : {}),
    },
    execute: options.execute,
    ...(options.timeoutMs ? { timeoutMs: options.timeoutMs } : {}),
    ...(options.isConcurrencySafe ? { isConcurrencySafe: options.isConcurrencySafe } : {}),
  }
}

const TYPE_ENUM = ['text', 'image', 'exif', 'frame', 'blur']
const FORMAT_ENUM = ['jpeg', 'png', 'original']

function text(value) {
  return [{ type: 'text', text: value }]
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
    'cache-control': 'no-store',
  })
  res.end(data)
}

function json(value) {
  return JSON.stringify(value, null, 2)
}

function formatBytes(bytes) {
  const size = Number(bytes) || 0
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(2)} MB`
}

function tailLines(chunks, limit = 12) {
  const lines = chunks.join('').split(/\r?\n/).filter(Boolean)
  return lines.slice(-limit).join('\n')
}

async function probePython(command, prefix) {
  return new Promise((resolve) => {
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    let child
    try {
      // No pipes: a confined Host (or a test harness) may forbid named-pipe
      // stdio. The exit code is all this probe needs.
      child = spawn(
        command,
        [...prefix, '-c', 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)'],
        { windowsHide: true, stdio: 'ignore' },
      )
    } catch {
      done(false)
      return
    }
    child.on('error', () => done(false))
    child.on('close', (code) => done(code === 0))
    // A hung interpreter must not block plugin activation.
    setTimeout(() => {
      try { child.kill() } catch { /* already gone */ }
      done(false)
    }, 5000).unref?.()
  })
}

/**
 * Resolve the first usable Python 3 launcher once per plugin instance.
 * @returns {Promise<{ command: string, prefix: string[] }>}
 */
function makePythonResolver(config) {
  let cached
  return function resolvePython() {
    if (cached) return cached
    cached = (async () => {
      const candidates = [
        { command: config.pythonPath, prefix: [], explicit: true },
        { command: process.env.CAMERA_WATERMARK_PYTHON, prefix: [], explicit: true },
        { command: 'python', prefix: [] },
        { command: 'py', prefix: ['-3'] },
      ].filter((entry) => typeof entry.command === 'string' && entry.command.trim() !== '')
      for (const entry of candidates) {
        // eslint-disable-next-line no-await-in-loop
        if (await probePython(entry.command, entry.prefix)) {
          return { command: entry.command, prefix: entry.prefix }
        }
      }
      throw new Error(
        '找不到可用的 Python 3。请安装 Python 3 与 flask/Pillow/piexif，'
        + '或在插件配置里设置 pythonPath。',
      )
    })()
    return cached
  }
}

/**
 * Invoke the Python bridge and parse its single JSON reply.
 * @param {object} ctx plugin context (unused beyond diagnostics)
 * @param {object} runtime { resolvePython, config }
 * @param {string} command bridge command
 * @param {object} payload JSON request
 * @param {AbortSignal|undefined} signal cooperative cancellation
 */
function readText(file) {
  try {
    return readFileSync(file, 'utf8')
  } catch {
    return ''
  }
}

function removeDir(dir) {
  if (!dir) return
  try { rmSync(dir, { recursive: true, force: true }) } catch { /* best effort */ }
}

/**
 * Invoke the Python bridge using request/response files.
 *
 * A confined Host can forbid piped stdio (named pipes), so this deliberately
 * avoids `stdio: 'pipe'`: the request goes into a temp file, Python writes the
 * reply into another, and the child's stderr goes to a log file for diagnostics.
 */
async function runBridge(runtime, command, payload, signal) {
  const python = await runtime.resolvePython()
  const timeoutMs = runtime.config.timeoutSeconds * 1000
  const tempDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-camera-watermark-'))
  const requestPath = path.join(tempDir, 'request.json')
  const responsePath = path.join(tempDir, 'response.json')
  const errorPath = path.join(tempDir, 'stderr.log')
  writeFileSync(requestPath, JSON.stringify(payload ?? {}), 'utf8')

  let errorFd = 'ignore'
  try {
    errorFd = openSync(errorPath, 'w')
  } catch {
    errorFd = 'ignore'
  }

  return new Promise((resolve, reject) => {
    let settled = false
    let child
    try {
      child = spawn(
        python.command,
        [...python.prefix, BRIDGE, command, requestPath, responsePath],
        {
          cwd: HERE,
          windowsHide: true,
          stdio: ['ignore', 'ignore', errorFd],
          env: {
            ...process.env,
            PYTHONUTF8: '1',
            PYTHONIOENCODING: 'utf-8',
            CAMERA_WATERMARK_ENGINE: ENGINE,
          },
        },
      )
    } catch (error) {
      if (typeof errorFd === 'number') {
        try { closeSync(errorFd) } catch { /* already closed */ }
      }
      removeDir(tempDir)
      reject(new Error(`无法启动 Python（${python.command}）：${error.message}`))
      return
    }
    if (typeof errorFd === 'number') {
      try { closeSync(errorFd) } catch { /* already closed */ }
    }

    const finish = (fn, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal?.removeEventListener?.('abort', onAbort)
      removeDir(tempDir)
      fn(value)
    }
    const fail = (message) => finish(reject, message instanceof Error ? message : new Error(message))
    const kill = () => {
      try { child.kill() } catch { /* already gone */ }
    }

    const timer = setTimeout(() => {
      kill()
      fail(new Error(
        `${command} 超时（${runtime.config.timeoutSeconds}s）。`
        + '可在插件配置里调大 timeoutSeconds，或拆成更小的任务。',
      ))
    }, timeoutMs)
    timer.unref?.()

    const onAbort = () => {
      kill()
      fail(new Error('已取消'))
    }
    if (signal?.aborted) {
      onAbort()
      return
    }
    signal?.addEventListener?.('abort', onAbort, { once: true })

    child.on('error', (error) => {
      fail(new Error(`无法启动 Python（${python.command}）：${error.message}`))
    })
    child.on('close', (code) => {
      if (settled) return
      const body = readText(responsePath).trim()
      if (!body) {
        const diagnostic = readText(errorPath).trim()
        fail(new Error(
          `${command} 没有返回结果（退出码 ${code}）。`
          + (diagnostic ? `\n${tailLines([diagnostic], 10)}` : ''),
        ))
        return
      }
      let parsed
      try {
        parsed = JSON.parse(body)
      } catch (error) {
        fail(new Error(`无法解析 ${command} 的返回：${error.message}\n${body.slice(0, 500)}`))
        return
      }
      if (!parsed || parsed.ok !== true) {
        fail(new Error(parsed?.error || `${command} 失败`))
        return
      }
      finish(resolve, parsed)
    })
  })
}

function renderApply(value) {
  const sourceChanged = value.source_width
    && (value.source_width !== value.width || value.source_height !== value.height)
  const sizeLine = sourceChanged
    ? `原图 ${value.source_width}×${value.source_height} → 成片 ${value.width}×${value.height}`
    : `${value.width}×${value.height}`
  const lines = [
    `已生成：${value.output}`,
    `${sizeLine} · ${value.format} · ${formatBytes(value.bytes)} · ${value.ms}ms`,
    `样式：${value.config.type_label || value.config.type}`
      + (value.config.preset_name ? ` · 模板「${value.config.preset_name}」` : ''),
  ]
  const bar = value.applied?.bar
  if (bar?.segments) {
    const segments = Object.entries(bar.segments)
      .filter(([, segment]) => segment)
      .map(([key, segment]) => `${key}=${segment}`)
    if (segments.length) lines.push(`信息栏：${segments.join(' · ')}`)
  }
  const frame = value.applied?.frame
  if (frame?.lines) {
    const frameLines = Object.entries(frame.lines)
      .filter(([, line]) => line)
      .map(([key, line]) => `${key}=${line}`)
    if (frameLines.length) lines.push(`画框文字：${frameLines.join(' · ')}`)
  }
  if (value.warnings?.length) lines.push(`提示：${value.warnings.join('；')}`)
  if (value.invalid?.length) lines.push(`被丢弃的参数：${value.invalid.join('、')}`)
  return text(lines.join('\n'))
}

function renderBatch(value) {
  const lines = [
    `批量完成：${value.ok_count} 成功 / ${value.failed} 失败 / ${value.skipped} 跳过`
      + `（${value.total} 张，${value.seconds}s${value.dry_run ? '，试运行未写盘' : ''}）`,
    `输出：${value.output_dir || '（输入图同目录）'}`,
    `样式：${value.config.type_label || value.config.type}`
      + (value.config.preset_name ? ` · 模板「${value.config.preset_name}」` : ''),
  ]
  if (value.items?.length) {
    lines.push('逐张：')
    for (const item of value.items.slice(0, 20)) {
      const name = path.basename(item.src || '')
      lines.push(`- ${name}${item.ok ? ' ✓' : ' ✗'}${item.dest ? ` → ${path.basename(item.dest)}` : ''}${item.error ? `：${item.error}` : ''}`)
    }
    if (value.truncated || value.items.length > 20) {
      lines.push(`（仅显示前 20 张；共 ${value.total} 张）`)
    }
  }
  if (value.failures?.length) {
    lines.push('失败：')
    for (const failure of value.failures.slice(0, 10)) {
      lines.push(`- ${path.basename(failure.src || '')}：${failure.error}`)
    }
  }
  if (value.notes?.length) {
    for (const note of value.notes) lines.push(`[${note.level}] ${note.text}`)
  }
  if (value.warnings?.length) lines.push(`提示：${value.warnings.join('；')}`)
  return text(lines.join('\n'))
}

function renderExifRead(value) {
  const lines = [`EXIF：${value.input}（${Object.keys(value.fields).length} 个可编辑字段）`]
  if (value.camera_bar?.some(Boolean)) lines.push(`信息栏：${value.camera_bar.join(' | ')}`)
  lines.push('核心字段：')
  for (const [name, entry] of Object.entries(value.core)) {
    lines.push(`- ${name} = ${entry?.value ?? '（缺失）'}${entry?.source === 'ifd1' ? '（来自缩略图）' : ''}`)
  }
  lines.push('全部字段（键 = 字段名，值 = 当前显示值）：')
  for (const [name, entry] of Object.entries(value.fields)) {
    lines.push(`- ${name} = ${entry.value}`)
  }
  if (value.missing_core?.length) lines.push(`缺失：${value.missing_core.join('、')}`)
  if (value.warnings?.length) lines.push(`提示：${value.warnings.join('；')}`)
  return text(lines.join('\n'))
}

function renderExifWrite(value) {
  const lines = [
    `EXIF 已写入：${value.output}`,
    `修改：${value.changed.join('、')}`,
  ]
  for (const [name, entry] of Object.entries(value.fields)) {
    lines.push(`- ${name} = ${entry?.value ?? ''}`)
  }
  if (value.warnings?.length) lines.push(`提示：${value.warnings.join('；')}`)
  return text(lines.join('\n'))
}

function renderPresets(value) {
  if (value.preset) {
    return text(
      `模板 ${value.preset.id}（${value.preset.name}）\n`
      + `kind=${value.preset.kind} · type=${value.preset.type}\n`
      + `${value.preset.description || ''}\n\n`
      + `参数（当前模板值；调用时把要改的键放进 params）：\n${json(value.preset.params)}\n\n`
      + `控件顺序：${json(value.preset.controls)}`,
    )
  }
  const lines = ['可用模板：']
  for (const preset of value.presets) {
    lines.push(`- ${preset.id} · ${preset.name}（${preset.type}/${preset.kind}）— ${preset.description || ''}`)
  }
  lines.push('当前配置：')
  lines.push(`- ${value.current.preset_name || value.current.type_label}`
    + `（${value.current.params} 个参数${value.current.ok ? '' : `；${value.current.error}`}）`)
  return text(lines.join('\n'))
}

function renderStudio(value) {
  if (value.action === 'status') {
    return text(value.running
      ? `工作台运行中：${value.url}（PID ${value.pid}${value.reused ? '，外部进程' : ''}）`
      : '工作台未运行。调用 action=start 启动。')
  }
  if (value.action === 'stop') {
    return text(value.stopped ? '工作台已停止。' : '工作台原本没有运行。')
  }
  return text(
    `${value.reused ? '已复用' : '已启动'}相机水印工作台：${value.url}\n`
    + `PID ${value.pid} · 引擎 ${value.engine}\n`
    + '在浏览器打开上面的地址即可交互式调参；调好后可直接下载，或让我用工具批量套用同一份配置。',
  )
}

/**
 * Start or attach to the Flask studio.
 */
function makeStudio(runtime) {
  let state = null // { child, port, pid, url, reused, logDir, logPath, lastError }

  const logTail = (current) => {
    if (!current) return ''
    const diagnostic = readText(current.logPath).trim()
    const message = diagnostic || current.lastError || ''
    return message ? `\n${tailLines([message], 12)}` : ''
  }

  // Use node:http, not fetch: undici keeps pooled sockets alive, and killing the
  // studio while such a socket is open can trip an internal assertion in Node.
  const probe = (port, timeoutMs = 900) => new Promise((resolve) => {
    let settled = false
    const done = (value) => {
      if (settled) return
      settled = true
      resolve(value)
    }
    const request = http.get({
      host: '127.0.0.1',
      port,
      path: '/api/watermark/presets',
      agent: false,
      headers: { connection: 'close' },
    }, (response) => {
      response.resume()
      response.on('end', () => done(response.statusCode === 200))
      response.on('error', () => done(false))
    })
    request.setTimeout(timeoutMs, () => {
      request.destroy()
      done(false)
    })
    request.on('error', () => done(false))
  })

  const waitReady = async (port, child) => {
    const deadline = Date.now() + 20000
    while (Date.now() < deadline) {
      if (child.exitCode !== null) {
        throw new Error(`工作台进程提前退出（退出码 ${child.exitCode}）。${logTail(state)}`)
      }
      // eslint-disable-next-line no-await-in-loop
      if (await probe(port, 700)) return
      // eslint-disable-next-line no-await-in-loop
      await new Promise((resolve) => setTimeout(resolve, 250))
    }
    throw new Error(`工作台在 20 秒内没有就绪。${logTail(state)}`)
  }

  const status = () => {
    const alive = Boolean(state && (state.reused || (state.child && state.child.exitCode === null)))
    if (!alive) {
      return { action: 'status', running: false, url: '', pid: null, reused: false }
    }
    return {
      action: 'status',
      running: true,
      url: state.url,
      pid: state.pid,
      reused: state.reused,
      engine: state.engine,
    }
  }

  const stop = async () => {
    if (!state) return { action: 'stop', stopped: false }
    const current = state
    state = null
    if (current.child && current.child.exitCode === null) {
      try { current.child.kill() } catch { /* already gone */ }
      await Promise.race([
        new Promise((resolve) => current.child.once('exit', resolve)),
        new Promise((resolve) => setTimeout(resolve, 3000)),
      ])
    }
    removeDir(current.logDir)
    return { action: 'stop', stopped: true }
  }

  const start = async (port) => {
    const wanted = Number(port) || runtime.config.studioPort
    if (state && state.port === wanted && state.child && state.child.exitCode === null) {
      if (await probe(wanted)) return { ...status(), action: 'start', reused: state.reused }
    }
    if (state) await stop()

    if (await probe(wanted)) {
      state = {
        child: null,
        port: wanted,
        pid: null,
        url: `http://127.0.0.1:${wanted}`,
        reused: true,
        engine: ENGINE,
        logDir: '',
        logPath: '',
        lastError: '',
      }
      return { ...status(), action: 'start' }
    }

    const python = await runtime.resolvePython()
    const logDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-camera-watermark-studio-'))
    const logPath = path.join(logDir, 'studio.log')
    let logFd = 'ignore'
    try {
      logFd = openSync(logPath, 'w')
    } catch {
      logFd = 'ignore'
    }
    let child
    try {
      child = spawn(python.command, [...python.prefix, APP], {
        cwd: ENGINE,
        windowsHide: true,
        stdio: ['ignore', 'ignore', logFd],
        env: {
          ...process.env,
          FLASK_PORT: String(wanted),
          PYTHONUTF8: '1',
          PYTHONIOENCODING: 'utf-8',
        },
      })
    } catch (error) {
      if (typeof logFd === 'number') {
        try { closeSync(logFd) } catch { /* already closed */ }
      }
      removeDir(logDir)
      throw new Error(`无法启动工作台（${python.command}）：${error.message}`)
    }
    if (typeof logFd === 'number') {
      try { closeSync(logFd) } catch { /* already closed */ }
    }
    state = {
      child,
      port: wanted,
      pid: child.pid ?? null,
      url: `http://127.0.0.1:${wanted}`,
      reused: false,
      engine: ENGINE,
      logDir,
      logPath,
      lastError: '',
      startedAt: Date.now(),
    }
    child.on('error', (error) => {
      if (state && state.child === child) state.lastError = String(error.message)
    })
    child.on('exit', () => {
      if (state && state.child === child) {
        state = { ...state, child: null, pid: null }
      }
    })
    try {
      await waitReady(wanted, child)
    } catch (error) {
      try { child.kill() } catch { /* already gone */ }
      state = null
      removeDir(logDir)
      throw error
    }
    return { ...status(), action: 'start' }
  }

  const dispose = () => {
    if (!state) return
    const current = state
    state = null
    if (current.child && current.child.exitCode === null) {
      try { current.child.kill() } catch { /* already gone */ }
    }
    removeDir(current.logDir)
  }

  return { start, stop, status, dispose }
}

/** Register every tool; tools are effect-owned by the plugin context. */
export function apply(ctx, rawConfig) {
  const config = normalizeConfig(rawConfig)
  const runtime = {
    config,
    resolvePython: makePythonResolver(config),
  }
  const studio = makeStudio(runtime)

  ctx.effect(() => () => studio.dispose(), 'camera-watermark: studio process')

  // Same-origin entry point for the Client half's sidebar button. Registration
  // waits for the Web Host's webServer service, so a headless profile still
  // gets the tools and simply has no button route.
  ctx.inject(['webServer'], (webCtx) => {
    webCtx.effect(() => webCtx.webServer.register({
      kind: 'exact',
      path: '/camera-watermark/studio',
      handler: async (req, res) => {
        if (req.method !== 'POST') {
          sendJson(res, 405, { ok: false, error: 'method-not-allowed' })
          return
        }
        try {
          const result = await studio.start()
          sendJson(res, 200, {
            ok: true,
            url: result.url,
            pid: result.pid,
            reused: Boolean(result.reused),
          })
        } catch (error) {
          sendJson(res, 500, {
            ok: false,
            error: String(error?.message || error || '无法启动工作台'),
          })
        }
      },
    }), 'camera-watermark: studio route')
  })

  ctx.tools.register(defineTool({
    name: 'camera_watermark_presets',
    description:
      '列出相机水印工具里的水印模板（预设），或查看某一个模板的完整参数表。'
      + '调用 camera_watermark_apply 前先用它确认 preset id 与参数名；'
      + '不带 id 时还会返回当前工作台保存的样式。',
    parameters: {
      id: {
        type: 'string',
        description: '模板 id；省略则列出全部模板。传 id 会返回该模板的 params 与 controls。',
      },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderPresets(value),
    },
    async execute(args, exec) {
      return runBridge(runtime, 'presets', args.id ? { id: args.id } : {}, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'camera_watermark_apply',
    description:
      '给一张照片套用相机水印：EXIF 信息栏、画框水印、背景模糊、文字或图片水印。'
      + '默认套用工作台当前配置；传 preset 会从该模板的默认版式开始，params 覆盖其中任意参数。'
      + '输出写到新文件，绝不覆盖原图（除非 overwrite=true）。',
    parameters: {
      input: { type: 'string', required: true, description: '输入图片的绝对路径' },
      output: { type: 'string', description: '输出文件路径；省略则在原图旁生成 <原名>_wm.<扩展名>' },
      preset: { type: 'string', description: '模板 id（见 camera_watermark_presets）' },
      params: {
        type: 'object',
        additionalProperties: true,
        description: '模板参数覆盖，例如 {"bar_height_ratio":0.15,"left_color":"#111111"}',
      },
      type: {
        type: 'string',
        enum: TYPE_ENUM,
        description: '水印类型；一般由 preset 推导，显式传入可强制 text/image/exif/frame/blur',
      },
      config: {
        type: 'string',
        description: '配置来源：current（默认）、命名快照 id，或导出配置 .json 的绝对路径',
      },
      format: { type: 'string', enum: FORMAT_ENUM, description: '导出格式，默认跟随配置（通常 jpeg）' },
      quality: { type: 'integer', description: 'JPEG 质量 1–100，默认跟随配置（通常 95）' },
      overwrite: { type: 'boolean', description: '输出已存在时是否覆盖；默认 false（自动追加 _2）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderApply(value),
    },
    async execute(args, exec) {
      return runBridge(runtime, 'render', args, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'camera_watermark_batch',
    description:
      '离线批量水印：把同一份配置套到一个或多个文件夹/图片上。逐张保留各自 EXIF，'
      + '一张坏图不影响其余；默认不覆盖已有成品、绝不覆盖原图。'
      + '默认输出到输入图旁的 <原名>_wm.<ext>，也可指定 out 目录与 suffix。',
    parameters: {
      inputs: {
        type: 'array',
        items: { type: 'string' },
        required: true,
        description: '输入文件夹或图片的绝对路径数组',
      },
      out: { type: 'string', description: '输出文件夹；省略则写在每张输入图旁边' },
      suffix: { type: 'string', description: '输出文件名后缀，默认 _wm；传空串表示同名（仅换目录）' },
      recursive: { type: 'boolean', description: '是否递归扫描子文件夹，默认 true' },
      force: { type: 'boolean', description: '覆盖已存在的输出文件；默认 false（跳过）' },
      dry_run: { type: 'boolean', description: '试运行：照常渲染但不写盘' },
      preset: { type: 'string', description: '模板 id；默认用当前配置' },
      params: {
        type: 'object',
        additionalProperties: true,
        description: '模板参数覆盖（整批共用）',
      },
      type: { type: 'string', enum: TYPE_ENUM, description: '水印类型（一般由 preset 推导）' },
      config: { type: 'string', description: 'current、命名快照 id 或导出配置 .json 路径' },
      format: { type: 'string', enum: FORMAT_ENUM, description: '导出格式' },
      quality: { type: 'integer', description: 'JPEG 质量 1–100' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderBatch(value),
    },
    async execute(args, exec) {
      return runBridge(runtime, 'batch', args, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'camera_watermark_exif_read',
    description:
      '读取照片 EXIF（含相机厂商/型号/参数行/拍摄时间），返回可编辑字段名与当前显示值。'
      + '这些字段名可直接作为 camera_watermark_exif_write 的 edits 键。',
    parameters: {
      input: { type: 'string', required: true, description: '图片绝对路径' },
      include_binary: { type: 'boolean', description: '是否包含 MakerNote 等二进制字段，默认 false' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderExifRead(value),
    },
    async execute(args, exec) {
      return runBridge(runtime, 'exif_read', args, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'camera_watermark_exif_write',
    description:
      '修改照片 EXIF 相机参数（厂商、型号、ISO、光圈、快门、焦距、时间等），'
      + '只替换 APP1(EXIF) 段、像素不重新编码；无法原地替换的容器会回退 JPEG 并提示。'
      + '默认写到新文件 <原名>_exif.jpg，不覆盖原图。',
    parameters: {
      input: { type: 'string', required: true, description: '图片绝对路径' },
      edits: {
        type: 'object',
        additionalProperties: true,
        required: true,
        description: '字段名 → 新值，例如 {"相机型号":"ILCE-6700","ISO感光度":"ISO 200"}；字段名用 exif_read 返回的键',
      },
      output: { type: 'string', description: '输出路径；省略则生成 <原名>_exif.jpg' },
      overwrite: { type: 'boolean', description: '输出已存在时是否覆盖，默认 false' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderExifWrite(value),
    },
    async execute(args, exec) {
      return runBridge(runtime, 'exif_write', args, exec.signal)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'camera_watermark_studio',
    description:
      '启动/停止相机水印交互式工作台（原 Flask 页面）。'
      + '需要手动拖拽、实时预览或调模板时用 action=start，然后把返回的 URL 给用户；'
      + 'action=status 查看是否在运行，action=stop 关闭。批量与单张出片不需要启动它。',
    parameters: {
      action: {
        type: 'string',
        enum: ['start', 'stop', 'status'],
        required: true,
        description: 'start 启动或复用；status 查询；stop 停止',
      },
      port: { type: 'integer', description: '监听端口；默认用插件配置 studioPort（5000）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => renderStudio(value),
    },
    async execute(args) {
      if (args.action === 'status') return studio.status()
      if (args.action === 'stop') return studio.stop()
      return studio.start(args.port)
    },
  }))

  // Surface a useful, non-fatal diagnostic when the engine cannot be imported.
  runtime.resolvePython()
    .then(() => runBridge(runtime, 'info', {}))
    .then((info) => {
      ctx.logger?.info?.(
        '[camera-watermark] engine ready: %s preset(s); current=%s%s',
        info.presets.length,
        info.config.type,
        info.config.preset ? ` · ${info.config.preset}` : '',
      )
    })
    .catch((error) => {
      ctx.logger?.warn?.('[camera-watermark] engine not ready: %s', error.message)
    })
}

export { NAME }
