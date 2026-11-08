'use client';

import { useEffect } from 'react';
import { listen, UnlistenFn } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { useRecordingState, RecordingStatus } from '@/contexts/RecordingStateContext';
import { CAPTURE_MODE, writeCapture, captureScope, handOffCapture, readCapture, setCaptureHandoffReady } from './capture';
import { message } from './api';

/** Persist the closed receipt before HTTP so a reload can recover the handoff. */
export function CaptureHandoff() {
  const { setStatus } = useRecordingState();
  useEffect(() => {
    let active = true;
    let unlisten: UnlistenFn | undefined;
    let unlistenStart: UnlistenFn | undefined;
    const failed = () => { if (active) { setCaptureHandoffReady(false); setStatus(RecordingStatus.ERROR, 'No se pudo preparar el cierre de captura. Reinicia la app antes de grabar.'); } };
    void listen<{ capture_id?: string }>('recording-started', event => {
      const scope = captureScope();
      const options = readCapture();
      if (scope && options && event.payload.capture_id) writeCapture({ ...options, capture_id: event.payload.capture_id });
      void invoke<string | null>('get_recording_meeting_name').then(name => {
        if (active && captureScope() === scope && name !== readCapture()?.title) localStorage.setItem(CAPTURE_MODE, 'legacy');
      }).catch(() => { /* Only a legacy-mode hint; native ownership remains authoritative. */ });
    }).then(fn => { if (active) { unlistenStart = fn; setCaptureHandoffReady(!!unlisten); } else fn(); }).catch(failed);
    void listen<{ folder_path?: string; save_status?: string; capture_id?: string }>('recording-stopped', event => {
      if (localStorage.getItem(CAPTURE_MODE) !== 'synth') return;
      const options = readCapture();
      if (!options || (options.capture_id && options.capture_id !== event.payload.capture_id)) return;
      const scope = captureScope();
      if (event.payload.save_status !== 'saved' || !event.payload.folder_path) {
        setStatus(RecordingStatus.ERROR, 'El audio no se ha cerrado correctamente. Revisa la captura antes de procesarla.');
        return;
      }
      writeCapture({ ...options, folder_path: event.payload.folder_path });
      setStatus(RecordingStatus.SAVING, 'Enviando la captura al procesamiento local…');
      void handOffCapture().then(() => {
        if (active && captureScope() === scope) setStatus(RecordingStatus.COMPLETED, 'Captura en cola. Puedes consultar su procesamiento.');
      }).catch(error => {
        if (active && captureScope() === scope) setStatus(RecordingStatus.ERROR, message(error));
      });
    }).then(fn => { if (active) { unlisten = fn; setCaptureHandoffReady(!!unlistenStart); } else fn(); }).catch(failed);
    return () => { active = false; setCaptureHandoffReady(false); unlisten?.(); unlistenStart?.(); };
  }, [setStatus]);
  return null;
}
