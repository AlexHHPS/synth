'use client';
import { brand } from './brand';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dynamic from 'next/dynamic';
import { AudioLines, CalendarDays, Check, ChevronDown, Copy, FolderPlus, Link2, ListChecks, LockKeyhole, Mail, MoreHorizontal, RotateCw, Sparkles, AlignLeft, Users } from 'lucide-react';
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover';
import { MeetingPanel } from './MeetingPanel';
import { MeetingQuestion } from './MeetingQuestion';
const MeetingNotes = dynamic(() => import('./MeetingNotes'), { ssr: false });
import { Button } from './design-system/button';
import { type Folder, type Meeting, message, pipelineRequest, request } from './api';
import { DecisionList, TaskList } from './ItemsPanel';
import { MinutesDocument } from './MinutesDocument';
import { MinutesPreview } from './MinutesPreview';
import { useGeneration } from './useGeneration';
import { type SpeakerAction, SpeakerPrompt, TranscriptScript } from './TranscriptScript';
import {
  type Correction, type DecisionRow, type MinutesResponse, type TaskRow, type TranscriptVersion, type VoiceStatus,
  citationIndex, clock, duration, participantsOf, speakerNames, turnsOf,
} from './minutes';

type Tab = 'minutes' | 'transcript' | 'items' | 'notes';
const PROCESSING = ['queued', 'processing', 'capturing'];

interface MeetingDetailProps { id: string; refreshToken: number; folders: Folder[]; onBack: () => void; onChanged: () => Promise<void> }

export function MeetingDetail(props: MeetingDetailProps) {
  return <MeetingDetailContent key={props.id} {...props} />;
}

function MeetingDetailContent({ id, refreshToken, folders, onBack, onChanged }: MeetingDetailProps) {
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [transcript, setTranscript] = useState<TranscriptVersion | null>(null);
  const [minutes, setMinutes] = useState<MinutesResponse | null>(null);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [decisions, setDecisions] = useState<DecisionRow[]>([]);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [tab, setTab] = useState<Tab>('minutes');
  const [panel, setPanel] = useState<'transcript' | 'voices' | 'items' | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [scrollTo, setScrollTo] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState('');
  const [tick, setTick] = useState(0);
  const notesDirty = useRef(false);
  const notesValue = useRef(notes); notesValue.current = notes;
  const savedNotes = useRef('');
  const loadRun = useRef(0);
  const activeId = useRef(id); activeId.current = id;

  useEffect(() => {
    notesDirty.current = false; savedNotes.current = '';
    setNotes(''); setMeeting(null); setMinutes(null); setTranscript(null);
    return () => { loadRun.current++; };
  }, [id]);

  const load = useCallback(async () => {
    const run = ++loadRun.current;
    const current = () => run === loadRun.current && id === activeId.current;
    setError('');
    try {
      const m = await request<Meeting>('GET', `/v1/meetings/${id}`);
      if (!current()) return;
      setMeeting(old => notesDirty.current && old?.id === id ? { ...m, notes: old.notes, revision: old.revision } : m);
      if (!notesDirty.current) { setNotes(m.notes ?? ''); savedNotes.current = m.notes ?? ''; }
      const [t, d, ts, ds] = await Promise.allSettled([
        request<TranscriptVersion>('GET', `/v1/meetings/${id}/transcript`), request<MinutesResponse>('GET', `/v1/meetings/${id}/minutes`),
        request<{ items: TaskRow[] }>('GET', `/v1/meetings/${id}/tasks`), request<{ items: DecisionRow[] }>('GET', `/v1/meetings/${id}/decisions`),
      ]);
      if (!current()) return;
      setTranscript(t.status === 'fulfilled' ? t.value : null);
      setMinutes(d.status === 'fulfilled' ? d.value : null);
      setTasks(ts.status === 'fulfilled' ? ts.value.items : []);
      setDecisions(ds.status === 'fulfilled' ? ds.value.items : []);
      try { const voice = await pipelineRequest<VoiceStatus>('GET', `/v1/voice/meetings/${id}`); if (current()) setVoice(voice); }
      catch { if (current()) setVoice(null); }
    } catch (e) { if (current()) setError(message(e)); } finally { if (current()) setLoading(false); }
  }, [id]);
  useEffect(() => { setLoading(true); void load(); }, [load, refreshToken, tick]);
  useEffect(() => {
    if (!meeting || !PROCESSING.includes(meeting.state)) return;
    const timer = setTimeout(() => setTick(t => t + 1), 4000);
    return () => clearTimeout(timer);
  }, [meeting]);
  const generation = useGeneration(id, transcript?.version, Boolean(meeting && PROCESSING.includes(meeting.state)), () => { void load(); });

  const content = transcript?.content;
  const speaker = useMemo(() => content ? speakerNames(content) : () => 'Voz sin identificar', [content]);
  const cite = useMemo(() => content ? citationIndex(content) : new Map(), [content]);
  const people = useMemo(() => content ? participantsOf(content) : [], [content]);

  function openTurn(turnId: string) { setPanel('transcript'); setScrollTo(turnId); setFocused(turnId); }
  async function act(run: () => Promise<unknown>, done: string) {
    setBusy(true); setError(''); setToast('');
    try { await run(); setToast(done); await load(); await onChanged(); } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  const onSpeaker = (action: SpeakerAction) => act(() => pipelineRequest('POST', `/v1/voice/meetings/${id}/speakers`,
    { ...action, expected_version: transcript?.version }), 'Hablante actualizado. El acta se regenera con el cambio.');
  const onUndo = (c: Correction) => act(() => request('POST', `/v1/meetings/${id}/corrections/${c.id}/undo`),
    `Se restauró «${c.original}». El acta se regenera.`);
  const onAccept = (c: Correction) => act(() => request('POST', `/v1/meetings/${id}/corrections/${c.id}/accept`),
    `Ahora dice «${c.term}» y el acta se regenera.`);
  const onReject = (c: Correction) => act(() => request('POST', `/v1/meetings/${id}/corrections/${c.id}/reject`),
    `Se queda «${c.original}» y el glosario lo tendrá en cuenta.`);
  const updateTask = (task: TaskRow, change: object) => act(() => request('PATCH', `/v1/tasks/${task.id}`, change), 'Tarea actualizada.');
  const updateDecision = (d: DecisionRow, change: object) => act(() => request('PATCH', `/v1/decisions/${d.id}`, change), 'Decisión actualizada.');
  const regenerate = () => act(() => request('POST', `/v1/meetings/${id}/regenerate`), 'Se está generando un acta nueva.');
  async function copyEmail() {
    setBusy(true); setError(''); setToast('');
    try {
      const draft = await request<{ subject: string; body: string }>('GET', `/v1/meetings/${id}/follow-up-email`);
      await navigator.clipboard.writeText(`Asunto: ${draft.subject}\n\n${draft.body}`);
      setToast(`Borrador copiado. Pégalo en tu correo: ${brand.name} no envía nada.`);
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }
  const saveNotes = () => {
    const submitted = notesValue.current;
    return act(async () => {
      const result = await request<{ revision: number }>('PUT', `/v1/meetings/${id}/notes`, { notes: submitted, expected_revision: meeting?.revision });
      if (activeId.current !== id) return;
      savedNotes.current = submitted; notesDirty.current = notesValue.current !== submitted;
      setMeeting(old => old ? { ...old, notes: submitted, revision: result.revision } : old);
    }, 'Notas guardadas.');
  };
  async function move(folderId: string) {
    if (!meeting || busy) return;
    setBusy(true); setError('');
    try {
      await request('PUT', `/v1/meetings/${id}/folder`, { folder_id: folderId || null });
      const m = await request<Meeting>('GET', `/v1/meetings/${id}`);
      if (activeId.current !== id) return;
      // A move advances our revision only if it did not hide someone else's
      // notes edit. Keep the human draft and its conflict check in either case.
      setMeeting(old => notesDirty.current && old ? { ...m, notes: old.notes,
        revision: (m.notes ?? '') === savedNotes.current ? m.revision : old.revision } : m);
      if (!notesDirty.current) { setNotes(m.notes ?? ''); savedNotes.current = m.notes ?? ''; }
      await onChanged();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  }

  const turns = content ? turnsOf(content) : [];
  const labels = [...new Set(turns.flatMap(turn => turn.speaker_id ? [turn.speaker_id] : []))];
  const toggle = (next: 'transcript' | 'voices' | 'items') => setPanel(before => before === next ? null : next);
  async function copyLink() {
    try { await navigator.clipboard.writeText(`synth-voice://meeting/${id}`); setToast('Enlace interno copiado. Se mantienen los permisos de acceso.'); }
    catch (failure) { setError(message(failure)); }
  }
  async function copyMinutes() {
    try { const document = await request<{ markdown: string }>('GET', `/v1/meetings/${id}/document`); await navigator.clipboard.writeText(document.markdown); setToast('Acta copiada.'); }
    catch (failure) { setError(message(failure)); }
  }
  if (loading && !meeting) return <div className="meeting-editor" role="status" aria-busy="true">Cargando la reunión…</div>;
  return <div className="meeting-editor" data-slot="meeting-editor">
    <div className="meeting-editor-actions">
      <Popover><PopoverTrigger asChild><Button variant="ghost" aria-label="Más opciones de la reunión"><MoreHorizontal size={19} /></Button></PopoverTrigger>
        <PopoverContent className="meeting-popover" align="end"><div className="meeting-menu-actions">
          <Button variant="ghost" disabled={busy || !minutes} onClick={() => void copyMinutes()}><Copy size={17} /> Copiar acta</Button>
          <Button variant="ghost" disabled={busy || !minutes} onClick={() => void copyEmail()}><Mail size={17} /> Copiar borrador de email</Button>
          <Button variant="ghost" disabled={busy || !content} onClick={() => toggle('items')}><ListChecks size={17} /> Tareas y decisiones</Button>
          <Button variant="ghost" disabled={busy || !content || PROCESSING.includes(meeting?.state ?? '')} onClick={() => void regenerate()}><RotateCw size={17} /> Regenerar acta</Button>
        </div></PopoverContent></Popover>
      <MeetingAccess meeting={meeting} folders={folders} busy={busy} move={move} copyLink={copyLink} />
      <Button variant="ghost" aria-label="Copiar enlace interno" onClick={() => void copyLink()}><Link2 size={18} /></Button>
    </div>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {toast && <p role="status" className="meeting-receipt"><Check size={14} />{toast}</p>}
    {meeting && <header className="meeting-document-head"><h1>{meeting.title}</h1>
      <div className="meeting-metadata">
        <div className="meeting-view-toggle" aria-label="Vista de la reunión"><button type="button" aria-pressed={tab === 'notes'} onClick={() => setTab('notes')}><AlignLeft size={17} /> Mis notas</button><button type="button" aria-pressed={tab === 'minutes'} onClick={() => setTab('minutes')}><Sparkles size={17} /> Acta</button></div>
        <Popover><PopoverTrigger asChild><button type="button" className="meeting-chip"><CalendarDays size={17} />{meeting.created_at ? new Date(meeting.created_at).toLocaleDateString('es-ES', { day:'numeric', month:'short' }) : 'Reunión'}<Users size={17} />{labels.length}</button></PopoverTrigger>
          <PopoverContent className="meeting-popover" align="start"><h2>{meeting.title}</h2><p>{meeting.created_at && new Date(meeting.created_at).toLocaleDateString('es-ES',{weekday:'long',day:'numeric',month:'long'})}{content && ` · ${duration(content.duration_ms)}`}</p>
            <ul className="meeting-people-list">{labels.map(label => <li key={label}><span className="meeting-avatar">{speaker(label).slice(0,1)}</span>{speaker(label)}</li>)}</ul>
            <Button variant="ghost" onClick={() => toggle('voices')}><Users size={17} /> Revisar voces</Button>
          </PopoverContent></Popover>
        <Popover><PopoverTrigger asChild><button type="button" className="meeting-chip"><FolderPlus size={17} />{folders.find(folder => folder.id === meeting.folder_id)?.name ?? 'Añadir a carpeta'}</button></PopoverTrigger>
          <PopoverContent className="meeting-popover" align="start"><label>Guardar en<select className="voice-input" aria-label="Carpeta de la reunión" value={meeting.folder_id ?? ''} disabled={busy} onChange={event => void move(event.target.value)}><option value="">Privada · Sin carpeta</option>{folders.filter(folder => folder.can_edit).map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></label></PopoverContent></Popover>
      </div>
    </header>}
    <div hidden={tab !== 'notes'}><div className="meeting-save"><Button variant="outline" size="sm" loading={busy} disabled={notes === (meeting?.notes ?? '')} onClick={() => void saveNotes()}>Guardar notas</Button><span>{notesDirty.current ? 'Cambios sin guardar' : 'Notas guardadas'}</span></div>
      <MeetingNotes value={notes} onChange={value => { notesValue.current = value; notesDirty.current = value !== savedNotes.current; setNotes(value); }} label="Tus notas" /></div>
    {tab === 'minutes' && (
      generation.draft && (generation.draft.topics.length > 0 || generation.draft.text || !minutes) ? <div className="notebook-reading"><MinutesPreview draft={generation.draft} error={generation.error} reconnect={generation.reconnect} cite={cite} speaker={speaker} onFocus={setFocused} onOpen={openTurn} /></div> :
      minutes ? <div className="notebook-reading"><MinutesDocument minutes={minutes.minutes} cite={cite} speaker={speaker} onFocus={setFocused} onOpen={openTurn} /></div> : <Empty processing={Boolean(meeting && PROCESSING.includes(meeting.state))} onRegenerate={content ? regenerate : undefined} busy={busy} />)}
    <div className="meeting-bottom"><div className="meeting-dock">
      <div className="meeting-capture-panels"><button type="button" className="meeting-chip" aria-label="Transcripción" aria-controls="meeting-transcript" aria-expanded={panel === 'transcript'} onClick={() => toggle('transcript')}><AudioLines size={20} /><span>Transcripción</span><ChevronDown size={15} /></button><button type="button" className="meeting-chip" aria-label="Voces" aria-controls="meeting-voices" aria-expanded={panel === 'voices'} onClick={() => toggle('voices')}><Users size={19} /><span>Voces</span></button></div>
      {content ? <MeetingQuestion key={id} meetingId={id} onOpenCitation={openTurn} /> : <div className="meeting-ask-disabled">Las preguntas estarán disponibles con la transcripción.</div>}
    </div></div>
    <MeetingPanel id="meeting-transcript" title="Transcripción" open={panel === 'transcript'} onClose={() => setPanel(null)} footer={content ? `${duration(content.duration_ms)} · ${labels.length} hablantes` : 'Transcripción'} actions={content && <Button variant="ghost" aria-label="Copiar transcripción" onClick={() => { void navigator.clipboard.writeText(turns.map(turn => `${speaker(turn.speaker_id)}: ${turn.text}`).join('\n\n')).then(() => setToast('Transcripción copiada.')).catch(failure => setError(message(failure))); }}><Copy size={16} /></Button>}>
      {content ? <TranscriptScript content={content} speaker={speaker} voice={voice} focused={focused} scrollTo={scrollTo} busy={busy} onSpeaker={onSpeaker} onUndo={onUndo} onAccept={onAccept} onReject={onReject} showSpeakerPrompts={false} /> : <p className="meeting-empty">La transcripción aparecerá al terminar el análisis del audio.</p>}
    </MeetingPanel>
    <MeetingPanel id="meeting-voices" title="Voces" open={panel === 'voices'} onClose={() => setPanel(null)} footer="Las sugerencias necesitan tu confirmación">
      {content ? <div className="meeting-voices-list">{labels.map(label => {
        const excerpt = turns.find(turn => turn.speaker_id === label);
        const resolved = content.speakers?.[label];
        return <article key={label}><h3>{speaker(label)}{resolved && <span className="meeting-confirmed"> · Confirmada</span>}</h3>
          {excerpt && <blockquote><time>{clock(excerpt.start_ms)}</time> · {excerpt.text.slice(0,280)}</blockquote>}
          {!resolved ? <SpeakerPrompt label={label} name={speaker(label)} others={labels.filter(other => other !== label)} speakerName={speaker} suggestion={voice?.matches[label]} busy={busy} onSpeaker={onSpeaker} /> : <Button variant="ghost" disabled={busy} onClick={() => onSpeaker({ action:'reject', speaker_id:label })}>Deshacer identificación</Button>}
        </article>;
      })}</div> : <p className="meeting-empty">Las voces aparecerán con la transcripción.</p>}
    </MeetingPanel>
    <MeetingPanel id="meeting-items" title="Tareas y decisiones" open={panel === 'items'} onClose={() => setPanel(null)}>
      <div className="notebook-items"><section><h2>Tareas</h2><TaskList tasks={tasks} cite={cite} speaker={speaker} onUpdate={updateTask} onOpen={(_, turn) => turn && openTurn(turn)} /></section><section><h2>Decisiones</h2><DecisionList decisions={decisions} onUpdate={updateDecision} /></section></div>
    </MeetingPanel>
    <details className="notebook-diagnostics"><summary>Diagnóstico</summary><dl><dt>Transcripción</dt><dd>{transcript ? `v${transcript.version} · ${transcript.model_fingerprint}` : 'sin transcripción'}</dd><dt>Voces</dt><dd>{voice?.state ?? 'sin datos'}</dd><dt>Acta</dt><dd>{minutes ? `v${minutes.version} · transcripción v${minutes.transcript_version}` : 'sin acta'}</dd></dl></details>
  </div>;
}

function MeetingAccess({ meeting, folders, busy, move, copyLink }: { meeting: Meeting | null; folders: Folder[]; busy: boolean; move: (id: string) => Promise<void>; copyLink: () => Promise<void> }) {
  const [open, setOpen] = useState(false), [members, setMembers] = useState<{name:string;role:string}[]>([]), [error, setError] = useState('');
  useEffect(() => {
    let active = true; setMembers([]); setError('');
    if (!open || !meeting?.folder_id) return;
    void request<{items:{name:string;role:string}[]}>('GET', `/v1/folders/${meeting.folder_id}/members`).then(result => { if (active) setMembers(result.items); }).catch(failure => { if (active) setError(message(failure)); });
    return () => { active = false; };
  }, [open, meeting?.folder_id]);
  return <Popover open={open} onOpenChange={setOpen}><PopoverTrigger asChild><Button variant="outline"><LockKeyhole size={16} /> Compartir</Button></PopoverTrigger>
    <PopoverContent className="meeting-popover meeting-share" align="end"><h2>Compartir en {brand.name}</h2>
      <label>Carpeta con acceso<select className="voice-input" aria-label="Carpeta para compartir" value={meeting?.folder_id ?? ''} disabled={busy} onChange={event => void move(event.target.value)}><option value="">Privada · Solo el propietario</option>{folders.filter(folder => folder.can_edit).map(folder => <option key={folder.id} value={folder.id}>{folder.name}</option>)}</select></label>
      <div className="meeting-share-access"><h3>Personas con acceso</h3>{error ? <p role="alert">{error}</p> : <ul><li><span>Propietario de la reunión</span><small>Propietario</small></li>{members.map(member => <li key={member.name}><span>{member.name}</span><small>{member.role === 'editor' ? 'Puede editar' : 'Puede leer'}</small></li>)}</ul>}</div>
      <div className="meeting-share-bottom"><span><LockKeyhole size={14} /> Acceso restringido</span><Button variant="outline" onClick={() => void copyLink()}><Link2 size={16} /> Copiar enlace</Button></div>
    </PopoverContent>
  </Popover>;
}

function Empty({ processing, onRegenerate, busy }: { processing: boolean; onRegenerate?: () => void; busy: boolean }) {
  return <div className="voice-empty">
    <h2>{processing ? 'El acta se está generando' : 'Esta reunión aún no tiene acta'}</h2>
    <p>{processing ? 'Se actualizará sola en cuanto esté lista.' : 'Puedes leer la transcripción o pedir un acta nueva.'}</p>
    {!processing && onRegenerate && <Button size="sm" loading={busy} onClick={onRegenerate}>Generar acta</Button>}
  </div>;
}
