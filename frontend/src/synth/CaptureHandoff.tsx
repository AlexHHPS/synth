'use client';

import { useEffect } from 'react';
import { listen, UnlistenFn } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { useRecordingState, RecordingStatus } from '@/contexts/RecordingStateContext';
import { CAPTURE_MODE, CAPTURE_OPTIONS, handOffCapture, readCapture, setCaptureHandoffReady } from './capture';
import { message } from './api';

/** Persist the closed receipt before HTTP so a reload can recover the handoff. */
export function CaptureHandoff() {
  const { setStatus } = useRecordingState();
  useEffect(() => {
    let active = true;
    let unlisten: UnlistenFn | undefined;
    let unlistenStart: UnlistenFn | undefined;
    void listen('recording-started', () => {
      void invoke<string | null>('get_recording_meeting_name').then(name => {
        if (active && name !== readCapture()?.title) localStorage.setItem(CAPTURE_MODE, 'legacy');
      });
    }).then(fn => { if (active) unlistenStart = fn; else fn(); });
    void listen<{ folder_path?: string; save_status?: string }>('recording-stopped', event => {
      if (localStorage.getItem(CAPTURE_MODE) !== 'synth') return;
      const options = readCapture();
      if (!options) return;
      if (event.payload.save_status !== 'saved' || !event.payload.folder_path) {
        setStatus(RecordingStatus.ERROR, 'El audio no se ha cerrado correctamente. Revisa la captura antes de procesarla.');
        return;
      }
      localStorage.setItem(CAPTURE_OPTIONS, JSON.stringify({ ...options, folder_path: event.payload.folder_path }));
      setStatus(RecordingStatus.SAVING, 'Enviando la captura al procesamiento local…');
      void handOffCapture().then(() => {
        if (active) setStatus(RecordingStatus.COMPLETED, 'Captura en cola. Puedes consultar su procesamiento.');
      }).catch(error => {
        if (active) setStatus(RecordingStatus.ERROR, message(error));
      });
    }).then(fn => { if (active) { unlisten = fn; setCaptureHandoffReady(true); } else fn(); })
      .catch(() => { if (active) setStatus(RecordingStatus.ERROR, 'No se pudo preparar el cierre de captura. Reinicia la app antes de grabar.'); });
    return () => { active = false; setCaptureHandoffReady(false); unlisten?.(); unlistenStart?.(); };
  }, [setStatus]);
  return null;
}
