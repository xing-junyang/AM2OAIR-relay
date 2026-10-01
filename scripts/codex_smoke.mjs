#!/usr/bin/env node
// Run in a disposable Codex CLI container with this repository mounted read-only.
// The relay key and the host's ~/.codex must never be mounted in this container.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const baseURL = process.env.AM2OAIR_QA_BASE_URL || 'http://relay:8787';
const clientModel = process.env.AM2OAIR_QA_CLIENT_MODEL || 'claude-opus-5-5';
const workspace = await mkdtemp(join(tmpdir(), 'am2oair-codex-smoke-'));
const timeoutMs = Number(process.env.AM2OAIR_QA_TIMEOUT_MS || 180000);
const mode = process.env.AM2OAIR_QA_SANDBOX || 'workspace-write';

async function stats() {
  const response = await fetch(baseURL + '/api/admin/stats');
  if (!response.ok) throw new Error('admin_stats_unavailable');
  return (await response.json()).totals;
}

try {
  const before = await stats();
  const args = ['exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
    '--skip-git-repo-check', '--sandbox', mode, '--json', '-C', workspace,
    '-c', 'model_provider="am2oair_smoke"', '-c', 'model=' + JSON.stringify(clientModel),
    '-c', 'model_providers.am2oair_smoke={name="AM2OAIR smoke",base_url=' + JSON.stringify(baseURL + '/v1') + ',wire_api="responses",requires_openai_auth=false,supports_websockets=false,request_max_retries=0,stream_max_retries=0}',
    '-c', 'web_search="disabled"', '-c', 'model_reasoning_summary="none"',
    '-c', 'model_supports_reasoning_summaries=false', '-c', 'approval_policy="never"',
    'Use a local tool to create codex-smoke.txt in the working directory with exactly RELAY_OK followed by a newline. Use a local tool to read that file and verify its contents. Do not delegate, access the network, or touch any other directory. Finish with OK.'];
  const child = spawn('codex', args, { env: { ...process.env, NO_PROXY: 'relay,127.0.0.1,localhost', no_proxy: 'relay,127.0.0.1,localhost' } });
  child.stdin.end();
  let pending = '';
  const itemTypes = new Set();
  let toolCompletions = 0;
  let taskComplete = false;
  let protocolFailure = false;
  const consume = line => {
    try {
      const event = JSON.parse(line);
      const item = event.item;
      if (item?.type) itemTypes.add(item.type);
      if (event.type === 'item.completed' && item?.type === 'command_execution' && item.exit_code === 0) toolCompletions++;
      if (event.type === 'item.completed' && item?.type === 'file_change' && item.status === 'completed') toolCompletions++;
      if (event.type === 'turn.completed') taskComplete = true;
      if (event.type === 'turn.failed' || event.type === 'error') protocolFailure = true;
    } catch { /* Never print or persist arbitrary CLI output. */ }
  };
  child.stdout.on('data', data => {
    pending += data.toString();
    let end;
    while ((end = pending.indexOf('\n')) >= 0) {
      consume(pending.slice(0, end));
      pending = pending.slice(end + 1);
    }
  });
  child.stderr.on('data', () => {});
  const timer = setTimeout(() => child.kill('SIGTERM'), timeoutMs);
  const exitCode = await new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('close', resolve);
  });
  clearTimeout(timer);
  if (pending) consume(pending);
  const after = await stats();
  const contentOK = await readFile(join(workspace, 'codex-smoke.txt'), 'utf8').then(text => text === 'RELAY_OK\n', () => false);
  const passed = exitCode === 0 && taskComplete && !protocolFailure && contentOK && toolCompletions >= 1 && after.successes > before.successes;
  console.log(JSON.stringify({ codex_cli_smoke: passed ? 'passed' : 'failed', exit_code: exitCode, task_completed: taskComplete, file_verified: contentOK, successful_tool_items: toolCompletions, item_types: [...itemTypes], new_requests: after.requests - before.requests, new_failures: after.failures - before.failures, total_tokens: after.total_tokens - before.total_tokens }));
  if (!passed) process.exitCode = 1;
} catch (error) {
  console.error('Codex smoke failed; no sensitive CLI output printed (' + error.constructor.name + ')');
  process.exitCode = 1;
} finally {
  await rm(workspace, { recursive: true, force: true });
}
