'use client';
import { Button } from './design-system/button';
import { MinutesDocument } from './MinutesDocument';
import { type Draft, draftMinutes } from './useGeneration';
import type { Turn } from './minutes';

export function MinutesPreview({ draft, error, reconnect, cite, speaker, onFocus, onOpen }: {
  draft: Draft; error: string; reconnect: () => void; cite: Map<string, Turn>;
  speaker: (id: string | null) => string; onFocus: (id: string | null) => void; onOpen: (id: string) => void;
}) {
  const status = draft.phase === 'reconnecting' ? 'Reconectando…' : draft.phase === 'failed' ? 'La generación se ha detenido' :
    draft.phase === 'cancelled' ? 'Generación cancelada' : draft.phase === 'complete' ? 'Terminando el acta…' : 'Generando acta';
  return <section data-slot="minutes-generation" aria-label="Acta en generación" aria-busy={draft.phase === 'running'}>
    <header className="notebook-copy"><span role="status" className="notebook-muted">{status} · Pendiente de publicar</span></header>
    {error && <div role="alert" className="voice-alert">{error} <Button size="sm" variant="outline" onClick={reconnect}>Reconectar</Button></div>}
    {draft.topics.length > 0 ? <MinutesDocument minutes={draftMinutes(draft)} cite={cite} speaker={speaker} onFocus={onFocus} onOpen={onOpen} /> :
      draft.text ? <article className="notebook-page"><p className="notebook-prose">{draft.text}</p></article> :
        <p className="notebook-muted">Preparando los primeros temas…</p>}
  </section>;
}
