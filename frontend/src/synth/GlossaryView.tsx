'use client';

import { useCallback, useEffect, useState } from 'react';
import { Button } from './design-system/button';
import { message, request } from './api';
import { TaskList } from './ItemsPanel';
import type { AliasMode, GlossaryTerm, TaskRow } from './minutes';

/** Why a «suena como» corrects, only suggests or does nothing on its own. */
const ALIAS_REASON: Record<AliasMode['reason'], string> = {
  inventado: 'Corrige: no es una palabra en español, así que donde aparezca se cambia por el término.',
  palabra: 'Se sugiere: como también es una palabra o un nombre en español, solo se cambia si la reunión nombra el término.',
  aceptado: 'Corrige: se ha aceptado al menos dos veces y nunca se ha rechazado.',
  rechazado: 'Ya no corrige solo: se ha rechazado al menos dos veces y nunca se ha aceptado.',
  corto: 'No corrige porque tiene menos de tres letras, aunque sigue ayudando al reconocimiento de voz.',
  nombre: 'No corrige: es el nombre de otro término del glosario.',
  compartido: 'No corrige: es también «suena como» de otro término, así que no se sabe cuál es.',
};

export const KINDS: { id: string; label: string; hint: string }[] = [
  { id: 'persona', label: 'Personas', hint: 'Actores relevantes' },
  { id: 'organizacion', label: 'Organizaciones', hint: 'Clientes, proveedores y socios' },
  { id: 'producto', label: 'Productos', hint: '' },
  { id: 'proyecto', label: 'Proyectos', hint: 'Iniciativas' },
  { id: 'sistema', label: 'Sistemas y herramientas', hint: '' },
  { id: 'concepto', label: 'Conceptos', hint: 'Siglas, procesos y términos internos' },
];
const SINGULAR: Record<string, string> = {
  persona: 'Persona', organizacion: 'Organización', producto: 'Producto', proyecto: 'Proyecto', sistema: 'Sistema o herramienta', concepto: 'Concepto',
};

/** Why a proposal is still waiting: it sounds like a term already in the glossary, or the model was unsure of the name. */
function reason(term: GlossaryTerm, byId: Map<string, GlossaryTerm>): string {
  const alias = /^Posible alias de ([0-9a-f-]{36})$/.exec(term.definition);
  if (alias) return `Suena como «${byId.get(alias[1])?.term ?? 'un término del glosario'}»`;
  return term.origin === 'llm' ? 'Nombre dudoso en una transcripción' : 'Por revisar';
}

function KindSelect({ value, onChange, label }: { value: string; onChange: (kind: string) => void; label: string }) {
  return <select className="voice-input" aria-label={label} value={value} onChange={e => onChange(e.target.value)}>
    {KINDS.map(k => <option key={k.id} value={k.id}>{SINGULAR[k.id]}</option>)}
  </select>;
}

/** Company glossary: what each name the meetings use is, by category. Spellings merge by themselves;
 *  entities found in meetings arrive already added and can be edited or deleted; only doubtful ones wait here. */
export function GlossaryView() {
  const [terms, setTerms] = useState<GlossaryTerm[] | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [draft, setDraft] = useState({ term: '', kind: 'organizacion', definition: '' });
  const [editing, setEditing] = useState<{ id: string; term: string; kind: string; definition: string } | null>(null);
  const [mergeTarget, setMergeTarget] = useState<Record<string, string>>({});
  const load = useCallback(async () => {
    try { setTerms((await request<{ items: GlossaryTerm[] }>('GET', '/v1/glossary')).items); setError(''); }
    catch (e) { setError(message(e)); setTerms(old => old ?? []); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  async function act(id: string, run: () => Promise<unknown>) {
    setBusy(id); setError('');
    try { await run(); await load(); return true; } catch (e) { setError(message(e)); return false; } finally { setBusy(null); }
  }
  const byId = new Map((terms ?? []).map(t => [t.id, t]));
  const proposed = (terms ?? []).filter(t => t.status === 'propuesto');
  const approved = (terms ?? []).filter(t => t.status === 'aprobado');
  return <div className="notebook-detail">
    <header className="notebook-meeting-head"><p className="notebook-kicker">Compañía</p><h1>Glosario</h1>
      <p className="notebook-meeting-meta"><span>Quién y qué se nombra en las reuniones, y qué es. La transcripción los escribe bien y el acta los usa con su significado.</span></p></header>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {terms === null && <p role="status" aria-busy="true" className="notebook-muted">Cargando el glosario…</p>}
    {proposed.length > 0 && <section className="notebook-glossary-section">
      <h2>Por confirmar <small>{proposed.length}</small></h2>
      <ul className="notebook-glossary-list">{proposed.map(term => <li key={term.id} className="notebook-candidate">
        <div className="notebook-candidate-head"><strong>{term.term}</strong><span className="notebook-muted">{reason(term, byId)} · {term.occurrences} {term.occurrences === 1 ? 'vez' : 'veces'}</span></div>
        {term.examples.length > 0 && <ul className="notebook-examples">{term.examples.slice(0, 3).map((e, i) => <li key={i}>«{e}»</li>)}</ul>}
        <div className="notebook-candidate-actions">
          <Button size="xs" loading={busy === term.id} onClick={() => void act(term.id, () => request('POST', `/v1/glossary/${term.id}/approve`))}>Añadir</Button>
          <select className="voice-input" aria-label={`Fusionar ${term.term} como variante de`} value={mergeTarget[term.id] ?? ''}
            onChange={e => setMergeTarget({ ...mergeTarget, [term.id]: e.target.value })}>
            <option value="">Es una variante de…</option>{approved.map(a => <option key={a.id} value={a.id}>{a.term}</option>)}
          </select>
          <Button size="xs" variant="outline" disabled={!mergeTarget[term.id] || busy === term.id}
            onClick={() => void act(term.id, () => request('POST', `/v1/glossary/${term.id}/merge`, { into: mergeTarget[term.id] }))}>Fusionar</Button>
          <Button size="xs" variant="ghost" disabled={busy === term.id} onClick={() => void act(term.id, () => request('POST', `/v1/glossary/${term.id}/discard`))}>Descartar</Button>
        </div>
      </li>)}</ul>
    </section>}
    <section className="notebook-glossary-section">
      <form className="notebook-inline-form" onSubmit={e => { e.preventDefault(); if (draft.term.trim()) void act('new', () => request('POST', '/v1/glossary', { term: draft.term.trim(), kind: draft.kind, definition: draft.definition.trim() })).then(ok => { if (ok) setDraft({ ...draft, term: '', definition: '' }); }); }}>
        <input className="voice-input" aria-label="Término" placeholder="Término, p. ej. Valdivar" value={draft.term} maxLength={120} onChange={e => setDraft({ ...draft, term: e.target.value })} />
        <KindSelect label="Categoría del término nuevo" value={draft.kind} onChange={kind => setDraft({ ...draft, kind })} />
        <input className="voice-input" aria-label="Qué es" placeholder="Qué es, p. ej. cliente industrial" value={draft.definition} maxLength={500} onChange={e => setDraft({ ...draft, definition: e.target.value })} />
        <Button size="xs" type="submit" loading={busy === 'new'} disabled={!draft.term.trim()}>Añadir</Button>
      </form>
      {terms !== null && !approved.length && <p className="notebook-muted">Todavía no hay términos. Se añaden solos al procesar reuniones de una carpeta, o puedes añadirlos aquí.</p>}
    </section>
    {KINDS.map(kind => {
      const group = approved.filter(t => t.kind === kind.id);
      if (!group.length) return null;
      return <section key={kind.id} className="notebook-glossary-section">
        <h2>{kind.label} <small>{group.length}</small>{kind.hint && <span className="notebook-muted"> · {kind.hint}</span>}</h2>
        <dl className="notebook-approved">{group.map(term => <div key={term.id}>
          <dt>{term.term}</dt>
          {editing?.id === term.id ? <dd>
            <p className="notebook-muted">Si cambias el nombre, la grafía anterior queda como «suena como»; si ya existe otro término con ese nombre, se fusionan.</p>
            <form className="notebook-inline-form" onSubmit={e => { e.preventDefault(); void act(term.id, () => request('PATCH', `/v1/glossary/${term.id}`, { ...(editing.term.trim() && editing.term.trim() !== term.term ? { term: editing.term.trim() } : {}), kind: editing.kind, definition: editing.definition.trim() })).then(ok => { if (ok) setEditing(null); }); }}>
              <input className="voice-input" aria-label={`Nombre correcto de ${term.term}`} value={editing.term} maxLength={120} onChange={e => setEditing({ ...editing, term: e.target.value })} />
              <KindSelect label={`Categoría de ${term.term}`} value={editing.kind} onChange={k => setEditing({ ...editing, kind: k })} />
              <input className="voice-input" aria-label={`Qué es ${term.term}`} value={editing.definition} maxLength={500} onChange={e => setEditing({ ...editing, definition: e.target.value })} />
              <Button size="xs" type="submit" loading={busy === term.id}>Guardar</Button>
              <Button size="xs" variant="ghost" type="button" onClick={() => setEditing(null)}>Cancelar</Button>
            </form>
          </dd> : <dd>
            {term.definition || <span className="notebook-muted">Sin descripción</span>}
            {term.aliases.length > 0 && <span className="notebook-aliases">Suena como: {(term.alias_modes ?? term.aliases.map(alias => ({ alias, mode: 'corrige', reason: 'inventado' } as AliasMode)))
              .map((m, i) => <span key={m.alias}>{i > 0 && ', '}<span className={`notebook-alias is-${m.mode}`} title={ALIAS_REASON[m.reason]}>{m.alias}{m.mode !== 'corrige' && <small> · {m.mode === 'sugiere' ? 'se sugiere' : 'no corrige'}</small>}</span></span>)}</span>}
            <span className="notebook-term-actions">
              <Button size="xs" variant="ghost" onClick={() => setEditing({ id: term.id, term: term.term, kind: term.kind, definition: term.definition })}>Editar</Button>
              <Button size="xs" variant="ghost" disabled={busy === term.id} onClick={() => void act(term.id, () => request('POST', `/v1/glossary/${term.id}/discard`))}>Borrar</Button>
            </span>
          </dd>}
        </div>)}</dl>
      </section>;
    })}
  </div>;
}

/** «Mis tareas»: every open task assigned to me, across meetings. */
export function MyTasks({ onOpenMeeting }: { onOpenMeeting: (meetingId: string) => void }) {
  const [tasks, setTasks] = useState<TaskRow[] | null>(null);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    try { setTasks((await request<{ items: TaskRow[] }>('GET', '/v1/tasks')).items); setError(''); }
    catch (e) { setError(message(e)); setTasks(old => old ?? []); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="notebook-detail">
    <header className="notebook-meeting-head"><p className="notebook-kicker">Entre reuniones</p><h1>Mis tareas</h1>
      <p className="notebook-meeting-meta"><span>Lo que te comprometiste a hacer o te asignaron, con la reunión de origen.</span></p></header>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {tasks === null ? <p role="status" aria-busy="true" className="notebook-muted">Cargando tus tareas…</p>
      : tasks.length ? <TaskList tasks={tasks} showMeeting onOpen={task => onOpenMeeting(task.meeting_id)}
        onUpdate={async (task, change) => { await request('PATCH', `/v1/tasks/${task.id}`, change); await load(); }} />
      : <div className="voice-empty"><h2>No tienes tareas abiertas</h2><p>Cuando un acta te asigne algo, o digas «lo hago yo», aparecerá aquí.</p></div>}
  </div>;
}
