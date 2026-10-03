import { invoke } from '@tauri-apps/api/core';
import { CaptureTask } from './api';

export const CAPTURE_OPTIONS = 'synth_voice_capture_options';
export const CAPTURE_MODE = 'synth_voice_capture_mode';
export interface CaptureOptions {
  title: string; folder_id: string | null; consent_confirmed: true; folder_path?: string; notes?: string;
}
export function readCapture(): CaptureOptions | null {
  try {
    const value = JSON.parse(localStorage.getItem(CAPTURE_OPTIONS) ?? 'null');
    return value?.consent_confirmed === true && typeof value.title === 'string' ? value : null;
  } catch { return null; }
}
let handingOff: Promise<CaptureTask> | null = null;
let ready = false;
export function setCaptureHandoffReady(value: boolean) { ready = value; }
export function isCaptureHandoffReady() { return ready; }
export function handOffCapture(): Promise<CaptureTask> {
  if (handingOff) return handingOff;
  const options = readCapture();
  if (!options?.folder_path) return Promise.reject(new Error('No hay una captura cerrada para procesar.'));
  handingOff = invoke<CaptureTask>('synth_enqueue_capture', {
    folderPath: options.folder_path, title: options.title, folderId: options.folder_id, consentConfirmed: true, notes: options.notes ?? null,
  }).then(task => {
    localStorage.setItem('synth_voice_last_capture_task', task.id);
    localStorage.removeItem(CAPTURE_OPTIONS);
    window.dispatchEvent(new CustomEvent('synth-capture-queued', { detail: task }));
    return task;
  }).finally(() => { handingOff = null; });
  return handingOff;
}
