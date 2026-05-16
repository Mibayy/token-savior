#!/usr/bin/env node
// Token Savior Code Mode worker.
// Reads:  argv[2] = JSON list of allowed tool names
//         argv[3] = base64-encoded user script (body of an async function)
// Stdio protocol (line-delimited JSON):
//   worker -> parent: {type:"call",  id, tool, args}
//                     {type:"log",   level, value}
//                     {type:"final", value}
//                     {type:"error", message, stack}
//   parent -> worker: {type:"result", id, value}
//                     {type:"error",  id, error}

import readline from 'node:readline';

const rl = readline.createInterface({ input: process.stdin, terminal: false });
const pending = new Map();
let nextId = 1;

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function call(tool, args) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    emit({ type: 'call', id, tool, args: args ?? {} });
  });
}

rl.on('line', (line) => {
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  if (msg.type === 'result' || msg.type === 'error') {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if (msg.type === 'result') p.resolve(msg.value);
    else p.reject(new Error(msg.error || 'tool error'));
  }
});

const allowedTools = JSON.parse(process.argv[2] || '[]');
const tools = {};
for (const t of allowedTools) {
  tools[t] = (args) => call(t, args);
}

const _serialize = (a) => (typeof a === 'string' ? a : JSON.stringify(a));
console.log = (...args) => emit({ type: 'log', level: 'info', value: args.map(_serialize).join(' ') });
console.error = (...args) => emit({ type: 'log', level: 'error', value: args.map(_serialize).join(' ') });
console.warn = (...args) => emit({ type: 'log', level: 'warn', value: args.map(_serialize).join(' ') });

const scriptB64 = process.argv[3] || '';
const userBody = Buffer.from(scriptB64, 'base64').toString('utf8');

async function run() {
  const factory = new Function(
    'tools',
    'console',
    `return (async () => {\n${userBody}\n})();`
  );
  return await factory(tools, console);
}

run()
  .then((value) => {
    emit({ type: 'final', value: value === undefined ? null : value });
    process.exit(0);
  })
  .catch((err) => {
    emit({
      type: 'error',
      message: String(err && err.message ? err.message : err),
      stack: err && err.stack ? err.stack : '',
    });
    process.exit(1);
  });
