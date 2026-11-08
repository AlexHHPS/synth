'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import type { Transcript, TranscriptUpdate } from '@/types';
import { recordingService } from '@/services/recordingService';
import { message } from './api';

type SavedSegment = Omit<Transcript, 'timestamp'> & { display_time: string };

/** Native history is persisted before events; browser storage never owns the text. */
export function useLiveCapture() {
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [error, setError] = useState('');
  const [captureId, setCaptureId] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const epoch = useRef(0);
  const capture = useRef<string | null>(null);
  const clear = useCallback(() => {
    epoch.current++;
    capture.current = null;
    setCaptureId(null);
    setWaiting(false);
    setTranscripts([]);
    setError('');
  }, []);

  useEffect(() => {
    let disposed = false;
    const unlisten: UnlistenFn[] = [];
    let pending: TranscriptUpdate[] = [];
    let pendingState: {capture_id:string;state:string} | null = null;
    const key = (row: Pick<Transcript, 'capture_id' | 'block_id' | 'sequence_id'>) => `${row.capture_id}:${row.block_id ?? row.sequence_id}`;
    const merge = (items: Transcript[]) => setTranscripts(previous => {
      const rows = new Map(previous.map(row => [key(row), row]));
      for (const row of items) {
        const before = rows.get(key(row));
        if (before && ((row.revision ?? 0) < (before.revision ?? 0) ||
          (row.revision ?? 0) === (before.revision ?? 0) && (row.sequence_id ?? 0) < (before.sequence_id ?? 0))) continue;
        rows.set(key(row), row);
      }
      return [...rows.values()].sort((a, b) =>
        (a.audio_start_time ?? 0) - (b.audio_start_time ?? 0) || (a.sequence_id ?? 0) - (b.sequence_id ?? 0));
    });
    const row = (update: TranscriptUpdate): Transcript => ({ ...update, id: key(update) });
    const recover = async () => {
      const generation = ++epoch.current;
      capture.current = null;
      setCaptureId(null);
      setWaiting(false);
      pending = [];
      try {
        const state = await recordingService.getRecordingState();
        if (disposed || epoch.current !== generation || !state.capture_id) return;
        // This native command checks the capture owner against the current session.
        const history = await invoke<SavedSegment[]>('get_transcript_history');
        if (disposed || epoch.current !== generation) return;
        const valid = history.filter(segment => segment.capture_id === state.capture_id);
        capture.current = state.capture_id;
        setCaptureId(state.capture_id);
        setWaiting((pendingState?.capture_id === state.capture_id ? pendingState.state : state.transcription_state) === 'waiting');
        pendingState = null;
        const rows: Transcript[] = valid.map(segment => ({ ...segment, id: key(segment), timestamp: segment.display_time,
          is_partial: segment.evidence_state?.startsWith('provisional') ?? false }));
        rows.push(...pending.filter(update => update.capture_id === state.capture_id).map(row));
        pending = [];
        setTranscripts([]);
        merge(rows);
        setError('');
      } catch (e) {
        if (!disposed && epoch.current === generation) setError(message(e));
      }
    };
    const register = async <T,>(name: string, handler: (payload: T) => void) => {
      const remove = await listen<T>(name, event => { if (!disposed) handler(event.payload); });
      if (disposed) remove(); else unlisten.push(remove);
    };
    void (async () => {
      await register<TranscriptUpdate>('transcript-update', update => {
        if (!update.capture_id) return;
        if (update.capture_id === capture.current) merge([row(update)]);
        else if (capture.current === null) pending = [...pending.slice(-127), update];
      });
      await register<{capture_id:string;state:string}>('transcription-state', state => {
        if (!state.capture_id) return;
        if (state.capture_id === capture.current) setWaiting(state.state === 'waiting');
        else if (capture.current === null) pendingState = state;
      });
      if (disposed) return;
      await register('recording-started', () => { setTranscripts([]); void recover(); });
      if (!disposed) await recover();
    })().catch(e => { if (!disposed) setError(message(e)); });
    return () => { disposed = true; epoch.current++; capture.current = null; unlisten.forEach(remove => remove()); };
  }, []);

  // Keep empty final blocks internally so late provisional events cannot
  // resurrect removed words, while the visible transcript has no blank rows.
  return { transcripts: transcripts.filter(row => row.text.trim()), captureId, clear, error, waiting };
}
