'use client';

import { useEffect, useRef, useState } from 'react';
import { Channel, invoke } from '@tauri-apps/api/core';
import { request } from './api';
import { mockEnabled } from './mock';
import type { Minutes, Topic } from './minutes';

interface Generation {
  id: string; job_id: string; transcript_version: number; result_transcript_version?: number | null;
  state: string; last_sequence: number;
}
export interface GenerationEvent {
  generation_id: string; transcript_version: number; sequence: number; kind: string;
  content: { index?: number; topic?: Topic; text?: string };
}
interface ChannelEvent { event: string; data: GenerationEvent & { state?: string; status?: number; code?: string } }
const invalidatesDraft = (code: string) => /generation_(superseded|access_denied|context_changed|unavailable)|generation_session_changed/.test(code);
export interface Draft {
  id: string; version: number; cursor: number; topics: Topic[]; text: string;
  phase: 'running' | 'reconnecting' | 'failed' | 'complete' | 'cancelled';
}

/** A cursor only advances on a contiguous, matching event. Duplicates are harmless. */
export function applyGenerationEvent(draft: Draft, event: GenerationEvent): Draft {
  if (event.generation_id !== draft.id || event.transcript_version !== draft.version || event.sequence <= draft.cursor) return draft;
  if (event.sequence !== draft.cursor + 1) throw new Error('generation_sequence_gap');
  const next = { ...draft, cursor: event.sequence };
  if (event.kind === 'topic') {
    const index = event.content.index, topic = event.content.topic;
    if (index == null || index < 0 || index > draft.topics.length || !topic || !Array.isArray(topic.points)) throw new Error('generation_topic_invalid');
    next.topics = [...draft.topics]; next.topics[index] = topic;
  } else if (event.kind === 'delta' && typeof event.content.text === 'string') next.text += event.content.text;
  else if (event.kind === 'complete') next.phase = 'complete';
  else if (event.kind === 'failed') next.phase = 'failed';
  return next;
}

export function draftMinutes(draft: Draft): Minutes {
  return { schema_version: 2, meeting_type: null, tldr: '', topics: draft.topics,
           tasks: [], decisions: [], open_questions: [] };
}

export function useGeneration(meetingId: string, version: number | undefined, enabled: boolean, onComplete: () => void) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const complete = useRef(onComplete); complete.current = onComplete;
  useEffect(() => {
    let active = true, current: Draft | null = null, failures = 0;
    setDraft(null); setError('');
    let timer: ReturnType<typeof setTimeout> | undefined;
    let wake: (() => void) | undefined;
    const wait = (ms: number) => new Promise<void>(resolve => { wake = resolve; timer = setTimeout(resolve, ms); });
    function receive(event: GenerationEvent) {
      if (!active || !current) return;
      current = applyGenerationEvent(current, event);
      setDraft(current);
    }
    async function run() {
      if (!enabled || !version) return;
      while (active) {
        try {
          const result = await request<{ generation: Generation | null }>('GET', `/v1/meetings/${meetingId}/generation`);
          if (!active) return;
          const generation = result.generation;
          if (!generation) { current = null; setDraft(null); await wait(750); continue; }
          if (generation.transcript_version !== version && generation.result_transcript_version !== version) {
            setDraft(null); await wait(750); continue;
          }
          if (!current || current.id !== generation.id) {
            current = { id: generation.id, version: generation.transcript_version, cursor: 0,
                        topics: [], text: '', phase: 'running' };
          }
          if (generation.state === 'complete') { complete.current(); return; }
          if (generation.state === 'failed' || generation.state === 'cancelled') {
            setDraft({ ...current, phase: generation.state }); return;
          }
          setError(''); current = { ...current, phase: 'running' }; setDraft(current);
          if (mockEnabled()) {
            // Browser fixture path, never used by the installed application.
            const page = await request<{ events: GenerationEvent[] }>('GET',
              `/v1/meetings/${meetingId}/generations/${current.id}/events?after=${current.cursor}`);
            for (const event of page.events) receive(event);
            await wait(750);
          } else {
            let fault: Error | null = null;
            const channel = new Channel<ChannelEvent>();
            channel.onmessage = event => {
              if (!active || fault) return;
              try {
                if (event.event === 'generation') receive(event.data);
                else if (event.event === 'error') throw new Error(event.data.code ?? 'generation_stream_unavailable');
                else if (event.event === 'status' && ['failed', 'cancelled'].includes(event.data.state ?? '')) {
                  current = { ...current!, phase: event.data.state as 'failed' | 'cancelled' }; setDraft(current);
                }
              } catch (e) {
                fault = e instanceof Error ? e : new Error('generation_stream_invalid');
                if (invalidatesDraft(String(fault))) { current = null; setDraft(null); }
              }
            };
            await invoke('synth_generation_stream', { meetingId, generationId: current.id, after: current.cursor, onEvent: channel });
            channel.onmessage = () => undefined;
            if (!active) return;
            if (fault) throw fault;
          }
          failures = 0;
          if (current?.phase === 'complete') { complete.current(); return; }
          if (current?.phase === 'failed' || current?.phase === 'cancelled') return;
        } catch (e) {
          if (!active) return;
          const code = String(e);
          if (invalidatesDraft(code)) {
            current = null; setDraft(null); complete.current(); return;
          }
          failures++;
          if (current) setDraft({ ...current, phase: 'reconnecting' });
          if (failures >= 5) { setError('Se interrumpió la conexión con el acta.'); return; }
          await wait(Math.min(500 * 2 ** failures, 4000));
        }
      }
    }
    void run();
    return () => { active = false; clearTimeout(timer); wake?.(); };
  }, [meetingId, version, enabled, retry]);
  return { draft, error, reconnect: () => setRetry(value => value + 1) };
}
