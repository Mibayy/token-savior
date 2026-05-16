#!/usr/bin/env node
// Token Savior Code Mode warm worker.
//
// Long-lived process. Reads exec requests from stdin and runs each user
// script in an isolated `vm` context so globals do not leak between calls.
//
// Stdio protocol (line-delimited JSON):
//   parent -> worker: {type:"exec",   exec_id, script_b64, allowed_tools}
//                     {type:"result", id, value}             // tool reply
//                     {type:"error",  id, error}             // tool reply error
//                     {type:"shutdown"}
//   worker -> parent: {type:"ready"}                         // emitted once at boot
//                     {type:"call",   id, tool, args}        // tool dispatch
//                     {type:"log",    exec_id, level, value} // console.* output
//                     {type:"final",  exec_id, value}        // script success
//                     {type:"error",  exec_id, message, stack} // script failure
//
// `id` (tool-call id) is independent from `exec_id` (script id). Both are
// monotonic integers. Only one exec runs at a time.

import readline from 'node:readline';
import vm from 'node:vm';

const rl = readline.createInterface({ input: process.stdin, terminal: false });

// Tool-call bookkeeping. `pending` maps id -> {resolve, reject}.
const pending = new Map();
let nextCallId = 1;

// Current exec context (set in handleExec).
let currentExecId = null;

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function makeTools(allowed) {
  const tools = {};
  for (const t of allowed) {
    tools[t] = (args) => {
      const id = nextCallId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        emit({ type: 'call', id, tool: t, args: args ?? {} });
      });
    };
  }
  return tools;
}

function makeConsole(execId) {
  const serialize = (a) => (typeof a === 'string' ? a : JSON.stringify(a));
  return {
    log: (...args) =>
      emit({ type: 'log', exec_id: execId, level: 'info', value: args.map(serialize).join(' ') }),
    error: (...args) =>
      emit({ type: 'log', exec_id: execId, level: 'error', value: args.map(serialize).join(' ') }),
    warn: (...args) =>
      emit({ type: 'log', exec_id: execId, level: 'warn', value: args.map(serialize).join(' ') }),
    info: (...args) =>
      emit({ type: 'log', exec_id: execId, level: 'info', value: args.map(serialize).join(' ') }),
    debug: (...args) =>
      emit({ type: 'log', exec_id: execId, level: 'debug', value: args.map(serialize).join(' ') }),
  };
}

async function handleExec(msg) {
  const execId = msg.exec_id;
  currentExecId = execId;
  const allowed = msg.allowed_tools || [];
  const scriptB64 = msg.script_b64 || '';
  const userBody = Buffer.from(scriptB64, 'base64').toString('utf8');

  const tools = makeTools(allowed);
  const sandboxConsole = makeConsole(execId);

  // Fresh context per exec — no global leakage between scripts.
  const context = vm.createContext({
    tools,
    console: sandboxConsole,
    Promise,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    Buffer,
    URL,
    URLSearchParams,
    TextEncoder,
    TextDecoder,
    JSON,
    Math,
    Date,
    Array,
    Object,
    String,
    Number,
    Boolean,
    Error,
    RegExp,
    Map,
    Set,
    WeakMap,
    WeakSet,
    Symbol,
  });

  const wrapped = `(async () => {\n${userBody}\n})()`;

  try {
    const script = new vm.Script(wrapped, { filename: `ts_execute_${execId}.js` });
    const value = await script.runInContext(context);
    emit({ type: 'final', exec_id: execId, value: value === undefined ? null : value });
  } catch (err) {
    emit({
      type: 'error',
      exec_id: execId,
      message: String(err && err.message ? err.message : err),
      stack: err && err.stack ? err.stack : '',
    });
  } finally {
    currentExecId = null;
    // Drop any pending tool-call promises from a crashed script — the parent
    // is responsible for not sending stale {type:"result"} for this exec_id,
    // but defensive clear keeps the map bounded across many execs.
    pending.clear();
  }
}

rl.on('line', (line) => {
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const t = msg.type;
  if (t === 'result' || t === 'error') {
    // Tool-call reply (id refers to a tools.* dispatch).
    if (msg.id == null) return;
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (t === 'result') p.resolve(msg.value);
    else p.reject(new Error(msg.error || 'tool error'));
  } else if (t === 'exec') {
    // Fire-and-forget; the script's lifecycle is its own promise chain.
    handleExec(msg);
  } else if (t === 'shutdown') {
    process.exit(0);
  }
});

rl.on('close', () => {
  // Parent closed stdin → graceful exit.
  process.exit(0);
});

emit({ type: 'ready' });
