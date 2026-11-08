'use client';

import { useEffect, useState } from 'react';
import { type Page, request } from './api';
import { Button } from './design-system/button';
import type { SpeakerAction } from './TranscriptScript';

interface Person { id: string; name: string }
const EXTERNAL = 'external';

/** A human identity decision, independent of whether the person has a voice profile. */
export function SpeakerIdentityPicker({ label, name, busy, onSpeaker, onCancel }: {
  label: string; name: string; busy: boolean;
  onSpeaker: (action: SpeakerAction) => void; onCancel: () => void;
}) {
  const [people, setPeople] = useState<Person[]>([]);
  const [choice, setChoice] = useState('');
  const [externalName, setExternalName] = useState('');
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true); setFailed(false);
    request<Page<Person>>('GET', '/v1/people')
      .then(result => { if (active) setPeople(result.items); })
      .catch(() => { if (active) setFailed(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [retry]);
  const member = people.find(person => person.id === choice);
  const external = choice === EXTERNAL;
  return <form className="notebook-speaker-identity" data-slot="speaker-identity" onSubmit={event => {
    event.preventDefault();
    if (busy) return;
    if (external && externalName.trim()) onSpeaker({ action: 'rename', speaker_id: label, name: externalName.trim() });
    else if (member) onSpeaker({ action: 'confirm', speaker_id: label, name: member.name, employee_id: member.id });
  }}>
    <label className="notebook-identity-field">Persona
      <select className="voice-input" aria-label={`Persona para ${name}`} autoFocus value={choice} disabled={busy} onChange={event => setChoice(event.target.value)}>
        <option value="">Selecciona una persona</option>
        {people.length > 0 && <optgroup label="Miembros de la organización">{people.map(person => <option key={person.id} value={person.id}>{person.name}</option>)}</optgroup>}
        <option value={EXTERNAL}>Persona externa…</option>
      </select>
    </label>
    {external && <label className="notebook-identity-field">Nombre de la persona externa
      <input className="voice-input" aria-label={`Nombre externo para ${name}`} value={externalName} maxLength={200} disabled={busy} onChange={event => setExternalName(event.target.value)} />
    </label>}
    <div className="notebook-identity-actions">
      <Button size="xs" type="submit" disabled={busy || (external ? !externalName.trim() : !member)}>Guardar</Button>
      <Button size="xs" variant="ghost" type="button" disabled={busy} onClick={onCancel}>Cancelar</Button>
    </div>
    {loading && <p className="notebook-muted" role="status">Cargando miembros…</p>}
    {failed && <div className="notebook-identity-notice"><span role="alert">No se pudieron cargar los miembros.</span><Button size="xs" type="button" variant="ghost" disabled={busy} onClick={() => setRetry(value => value + 1)}>Reintentar</Button></div>}
    {!loading && !failed && !people.length && <p className="notebook-muted" role="status">No hay miembros disponibles.</p>}
  </form>;
}
