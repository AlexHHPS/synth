import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const handlers = new Map<string, Set<(event: {payload: any}) => void>>();
let enqueue: () => Promise<any>;
mock.module('@tauri-apps/api/event', () => ({listen: async (name: string, fn: any) => {
  if(!handlers.has(name))handlers.set(name,new Set());
  handlers.get(name)!.add(fn);
  return () => {handlers.get(name)?.delete(fn);};
}}));
mock.module('@tauri-apps/api/core', () => ({invoke: async (command: string) => {
  if (command === 'synth_enqueue_capture') return enqueue();
  if (command === 'get_recording_meeting_name') return 'Prueba sintética';
  if (command === 'get_recording_state') return {is_recording: false, is_paused: false, is_active: false};
  throw new Error('Unexpected native command');
}}));
mock.module('sonner', () => ({toast: {info() {}, error() {}}}));
const values = new Map<string, string>();
const storage = {getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value), removeItem: (key: string) => values.delete(key)};
Object.defineProperty(globalThis, 'localStorage', {configurable: true, value: storage});
Object.defineProperty(globalThis, 'window', {configurable: true, value: {dispatchEvent() {}}});
const {RecordingStateProvider, useRecordingState, RecordingStatus} = await import('../../src/contexts/RecordingStateContext');
const {CaptureHandoff} = await import('../../src/synth/CaptureHandoff');
const {CAPTURE_MODE, setCaptureIdentity, writeCapture, readCapture} = await import('../../src/synth/capture');
let state: ReturnType<typeof useRecordingState>;
function Probe() { state = useRecordingState(); return <CaptureHandoff/>; }
let renderer: ReactTestRenderer;
async function prepare() {
  setCaptureIdentity('handoff-user');
  writeCapture({title: 'Prueba sintética', folder_id: null, consent_confirmed: true, capture_id: 'capture-a'});
  storage.setItem(CAPTURE_MODE, 'synth');
  await act(async () => {renderer = create(<RecordingStateProvider><Probe/></RecordingStateProvider>);});
}
afterEach(async () => {await act(async () => renderer?.unmount()); handlers.clear(); values.clear(); setCaptureIdentity(null);});

test('accepted handoff leaves saving state after the provider status changes', async () => {
  let resolve!: (value: any) => void;
  enqueue = () => new Promise(done => {resolve = done;});
  await prepare();
  const setter = state.setStatus;
  const stopped = handlers.get('recording-stopped');
  await act(async () => stopped?.forEach(fn=>fn({payload: {capture_id: 'capture-a', folder_path: '/synthetic/closed', save_status: 'saved'}})));
  expect(state.status).toBe(RecordingStatus.SAVING);
  expect(state.setStatus).toBe(setter);
  expect(handlers.get('recording-stopped')).toBe(stopped);
  await act(async () => resolve({id: 'task-a'}));
  expect(state.status).toBe(RecordingStatus.COMPLETED);
  expect(readCapture()).toBeNull();
});

test('failed handoff exits saving and preserves the closed capture for retry', async () => {
  let reject!: (error: Error) => void;
  enqueue = () => new Promise((_, fail) => {reject = fail;});
  await prepare();
  await act(async () => handlers.get('recording-stopped')?.forEach(fn=>fn({payload: {capture_id: 'capture-a', folder_path: '/synthetic/closed', save_status: 'saved'}})));
  await act(async () => reject(new Error('Servicio temporalmente no disponible')));
  expect(state.status).toBe(RecordingStatus.ERROR);
  expect(state.statusMessage).toContain('Servicio temporalmente no disponible');
  expect(readCapture()?.folder_path).toBe('/synthetic/closed');
});
