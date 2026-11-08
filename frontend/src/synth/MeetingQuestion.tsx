'use client';

import { useEffect, useRef, useState } from 'react';
import { ArrowUp, MessageSquareText } from 'lucide-react';
import { Button } from './design-system/button';
import { MeetingPanel } from './MeetingPanel';
import { message, request } from './api';
import type { QuestionResult } from './minutes';

export function MeetingQuestion({ meetingId, onOpenCitation }: { meetingId: string; onOpenCitation: (turnId: string) => void }) {
  const [question, setQuestion] = useState(''), [results, setResults] = useState<QuestionResult[]>([]);
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const current = useRef(meetingId); current.current = meetingId;
  const pending = results.find(result => result.state === 'queued' || result.state === 'running');
  useEffect(() => {
    if (!pending) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      void request<QuestionResult>('GET', `/v1/questions/${pending.id}`).then(next => {
        if (active) { setError(''); setResults(before => before.map(result => result.id === next.id ? next : result)); }
      }).catch(failure => { if (active) { setError(message(failure)); timer = setTimeout(poll, 1500); } });
    };
    timer = setTimeout(poll, 1500);
    return () => { active = false; clearTimeout(timer); };
  }, [pending]);
  useEffect(() => () => { current.current = ''; }, []);
  async function ask(event: React.FormEvent) {
    event.preventDefault();
    if (busy || pending || question.trim().length < 3) return;
    const expected = meetingId;
    setBusy(true); setOpen(true); setError('');
    try {
      const result = await request<QuestionResult>('POST', '/v1/questions', { question: question.trim(), meeting_id: meetingId });
      if (current.current === expected) { setResults(before => [...before, result].slice(-10)); setQuestion(''); }
    } catch (failure) { if (current.current === expected) setError(message(failure)); }
    finally { if (current.current === expected) setBusy(false); }
  }
  return <>
    <form className="meeting-ask" onSubmit={event => void ask(event)}><input aria-label="Pregunta sobre esta reunión" value={question} maxLength={500} placeholder="Pregunta sobre esta reunión…" onChange={event => setQuestion(event.target.value)} />
      {results.length > 0 && <Button type="button" variant="ghost" aria-label="Abrir conversación" aria-controls="meeting-chat" onClick={() => setOpen(true)}><MessageSquareText size={18} /></Button>}
      <Button type="submit" aria-label="Enviar pregunta" loading={busy} disabled={busy || !!pending || question.trim().length < 3}><ArrowUp size={18} /></Button>
    </form>
    <MeetingPanel id="meeting-chat" title="Preguntar a la reunión" open={open} onClose={() => setOpen(false)} footer="Respuestas con sus fuentes">
      {error && <p role="alert" className="voice-alert">{error}</p>}
      <ol className="meeting-chat-list">{results.map(result => <li key={result.id}><p className="meeting-chat-question">{result.question}</p>
        {result.state === 'answered' && result.answer ? <><p className="meeting-chat-answer">{result.answer.text}</p>
          {result.answer.citations.map(citation => <button type="button" key={citation.turn_id} className="notebook-source" onClick={() => { setOpen(false); onOpenCitation(citation.turn_id); }}>{citation.time} · {citation.speaker}</button>)}
        </> : result.state === 'failed' ? <p role="alert" className="voice-alert">No se pudo responder. Vuelve a enviar tu pregunta.</p> : <p role="status" className="meeting-empty">Consultando la conversación…</p>}
      </li>)}</ol>
      {busy && <p role="status">Preparando tu pregunta…</p>}
    </MeetingPanel>
  </>;
}
