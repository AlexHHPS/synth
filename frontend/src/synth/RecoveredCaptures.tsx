'use client';

import { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Button } from './design-system/button';
import { type CaptureTask, message } from './api';

interface RecoveredCapture { capture_id: string; folder_path: string; title: string; started_at: string | null; duration_seconds: number }

function describe(capture: RecoveredCapture) {
  const minutes = Math.max(1, Math.round(capture.duration_seconds / 60));
  const started = capture.started_at ? new Date(capture.started_at) : null;
  const when = started && !Number.isNaN(started.getTime())
    ? started.toLocaleString('es-ES', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
  return `${capture.title}${when ? ` · ${when}` : ''} · ${minutes} min`;
}

/**
 * Meetings whose recording was interrupted (the app closed, the Mac slept)
 * and never reached processing. Their audio was sealed at launch; the person
 * decides whether to transcribe each one.
 */
export function RecoveredCaptures({ onQueued }: { onQueued: () => void }) {
  const [captures, setCaptures] = useState<RecoveredCapture[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    // Older native builds do not know this command: nothing to recover there.
    void invoke<RecoveredCapture[]>('synth_list_recovered_captures')
      .then(found => { if (active) setCaptures(found); }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  async function process(capture: RecoveredCapture) {
    setBusy(capture.capture_id); setError('');
    try {
      const task = await invoke<CaptureTask>('synth_enqueue_capture', {
        folderPath: capture.folder_path, title: capture.title, folderId: null, consentConfirmed: true, notes: null, calendarEventId: null,
      });
      setCaptures(old => old.filter(item => item.capture_id !== capture.capture_id));
      window.dispatchEvent(new CustomEvent('synth-capture-queued', { detail: task }));
      onQueued();
    } catch (failure) { setError(message(failure)); } finally { setBusy(null); }
  }
  if (!captures.length) return null;
  return <section className="voice-alert" role="status" aria-label="Grabaciones recuperadas">
    <strong>{captures.length === 1 ? 'Hay una grabación que no llegó a procesarse' : `Hay ${captures.length} grabaciones que no llegaron a procesarse`}</strong>
    <p>El audio se guardó en este Mac. Al procesarla confirmas que las personas que participaron fueron informadas y aceptaron.</p>
    {error && <p role="alert">{error}</p>}
    <ul>{captures.map(capture => <li key={capture.capture_id}>
      <span>{describe(capture)}</span>
      <Button size="sm" variant="outline" loading={busy === capture.capture_id} disabled={busy !== null} onClick={() => void process(capture)}>Procesar</Button>
    </li>)}</ul>
  </section>;
}
