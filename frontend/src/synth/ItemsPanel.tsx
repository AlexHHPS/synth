'use client';

import { useState } from 'react';
import { DECISION_STATES, type DecisionRow, type TaskRow, type Turn, clock } from './minutes';

interface TaskListProps {
  tasks: TaskRow[];
  cite?: Map<string, Turn>;
  speaker?: (id: string | null) => string;
  showMeeting?: boolean;
  onUpdate: (task: TaskRow, change: Partial<Pick<TaskRow, 'status' | 'owner_name' | 'due_date' | 'title'>>) => Promise<void>;
  onOpen?: (task: TaskRow, turnId?: string) => void;
}

const EVIDENCE: Record<string, string> = { autocompromiso: 'se comprometió', explicito: 'nombrado', inferido: 'inferido', ninguno: 'sin asignar' };

/** Tasks as an editable checklist: owner, date and where it came from. */
export function TaskList({ tasks, cite, speaker, showMeeting, onUpdate, onOpen }: TaskListProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState({ owner: '', due: '' });
  const [busy, setBusy] = useState<string | null>(null);
  async function apply(task: TaskRow, change: Parameters<TaskListProps['onUpdate']>[1]) {
    setBusy(task.id);
    try { await onUpdate(task, change); } finally { setBusy(null); }
  }
  if (!tasks.length) return <p className="notebook-muted">No hay tareas.</p>;
  return <ul className="notebook-tasks">
    {tasks.map(task => {
      const source = task.citations.map(id => cite?.get(id)).find(Boolean);
      const done = task.status === 'hecha';
      return <li key={task.id} className={`notebook-task${done ? ' is-done' : ''}${task.status === 'descartada' ? ' is-dropped' : ''}`}>
        <input type="checkbox" checked={done} disabled={busy === task.id || task.status === 'descartada'} aria-label={`Marcar «${task.title}» como hecha`}
          onChange={() => void apply(task, { status: done ? 'abierta' : 'hecha' })} />
        <div className="notebook-task-body">
          <p className="notebook-task-title">{task.title}</p>
          {editing === task.id ? <form className="notebook-inline-form" onSubmit={e => { e.preventDefault(); void apply(task, { owner_name: draft.owner.trim() || undefined, due_date: draft.due || undefined }).then(() => setEditing(null)); }}>
            <input className="voice-input" aria-label="Responsable" value={draft.owner} maxLength={200} onChange={e => setDraft({ ...draft, owner: e.target.value })} />
            <input className="voice-input" type="date" aria-label="Fecha" value={draft.due} onChange={e => setDraft({ ...draft, due: e.target.value })} />
            <button type="submit" className="notebook-link">Guardar</button><button type="button" className="notebook-link" onClick={() => setEditing(null)}>Cancelar</button>
          </form> : <p className="notebook-task-meta">
            <span>{task.owner_name ?? 'Sin asignar'}<small> · {EVIDENCE[task.owner_evidence] ?? task.owner_evidence}</small></span>
            <span>{task.due_date ? new Date(task.due_date + 'T00:00:00').toLocaleDateString('es-ES', { weekday: 'short', day: 'numeric', month: 'short' }) : task.due_text ?? 'Sin fecha'}</span>
            {showMeeting && task.meeting_title && <button type="button" className="notebook-link" onClick={() => onOpen?.(task)}>{task.meeting_title}</button>}
            {!showMeeting && source && speaker && <button type="button" className="notebook-link" onClick={() => onOpen?.(task, source.id)}>{clock(source.start_ms)} · {speaker(source.speaker_id)}</button>}
            <button type="button" className="notebook-link" onClick={() => { setEditing(task.id); setDraft({ owner: task.owner_name ?? '', due: task.due_date ?? '' }); }}>Editar</button>
            {task.status !== 'descartada' && <button type="button" className="notebook-link" onClick={() => void apply(task, { status: 'descartada' })}>Descartar</button>}
          </p>}
        </div>
      </li>;
    })}
  </ul>;
}

interface DecisionListProps {
  decisions: DecisionRow[];
  onUpdate: (decision: DecisionRow, change: Partial<Pick<DecisionRow, 'status' | 'reason'>>) => Promise<void>;
}

export function DecisionList({ decisions, onUpdate }: DecisionListProps) {
  if (!decisions.length) return <p className="notebook-muted">No se registraron decisiones.</p>;
  return <ul className="notebook-decisions">
    {decisions.map(decision => <li key={decision.id} className="notebook-decision">
      <div>
        <p className="notebook-task-title">{decision.decision}</p>
        <p className="notebook-task-meta">{decision.reason ? <>Motivo: {decision.reason}</> : <em>Sin motivo dicho en la reunión</em>}</p>
      </div>
      <select className="notebook-status" aria-label={`Estado de «${decision.decision}»`} value={decision.status}
        onChange={e => void onUpdate(decision, { status: e.target.value as DecisionRow['status'] })}>
        {DECISION_STATES.map(s => <option key={s} value={s}>{s}</option>)}
      </select>
    </li>)}
  </ul>;
}
