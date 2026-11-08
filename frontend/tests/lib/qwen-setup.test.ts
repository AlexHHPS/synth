import { expect, mock, test } from 'bun:test';

let status: any;
const calls: string[] = [];
mock.module('@tauri-apps/api/core', () => ({ invoke: async (name: string) => { throw new Error(`Setup must not require another ASR: ${name}`); } }));
mock.module('../../src/synth/api', () => ({
  request: async () => ({ name: 'Synthetic user' }),
  pipelineRequest: async (_method: string, path: string) => { calls.push(path); return status; },
}));
const { runChecks, readyToRecord } = await import('../../src/synth/setup');

test('a fresh Qwen installation needs no Whisper and checks local readiness once', async () => {
  status = { audio_setup: { state: 'ready' }, asr_backend: 'qwen3', document_route: 'configured_gateway' };
  calls.length = 0;
  const checks = await runChecks();
  expect(readyToRecord(checks)).toBe(true);
  expect(checks.find(row => row.key === 'model')?.value).toBe('Qwen3-ASR-1.7B');
  expect(calls).toEqual(['/v1/status']);
});

test('preparing, failed, old-host and wrong-backend responses cannot enable recording', async () => {
  for (const response of [{ audio_setup: { state: 'preparing' }, asr_backend: 'qwen3' },
    { audio_setup: { state: 'failed' }, asr_backend: 'qwen3' },
    { asr_backend: 'qwen3' }, { audio_setup: { state: 'ready' }, asr_backend: 'whisper' }]) {
    status = response;
    expect(readyToRecord(await runChecks())).toBe(false);
  }
});
