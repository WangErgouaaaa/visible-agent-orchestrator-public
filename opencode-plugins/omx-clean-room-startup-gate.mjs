import { createHash, randomUUID } from "node:crypto";
import {
  access,
  link,
  lstat,
  mkdir,
  readFile,
  readdir,
  realpath,
  rename,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";


const PLUGIN_ID = "omx-clean-room-startup-gate";
const GATE_VERSION = 1;
const TERMINAL_MARKERS = [".done", ".fail", ".blocked"];
const pendingReads = new Set();
const taskCache = new Map();
const taskQueues = new Map();


function controlRoot() {
  return path.resolve(process.env.OMX_VISIBLE_CONTROL_ROOT || process.cwd());
}


function sessionId(input = {}) {
  return String(input.sessionID || input.sessionId || input.session_id || "");
}


function callId(input = {}) {
  return String(input.callID || input.callId || input.call_id || "");
}


function toolArgs(output = {}) {
  return output && typeof output.args === "object" && output.args
    ? output.args
    : {};
}


function argumentPath(args = {}) {
  return String(
    args.filePath ||
      args.file_path ||
      args.path ||
      args.target ||
      "",
  );
}


function commandText(args = {}) {
  return String(args.command || args.cmd || args.script || "");
}


function isCompleteParentRead(args, parentPath) {
  return (
    path.resolve(argumentPath(args)) === parentPath &&
    !["offset", "limit", "start", "end"].some((key) =>
      Object.hasOwn(args, key),
    )
  );
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


async function readJsonRequired(target, errorCode) {
  try {
    const value = JSON.parse(await readFile(target, "utf8"));
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return value;
    }
  } catch (error) {
    if (errorCode === "clean_room_startup_session_binding_missing" &&
        error?.code === "ENOENT") {
      return null;
    }
  }
  throw new Error(errorCode);
}


function sessionBindingPath(root, currentSession) {
  const digest = createHash("sha256").update(currentSession).digest("hex");
  return path.join(
    root,
    ".omx",
    "framework",
    "team",
    "visible-runs",
    "_clean-room-startup-sessions",
    `${digest}.json`,
  );
}


async function processStartTimeTicks() {
  try {
    const text = await readFile(`/proc/${process.pid}/stat`, "utf8");
    const fields = text.slice(text.lastIndexOf(")") + 1).trim().split(/\s+/);
    return Number(fields[19]) || 0;
  } catch {
    return 0;
  }
}


async function writeJsonAtomic(target, value) {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporary, target);
}


async function writeJsonExclusive(target, value) {
  await mkdir(path.dirname(target), { recursive: true });
  const temporary = `${target}.tmp-${process.pid}-${randomUUID()}`;
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  try {
    await link(temporary, target);
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
  } finally {
    await unlink(temporary).catch(() => {});
  }
}


function serializeTask(taskDir, action) {
  const previous = taskQueues.get(taskDir) || Promise.resolve();
  const current = previous.catch(() => {}).then(action);
  taskQueues.set(taskDir, current);
  return current.finally(() => {
    if (taskQueues.get(taskDir) === current) taskQueues.delete(taskDir);
  });
}


async function writeReadiness(root) {
  const target = path.join(
    root,
    ".omx",
    "framework",
    "team",
    `opencode_clean_room_startup_gate_${process.pid}.json`,
  );
  await writeJsonAtomic(target, {
    schema_version: 1,
    plugin_id: PLUGIN_ID,
    gate_version: GATE_VERSION,
    loaded: true,
    control_root: root,
    opencode_pid: process.pid,
    opencode_start_time_ticks: await processStartTimeTicks(),
    hooks: ["tool.execute.before", "tool.execute.after"],
    loaded_at: new Date().toISOString(),
  });
}


async function taskIsArmed(root, taskDir, expectedSession = "") {
  if (await exists(path.join(taskDir, ".submitted"))) return true;
  const armed = await readJson(
    path.join(taskDir, "startup_gate_armed.json"),
  );
  if (
    armed.status === "armed" &&
    (
      !expectedSession ||
      String(armed.session_id || "") === expectedSession
    )
  ) {
    return true;
  }
  const worker = await taskWorker(taskDir);
  if (!worker || path.basename(worker) !== worker) return false;
  const lock = await readJson(
    path.join(
      root,
      ".omx",
      "framework",
      "team",
      "visible-runs",
      "_locks",
      `${worker}.lock.json`,
    ),
  );
  return (
    lock.status === "submitting" &&
    String(lock.worker_id || "") === worker &&
    path.resolve(root, String(lock.task_dir || "")) === taskDir
  );
}


async function activeTask(root, taskDir, expectedSession) {
  if (!(await taskIsArmed(root, taskDir, expectedSession))) return false;
  for (const marker of TERMINAL_MARKERS) {
    if (await exists(path.join(taskDir, marker))) return false;
  }
  const fence = await readJson(path.join(taskDir, "acceptance_fence.json"));
  return String(fence.session_id || "") === expectedSession;
}


async function activeStrictTask(root, taskDir) {
  if (!(await taskIsArmed(root, taskDir))) return false;
  for (const marker of TERMINAL_MARKERS) {
    if (await exists(path.join(taskDir, marker))) return false;
  }
  return true;
}


function lockOwnsCurrentProcess(lock) {
  const pids = [
    ...(Array.isArray(lock.opencode_pids) ? lock.opencode_pids : []),
    lock.opencode_pid,
  ].map((value) => Number(value) || 0);
  return pids.includes(process.pid);
}


async function boundTaskForSession(
  root,
  expectedSession,
  input = null,
  output = null,
) {
  const binding = await readJsonRequired(
    sessionBindingPath(root, expectedSession),
    "clean_room_startup_session_binding_missing",
  );
  if (!binding) return "";
  const runsRoot = path.join(
    root,
    ".omx",
    "framework",
    "team",
    "visible-runs",
  );
  const taskDir = path.resolve(root, String(binding.task_path || ""));
  if (
    binding.status !== "armed" ||
    binding.mode !== "exact_parent_read" ||
    String(binding.session_id || "") !== expectedSession ||
    !taskDir.startsWith(`${runsRoot}${path.sep}`)
  ) {
    throw new Error("clean_room_startup_session_binding_invalid");
  }
  for (const marker of TERMINAL_MARKERS) {
    if (await exists(path.join(taskDir, marker))) return "";
  }
  try {
    const armed = await readJsonRequired(
      path.join(taskDir, "startup_gate_armed.json"),
      "clean_room_startup_armed_evidence_invalid",
    );
    const fence = await readJsonRequired(
      path.join(taskDir, "acceptance_fence.json"),
      "clean_room_startup_acceptance_fence_invalid",
    );
    const startup = await requiredStartupContract(taskDir);
    const worker = await taskWorker(taskDir);
    if (
      armed.status !== "armed" ||
      String(armed.session_id || "") !== expectedSession ||
      String(armed.worker_id || "") !== String(binding.worker_id || "") ||
      String(armed.task_path || "") !== String(binding.task_path || "") ||
      String(fence.session_id || "") !== expectedSession ||
      String(fence.worker_id || "") !== String(binding.worker_id || "") ||
      worker !== String(binding.worker_id || "") ||
      startup.mode !== binding.mode ||
      startup.parent_path !== binding.parent_path ||
      startup.parent_sha256 !== binding.parent_sha256
    ) {
      throw new Error("clean_room_startup_session_binding_mismatch");
    }
  } catch (error) {
    if (input) {
      await writeContamination(
        taskDir,
        input,
        output,
        String(binding.parent_path || ""),
      );
    }
    throw error;
  }
  return taskDir;
}


async function taskForSession(
  root,
  expectedSession,
  input = null,
  output = null,
) {
  const bound = await boundTaskForSession(
    root,
    expectedSession,
    input,
    output,
  );
  if (bound) {
    taskCache.set(expectedSession, bound);
    return bound;
  }
  const cached = taskCache.get(expectedSession);
  if (
    cached &&
    (await startupContract(cached)) &&
    (await activeTask(root, cached, expectedSession))
  ) {
    return cached;
  }

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
  const candidates = new Map();
  for (const entry of locks) {
    if (!entry.isFile() || !entry.name.endsWith(".lock.json")) continue;
    const lock = await readJson(path.join(runsRoot, "_locks", entry.name));
    const taskDir = path.resolve(root, String(lock.task_dir || ""));
    if (taskDir.startsWith(`${runsRoot}${path.sep}`)) {
      candidates.set(taskDir, lockOwnsCurrentProcess(lock));
    }
  }
  const mismatches = [];
  for (const [taskDir, processBound] of candidates) {
    if (!(await startupContract(taskDir))) continue;
    if (await activeTask(root, taskDir, expectedSession)) {
      matches.push(taskDir);
    } else if (
      processBound &&
      (await activeStrictTask(root, taskDir))
    ) {
      mismatches.push(taskDir);
    }
  }
  if (matches.length > 1) {
    throw new Error("clean_room_startup_gate_ambiguous_task");
  }
  const found = matches[0] || "";
  if (found) taskCache.set(expectedSession, found);
  if (!found && mismatches.length) {
    if (mismatches.length > 1) {
      throw new Error("clean_room_startup_gate_ambiguous_task");
    }
    if (input) {
      const startup = await startupContract(mismatches[0]);
      await writeContamination(
        mismatches[0],
        input,
        output,
        String(startup?.parent_path || ""),
      );
    }
    throw new Error("clean_room_startup_session_mismatch");
  }
  return found;
}


async function startupContract(taskDir) {
  const task = await readJson(path.join(taskDir, "task.json"));
  const startup = task?.task_contract?.clean_room_startup;
  return startup && typeof startup === "object" ? startup : null;
}


async function requiredStartupContract(taskDir) {
  const task = await readJsonRequired(
    path.join(taskDir, "task.json"),
    "clean_room_startup_task_metadata_invalid",
  );
  const startup = task?.task_contract?.clean_room_startup;
  if (!startup || typeof startup !== "object" || Array.isArray(startup)) {
    throw new Error("clean_room_startup_task_metadata_invalid");
  }
  return startup;
}


async function validatedParent(root, startup) {
  if (startup.mode !== "exact_parent_read") {
    throw new Error("clean_room_startup_contract_invalid_mode");
  }
  const parentPath = String(startup.parent_path || "");
  const expectedHash = String(startup.parent_sha256 || "");
  const runsRoot = path.join(
    root,
    ".omx",
    "framework",
    "team",
    "visible-runs",
  );
  const canonical = await realpath(parentPath);
  const metadata = await lstat(parentPath);
  if (
    canonical !== parentPath ||
    !metadata.isFile() ||
    !canonical.startsWith(`${runsRoot}${path.sep}`)
  ) {
    throw new Error("clean_room_startup_parent_invalid");
  }
  const actualHash = createHash("sha256")
    .update(await readFile(parentPath))
    .digest("hex");
  if (actualHash !== expectedHash) {
    throw new Error("clean_room_startup_parent_hash_mismatch");
  }
  return { parentPath, parentSha256: actualHash };
}


async function taskWorker(taskDir) {
  try {
    return (await readFile(path.join(taskDir, "owner"), "utf8")).trim();
  } catch {
    return "";
  }
}


function signalCommandKind(root, taskDir, worker, fence, command) {
  if (!command || /[\n;&|`<>]|\$\(/.test(command)) return false;
  const tokens = command
    .trim()
    .replace(/["']/g, "")
    .split(/\s+/);
  if (
    ![path.join(root, "bin", "omx_task_signal"), "./bin/omx_task_signal"].includes(
      tokens[0],
    )
  ) {
    return "";
  }
  const commandName = tokens[1];
  const flags = new Map();
  for (let index = 2; index < tokens.length; index += 2) {
    const flag = tokens[index];
    if (!flag?.startsWith("--") || flags.has(flag) || !tokens[index + 1]) {
      return "";
    }
    flags.set(flag, tokens[index + 1]);
  }
  if (
    flags.get("--root") !== root ||
    flags.get("--task-dir") !== taskDir ||
    flags.get("--worker") !== worker
  ) {
    return "";
  }
  if (["received", "started", "heartbeat"].includes(commandName)) {
    return flags.size === 3 ? "protocol" : "";
  }
  if (
    commandName === "finish" &&
    flags.size === 11 &&
    ["done", "fail", "blocked"].includes(flags.get("--state")) &&
    [
      ["--task-id", fence.task_id],
      ["--attempt-id", fence.attempt_id],
      ["--contract-hash", fence.contract_hash],
      ["--session-id", fence.session_id],
      ["--instance-id", fence.instance_id],
      ["--binding-generation", fence.binding_generation],
      ["--fencing-generation", fence.fencing_generation],
    ].every(([flag, expected]) =>
      flags.get(flag) === String(expected ?? ""),
    )
  ) {
    return `finish:${flags.get("--state")}`;
  }
  return "";
}


function isReportWrite(taskDir, input, output) {
  if (!["write", "edit", "apply_patch"].includes(String(input.tool || ""))) {
    return false;
  }
  return path.resolve(argumentPath(toolArgs(output))) ===
    path.join(taskDir, "report.md");
}


async function writeContamination(taskDir, input, output, expectedParent) {
  const args = toolArgs(output);
  await writeJsonExclusive(path.join(taskDir, "startup_contamination.json"), {
    schema_version: 1,
    evidence_kind: "clean_room_startup_contamination",
    status: "blocked",
    session_id: sessionId(input),
    call_id: callId(input),
    tool: String(input.tool || ""),
    attempted_command: commandText(args),
    attempted_path: argumentPath(args),
    expected_parent_path: expectedParent,
    attempted_at: new Date().toISOString(),
  });
}


async function validAdmission(taskDir, currentSession, parent) {
  const admission = await readJson(
    path.join(taskDir, "startup_admission.json"),
  );
  return (
    admission.schema_version === 1 &&
    admission.evidence_kind === "clean_room_startup_admission" &&
    admission.status === "admitted" &&
    String(admission.session_id || "") === currentSession &&
    Boolean(String(admission.call_id || "")) &&
    admission.tool === "read" &&
    admission.parent_path === parent.parentPath &&
    admission.parent_sha256 === parent.parentSha256
  );
}


async function beforeTool(root, input, output) {
  const currentSession = sessionId(input);
  if (!currentSession) return;
  const taskDir = await taskForSession(
    root,
    currentSession,
    input,
    output,
  );
  if (!taskDir) return;
  return serializeTask(taskDir, () =>
    enforceBeforeTool(root, taskDir, currentSession, input, output),
  );
}


async function enforceBeforeTool(
  root,
  taskDir,
  currentSession,
  input,
  output,
) {
  const startup = await startupContract(taskDir);
  if (!startup) return;
  const contaminationPath = path.join(taskDir, "startup_contamination.json");
  const admissionPath = path.join(taskDir, "startup_admission.json");
  const worker = await taskWorker(taskDir);
  const fence = await readJson(path.join(taskDir, "acceptance_fence.json"));
  const command = commandText(toolArgs(output));
  const signalKind = signalCommandKind(
    root,
    taskDir,
    worker,
    fence,
    command,
  );

  if (await exists(contaminationPath)) {
    if (
      isReportWrite(taskDir, input, output) ||
      (input.tool === "bash" &&
        ["protocol", "finish:blocked"].includes(signalKind))
    ) {
      return;
    }
    throw new Error("clean_room_startup_already_contaminated");
  }
  const parent = await validatedParent(root, startup);
  if (await exists(admissionPath)) {
    if (await validAdmission(taskDir, currentSession, parent)) return;
    await writeContamination(
      taskDir,
      input,
      output,
      parent.parentPath,
    );
    throw new Error("clean_room_startup_admission_invalid");
  }
  if (
    input.tool === "bash" &&
    signalKind === "protocol"
  ) {
    return;
  }
  if (
    input.tool === "read" &&
    isCompleteParentRead(toolArgs(output), parent.parentPath)
  ) {
    pendingReads.add(`${currentSession}:${callId(input)}`);
    return;
  }

  await writeContamination(
    taskDir,
    input,
    output,
    parent.parentPath,
  );
  throw new Error(
    `clean_room_startup_contamination:${String(input.tool || "unknown")}`,
  );
}


async function afterTool(root, input, output) {
  if (input.tool !== "read") return;
  const key = `${sessionId(input)}:${callId(input)}`;
  if (!pendingReads.delete(key)) return;
  const taskDir = await taskForSession(
    root,
    sessionId(input),
    input,
    { args: input.args || {} },
  );
  if (!taskDir) return;
  return serializeTask(taskDir, async () => {
    if (await exists(path.join(taskDir, "startup_contamination.json"))) return;
    const startup = await startupContract(taskDir);
    if (!startup) return;
    const parent = await validatedParent(root, startup);
    const metadata = output?.metadata;
    const display = metadata?.display;
    if (
      metadata?.truncated !== false ||
      !Array.isArray(metadata?.loaded) ||
      metadata.loaded.length !== 0 ||
      display?.type !== "file" ||
      path.resolve(String(display.path || "")) !== parent.parentPath ||
      Number(display.lineStart) !== 1 ||
      Number(display.lineEnd) !== Number(display.totalLines)
    ) {
      await writeContamination(
        taskDir,
        input,
        { args: input.args || {} },
        parent.parentPath,
      );
      throw new Error("clean_room_startup_parent_read_incomplete");
    }
    const admissionPath = path.join(taskDir, "startup_admission.json");
    await writeJsonExclusive(admissionPath, {
      schema_version: 1,
      evidence_kind: "clean_room_startup_admission",
      status: "admitted",
      session_id: sessionId(input),
      call_id: callId(input),
      tool: "read",
      parent_path: parent.parentPath,
      parent_sha256: parent.parentSha256,
      admitted_at: new Date().toISOString(),
    });
    if (!(await validAdmission(taskDir, sessionId(input), parent))) {
      await writeContamination(
        taskDir,
        input,
        { args: input.args || {} },
        parent.parentPath,
      );
      throw new Error("clean_room_startup_admission_invalid");
    }
  });
}


async function server() {
  const root = controlRoot();
  await writeReadiness(root);
  return {
    "tool.execute.before": async (input, output) =>
      beforeTool(root, input, output),
    "tool.execute.after": async (input, output) =>
      afterTool(root, input, output),
  };
}


export default {
  id: PLUGIN_ID,
  server,
};
