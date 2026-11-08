'use client';

import { useEffect, useMemo, useState } from 'react';
import { Button } from './design-system/button';
import { SpeakerIdentityPicker } from './SpeakerIdentityPicker';
import { type Correction, type TranscriptContent, type VoiceStatus, clock, markCorrections, turnsOf } from './minutes';

export type SpeakerAction =
  | { action: 'confirm'; speaker_id: string; name: string; employee_id?: string | null }
  | { action: 'reject'; speaker_id: string }
  | { action: 'rename'; speaker_id: string; name: string }
  | { action: 'merge'; speaker_id: string; into: string };

interface TranscriptScriptProps {
  content: TranscriptContent;
  speaker: (id: string | null) => string;
  voice: VoiceStatus | null;
  focused: string | null;
  scrollTo: string | null;
  busy: boolean;
  onSpeaker: (action: SpeakerAction) => void;
  onUndo: (correction: Correction) => void;
  onAccept: (correction: Correction) => void;
  onReject: (correction: Correction) => void;
  showSpeakerPrompts?: boolean;
}

/** The transcript as a script: speaker and time in the margin, one block per turn. */
export function TranscriptScript({ content, speaker, voice, focused, scrollTo, busy, onSpeaker, onUndo, onAccept, onReject, showSpeakerPrompts = true }: TranscriptScriptProps) {
  const turns = turnsOf(content);
  const corrections = content.corrections ?? [];
  const firstTurn = useMemo(() => {
    const seen = new Map<string, string>();
    for (const turn of turns) if (turn.speaker_id && !seen.has(turn.speaker_id)) seen.set(turn.speaker_id, turn.id);
    return seen;
  }, [turns]);
  const labels = useMemo(() => [...firstTurn.keys()], [firstTurn]);
  useEffect(() => {
    if (scrollTo) window.document.getElementById(`notebook-turn-${scrollTo}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }, [scrollTo]);
  if (!turns.length) return <p className="notebook-muted">La transcripción no tiene texto.</p>;
  return <div className="notebook-script">
    {turns.map(turn => {
      const label = turn.speaker_id;
      const suggestion = label && firstTurn.get(label) === turn.id && !content.speakers?.[label] ? voice?.matches?.[label] : undefined;
      const unresolved = label && firstTurn.get(label) === turn.id && !content.speakers?.[label] && !label.endsWith(':local');
      return <div key={turn.id} id={`notebook-turn-${turn.id}`} className={`notebook-line${focused === turn.id || scrollTo === turn.id ? ' is-focused' : ''}${turn.interjection ? ' is-aside' : ''}`}>
        <div className="notebook-line-margin"><span className="notebook-speaker">{speaker(label)}</span><time>{clock(turn.start_ms)}</time></div>
        <div className="notebook-line-body">
          <p>{markCorrections(turn.id, turn.text, corrections).map((part, i) => part.correction?.status === 'suggested'
            ? <mark key={i} className="notebook-suggested" title={`El glosario sugiere «${part.correction.term}»: «${part.correction.original}» también es una palabra`}>{part.text}
                <button type="button" className="notebook-suggestion" disabled={busy} onClick={() => onAccept(part.correction!)}>¿{part.correction.term}?</button>
                <button type="button" className="notebook-undo" disabled={busy} aria-label={`No, es «${part.correction.original}»`} onClick={() => onReject(part.correction!)}>no</button></mark>
            : part.correction
            ? <mark key={i} className="notebook-corrected" title={`Corregido por el glosario${part.correction.origin === 'contexto' ? ' (la reunión nombra el término)' : ''}: se oyó «${part.correction.original}»`}>{part.text}
                <button type="button" className="notebook-undo" disabled={busy} onClick={() => onUndo(part.correction!)}>deshacer</button></mark>
            : <span key={i}>{part.text}</span>)}</p>
          {unresolved && showSpeakerPrompts && <SpeakerPrompt label={label} name={speaker(label)} others={labels.filter(l => l !== label)} speakerName={speaker}
            suggestion={suggestion} busy={busy} onSpeaker={onSpeaker} />}
        </div>
      </div>;
    })}
  </div>;
}

interface SpeakerPromptProps {
  label: string; name: string; others: string[]; speakerName: (id: string | null) => string;
  suggestion?: VoiceStatus['matches'][string]; busy: boolean; onSpeaker: (action: SpeakerAction) => void;
}

export function SpeakerPrompt({ label, name, others, speakerName, suggestion, busy, onSpeaker }: SpeakerPromptProps) {
  const [mode, setMode] = useState<'idle' | 'rename' | 'merge'>('idle');
  const [value, setValue] = useState('');
  const candidate = suggestion && suggestion.state !== 'unknown' ? suggestion.candidate_name : null;
  return <div className="notebook-speaker-prompt" role="group" aria-label={`Identificar a ${name}`}>
    {candidate ? <>
      <span>{suggestion?.state === 'suggested' ? <>Probablemente <strong>{candidate}</strong></> : <>¿Es <strong>{candidate}</strong>?</>}</span>
      <Button size="xs" disabled={busy} onClick={() => onSpeaker({ action: 'confirm', speaker_id: label, name: candidate, employee_id: suggestion?.candidate_employee_id })}>Sí, es {candidate.split(' ')[0]}</Button>
      <Button size="xs" variant="ghost" disabled={busy} onClick={() => onSpeaker({ action: 'reject', speaker_id: label })}>No</Button>
    </> : <span>¿Quién es {name}?</span>}
    {mode === 'idle' && <>
      <Button size="xs" variant="ghost" disabled={busy} onClick={() => setMode('rename')}>Identificar persona</Button>
      {others.length > 0 && <Button size="xs" variant="ghost" disabled={busy} onClick={() => { setValue(''); setMode('merge'); }}>Es la misma persona que…</Button>}
    </>}
    {mode === 'rename' && <SpeakerIdentityPicker label={label} name={name} busy={busy} onSpeaker={onSpeaker} onCancel={() => setMode('idle')} />}
    {mode === 'merge' && <form className="notebook-inline-form" onSubmit={e => { e.preventDefault(); if (value) onSpeaker({ action: 'merge', speaker_id: label, into: value }); }}>
      <select className="voice-input" aria-label={`Unir ${name} con`} value={value} onChange={e => setValue(e.target.value)}>
        <option value="">Elige un hablante</option>{others.map(o => <option key={o} value={o}>{speakerName(o)}</option>)}
      </select>
      <Button size="xs" type="submit" disabled={busy || !value}>Unir</Button>
      <Button size="xs" variant="ghost" type="button" onClick={() => setMode('idle')}>Cancelar</Button>
    </form>}
  </div>;
}
