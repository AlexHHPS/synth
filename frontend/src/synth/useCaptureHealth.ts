'use client';

import { useEffect, useState } from 'react';
import { listen } from '@tauri-apps/api/event';

export interface CaptureHealth { kind: string; source: 'microphone' | 'system' | null; message: string }

const FALLBACK = 'La grabación tiene un problema de audio. Comprueba el micrófono y el audio del ordenador.';

/**
 * What the native capture reports while a meeting is being recorded: a slow
 * disk, a source that went quiet, live transcription giving up. The recording
 * continues in every case, so this is a notice to act on, not an error state.
 */
export function useCaptureHealth(recording: boolean): CaptureHealth | null {
  const [health, setHealth] = useState<CaptureHealth | null>(null);
  useEffect(() => {
    if (!recording) { setHealth(null); return; }
    let active = true;
    let clear: ReturnType<typeof setTimeout> | undefined;
    const show = (next: CaptureHealth) => {
      if (!active) return;
      clearTimeout(clear);
      setHealth(next);
      // "Recovered" is good news: it replaces the warning and then leaves.
      if (next.kind === 'recovered') clear = setTimeout(() => { if (active) setHealth(null); }, 6000);
    };
    const stops = [
      listen<CaptureHealth>('synth-capture-health', event => show(event.payload)),
      // The native text of this older event is English; say it in Spanish.
      listen<string>('recording-error', () => show({ kind: 'recording_error', source: null, message: FALLBACK })),
    ];
    return () => { active = false; clearTimeout(clear); stops.forEach(stop => void stop.then(off => off()).catch(() => undefined)); };
  }, [recording]);
  return health;
}
