'use client';
import { brand } from './brand';

import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import Image from 'next/image';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import ReactMarkdown from 'react-markdown';
import { Field } from '@base-ui/react/field';
import { Activity, BookA, Home, Info, Settings2, MessageSquareText, ChevronRight, FileText, FolderClosed, Library, ListChecks, LockKeyhole, Mic, Plug, Plus, RefreshCw, Search } from 'lucide-react';
import { Button } from './design-system/button';
import { FormField } from './design-system/form-field';
import { TooltipProvider } from './design-system/tooltip';
import { Folder, Meeting, Page, SearchHit, message, request } from './api';
import './theme.css';
import './voice.css';
import './notebook.css';
import './meeting-editor.css';
import { MeetingDetail } from './MeetingDetail';
import { GlossaryView, MyTasks } from './GlossaryView';
import { AskView } from './AskView';
import { FolderManagement } from './FolderManagement';
import { OperationsView } from './OperationsView';
import { CaptureView } from './CaptureView';
import { IntegrationsView } from './IntegrationsView';
import { MeetingDetection } from './MeetingDetection';
import { VoiceOnboarding } from './VoiceOnboarding';
import { CorporateSession, SessionEmail } from './CorporateSession';
import { SetupFlow, SetupExit } from './SetupFlow';
import { SettingsView } from './SettingsView';
import { CalendarHome, CalendarPreparation } from './CalendarHome';
import { setupCompleted } from './setup';
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
  const [view, setView] = useState<'home' | 'library' | 'operations' | 'capture' | 'integrations' | 'voice' | 'tasks' | 'glossary' | 'ask' | 'settings' | 'about'>('home');
  const [prepared, setPrepared] = useState<CalendarPreparation | null>(null);
  const email = useContext(SessionEmail);
  const [setupDone, setSetupDone] = useState<boolean | null>(null);
  useEffect(() => { void setupCompleted().then(setSetupDone).catch(() => setSetupDone(true)); }, []);
  const showProcessing = useCallback(() => { setView('operations'); setSelected(null); }, []);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('capture') === '1') setView('capture');
    const linked = params.get('view');
    if (linked === 'tasks' || linked === 'glossary' || linked === 'ask' || linked === 'settings') setView(linked);
    if (params.get('meeting')) { setView('library'); setSelected(params.get('meeting')); }
  }, []);
  useEffect(() => {
    let active = true, unlisten: (() => void) | undefined;
    const open = (id: string) => { if (active && /^[0-9a-f-]{36}$/i.test(id)) { setView('library'); setSelected(id); } };
    void listen<string>('synth-open-meeting', event => open(event.payload)).then(dispose => {
      if (!active) { dispose(); return; }
      unlisten = dispose;
      void invoke<string | null>('take_meeting_link').then(id => { if (id) open(id); }).catch(() => undefined);
    }).catch(() => undefined);
    return () => { active = false; unlisten?.(); };
  }, []);
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
    if (!cursor || loading) return;
    const run = generation.current;
    setLoading(true); setError('');
    try {
      const page = await request<Page<Meeting>>('GET', `/v1/library?limit=100&after=${cursor}`);
      if (!mounted.current || run !== generation.current) return;
      setMeetings(old => [...old, ...page.items.filter(item => !old.some(m => m.id === item.id))]);
      setCursor(page.next_cursor ?? null);
    } catch (e) { if (mounted.current && run === generation.current) setError(message(e)); }
    finally { if (mounted.current && run === generation.current) setLoading(false); }
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
    event.preventDefault(); if (!query.trim()) { resetSearch(); return; }
    const run = ++searchGeneration.current;
    setSearching(true); setError('');
    try {
      const result = await request<Page<SearchHit>>('GET', `/v1/search?q=${encodeURIComponent(query.trim())}${folder ? `&folder_id=${folder}` : ''}`);
      if (!mounted.current || run !== searchGeneration.current) return;
      setHits(result.items); setSelected(null);
    } catch (e) { if (mounted.current && run === searchGeneration.current) setError(message(e)); }
    finally { if (mounted.current && run === searchGeneration.current) setSearching(false); }
  }
  function resetSearch() { searchGeneration.current++; setSearching(false); setHits(null); }
  function chooseFolder(id: string | null) { setView('library'); searchGeneration.current++; setSearching(false); setFolder(id); setSelected(null); setHits(null); setManageFolder(false); }
  const visible = meetings.filter(m => !folder || m.folder_id === folder);
  const currentFolder = folders.find(f => f.id === folder);
  const folderTitle = currentFolder?.name ?? 'Todas las reuniones';

  function leaveSetup(exit: SetupExit) { setSetupDone(true); setSelected(null); setView(exit); }
  if (setupDone === null) return null;
  if (!setupDone) return <TooltipProvider><SetupFlow email={email} onDone={leaveSetup} /></TooltipProvider>;

  return <TooltipProvider><div className={`voice-workspace${view === 'capture' || (view === 'library' && selected) ? ' is-editor' : view === 'home' ? ' is-home' : ''}`}>
    <aside className="voice-sidebar" aria-label={`Navegación de ${brand.name}`}>
      <div className="voice-brand"><Image src={brand.logo} width={28} height={28} alt="" /><div><strong>{brand.name}</strong><span>{brand.tagline}</span></div></div>
      <div className="voice-sidebar-body">
        <button type="button" className="voice-capture-link" onClick={() => { setPrepared(null); setView('capture'); setSelected(null); }}><Mic size={16} /> Nueva nota <ChevronRight size={14} /></button>
        <p className="voice-nav-label">TU ESPACIO</p>
        <button type="button" className={`voice-nav-row ${view === 'home' ? 'is-selected' : ''}`} onClick={() => { setView('home'); setSelected(null); }}><Home size={16} /> Inicio</button>
        <button type="button" className={`voice-nav-row ${view === 'library' && !folder ? 'is-selected' : ''}`} onClick={() => chooseFolder(null)}><Library size={16} /> Biblioteca</button>
        <button type="button" className={`voice-nav-row ${view === 'ask' ? 'is-selected' : ''}`} onClick={() => { setView('ask'); setSelected(null); }}><MessageSquareText size={16} /> Preguntar</button>
        <button type="button" className={`voice-nav-row ${view === 'tasks' ? 'is-selected' : ''}`} onClick={() => { setView('tasks'); setSelected(null); }}><ListChecks size={16} /> Mis tareas</button>
        <button type="button" className={`voice-nav-row ${view === 'glossary' ? 'is-selected' : ''}`} onClick={() => { setView('glossary'); setSelected(null); }}><BookA size={16} /> Glosario</button>
        <button type="button" className={`voice-nav-row ${view === 'operations' ? 'is-selected' : ''}`} onClick={() => { setView('operations'); setSelected(null); }}><Activity size={16} /> Procesamiento</button>
        <button type="button" className={`voice-nav-row ${view === 'integrations' ? 'is-selected' : ''}`} onClick={() => { setView('integrations'); setSelected(null); }}><Plug size={16} /> Integraciones</button>
        <button type="button" className={`voice-nav-row ${view === 'voice' ? 'is-selected' : ''}`} onClick={() => { setView('voice'); setSelected(null); }}><Mic size={16} /> Mi perfil de voz</button>
        <div className="voice-nav-label voice-folder-heading">CARPETAS <Button variant="ghost" size="xs" aria-label="Crear carpeta" onClick={() => setNewFolder(!newFolder)}><Plus size={14} /></Button></div>
        {folders.map(f => <button type="button" key={f.id} className={`voice-nav-row ${folder === f.id ? 'is-selected' : ''}`} onClick={() => chooseFolder(f.id)}><FolderClosed size={16} /><span>{f.name}</span></button>)}
        {newFolder && <form className="voice-folder-form" onSubmit={createFolder}><FormField label="Nueva carpeta" hint="Privada por defecto."><Field.Control className="voice-input" value={folderName} onChange={e => setFolderName(e.target.value)} maxLength={200} autoFocus /></FormField><div className="flex gap-2"><Button loading={creating} type="submit" size="sm" disabled={!folderName.trim()}>Crear</Button><Button type="button" variant="ghost" size="sm" onClick={() => setNewFolder(false)}>Cancelar</Button></div></form>}
      </div>
      <button type="button" className={`voice-nav-row voice-settings-link ${view === 'settings' ? 'is-selected' : ''}`} onClick={() => { setView('settings'); setSelected(null); }}><Settings2 size={16} /> Ajustes</button>
      <button type="button" className={`voice-nav-row ${view === 'about' ? 'is-selected' : ''}`} onClick={() => { setView('about'); setSelected(null); }}><Info size={16} /> Acerca de {brand.name}</button>
      <div className="voice-sidebar-footer"><LockKeyhole size={14} /><div><span>Acceso privado</span><small>{actor || 'Conectando a tu biblioteca…'}</small></div></div>
    </aside>
    <main className="voice-main" tabIndex={0} aria-label={`Contenido de ${brand.name}`}>
      {(view === 'capture' || (view === 'library' && selected)) && <div className="meeting-home"><Button variant="ghost" aria-label="Volver a la biblioteca" onClick={() => { setView('home'); setSelected(null); }}><Home size={18} /></Button><span>{brand.name}</span></div>}
      <header className="voice-topbar"><span>{brand.name} <ChevronRight size={12} /> {view === 'about' ? 'Acerca de' : view === 'settings' ? 'Ajustes' : view === 'voice' ? 'Perfil de voz' : view === 'ask' ? 'Preguntar' : view === 'tasks' ? 'Mis tareas' : view === 'glossary' ? 'Glosario' : view === 'integrations' ? 'Integraciones' : view === 'capture' ? 'Nueva nota' : view === 'operations' ? 'Procesamiento' : selected ? 'Reunión' : view === 'home' ? 'Inicio' : folderTitle}</span><Button variant="ghost" size="xs" loading={loading} onClick={() => { setDetailRefresh(old => old + 1); void refresh(); }}><RefreshCw size={14} /> Actualizar</Button></header>
      <MeetingDetection showSettings={view === 'settings'} onPrepare={() => { setPrepared(null); setView('capture'); setSelected(null); }} />
      {error && <div className="voice-alert" role="alert">{error}</div>}
      {view === 'about' ? <div className="voice-library"><About /></div> : view === 'home' ? <CalendarHome folders={folders} meetings={meetings} loading={loading} refreshToken={detailRefresh} onRefresh={() => void refresh()} onOpen={id => { setView('library'); setSelected(id); }} onConnect={() => setView('integrations')} onPrepare={pack => { setPrepared(pack); setView('capture'); setSelected(null); }} /> : view === 'settings' ? <SettingsView email={email} onOpenVoice={() => setView('voice')} /> : view === 'voice' ? <VoiceOnboarding /> : view === 'ask' ? <AskView folders={folders} onOpenMeeting={meetingId => { setView('library'); setSelected(meetingId); }} /> : view === 'tasks' ? <MyTasks onOpenMeeting={meetingId => { setView('library'); setSelected(meetingId); }} /> : view === 'glossary' ? <GlossaryView /> : view === 'integrations' ? <IntegrationsView folders={folders} refreshToken={detailRefresh} onCreateFolder={() => { chooseFolder(null); setNewFolder(true); }} /> : view === 'capture' ? <CaptureView folders={folders} prepared={prepared} onQueued={showProcessing} /> : view === 'operations' ? <OperationsView folders={folders} meetings={meetings} refreshToken={detailRefresh} onOpen={id => { setView('library'); setSelected(id); void refresh(); }} /> : selected ? <MeetingDetail key={selected} id={selected} refreshToken={detailRefresh} folders={folders} onBack={() => setSelected(null)} onChanged={refresh} /> : <div className="voice-library">
        <div className="voice-page-heading"><div><p className="voice-eyebrow">REUNIONES Y CONOCIMIENTO</p><h1>{folderTitle}</h1><p>Notas, conversaciones y decisiones, con sus fuentes.</p></div>{currentFolder?.can_manage ? <Button variant="outline" size="sm" onClick={() => setManageFolder(!manageFolder)}>{manageFolder ? 'Cerrar ajustes' : 'Gestionar carpeta'}</Button> : <LockKeyhole size={22} className="text-muted-foreground" />}</div>
        {manageFolder && currentFolder?.can_manage && <FolderManagement key={currentFolder.id} folder={currentFolder} onChanged={refresh} onDeleted={() => { chooseFolder(null); void refresh(); }} />}
        <form className="voice-search" role="search" onSubmit={search}><Search size={17} /><input aria-label="Buscar en transcripciones" placeholder="Buscar en las conversaciones…" value={query} maxLength={500} onChange={e => { setQuery(e.target.value); resetSearch(); }} /><Button type="submit" variant="outline" size="sm" loading={searching}>Buscar</Button></form>
        {hits !== null ? <><div className="voice-section-label">{hits.length} resultados <Button variant="ghost" size="xs" onClick={() => { resetSearch(); setQuery(''); }}>Limpiar búsqueda</Button></div>{hits.map(hit => <button type="button" className="voice-result" key={hit.meeting_id} onClick={() => setSelected(hit.meeting_id)}><strong>{meetings.find(m => m.id === hit.meeting_id)?.title ?? 'Reunión accesible'}</strong>{hit.evidence.map(e => <p key={e.segment_id}>{e.quote}</p>)}<small>Transcripción v{hit.transcript_version}</small></button>)}{!hits.length && <Empty title="No hay coincidencias" detail="Prueba otra palabra o consulta otra carpeta." />}</> : <>
          <div className="voice-section-label">{visible.length} reuniones cargadas</div>
          {!loading && !visible.length && <Empty title="Tu biblioteca empieza aquí" detail="Graba una reunión o guarda una conversación en esta carpeta." />}
          {loading && !meetings.length && <p role="status" className="text-muted-foreground">Cargando tu biblioteca…</p>}
          <div className="voice-meeting-list">{visible.map(meeting => <button type="button" className="voice-meeting-row" key={meeting.id} onClick={() => setSelected(meeting.id)}><div className="voice-row-icon"><FileText size={18} /></div><div className="voice-row-content"><strong>{meeting.title}</strong><small>{meeting.created_at ? `${new Date(meeting.created_at).toLocaleDateString('es-ES', { day: 'numeric', month: 'short' })} · ` : ''}{folders.find(f => f.id === meeting.folder_id)?.name ?? 'Privada · Sin carpeta'}</small></div><span className="voice-state" data-state={meeting.state}>{stateLabels[meeting.state] ?? meeting.state}</span><ChevronRight size={16} /></button>)}</div>
          {cursor && <Button variant="outline" loading={loading} onClick={() => void more()}>Cargar más reuniones</Button>}
        </>}
      </div>}
    </main>
  </div></TooltipProvider>;
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="voice-empty"><FileText size={26} /><h2>{title}</h2><p>{detail}</p></div>;
}
