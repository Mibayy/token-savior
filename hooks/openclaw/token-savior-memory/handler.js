/**
 * Moteur memoire Token Savior — handler de hook OpenClaw.
 *
 * Volontairement sans aucun import interne d'OpenClaw : les modules livres
 * portent un nom hache (`../../internal-hooks-D52pUqod.js`) qui change a
 * chaque version. Ce fichier n'utilise que des modules natifs de Node, il
 * survit donc aux mises a jour.
 *
 * Il ne reimplemente pas le moteur memoire : il execute les scripts Token
 * Savior existants en leur passant sur stdin une charge utile de forme Claude
 * Code, seule forme qu'ils savent lire.
 */
import { execFile } from "node:child_process";
import path from "node:path";
import os from "node:os";
import fs from "node:fs/promises";

const HOOKS_DIR = process.env.TS_HOOKS_DIR || path.resolve(import.meta.dirname, "..", "..");
const MEMORY_FILENAME = "TOKEN-SAVIOR-MEMORY.md";
const DEFAULT_TIMEOUT_MS = 3000;

/** `${type}:${action}` -> script Token Savior + nom d'evenement Claude equivalent. */
const ROUTES = {
  "agent:bootstrap": { script: "memory-session-start.sh", claudeEvent: "SessionStart", timeout: 3000 },
  "message:received": { script: "memory-userprompt.sh", claudeEvent: "UserPromptSubmit", timeout: 1000 },
  "session:compact:before": { script: "memory-precompact.sh", claudeEvent: "PreCompact", timeout: 5000 },
  "command:new": { script: "memory-session-stop.sh", args: ["end"], claudeEvent: "SessionEnd", timeout: 120000 },
  "command:reset": { script: "memory-session-stop.sh", args: ["end"], claudeEvent: "SessionEnd", timeout: 120000 },
};

/**
 * Execute un script de hook Token Savior et rend sa sortie standard.
 * Ne jette jamais : un moteur memoire qui casse la session qu'il devait
 * enrichir est pire que pas de moteur du tout.
 */
function runScript(script, args, payload, timeout) {
  return new Promise((resolve) => {
    const child = execFile(
      "bash",
      [path.join(HOOKS_DIR, script), ...args],
      { timeout: timeout ?? DEFAULT_TIMEOUT_MS, maxBuffer: 4 * 1024 * 1024 },
      (err, stdout) => resolve(err ? "" : String(stdout || "")),
    );
    try {
      child.stdin.end(JSON.stringify(payload));
    } catch {
      resolve("");
    }
  });
}

const tokenSaviorMemory = async (event) => {
  const key = `${event?.type}:${event?.action}`;
  const route = ROUTES[key];
  if (!route) return;

  const context = event?.context;
  if (!context) return;

  const payload = {
    hook_event_name: route.claudeEvent,
    session_id: context.sessionKey ?? "",
    cwd: context.workspaceDir ?? process.cwd(),
    source: "openclaw",
  };

  const stdout = await runScript(route.script, route.args ?? [], payload, route.timeout);

  // Seul `agent:bootstrap` peut injecter, et seulement en poussant une entree
  // dans context.bootstrapFiles (mecanisme de bootstrap-extra-files).
  if (route.claudeEvent !== "SessionStart") return;
  if (!Array.isArray(context.bootstrapFiles)) return;
  const content = stdout.trim();
  if (!content) return;

  // Le contenu doit exister sur disque : le consommateur lit `path`.
  const dir = context.workspaceDir || os.tmpdir();
  const filePath = path.join(dir, MEMORY_FILENAME);
  try {
    await fs.writeFile(filePath, content, "utf-8");
  } catch {
    return;
  }

  context.bootstrapFiles = [
    ...context.bootstrapFiles,
    { name: MEMORY_FILENAME, path: filePath, content, missing: false },
  ];
};

export { tokenSaviorMemory as default };
