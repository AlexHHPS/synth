'use client';

import { useEffect, useRef, useState } from 'react';
import type { Transcript } from '@/types';
import { request } from './api';

export interface LiveNote {
  kind: 'fact' | 'task' | 'decision';
  text: string;
  status: 'observed' | 'proposed' | 'confirmed' | 'withdrawn' | 'pending';
  owner: string | null;
  due: string | null;
  citations: string[];
}
interface Line { id: string; speaker: string; text: string }
interface Update { id: string; state: string; items?: LiveNote[] | null }

const EVERY_MS = 60_000;
const ENOUGH_CHARS = 500;
const MAX_LINES = 240;
const POLL_MS = 3_000;
const POLLS = 30;
export const LIVE_NOTES_SETTING = 'synth_voice_live_notes';

/** On unless the person turned it off: it sends the text to Synth during the meeting, not only at the end. */
export function liveNotesEnabled(): boolean {
  try { return localStorage.getItem(LIVE_NOTES_SETTING) !== 'off'; } catch { return true; }
}
export function setLiveNotesEnabled(value: boolean) {
  try { localStorage.setItem(LIVE_NOTES_SETTING, value ? 'on' : 'off'); } catch { /* storage unavailable: keep the default */ }
}

/** Lines not sent yet, plus the earlier ones the current notes rest on. */
export function nextLines(all: Line[], sent: ReadonlySet<string>, notes: LiveNote[]): { lines: Line[]; fresh: Line[] } {
  const cited = new Set(notes.flatMap(note => note.citations));
  const fresh = all.filter(line => !sent.has(line.id)).slice(0, MAX_LINES);
  const kept = all.filter(line => sent.has(line.id) && cited.has(line.id)).slice(-(300 - fresh.length));
  return { lines: [...kept, ...fresh], fresh };
}

/**
 * Decisions, tasks and facts as the meeting goes. Provisional by design: the
 * acta is written from the whole transcript at the end. Any failure is silent
 * and retried on the next turn; recording never depends on it.
 */
export function useLiveNotes(captureId: string | null, transcripts: Transcript[], recording: boolean, humanNotes: string, enabled = true) {
  const [notes, setNotes] = useState<LiveNote[]>([]);
  const [updating, setUpdating] = useState(false);
  const latest = useRef({ transcripts, humanNotes, notes });
  latest.current = { transcripts, humanNotes, notes };
  const names = useRef(new Map<string, string>());
  const sent = useRef(new Set<string>());

  useEffect(() => {
    names.current = new Map(); sent.current = new Set(); setNotes([]); setUpdating(false);
    if (!captureId || !recording || !enabled) return;
    let active = true, busy = false;
    const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
    const update = async () => {
      if (busy) return;
      const all: Line[] = latest.current.transcripts
        .filter(row => row.capture_id === captureId && !row.is_partial && row.text.trim())
        .map(row => {
          if (!names.current.has(row.id)) names.current.set(row.id, `l${names.current.size + 1}`);
          return { id: names.current.get(row.id)!, speaker: row.source === 'microphone' ? 'Tú' : 'Participante', text: row.text.trim().slice(0, 4000) };
        });
      const { lines, fresh } = nextLines(all, sent.current, latest.current.notes);
      if (fresh.reduce((total, line) => total + line.text.length, 0) < ENOUGH_CHARS) return;
      busy = true; setUpdating(true);
      try {
        const queued = await request<Update>('POST', '/v1/live-notes', {
          capture_id: captureId, lines, state: latest.current.notes.length ? latest.current.notes : null,
          notes: latest.current.humanNotes.slice(0, 4000),
        });
        for (let attempt = 0; active && attempt < POLLS; attempt++) {
          await sleep(POLL_MS);
          const result = await request<Update>('GET', `/v1/live-notes/${queued.id}`);
          if (result.state === 'answered' && result.items) {
            if (active) { setNotes(result.items); fresh.forEach(line => sent.current.add(line.id)); }
            break;
          }
          if (result.state === 'failed') break;
        }
      } catch {
        // Optional help: the next turn tries again with the same lines.
      } finally { busy = false; if (active) setUpdating(false); }
    };
    const timer = setInterval(() => void update(), EVERY_MS);
    return () => { active = false; clearInterval(timer); };
  }, [captureId, recording, enabled]);

  return { notes, updating };
}
