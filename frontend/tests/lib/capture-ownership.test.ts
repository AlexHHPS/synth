import { expect, mock, test } from 'bun:test';

let resolve!: (value: { id: string }) => void;
mock.module('@tauri-apps/api/core', () => ({ invoke: () => new Promise(done => { resolve = done; }) }));
const storage = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  removeItem: (key: string) => storage.delete(key),
} });
const events: Event[] = [];
Object.defineProperty(globalThis, 'window', { configurable: true, value: { dispatchEvent: (event: Event) => events.push(event) } });
const { readCapture, writeCapture, setCaptureIdentity, handOffCapture, CAPTURE_OPTIONS } = await import('../../src/synth/capture');

test('accounts cannot read each other’s draft and late handoff does not clear or notify the new account', async () => {
  storage.set(CAPTURE_OPTIONS, JSON.stringify({ title: 'Legacy unowned notes', consent_confirmed: true }));
  setCaptureIdentity(null);
  expect(readCapture()).toBeNull();
  setCaptureIdentity('user-a');
  expect(readCapture()).toBeNull();
  writeCapture({ title: 'A', notes: 'Notas A', consent_confirmed: true, folder_id: null, folder_path: '/fixture/a' });
  const pending = handOffCapture();
  expect(handOffCapture()).toBe(pending);
  setCaptureIdentity('user-b');
  expect(readCapture()).toBeNull();
  writeCapture({ title: 'B', notes: 'Notas B', consent_confirmed: true, folder_id: null, folder_path: '/fixture/b' });
  resolve({ id: 'task-a' });
  await pending;
  expect(readCapture()?.notes).toBe('Notas B');
  expect(events.length).toBe(0);
  setCaptureIdentity('user-a');
  expect(readCapture()?.notes).toBe('Notas A');
  expect(storage.has(CAPTURE_OPTIONS)).toBe(true);
});
