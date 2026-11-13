import { execFile } from "node:child_process";
import { access, mkdir, readFile, readdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";


const execFileAsync = promisify(execFile);

const PLUGIN_ID = "omx-draft-check";
const HOOK_VERSION = 2;
const TERMINAL_MARKERS = [".done", ".fail", ".blocked"];
const ARG_PATH_TOOLS = new Set(["write", "edit"]);
const METADATA_FILES_TOOLS = new Set(["apply_patch"]);
const DRAFT_FILE_NAME = "draft.json";
const STATE_DIR_NAME = "draft-check";
const CLI_NAME = "omx_draft_delivery";
const CLI_TIMEOUT_MS = 30_000;
const CLI_MAX_BUFFER_BYTES = 256 * 1024;
const MAX_FEEDBACK_BYTES = 16 * 1024;
const ADVISORY_TAG = "omx-draft-check (advisory)";


// Host registration is explicit: the host that owns the control root sets
// OMX_VISIBLE_CONTROL_ROOT when it launches this OpenCode process. Without
// that single explicit configuration the plugin stays dormant — it never
// guesses a root from the current working directory (a real worker often
// runs in a sub-project) and never scans the disk for one.
function controlRoot() {
  const raw = String(process.env.OMX_VISIBLE_CONTROL_ROOT || "").trim();
  return raw ? path.resolve(raw) : "";
}


function sessionKey(input = {}) {
  return String(input.sessionID || "");
}


// Deterministic public tool interfaces only: write/edit expose the target
// as args.filePath, and apply_patch exposes the applied per-file list as
// result metadata files[].filePath. There are no alias fallbacks, no
// patch-text parsing, and no shell parsing; tools without one of these
// certain interfaces resolve no paths and honestly take no action.
function argumentPaths(input = {}, output = null) {
  const tool = String(input.tool || "");
  const args = input.args && typeof input.args === "object" ? input.args : {};
  if (ARG_PATH_TOOLS.has(tool)) {
    const value = String(args.filePath || "");
    return value ? [value] : [];
  }
  if (METADATA_FILES_TOOLS.has(tool) && output) {
    const metadata =
      output.metadata && typeof output.metadata === "object"
        ? output.metadata
        : {};
    const files = Array.isArray(metadata.files) ? metadata.files : [];
    const names = [];
    for (const file of files) {
      if (!file || typeof file !== "object") continue;
      const value = String(file.filePath || "");
      if (value) names.push(value);
    }
    return names;
  }
  return [];
}


async function exists(target) {
  try {
    await access(target);
    return true;
  } catch {
    return false;
  }
}


async function readJson(target) {
  try {
    const value = JSON.parse(await readFile(target, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  } catch {
    return {};
  }
}


// Readiness is written only inside the explicitly registered control root.
async function writeReadiness(root) {
  const target = path.join(
    root,
    ".omx",
    "framework",
    "team",
    `opencode_draft_check_${process.pid}.json`,
  );
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  await writeFile(
    temporary,
    `${JSON.stringify(
      {
        schema_version: 1,
        plugin_id: PLUGIN_ID,
        hook_version: HOOK_VERSION,
        loaded: true,
        control_root: root,
        control_root_source: "OMX_VISIBLE_CONTROL_ROOT",
        opencode_pid: process.pid,
        hooks: ["tool.execute.after"],
        loaded_at: new Date().toISOString(),
      },
      null,
      2,
    )}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  await rename(temporary, target);
}


async function taskWorker(taskDir) {
  try {
    return (await readFile(path.join(taskDir, "owner"), "utf8")).trim();
  } catch {
    return "";
  }
}


function sameText(left, right) {
  return String(left ?? "") === String(right ?? "");
}


function sameGeneration(left, right) {
  return Number(left) === Number(right);
}


// Every call re-verifies the full identity chain from the small active
// lock set against the current session — there is no cache. The lock,
// owner file, task metadata, acceptance fence, and dispatch record must
// agree on task, attempt, worker, contract hash, instance, and binding /
// fencing generations; a lock that belongs to another task or session
// never reaches the CLI call.
async function draftCheckTaskDir(root, currentSession) {
  if (!currentSession) return "";
  const runsRoot = path.join(
    root,
    ".omx",
    "framework",
    "team",
    "visible-runs",
  );
  let locks = [];
  try {
    locks = await readdir(path.join(runsRoot, "_locks"), {
      withFileTypes: true,
    });
  } catch {
    return "";
  }
  const matches = [];
  for (const entry of locks) {
    if (!entry.isFile() || !entry.name.endsWith(".lock.json")) continue;
    const lock = await readJson(path.join(runsRoot, "_locks", entry.name));
    const worker = String(lock.worker_id || "");
    if (!worker || entry.name !== `${worker}.lock.json`) continue;
    if (String(lock.status || "") !== "submitted") continue;
    const taskDir = path.resolve(root, String(lock.task_dir || ""));
    if (!taskDir.startsWith(`${runsRoot}${path.sep}`)) continue;
    if (!(await exists(path.join(taskDir, ".submitted")))) continue;
    let terminal = false;
    for (const marker of TERMINAL_MARKERS) {
      if (await exists(path.join(taskDir, marker))) {
        terminal = true;
        break;
      }
    }
    if (terminal) continue;
    const task = await readJson(path.join(taskDir, "task.json"));
    const selected = task?.task_contract?.draft_check;
    if (
      !selected ||
      typeof selected !== "object" ||
      selected.enabled !== true
    ) {
      continue;
    }
    const fence = await readJson(path.join(taskDir, "acceptance_fence.json"));
    const owner = await taskWorker(taskDir);
    const taskId = String(task.id || "");
    if (!sameText(fence.session_id, currentSession)) continue;
    if (!owner || !sameText(owner, worker) || !sameText(fence.worker_id, worker)) {
      continue;
    }
    if (!taskId || !sameText(fence.task_id, taskId) || !sameText(lock.task, taskId)) {
      continue;
    }
    if (!String(fence.attempt_id || "") || !String(fence.contract_hash || "")) {
      continue;
    }
    if (
      lock.instance_id !== undefined &&
      !sameText(fence.instance_id, lock.instance_id)
    ) {
      continue;
    }
    if (
      lock.binding_generation !== undefined &&
      !sameGeneration(fence.binding_generation, lock.binding_generation)
    ) {
      continue;
    }
    if (
      lock.fencing_generation !== undefined &&
      !sameGeneration(fence.fencing_generation, lock.fencing_generation)
    ) {
      continue;
    }
    const dispatch = await readJson(path.join(taskDir, "dispatch.json"));
    if (
      dispatch.attempt_id !== undefined &&
      !sameText(dispatch.attempt_id, fence.attempt_id)
    ) {
      continue;
    }
    if (
      dispatch.contract_hash !== undefined &&
      !sameText(dispatch.contract_hash, fence.contract_hash)
    ) {
      continue;
    }
    matches.push({ taskDir, taskId });
  }
  if (matches.length !== 1) return "";
  return matches[0].taskDir;
}


// execFile's own timeout/maxBuffer machinery owns the process lifecycle: on
// timeout it kills the child, so no CLI process is left running behind the
// resolved advisory.
async function runCli(root, argv, timeoutMs = CLI_TIMEOUT_MS) {
  try {
    const { stdout, stderr } = await execFileAsync(argv[0], argv.slice(1), {
      cwd: root,
      env: process.env,
      timeout: timeoutMs,
      maxBuffer: CLI_MAX_BUFFER_BYTES,
      encoding: "utf8",
    });
    return {
      ok: true,
      kind: "exit",
      stdout: String(stdout || ""),
      stderr: String(stderr || ""),
    };
  } catch (error) {
    const failure = error && typeof error === "object" ? error : {};
    if (failure.killed) {
      return {
        ok: false,
        kind: String(failure.message || "").includes("maxBuffer")
          ? "overflow"
          : "timeout",
      };
    }
    if (typeof failure.code === "number") {
      return {
        ok: false,
        kind: "exit",
        code: failure.code,
        stdout: String(failure.stdout || ""),
        stderr: String(failure.stderr || ""),
      };
    }
    return {
      ok: false,
      kind: "spawn",
      detail: String(failure.code || failure.message || error),
    };
  }
}


function bounded(text) {
  const value = String(text || "").trim();
  if (!value) return "";
  const bytes = Buffer.byteLength(value, "utf8");
  if (bytes <= MAX_FEEDBACK_BYTES) return value;
  return `${value.slice(0, MAX_FEEDBACK_BYTES)}\n[${ADVISORY_TAG}: feedback truncated at ${MAX_FEEDBACK_BYTES} bytes]`;
}


// The live selection is read only from the leader-authorized task
// contract after the full identity chain above has verified session,
// task, fence, owner, and lock. The task document and the draft text
// can never add it; the plugin forwards the fixed task path and the
// contract's authorized evidence root to the CLI, whose flags are the
// host-explicit authorization for the one live Jev question.
async function computeAdvisory(root, input, output) {
  const currentSession = sessionKey(input);
  if (!currentSession) return "";
  const taskDir = await draftCheckTaskDir(root, currentSession);
  if (!taskDir) return "";
  const draftPath = path.join(taskDir, DRAFT_FILE_NAME);
  const targets = argumentPaths(input, output);
  const matched = targets.some(
    (target) => target && path.resolve(target) === draftPath,
  );
  if (!matched) return "";

  const task = await readJson(path.join(taskDir, "task.json"));
  const taskId = String(task.id || "");
  const selection =
    task.task_contract && typeof task.task_contract === "object"
      ? task.task_contract.draft_check
      : null;
  const liveRoot =
    selection && selection.jev_live === true &&
    typeof selection.evidence_root === "string" && selection.evidence_root
      ? selection.evidence_root
      : "";
  const cli = path.join(root, "bin", CLI_NAME);
  const stateDir = path.join(taskDir, STATE_DIR_NAME);
  const result = await runCli(root, [
    cli,
    "submit",
    "--draft",
    draftPath,
    "--state-dir",
    stateDir,
    ...(taskId ? ["--task", taskId] : []),
    ...(liveRoot
      ? ["--jev-live", "--jev-evidence-root", liveRoot]
      : []),
  ]);
  if (result.ok && bounded(result.stdout)) {
    return (
      `${ADVISORY_TAG} (task ${taskId || "-"}):\n` + bounded(result.stdout)
    );
  }
  if (result.kind === "exit" && bounded(result.stderr)) {
    return (
      `${ADVISORY_TAG} (task ${taskId || "-"}): draft rejected by the ` +
      `local check:\n${bounded(result.stderr)}`
    );
  }
  return (
    `${ADVISORY_TAG} (task ${taskId || "-"}): advisory feedback unavailable ` +
    `(${result.kind}${result.detail ? `:${result.detail}` : ""}); ` +
    "continue with the formal report and the unchanged finish protocol."
  );
}


// The hook never throws: any internal failure collapses into one short
// advisory line, and even appending that line must not break the
// underlying write tool.
async function afterTool(root, input, output) {
  let appended = "";
  try {
    appended = await computeAdvisory(root, input, output);
  } catch {
    appended =
      `${ADVISORY_TAG}: internal check failure; the formal delivery does ` +
      "not depend on it; continue with the formal report and the " +
      "unchanged finish protocol.";
  }
  if (!appended) return;
  try {
    const previous = String(output?.output ?? "");
    output.output =
      previous +
      (previous.endsWith("\n") || previous === "" ? "" : "\n") +
      `${appended}\n`;
  } catch {
    // The advisory must never fail the write tool it decorates.
  }
}


async function server() {
  const root = controlRoot();
  if (!root) return {};
  await writeReadiness(root).catch(() => {});
  return {
    "tool.execute.after": async (input, output) =>
      afterTool(root, input, output),
  };
}


export default {
  id: PLUGIN_ID,
  server,
};


export const __internals = {
  PLUGIN_ID,
  HOOK_VERSION,
  TERMINAL_MARKERS,
  ARG_PATH_TOOLS,
  METADATA_FILES_TOOLS,
  CLI_TIMEOUT_MS,
  controlRoot,
  argumentPaths,
  draftCheckTaskDir,
  runCli,
  afterTool,
  bounded,
  server,
};
