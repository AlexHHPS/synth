'use client';

import { useEffect, useState } from 'react';
import type { Transcript } from '@/types';
import { Button } from './design-system/button';
import { time } from './api';
import type { LiveVoiceMatch, LiveVoiceStatus } from './useLiveVoices';

/** Only adjacent human decisions may bridge windows; mixed voices stay uncertain. */
export function voiceForSegment(segment: Transcript, matches: LiveVoiceMatch[]) {
  const start = (segment.audio_start_time ?? 0)*1000, end = (segment.audio_end_time ?? 0)*1000;
  if (end <= start) return null;
  const hits = matches.filter(match => match.source === segment.source && match.speech_spans_ms.some(([a,b]) => Math.max(a,start) < Math.min(b,end)));
  if (!hits.length) return null;
  if (hits.length > 1) {
    const employee = hits[0].candidate_employee_id;
    if (!employee || hits.some(match => !match.confirmed || match.candidate_employee_id !== employee)) return null;
    for (let index = 0; index < hits.length; index++) {
      for (const other of hits.slice(index+1)) {
        if (hits[index].speech_spans_ms.some(([a,b]) => other.speech_spans_ms.some(([c,d]) => Math.max(a,c,start) < Math.min(b,d,end)))) return null;
      }
    }
  }
  const spans = hits.flatMap(match => match.speech_spans_ms).map(([a,b]) => [Math.max(a,start),Math.min(b,end)]).filter(([a,b]) => a < b).sort((a,b) => a[0]-b[0]);
  let covered = 0, previous = start;
  for (const [a,b] of spans) { covered += Math.max(0,b-Math.max(a,previous)); previous = Math.max(previous,b); }
  return covered >= .95*(end-start) ? hits[0] : null;
}

function VoiceRow({ match, profiles, excerpt, disabled, decide }: {
  match: LiveVoiceMatch; profiles: NonNullable<LiveVoiceStatus['eligible_profiles']>; excerpt: string; disabled: boolean;
  decide: (match: LiveVoiceMatch, profileId: string | null) => Promise<void>;
}) {
  const [choice, setChoice] = useState(match.profile_id ?? '');
  const signature = profiles.map(profile => profile.id).join('|');
  useEffect(() => {
    setChoice(before => profiles.some(p => p.id === before) ? before : match.profile_id ?? '');
  }, [signature, match.profile_id]); // Polling must not erase an in-progress choice.
  const at = match.source_ranges_ms[0]?.[0] ?? match.start_ms;
  return <li className="voice-live-person" data-slot="live-voice">
    <div className="voice-live-person-heading">
      <strong>{match.candidate_name ?? 'Voz sin identificar'}</strong>
      {match.candidate_name && <span className="voice-live-identity-state" data-confirmed={match.confirmed}>{match.confirmed ? 'Confirmada por ti' : 'Sugerida'}</span>}
      <time>{time(at)} · {match.source === 'microphone' ? 'Micrófono' : 'Ordenador'}</time>
    </div>
    {excerpt && <p className="voice-live-excerpt">{excerpt}</p>}
    {match.confirmed ? <Button variant="ghost" disabled={disabled} onClick={() => void decide(match, null)}>Deshacer identificación</Button> :
      match.can_identify && profiles.length > 0 ? <form className="voice-live-identify" onSubmit={event => { event.preventDefault(); if (choice) void decide(match, choice); }}>
        <label><span>Quién habla</span><select className="voice-input" value={choice} disabled={disabled} onChange={event => setChoice(event.target.value)}>
          <option value="">Elegir persona</option>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
        </select></label>
        <Button variant="outline" disabled={disabled || !choice} type="submit">{match.candidate_name && choice === match.profile_id ? 'Confirmar voz' : 'Identificar voz'}</Button>
      </form> : <span className="voice-notice">{match.can_identify ? 'No hay perfiles de voz disponibles.' : 'Esperando más voz para identificar.'}</span>}
  </li>;
}

export function LiveVoices({ status, error, saving, transcripts, refresh, decide, expectedParticipants = [] }: {
  status: LiveVoiceStatus | null; error: string; saving: string | null; transcripts: Transcript[];
  refresh: () => Promise<void>; decide: (match: LiveVoiceMatch, profileId: string | null) => Promise<void>;
  expectedParticipants?: string[];
}) {
  const [expanded, setExpanded] = useState(false);
  const matches = status?.matches ?? [];
  const recent = expanded ? matches : matches.slice(-4);
  const confirmed = matches.filter(match => match.confirmed).length;
  const unavailable = status?.state === 'unavailable';
  return <section className="voice-live-widget" aria-label="Voces" data-slot="live-voices">
    <details className="voice-live-voices" open>
    <summary><h2>Voces</h2><span>{error || unavailable ? 'No disponible' : matches.length ? `${confirmed} ${confirmed === 1 ? 'confirmada' : 'confirmadas'}` : 'Escuchando…'}</span></summary>
    {error ? <div className="voice-live-voice-error" role="status"><span>{error}</span><Button variant="outline" onClick={() => void refresh()}>Actualizar voces</Button></div> :
      unavailable ? <p className="voice-notice">La identificación de voces no está disponible. La grabación continúa.</p> :
      matches.length > 0 && <><p className="voice-notice">Una confirmación ayuda a reconocer esta voz durante la reunión.</p>
      <ul>{recent.map(match => <VoiceRow key={match.group_id} match={match} profiles={[...(status?.eligible_profiles ?? [])].sort((a, b) => Number(expectedParticipants.includes(b.employee_id)) - Number(expectedParticipants.includes(a.employee_id)))}
        disabled={saving !== null} decide={decide}
        excerpt={transcripts.filter(segment => voiceForSegment(segment, matches)?.group_id === match.group_id).slice(0,2).map(segment => segment.text).join(' ').slice(0,220)} />)}</ul>
      {matches.length > 4 && <Button variant="ghost" onClick={() => setExpanded(value => !value)}>{expanded ? 'Mostrar solo recientes' : 'Ver voces anteriores'}</Button>}
    </>}
  </details></section>;
}
