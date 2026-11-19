'use client';

import type { LiveNote } from './useLiveNotes';

export function LiveNotesSwitch({ enabled, onChange }: { enabled: boolean; onChange: (value: boolean) => void }) {
  return <label className="meeting-consent"><input type="checkbox" checked={enabled} onChange={event => onChange(event.target.checked)} />
    <span>Analizar acuerdos durante la reunión. El texto se envía a Synth mientras grabas, no solo al terminar.</span></label>;
}

const STATUS: Record<LiveNote['status'], string> = {
  observed: '', proposed: 'Propuesta', confirmed: 'Acordado', withdrawn: 'Retirado', pending: 'Pendiente',
};
const GROUPS: { kind: LiveNote['kind']; title: string }[] = [
  { kind: 'decision', title: 'Decisiones' }, { kind: 'task', title: 'Tareas' }, { kind: 'fact', title: 'Datos' },
];

/** What the meeting has produced so far. Always labelled provisional. */
export function LiveNotes({ notes, updating, recording, enabled = true }: { notes: LiveNote[]; updating: boolean; recording: boolean; enabled?: boolean }) {
  if (!enabled) return <p className="meeting-empty">Desactivado. Las decisiones y tareas aparecerán en el acta al terminar.</p>;
  if (!notes.length) {
    return <p className="meeting-empty">{recording
      ? 'Las decisiones y tareas aparecerán aquí a medida que avance la reunión.'
      : 'Aquí verás decisiones y tareas mientras grabas.'}</p>;
  }
  return <div className="meeting-live-notes" aria-live="polite" aria-busy={updating}>
    {GROUPS.map(group => {
      const items = notes.filter(note => note.kind === group.kind);
      return items.length ? <section key={group.kind}><h3>{group.title}</h3><ul>
        {items.map(note => <li key={`${note.kind}:${note.text}`} className={note.status === 'withdrawn' ? 'is-withdrawn' : undefined}>
          <span>{note.text}</span>
          {(STATUS[note.status] || note.owner || note.due) && <small>{[STATUS[note.status], note.owner, note.due].filter(Boolean).join(' · ')}</small>}
        </li>)}
      </ul></section> : null;
    })}
  </div>;
}
