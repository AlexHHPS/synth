'use client';

import { Fragment } from 'react';
import { MEETING_TYPES, type Minutes, type Point, type Turn, clock } from './minutes';

interface MinutesDocumentProps {
  minutes: Minutes;
  cite: Map<string, Turn>;
  speaker: (id: string | null) => string;
  onFocus: (turnId: string | null) => void;
  onOpen: (turnId: string) => void;
}

/** The minutes read as a printed page; each point's sources sit in the right margin. */
export function MinutesDocument({ minutes, cite, speaker, onFocus, onOpen }: MinutesDocumentProps) {
  const kind = minutes.meeting_type ? MEETING_TYPES[minutes.meeting_type] ?? minutes.meeting_type : null;
  const agreed=minutes.decisions.filter(item=>item.status==='acordada');
  const pending=minutes.decisions.filter(item=>item.status!=='acordada');
  return <article className="notebook-page" aria-label="Acta">
    {(kind || minutes.tldr) && <header className="notebook-page-head">
      {kind && <p className="notebook-kicker">{kind}</p>}
      <p className="notebook-tldr">{minutes.tldr}</p>
    </header>}
    {minutes.topics.map((topic, index) => <section className="notebook-topic" key={`${topic.title}-${index}`}>
      <h2><span className="notebook-topic-number">{String(index + 1).padStart(2, '0')}</span>{topic.title}</h2>
      {!topic.points.length && <p className="notebook-prose">{topic.outcome || topic.context || topic.discussion}</p>}
      {topic.points.length > 0 && <ul className="notebook-points">
        {topic.points.map((point, i) => <PointRow key={i} point={point} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen} />)}
      </ul>}
      {(topic.points.length>0 ? [topic.context,topic.discussion,topic.outcome].some(Boolean) : [topic.context,topic.discussion,topic.outcome].filter(Boolean).length>1) &&
        <details className="notebook-topic-depth"><summary>Ver contexto y desarrollo</summary>
          {[topic.context,topic.discussion,topic.outcome].filter(Boolean).map((paragraph,i)=><p className="notebook-prose" key={i}>{paragraph}</p>)}
        </details>}
    </section>)}
    {minutes.decisions.length > 0 && <section className="notebook-topic">
      <h2><span className="notebook-topic-number">§</span>Acuerdos</h2>
      {agreed.length>0 ? <ul className="notebook-points">{agreed.map(d => <PointRow key={d.key} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen}
        point={{ text: d.decision + (d.reason ? ` — porque ${d.reason.replace(/^porque\s+/i, '')}` : ''), citations: d.citations }}
        badge={d.status} />)}</ul> : <p className="notebook-muted">No hay acuerdos confirmados en esta acta.</p>}
      {pending.length>0 && <details className="notebook-topic-depth"><summary>Propuestas y decisiones pendientes · {pending.length}</summary>
        <ul className="notebook-points">{pending.map(d=><PointRow key={d.key} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen}
          point={{text:d.decision+(d.reason?` — ${d.reason}`:''),citations:d.citations}} badge={d.status} />)}</ul>
      </details>}
    </section>}
    {(minutes.continuity ?? []).length > 0 && <section className="notebook-topic" aria-label="Pendientes de reuniones anteriores">
      <h2><span className="notebook-topic-number">↺</span>Pendientes de reuniones anteriores</h2>
      <ul className="notebook-points">{[...minutes.continuity!].sort((a, b) => Number(a.status === 'sin mencionar') - Number(b.status === 'sin mencionar'))
        .map(item => <PointRow key={item.key} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen}
          point={{ text: `${item.kind === 'tarea' ? 'Tarea' : 'Decisión'}: ${item.text}`, citations: item.citations }} badge={item.status}
          emptyNote={item.status === 'sin mencionar' ? 'No se habló de ello' : undefined} />)}</ul>
    </section>}
    {minutes.open_questions.length > 0 && <section className="notebook-topic">
      <h2><span className="notebook-topic-number">?</span>Dudas abiertas</h2>
      <ul className="notebook-points">{minutes.open_questions.map((q, i) => <PointRow key={i} point={q} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen} />)}</ul>
    </section>}
    {!minutes.topics.length && !minutes.decisions.length && <p className="notebook-muted">El acta no recoge temas para esta reunión.</p>}
  </article>;
}

interface PointRowProps {
  point: Point; cite: Map<string, Turn>; speaker: (id: string | null) => string;
  onFocus: (turnId: string | null) => void; onOpen: (turnId: string) => void; badge?: string; emptyNote?: string;
}

function PointRow({ point, cite, speaker, onFocus, onOpen, badge, emptyNote }: PointRowProps) {
  const sources = point.citations.map(id => cite.get(id)).filter((t): t is Turn => Boolean(t));
  return <li className="notebook-point">
    <p>{point.text}{badge && <span className="notebook-badge" data-status={badge}>{badge}</span>}</p>
    <aside className="notebook-margin" aria-label="Fuentes">
      {sources.map((turn, i) => <Fragment key={`${turn.id}-${i}`}>
        <button type="button" className="notebook-source" title={turn.text}
          onMouseEnter={() => onFocus(turn.id)} onMouseLeave={() => onFocus(null)} onFocus={() => onFocus(turn.id)} onBlur={() => onFocus(null)}
          onClick={() => onOpen(turn.id)}>
          <span className="notebook-source-time">{clock(turn.start_ms)}</span> {speaker(turn.speaker_id)}
        </button>
      </Fragment>)}
      {!sources.length && <span className="notebook-muted">{emptyNote ?? 'Sin fuente en esta versión'}</span>}
    </aside>
  </li>;
}
