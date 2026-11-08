'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { pipelineRequest } from './api';

export interface LiveVoiceMatch {
  group_id: string; source: string; state: 'unknown' | 'suggested' | 'ask' | 'confirmed';
  candidate_name: string | null; candidate_employee_id: string | null; profile_id: string | null;
  confirmed: boolean; confirmation_id?: string; evidence_ids: string[]; can_identify: boolean;
  speech_spans_ms: number[][]; source_ranges_ms: number[][]; start_ms: number; end_ms: number;
}
export interface LiveVoiceStatus {
  capture_id: string; state: string; confirmation_revision?: number; matches: LiveVoiceMatch[];
  eligible_profiles?: { id: string; name: string; employee_id: string }[];
}
export function useLiveVoices(captureId: string | null) {
  const [status, setStatus] = useState<LiveVoiceStatus | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState<string | null>(null);
  const generation = useRef(0);
  const sequence = useRef(0);
  const writing = useRef(false);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const active = useRef<string | null>(null);

  useEffect(() => {
    const epoch = ++generation.current;
    let timer: ReturnType<typeof setTimeout>;
    let disposed = false;
    active.current = captureId;
    writing.current = false;
    setStatus(null); setError(''); setSaving(null);
    const load = async () => {
      if (!captureId || writing.current || disposed) return;
      const ticket = ++sequence.current;
      try {
        const next = await pipelineRequest<LiveVoiceStatus>('GET', '/v1/voice/captures/'+captureId);
        if (!disposed && generation.current === epoch && ticket === sequence.current && next.capture_id === captureId) {
          setStatus(next); setError('');
        }
      } catch {
        if (!disposed && generation.current === epoch && ticket === sequence.current) {
          // A failed permission/catalog refresh cannot leave an old name visible.
          setStatus(null); setError('No se pudieron actualizar las voces.');
        }
      }
    };
    refreshRef.current = load;
    const tick = async () => {
      await load();
      if (!disposed) timer = setTimeout(() => void tick(), 10000);
    };
    if (captureId) void tick();
    return () => { disposed = true; generation.current++; active.current = null; clearTimeout(timer); };
  }, [captureId]);

  const refresh = useCallback(() => refreshRef.current(), []);
  const decide = useCallback(async (match: LiveVoiceMatch, profileId: string | null) => {
    if (!captureId || active.current !== captureId || writing.current || !status) return;
    const epoch = generation.current;
    writing.current = true; sequence.current++;
    setSaving(match.group_id); setError('');
    try {
      const next = await pipelineRequest<LiveVoiceStatus>('POST', '/v1/voice/captures/'+captureId+'/speakers', {
        action: profileId === null ? 'clear' : 'confirm', group_id: match.group_id, profile_id: profileId,
        evidence_ids: match.evidence_ids, expected_revision: status.confirmation_revision ?? 0,
        decision_id: crypto.randomUUID(), confirmed_by_user: true,
      });
      if (generation.current === epoch && active.current === captureId && next.capture_id === captureId) setStatus(next);
    } catch {
      if (generation.current === epoch) {
        setStatus(null); setError('No se pudo guardar la identificación. Actualiza las voces.');
      }
    } finally {
      if (generation.current === epoch) { writing.current = false; setSaving(null); }
    }
  }, [captureId, status]);

  return { status: status?.capture_id === captureId ? status : null, error, saving, refresh, decide };
}
