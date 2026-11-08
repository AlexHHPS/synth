import { invoke } from '@tauri-apps/api/core';
import { CaptureTask } from './api';

export const CAPTURE_OPTIONS = 'synth_voice_capture_options';
export const CAPTURE_MODE = 'synth_voice_capture_mode';
export interface CaptureOptions {
  title: string; folder_id: string | null; consent_confirmed: true; folder_path?: string; notes?: string; capture_id?: string;
  calendar_event_id?: string;
}
let identity: string | null = null;
let identityEpoch = 0;
export function setCaptureIdentity(value: string | null) {
  if (identity !== value) { identity = value; identityEpoch++; }
}
export function captureScope() { return identity === null ? null : `${identity}:${identityEpoch}`; }
function storageKey() { return identity === null ? null : `${CAPTURE_OPTIONS}:${identity}`; }
export function writeCapture(value: CaptureOptions) {
  const key = storageKey();
  if (!key) throw new Error('Inicia sesión antes de preparar una captura.');
  localStorage.setItem(key, JSON.stringify(value));
}
export function readCapture(): CaptureOptions | null {
  try {
    const key = storageKey();
    const value = JSON.parse(key ? localStorage.getItem(key) ?? 'null' : 'null');
    return value?.consent_confirmed === true && typeof value.title === 'string' ? value : null;
  } catch { return null; }
}
const handingOff = new Map<string, Promise<CaptureTask>>();
let ready = false;
export function setCaptureHandoffReady(value: boolean) { ready = value; }
export function isCaptureHandoffReady() { return ready; }
export function handOffCapture(): Promise<CaptureTask> {
  const key = storageKey(), scope = captureScope();
  const options = readCapture();
  if (!key || !scope || !options?.folder_path) return Promise.reject(new Error('No hay una captura cerrada para procesar.'));
  const operation = `${scope}:${options.folder_path}`;
  const pending = handingOff.get(operation);
  if (pending) return pending;
  const taskPromise = invoke<CaptureTask>('synth_enqueue_capture', {
    folderPath: options.folder_path, title: options.title, folderId: options.folder_id, consentConfirmed: true, notes: options.notes ?? null,
    calendarEventId: options.calendar_event_id ?? null,
  }).then(task => {
    if (captureScope() === scope && readCapture()?.folder_path === options.folder_path) {
      localStorage.setItem(`synth_voice_last_capture_task:${identity}`, task.id);
      localStorage.removeItem(key);
      window.dispatchEvent(new CustomEvent('synth-capture-queued', { detail: task }));
    }
    return task;
  }).finally(() => { handingOff.delete(operation); });
  handingOff.set(operation, taskPromise);
  return taskPromise;
}
