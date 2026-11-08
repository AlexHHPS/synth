'use client';

import { useEffect, useState } from 'react';
import { Button } from './design-system/button';
import { type Folder, message, request } from './api';
import type { QuestionResult } from './minutes';

interface AskViewProps { folders: Folder[]; onOpenMeeting: (meetingId: string) => void }

const EXAMPLES = ['¿Por qué elegimos Nimbus?', '¿Qué fecha se acordó para el piloto?', '¿Quién revisa los permisos?'];

/** Ask the meetings: an answer with its sources, or «No consta». Only meetings you can read are searched. */
export function AskView({ folders, onOpenMeeting }: AskViewProps) {
  const [question, setQuestion] = useState('');
  const [folderId, setFolderId] = useState('');
  const [results, setResults] = useState<QuestionResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pending = results.find(r => r.state === 'queued' || r.state === 'running');

  useEffect(() => {
    // Deep link `?q=…` asks right away (also used to preview an answered state).
    const linked = new URLSearchParams(window.location.search).get('q');
    if (linked) void ask(linked);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!pending) return;
    const timer = setTimeout(async () => {
      try {
        const next = await request<QuestionResult>('GET', `/v1/questions/${pending.id}`);
        setResults(old => old.map(r => r.id === next.id ? next : r));
      } catch (e) { setError(message(e)); }
    }, 1500);
    return () => clearTimeout(timer);
  }, [pending]);

  async function ask(text: string) {
    if (text.trim().length < 3) return;
    setBusy(true); setError('');
    try {
      const asked = await request<QuestionResult>('POST', '/v1/questions', { question: text.trim(), ...(folderId ? { folder_id: folderId } : {}) });
      setResults(old => [asked, ...old].slice(0, 10)); setQuestion('');
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  return <div className="notebook-detail">
    <header className="notebook-meeting-head"><p className="notebook-kicker">Entre reuniones</p><h1>Preguntar</h1>
      <p className="notebook-meeting-meta"><span>Respuestas con sus fuentes, solo de las reuniones que puedes leer.</span></p></header>
    <form className="notebook-ask" onSubmit={e => { e.preventDefault(); void ask(question); }}>
      <textarea className="voice-textarea" aria-label="Tu pregunta" rows={2} maxLength={500} value={question} placeholder="¿Qué se decidió sobre…?"
        onChange={e => setQuestion(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void ask(question); } }} />
      <div className="notebook-ask-row">
        <select className="voice-input" aria-label="Dónde buscar" value={folderId} onChange={e => setFolderId(e.target.value)}>
          <option value="">Todas mis reuniones</option>{folders.map(f => <option key={f.id} value={f.id}>{f.name}</option>)}
        </select>
        <Button type="submit" size="sm" loading={busy} disabled={question.trim().length < 3}>Preguntar</Button>
      </div>
    </form>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {!results.length && <div className="notebook-ask-examples"><p className="notebook-muted">Por ejemplo:</p>
      {EXAMPLES.map(example => <button key={example} type="button" className="notebook-link" onClick={() => setQuestion(example)}>{example}</button>)}</div>}
    <ol className="notebook-answers">{results.map(result => <li key={result.id} className="notebook-answer">
      <p className="notebook-question">{result.question}</p>
      {result.state === 'answered' && result.answer ? <>
        <p className={result.answer.status === 'no_consta' ? 'notebook-muted' : 'notebook-answer-text'}>{result.answer.text}</p>
        {result.answer.citations.length > 0 && <ul className="notebook-points">{result.answer.citations.map(c => <li key={c.meeting_id + c.turn_id} className="notebook-point">
          <p>«{c.text}»</p>
          <aside className="notebook-margin" aria-label="Fuente">
            <button type="button" className="notebook-source" onClick={() => onOpenMeeting(c.meeting_id)}>
              <span className="notebook-source-time">{c.time}</span> {c.speaker}<br />{c.meeting_title} · {new Date(c.meeting_date + 'T00:00:00').toLocaleDateString('es-ES', { day: 'numeric', month: 'short' })}
            </button>
          </aside>
        </li>)}</ul>}
      </> : result.state === 'failed'
        ? <p className="voice-alert" role="alert">No se pudo responder ahora. Vuelve a preguntar en unos minutos.</p>
        : <p className="notebook-muted" role="status" aria-busy="true">Buscando en las reuniones…</p>}
    </li>)}</ol>
  </div>;
}
