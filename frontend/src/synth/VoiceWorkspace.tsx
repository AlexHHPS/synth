'use client';
import { brand } from './brand';

import { useCallback, useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { Field } from '@base-ui/react/field';
import { Activity, ArrowLeft, ChevronRight, FileText, FolderClosed, Info, Library, LockKeyhole, Mic, Plug, Plus, RefreshCw, Search } from 'lucide-react';
import { Button } from './design-system/button';
import { FormField } from './design-system/form-field';
import { TooltipProvider } from './design-system/tooltip';
import { Document, Folder, Meeting, Page, SearchHit, Statement, Transcript, message, request, pipelineRequest, time } from './api';
import './voice.css';
import { AudioPermissions } from './AudioPermissions';
import { FolderManagement } from './FolderManagement';
import { OperationsView } from './OperationsView';
import { CaptureView } from './CaptureView';
import { IntegrationsView } from './IntegrationsView';
import { MeetingDetection } from './MeetingDetection';
import { VoiceOnboarding } from './VoiceOnboarding';
import { CorporateSession } from './CorporateSession';
import { About } from '@/components/About';

const stateLabels: Record<string, string> = {
  capturing: 'Captura', queued: 'En cola', transcribing: 'Transcribiendo', processing: 'Procesando',
  completed: 'Acta disponible', ready: 'Disponible', failed: 'Requiere atención', cancelled: 'Cancelada',
};

export function VoiceWorkspace() {
  return <CorporateSession><VoiceWorkspaceContent /></CorporateSession>;
}

function VoiceWorkspaceContent() {
  const [actor, setActor] = useState('');
  const [folders, setFolders] = useState<Folder[]>([]);
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [folder, setFolder] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [newFolder, setNewFolder] = useState(false);
  const [folderName, setFolderName] = useState('');
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [manageFolder, setManageFolder] = useState(false);
  const [detailRefresh, setDetailRefresh] = useState(0);
  const [view, setView] = useState<'library' | 'operations' | 'capture' | 'integrations' | 'voice' | 'about'>('library');
  const showProcessing = useCallback(() => { setView('operations'); setSelected(null); }, []);
  useEffect(() => { if (new URLSearchParams(window.location.search).get('capture') === '1') setView('capture'); }, []);
  const generation = useRef(0);
  const searchGeneration = useRef(0);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    const run = ++generation.current;
    setLoading(true); setError('');
    try {
      const [me, fs, library] = await Promise.all([
        request<{ name: string }>('GET', '/v1/me'), request<Page<Folder>>('GET', '/v1/folders'),
        request<Page<Meeting>>('GET', '/v1/library?limit=100'),
      ]);
      if (!mounted.current || run !== generation.current) return;
      setActor(me.name); setFolders(fs.items); setMeetings(library.items); setCursor(library.next_cursor ?? null);
    } catch (e) { if (mounted.current && run === generation.current) setError(message(e)); }
    finally { if (mounted.current && run === generation.current) setLoading(false); }
  }, []);
  useEffect(() => { mounted.current = true; void refresh(); return () => { mounted.current = false; generation.current++; }; }, [refresh]);

  async function more() {
    if (!cursor) return;
    setLoading(true); setError('');
    try {
      const page = await request<Page<Meeting>>('GET', `/v1/library?limit=100&after=${cursor}`);
      setMeetings(old => [...old, ...page.items.filter(item => !old.some(m => m.id === item.id))]);
      setCursor(page.next_cursor ?? null);
    } catch (e) { setError(message(e)); } finally { setLoading(false); }
  }
  async function createFolder(event: React.FormEvent) {
    event.preventDefault(); if (!folderName.trim()) return;
    setCreating(true); setError('');
    try {
      const created = await request<Folder>('POST', '/v1/folders', { name: folderName.trim() });
      setFolders(old => [...old, created]); setFolder(created.id); setSelected(null);
      setNewFolder(false); setFolderName(''); setHits(null);
    } catch (e) { setError(message(e)); } finally { setCreating(false); }
  }
  async function search(event: React.FormEvent) {
    event.preventDefault(); if (!query.trim()) { setHits(null); return; }
    const run = ++searchGeneration.current;
    setSearching(true); setError('');
    try {
      const result = await request<Page<SearchHit>>('GET', `/v1/search?q=${encodeURIComponent(query.trim())}${folder ? `&folder_id=${folder}` : ''}`);
      if (run !== searchGeneration.current) return;
      setHits(result.items); setSelected(null);
    } catch (e) { if (run === searchGeneration.current) setError(message(e)); }
    finally { if (run === searchGeneration.current) setSearching(false); }
  }
  function chooseFolder(id: string | null) { setView('library'); searchGeneration.current++; setSearching(false); setFolder(id); setSelected(null); setHits(null); setManageFolder(false); }
  const visible = meetings.filter(m => !folder || m.folder_id === folder);
  const currentFolder = folders.find(f => f.id === folder);
  const folderTitle = currentFolder?.name ?? 'Todas las reuniones';

  return <TooltipProvider><div className="voice-workspace">
    <aside className="voice-sidebar" aria-label={`Navegación de ${brand.name}`}>
      <div className="voice-brand"><Image src={brand.logo} width={28} height={28} alt="" /><div><strong>{brand.name}</strong><span>{brand.tagline}</span></div></div>
      <div className="voice-sidebar-body">
        <button type="button" className="voice-capture-link" onClick={() => { setView('capture'); setSelected(null); }}><Mic size={16} /> Nueva nota <ChevronRight size={14} /></button>
        <p className="voice-nav-label">TU ESPACIO</p>
        <button type="button" className={`voice-nav-row ${view === 'library' && !folder ? 'is-selected' : ''}`} onClick={() => chooseFolder(null)}><Library size={16} /> Biblioteca</button>
        <button type="button" className={`voice-nav-row ${view === 'operations' ? 'is-selected' : ''}`} onClick={() => { setView('operations'); setSelected(null); }}><Activity size={16} /> Procesamiento</button>
        <button type="button" className={`voice-nav-row ${view === 'integrations' ? 'is-selected' : ''}`} onClick={() => { setView('integrations'); setSelected(null); }}><Plug size={16} /> Integraciones</button>
        <button type="button" className={`voice-nav-row ${view === 'voice' ? 'is-selected' : ''}`} onClick={() => { setView('voice'); setSelected(null); }}><Mic size={16} /> Mi perfil de voz</button>
        <div className="voice-nav-label voice-folder-heading">CARPETAS <Button variant="ghost" size="xs" aria-label="Crear carpeta" onClick={() => setNewFolder(!newFolder)}><Plus size={14} /></Button></div>
        {folders.map(f => <button type="button" key={f.id} className={`voice-nav-row ${folder === f.id ? 'is-selected' : ''}`} onClick={() => chooseFolder(f.id)}><FolderClosed size={16} /><span>{f.name}</span></button>)}
        {newFolder && <form className="voice-folder-form" onSubmit={createFolder}><FormField label="Nueva carpeta" hint="Privada por defecto."><Field.Control className="voice-input" value={folderName} onChange={e => setFolderName(e.target.value)} maxLength={200} autoFocus /></FormField><div className="flex gap-2"><Button loading={creating} type="submit" size="sm" disabled={!folderName.trim()}>Crear</Button><Button type="button" variant="ghost" size="sm" onClick={() => setNewFolder(false)}>Cancelar</Button></div></form>}
      </div>
      <button type="button" className={`voice-nav-row ${view === 'about' ? 'is-selected' : ''}`} onClick={() => { setView('about'); setSelected(null); }}><Info size={16} /> Acerca de {brand.name}</button>
      <div className="voice-sidebar-footer"><LockKeyhole size={14} /><div><span>Acceso privado</span><small>{actor || 'Conectando a tu biblioteca…'}</small></div></div>
    </aside>
    <main className="voice-main">
      <header className="voice-topbar"><span>{brand.name} <ChevronRight size={12} /> {view === 'about' ? 'Acerca de' : view === 'voice' ? 'Perfil de voz' : view === 'integrations' ? 'Integraciones' : view === 'capture' ? 'Nueva nota' : view === 'operations' ? 'Procesamiento' : selected ? 'Reunión' : folderTitle}</span><Button variant="ghost" size="xs" loading={loading} onClick={() => { setDetailRefresh(old => old + 1); void refresh(); }}><RefreshCw size={14} /> Actualizar</Button></header>
      <MeetingDetection onPrepare={() => { setView('capture'); setSelected(null); }} />
      <AudioPermissions />
      {error && <div className="voice-alert" role="alert">{error}</div>}
      {view === 'about' ? <div className="voice-library"><About /></div> : view === 'voice' ? <VoiceOnboarding /> : view === 'integrations' ? <IntegrationsView folders={folders} refreshToken={detailRefresh} onCreateFolder={() => { chooseFolder(null); setNewFolder(true); }} /> : view === 'capture' ? <CaptureView folders={folders} onQueued={showProcessing} /> : view === 'operations' ? <OperationsView folders={folders} meetings={meetings} refreshToken={detailRefresh} onOpen={id => { setView('library'); setSelected(id); void refresh(); }} /> : selected ? <MeetingView key={selected} id={selected} refreshToken={detailRefresh} folders={folders} onBack={() => setSelected(null)} onChanged={refresh} /> : <div className="voice-library">
        <div className="voice-page-heading"><div><p className="voice-eyebrow">REUNIONES Y CONOCIMIENTO</p><h1>{folderTitle}</h1><p>Notas, conversaciones y decisiones, con sus fuentes.</p></div>{currentFolder?.can_manage ? <Button variant="outline" size="sm" onClick={() => setManageFolder(!manageFolder)}>{manageFolder ? 'Cerrar ajustes' : 'Gestionar carpeta'}</Button> : <LockKeyhole size={22} className="text-muted-foreground" />}</div>
        {manageFolder && currentFolder?.can_manage && <FolderManagement key={currentFolder.id} folder={currentFolder} onChanged={refresh} onDeleted={() => { chooseFolder(null); void refresh(); }} />}
        <form className="voice-search" role="search" onSubmit={search}><Search size={17} /><input aria-label="Buscar en transcripciones" placeholder="Buscar en las conversaciones…" value={query} maxLength={500} onChange={e => { setQuery(e.target.value); if (!e.target.value) setHits(null); }} /><Button type="submit" variant="outline" size="sm" loading={searching}>Buscar</Button></form>
        {hits !== null ? <><div className="voice-section-label">{hits.length} resultados <Button variant="ghost" size="xs" onClick={() => { setHits(null); setQuery(''); }}>Limpiar búsqueda</Button></div>{hits.map(hit => <button type="button" className="voice-result" key={hit.meeting_id} onClick={() => setSelected(hit.meeting_id)}><strong>{meetings.find(m => m.id === hit.meeting_id)?.title ?? 'Reunión accesible'}</strong>{hit.evidence.map(e => <p key={e.segment_id}>{e.quote}</p>)}<small>Transcripción v{hit.transcript_version}</small></button>)}{!hits.length && <Empty title="No hay coincidencias" detail="Prueba otra palabra o consulta otra carpeta." />}</> : <>
          <div className="voice-section-label">{visible.length} reuniones cargadas</div>
          {!loading && !visible.length && <Empty title="Tu biblioteca empieza aquí" detail="Graba una reunión o guarda una conversación en esta carpeta." />}
          {loading && !meetings.length && <p role="status" className="text-muted-foreground">Cargando tu biblioteca…</p>}
          <div className="voice-meeting-list">{visible.map(meeting => <button type="button" className="voice-meeting-row" key={meeting.id} onClick={() => setSelected(meeting.id)}><div className="voice-row-icon"><FileText size={18} /></div><div className="voice-row-content"><strong>{meeting.title}</strong><small>{folders.find(f => f.id === meeting.folder_id)?.name ?? 'Privada · Sin carpeta'}</small></div><span className="voice-state" data-state={meeting.state}>{stateLabels[meeting.state] ?? meeting.state}</span><ChevronRight size={16} /></button>)}</div>
          {cursor && <Button variant="outline" loading={loading} onClick={() => void more()}>Cargar más reuniones</Button>}
        </>}
      </div>}
    </main>
  </div></TooltipProvider>;
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="voice-empty"><FileText size={26} /><h2>{title}</h2><p>{detail}</p></div>;
}

type VoiceMatch = { state: string; label?: string; similarity: number | null; reason: string };
type MeetingIdentity = { state: string; reason?: string; transcript_version?: number; matches: Record<string, VoiceMatch> };
function voiceStatusText(identity: MeetingIdentity | null) {
  if (!identity) return 'Consultando el resultado del reconocimiento automático…';
  const reasons: Record<string, string> = {
    analysis_pending: 'El reconocimiento se está completando junto con la transcripción.',
    audio_deleted: 'El audio ya se eliminó. Los perfiles actuales se usarán automáticamente en las próximas grabaciones.',
    native_audio_unavailable: 'Esta grabación no tiene un resultado acústico disponible en este Mac.',
    transcript_version_changed: 'El texto se recuperó en otra versión. No hay una correspondencia fiable entre sus segmentos y las voces analizadas.',
    transcript_has_no_speaker_mapping: 'Esta transcripción no conserva turnos de voz que permitan asignar nombres con fiabilidad.',
    no_active_profiles: 'No había perfiles disponibles al analizar esta grabación. Crea o comparte tu perfil para las próximas notas.',
    no_model_compatible_profiles: 'Los perfiles disponibles necesitan actualizarse para este modelo de voz.',
    voice_comparison_failed: 'No se pudo completar el reconocimiento. La transcripción se conserva.',
    profile_changed: 'Los perfiles han cambiado desde el análisis. Se usarán los actuales en las próximas grabaciones.',
    profile_changed_audio_deleted: 'Los perfiles han cambiado y el audio ya se eliminó. Se usarán los actuales en las próximas grabaciones.',
  };
  if (identity.reason) return reasons[identity.reason] ?? 'No hay una identificación fiable para esta grabación.';
  const candidates = Object.values(identity.matches).filter(m => m.state === 'candidate').length;
  return candidates ? `Análisis completado · ${candidates} ${candidates === 1 ? 'coincidencia propuesta' : 'coincidencias propuestas'}.` : 'Análisis completado · no hay coincidencias con confianza suficiente.';
}
function MeetingView({ id, refreshToken, folders, onBack, onChanged }: { id: string; refreshToken: number; folders: Folder[]; onBack: () => void; onChanged: () => Promise<void> }) {
  const [meeting, setMeeting] = useState<Meeting | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [document, setDocument] = useState<Document | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [notes, setNotes] = useState('');
  const [savedNotes, setSavedNotes] = useState('');
  const [saving, setSaving] = useState(false);
  const [moving, setMoving] = useState(false);
  const [tab, setTab] = useState<'document' | 'transcript' | 'notes'>('document');
  const [identity, setIdentity] = useState<MeetingIdentity | null>(null);
  const [voiceError, setVoiceError] = useState('');
  const [highlighted, setHighlighted] = useState<string | null>(null);
  const [toast, setToast] = useState('');
  const [sourceVersion, setSourceVersion] = useState<number | null>(null);
  const [readingSource, setReadingSource] = useState(false);
  const sourceGeneration = useRef(0);
  const dirtyNotes = useRef(false);
  dirtyNotes.current = notes !== savedNotes;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function load(initial = true) {
      if (initial) { setLoading(true); setTranscript(null); setDocument(null); setSourceVersion(null); setHighlighted(null); }
      setError('');
      try {
        const m = await request<Meeting>('GET', `/v1/meetings/${id}`);
        if (!active) return;
        // A refresh must not discard edits or silently acknowledge a conflicting revision.
        if (dirtyNotes.current) setMeeting(old => old ? { ...m, notes: old.notes, revision: old.revision } : m);
        else { setMeeting(m); setNotes(m.notes ?? ''); setSavedNotes(m.notes ?? ''); }
        const results = await Promise.allSettled([
          request<Transcript>('GET', `/v1/meetings/${id}/transcript`), request<Document>('GET', `/v1/meetings/${id}/document`),
        ]);
        if (!active) return;
        if (results[0].status === 'fulfilled') { setTranscript(results[0].value); setSourceVersion(results[0].value.version); }
        else { setTranscript(null); setSourceVersion(null); }
        if (results[1].status === 'fulfilled' && results[0].status === 'fulfilled' && results[1].value.transcript_version === results[0].value.version) setDocument(results[1].value);
        else setDocument(null);
        setNotices(results.filter((r): r is PromiseRejectedResult => r.status === 'rejected').map(r => message(r.reason)));
        if (['queued', 'processing', 'capturing'].includes(m.state)) timer = setTimeout(() => void load(false), 3000);
      } catch (e) { if (active) setError(message(e)); }
      finally { if (active) setLoading(false); }
    }
    void load(); return () => { active = false; clearTimeout(timer); sourceGeneration.current++; };
  }, [id, refreshToken]);
  useEffect(() => {
    if (tab !== 'transcript' || !highlighted) return;
    window.document.getElementById(`voice-segment-${highlighted}`)?.scrollIntoView({ block: 'center' });
  }, [tab, highlighted, transcript]);

  async function saveNotes(event: React.FormEvent) {
    event.preventDefault(); if (!meeting) return;
    setSaving(true); setError(''); setToast('');
    try {
      const result = await request<{ revision: number }>('PUT', `/v1/meetings/${id}/notes`, { notes, expected_revision: meeting.revision });
      setMeeting(old => old ? { ...old, notes, revision: result.revision } : old); setSavedNotes(notes);
      setToast('Notas guardadas. El acta existente conserva su versión.'); await onChanged();
    } catch (e) { setError(message(e)); } finally { setSaving(false); }
  }
  async function move(folderId: string) {
    if (!meeting) return; setMoving(true); setError('');
    try {
      await request('PUT', `/v1/meetings/${id}/folder`, { folder_id: folderId || null });
      const m = await request<Meeting>('GET', `/v1/meetings/${id}`); setMeeting(m); await onChanged();
    } catch (e) { setError(message(e)); } finally { setMoving(false); }
  }
  async function reveal(segment: string) {
    if (!document) return;
    const run = ++sourceGeneration.current;
    setError(''); setReadingSource(true);
    try {
      // Citations belong to the acta's pinned version, never the latest by accident.
      const source = sourceVersion === document.transcript_version && transcript ? transcript :
        await request<Transcript>('GET', `/v1/meetings/${id}/transcript?version=${document.transcript_version}`);
      if (run !== sourceGeneration.current) return;
      if (!source.content.segments.some(s => s.id === segment)) throw new Error('La referencia no aparece en esta versión de la transcripción.');
      setTranscript(source); setSourceVersion(source.version); setHighlighted(segment); setTab('transcript');
    } catch (e) { if (run === sourceGeneration.current) setError(message(e)); }
    finally { if (run === sourceGeneration.current) setReadingSource(false); }
  }
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    setIdentity(null); setVoiceError('');
    async function readIdentity() {
      try { const v = await pipelineRequest<MeetingIdentity>('GET', `/v1/voice/meetings/${id}`); if (active) { setIdentity(v); setVoiceError(''); } }
      catch { if (active) setVoiceError('No se pudo consultar el reconocimiento local. Se actualizará automáticamente.'); }
      finally { if (active) timer = setTimeout(() => void readIdentity(), 5000); }
    }
    void readIdentity();
    return () => { active = false; clearTimeout(timer); };
  }, [id, refreshToken]);
  function voiceLabel(s: Transcript['content']['segments'][number]) {
    const match = identity?.transcript_version === sourceVersion && s.speaker_id ? identity.matches[s.speaker_id] : null;
    if (match?.state === 'candidate') return `${match.label ?? 'Tu voz'} · Coincidencia piloto (${match.similarity?.toFixed(3)})`;
    return s.employee_id ? `Persona ${s.employee_id}` : s.speaker_id ? `Voz ${s.speaker_id} · Sin identificar` : 'Voz sin identificar';
  }
  async function copy(format: 'markdown' | 'json') {
    if (!document) return;
    try { await navigator.clipboard.writeText(format === 'markdown' ? document.markdown : JSON.stringify(document.content, null, 2)); setToast(`${format === 'markdown' ? 'Markdown' : 'JSON'} copiado.`); }
    catch { setError('No se pudo copiar al portapapeles.'); }
  }
  if (loading) return <div className="voice-library" role="status">Cargando reunión…</div>;
  return <div className="voice-detail">
    <Button className="self-start" variant="ghost" size="sm" onClick={onBack}><ArrowLeft size={14} /> Biblioteca</Button>
    {error && <div className="voice-alert" role="alert">{error}</div>}
    {toast && <p role="status" className="voice-success">{toast}</p>}
    {meeting && <>
      <div className="voice-page-heading"><div><p className="voice-eyebrow">CONVERSACIÓN</p><h1>{meeting.title}</h1><p>{stateLabels[meeting.state] ?? meeting.state}{meeting.created_at && ` · ${new Date(meeting.created_at).toLocaleString('es-ES')}`}</p></div></div>
      <label className="voice-folder-select">Carpeta <select aria-label="Mover reunión a carpeta" disabled={moving || saving} value={meeting.folder_id ?? ''} onChange={e => void move(e.target.value)}><option value="">Privada · Sin carpeta</option>{folders.map(f => <option key={f.id} value={f.id}>{f.name}</option>)}</select>{moving && <span role="status">Moviendo…</span>}</label>
      <div className="voice-tabs" aria-label="Contenido de la reunión">{([['document', 'Acta'], ['transcript', 'Transcripción'], ['notes', 'Mis notas']] as const).map(([value, label]) => <button type="button" key={value} aria-pressed={tab === value} className={tab === value ? 'is-selected' : ''} onClick={() => setTab(value)}>{label}{value === 'notes' && notes !== savedNotes ? ' · Sin guardar' : ''}</button>)}</div>
      {notices.map((n, i) => <p key={i} className="voice-notice">{n}</p>)}
      {tab === 'notes' && <form className="voice-notes" onSubmit={saveNotes}><FormField label="Tus notas" hint="Se guardan en esta reunión. Guardarlas no sustituye ni regenera un acta publicada."><Field.Control render={<textarea />} className="voice-textarea" value={notes} onChange={e => setNotes(e.target.value)} maxLength={200000} /></FormField><Button type="submit" loading={saving} disabled={notes === savedNotes || moving}>Guardar notas</Button></form>}
      {tab === 'document' && (document ? <>
        <div className="voice-document-meta"><span>Acta v{document.version} · Transcripción v{document.transcript_version}</span><div className="flex gap-2"><Button variant="outline" size="xs" onClick={() => void copy('markdown')}>Copiar Markdown</Button><Button variant="outline" size="xs" onClick={() => void copy('json')}>Copiar JSON</Button></div></div>
        <p className="voice-notice">Acta generada con OmniRoute. La transcripción se ha enviado al proveedor configurado. Modelo: {document.model_fingerprint}.</p>
        {readingSource && <p role="status">Abriendo la fuente…</p>}
        <div className="voice-acta">{([['summary', 'Resumen'], ['decisions', 'Decisiones'], ['actions', 'Tareas'], ['open_questions', 'Dudas abiertas']] as const).map(([section, label]) => <section key={section}><h2>{label}</h2>{document.content[section].length ? document.content[section].map((item, i) => <StatementView key={i} item={item} action={section === 'actions'} onReference={s => void reveal(s)} />) : <p className="text-muted-foreground">Sin elementos registrados.</p>}</section>)}</div>
      </> : <Empty title="El acta aún no está disponible" detail="Consulta la transcripción o actualiza la reunión cuando termine el procesamiento." />)}
      {tab === 'transcript' && (transcript ? <><div className="voice-identity-summary"><div><strong>Reconocimiento de voces</strong><p className="voice-notice">Se analiza con la transcripción en este Mac, usando los perfiles disponibles de tu organización y antes de eliminar el audio. Las coincidencias siguen en piloto.</p></div><span className="voice-state">Automático</span></div><p className="voice-notice" role="status">{voiceStatusText(identity)}</p>{voiceError && <p role="alert" className="voice-alert">{voiceError}</p>}<p className="voice-document-meta">Transcripción v{transcript.version} · {time(transcript.content.duration_ms)} · {transcript.content.language.toUpperCase()}</p><div className="voice-transcript">{transcript.content.segments.map(s => <article key={s.id} id={`voice-segment-${s.id}`} className={highlighted === s.id ? 'is-highlighted' : ''}><div><span>{time(s.start_ms)}</span><strong>{voiceLabel(s)}</strong><small>{s.source_id}</small></div><p>{s.text}</p></article>)}</div></> : <Empty title="No hay transcripción disponible" detail="El servicio mostrará aquí los segmentos cuando termine el análisis de audio." />)}
    </>}
  </div>;
}

function StatementView({ item, action, onReference }: { item: Statement; action: boolean; onReference: (segment: string) => void }) {
  return <div className="voice-statement"><p>{item.text}</p>{action && <div className="voice-task-meta"><span>{item.owner ?? 'Sin responsable asignado'}</span><span>{item.due_date ?? 'Sin fecha acordada'}</span></div>}<div className="voice-citations">{item.evidence.map((e, i) => <button type="button" key={`${e.segment_id}-${i}`} title={e.quote} onClick={() => onReference(e.segment_id)}>↗ {e.segment_id}</button>)}</div></div>;
}
