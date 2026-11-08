/**
 * Simulated native bridge for browser checks of the Next UI. Active only when
 * NEXT_PUBLIC_SYNTH_MOCK=1 and the page is not inside Tauri. `?mock=` selects a state:
 * empty | error | loading | long | nospeakers | processing | setup (default: a complete meeting).
 */
import type { GlossaryTerm, MinutesResponse, TaskRow, TranscriptVersion, VoiceStatus } from './minutes';
import type { LiveVoiceStatus } from './useLiveVoices';

const MEETING = '11111111-1111-4111-8111-111111111111';
const FOLDER = '22222222-2222-4222-8222-222222222222';
const MARIA = '33333333-3333-4333-8333-333333333333';
const QUESTION = '66666666-6666-4666-8666-666666666666';
const CAPTURE = '99999999-9999-4999-8999-999999999999';
let liveVoices: LiveVoiceStatus | null = null;
let savedHumanNotes = '';
let learningEnabled = false;

function liveVoiceFixture(): LiveVoiceStatus {
  const profiles = [{ id: MARIA, name: state().endsWith('long') ? 'María de los Ángeles Fernández de la Fuente' : 'María', employee_id: MARIA },
    { id: '44444444-4444-4444-8444-444444444444', name: 'Ana', employee_id: '44444444-4444-4444-8444-444444444444' }];
  return { capture_id: CAPTURE, state: 'collecting', confirmation_revision: 0, eligible_profiles: profiles,
    matches: state().endsWith('empty') ? [] : [0,1].map(index => ({
      group_id: index ? 'microphone:480000:S1' : 'system:0:S1', source: index ? 'microphone' : 'system',
      state: index ? 'unknown' : 'suggested', confirmed: false, can_identify: true,
      candidate_name: index ? null : profiles[0].name, candidate_employee_id: index ? null : MARIA,
      profile_id: index ? null : MARIA, evidence_ids: [1,2,3].map(n => String(n+index*3).repeat(64)),
      source_ranges_ms: [[index*30000,index*30000+5000],[index*30000+5000,index*30000+10000]],
      speech_spans_ms: [[index*30000,(index+1)*30000]], start_ms: index*30000, end_ms: (index+1)*30000,
    })) };
}

function liveHistory() {
  return state().endsWith('empty') ? [] : [
    { source: 'system', text: 'Podemos preparar la propuesta para el jueves, pero falta validar el presupuesto.', start: 0 },
    { source: 'microphone', text: 'Lo reviso con el equipo esta tarde. Todavía no confirmamos la fecha.', start: 30 },
  ].map((row,index) => ({ id: String(index), capture_id: CAPTURE, block_id: row.source+':'+row.start*48000,
    sequence_id: index+1, source: row.source, text: row.text, display_time: '10:00:00',
    audio_start_time: row.start, audio_end_time: row.start+12, revision: 2, evidence_state: 'aligned' }));
}

interface MockWindow { __SYNTH_MOCK__?: boolean; __TAURI_INTERNALS__?: unknown }

export function mockEnabled(): boolean {
  if (typeof window === 'undefined' || process.env.NEXT_PUBLIC_SYNTH_MOCK !== '1') return false;
  installTauriShim();
  return Boolean((window as MockWindow).__SYNTH_MOCK__);
}

const NATIVE_IDLE: Record<string, unknown> = {
  api_get_meetings: [], api_search_transcripts: [], check_first_launch: false,
  get_onboarding_status: { completed: true, current_step: 4, model_status: { parakeet: 'downloaded', summary: 'downloaded' } },
  get_ollama_models: [],
  whisper_get_available_models: [{ name: 'large-v3-turbo-q5_0', status: 'Available', size_mb: 547 }],
  trigger_microphone_permission: true, trigger_system_audio_permission_command: true, 'plugin:app|version': '0.4.1',
  get_recording_state: { is_recording: false, is_paused: false, is_active: false, recording_duration: null, active_duration: null },
  get_audio_devices: [],
  'plugin:store|load': 1, 'plugin:store|get': [null, false], 'plugin:store|has': false,
};

/** Outside Tauri, answer native invokes so hooks that listen for events or poll status render a calm idle state. */
export function installTauriShim(): void {
  const w = window as MockWindow;
  if (w.__SYNTH_MOCK__ !== undefined) return;
  if (w.__TAURI_INTERNALS__) { w.__SYNTH_MOCK__ = false; return; }
  let next = 1;
  w.__SYNTH_MOCK__ = true;
  (w as MockWindow & { __TAURI_EVENT_PLUGIN_INTERNALS__?: unknown }).__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener: () => undefined };
  w.__TAURI_INTERNALS__ = {
    transformCallback: () => next++,
    unregisterCallback: () => undefined,
    convertFileSrc: (path: string) => path,
    metadata: { currentWindow: { label: 'main' }, currentWebview: { windowLabel: 'main', label: 'main' } },
    invoke: async (cmd: string, args: { method?: string; path?: string; body?: unknown } = {}) => {
      if (cmd === 'synth_desktop_request' || cmd === 'synth_pipeline_request') return mockRequest(args.method ?? 'GET', args.path ?? '/', args.body);
      if (state().startsWith('livevoices')) {
        if (cmd === 'get_recording_state') return { is_recording: true, is_paused: false, is_active: true, recording_duration: 45, active_duration: 45, capture_id: CAPTURE };
        if (cmd === 'get_transcript_history') return liveHistory();
        if (cmd === 'get_audio_devices') return [];
      }
      if (cmd === 'synth_detection_status') return { settings: { enabled: false, excluded_apps: [] }, available: false, error: null, pending: [] };
      if (cmd === 'plugin:event|listen') return next++;
      if (cmd === 'get_onboarding_status' && state() === 'setup') return { completed: false, current_step: 0, model_status: {} };
      const idle = NATIVE_IDLE[cmd];
      if (idle !== undefined) return idle;
      return null;
    },
  };
}

const SIGNED_OUT = { mode: 'supabase', signed_in: false, allowed_domains: ['example.com'] };

/** ?mock=signin | waiting | failed | offline render each sign-in state; offline fails like a host that has not started. */
function authState() {
  const mode = state();
  if (mode === 'offline') throw new Error('Falta configurar el acceso de esta app al servicio local.');
  if (mode === 'signin') return SIGNED_OUT;
  if (mode === 'waiting' || mode === 'failed') return { ...SIGNED_OUT, flow_state: mode };
  return { mode: 'keys', signed_in: true };
}

function state(): string {
  const params = new URLSearchParams(window.location.search);
  const variant = params.get('state');
  return variant?.startsWith('livevoices') ? variant : params.get('mock') ?? 'complete';
}

const lines: [string, string, number][] = [
  ['system:S1', 'Buenos días. Hoy cerramos el piloto de Valdivar y la infraestructura para Lucerno.', 4],
  ['microphone:local', 'Perfecto. Por mi parte traigo el presupuesto revisado: son 40.000 euros para el primer trimestre.', 9],
  ['system:S2', 'Valdivar pide adelantar el piloto al 15 de noviembre porque su comité se reúne a final de mes.', 16],
  ['system:S1', 'Si lo adelantamos, necesitamos el entorno de Nimbus listo antes del viernes.', 23],
  ['microphone:local', 'Lo preparo yo. Montamos Postgres como servicio y el bucket en el mismo proyecto.', 30],
  ['system:S2', 'Vale.', 33],
  ['system:S2', 'Decidimos usar Nimbus porque Lucerno exige que los datos se queden en la Unión Europea.', 38],
  ['system:S1', 'Ana revisará los permisos de las carpetas antes de dar acceso al cliente.', 46],
  ['microphone:local', 'Queda abierta la retención del audio: no hemos decidido el plazo.', 54],
  ['system:S1', 'La demo de Cinta con el glosario queda para el jueves.', 58],
];

function transcript(): TranscriptVersion {
  const long = state() === 'long';
  const repeat = long ? 14 : 1;
  const turns = [] as NonNullable<TranscriptVersion['content']['turns']>;
  const segments = [] as TranscriptVersion['content']['segments'];
  for (let r = 0; r < repeat; r++) {
    for (const [speaker, text, at] of lines) {
      const i = turns.length;
      const start = (r * 60 + at) * 1000;
      const id = `s${String(i).padStart(6, '0')}`;
      const speakerId = state() === 'nospeakers' ? null : speaker;
      segments.push({ id, text, start_ms: start, end_ms: start + 5000, speaker_id: speakerId, source_id: speaker.split(':')[0] });
      turns.push({ id: `t${String(i).padStart(6, '0')}`, text, start_ms: start, end_ms: start + 5000, speaker_id: speakerId,
        segment_ids: [id], interjection: text === 'Vale.', word_count: text.split(' ').length });
    }
  }
  return { version: 2, model_fingerprint: 'glossary-corrected:whisper:3942…', content: {
    language: 'es', duration_ms: (repeat * 60 + 5) * 1000, segments, turns, raw_version: 1,
    speakers: state() === 'nospeakers' ? undefined : { 'system:S2': { status: 'confirmed', name: 'María', employee_id: MARIA } },
    corrections: [{ id: 'c0001', segment_id: 's000000', original: 'Baldibar', replacement: 'Valdivar', term: 'Valdivar', status: 'applied', origin: 'alias' },
      { id: 'c0002', segment_id: 's000009', original: 'Cinta', replacement: 'Synth', term: 'Synth', status: 'suggested', origin: 'alias',
        start: 11, turn_id: 't000009', turn_start: 11 }],
  } };
}

function minutes(): MinutesResponse {
  return { version: 3, transcript_version: 2, created_at: '2026-10-27T10:30:00Z', minutes: {
    schema_version: 2, meeting_type: 'comite',
    tldr: 'El comité adelanta el piloto de Valdivar al 15 de noviembre y elige Nimbus porque Lucerno exige datos en la UE. Queda abierto el plazo de retención del audio.',
    topics: [
      { title: 'Piloto de Valdivar', context: 'Valdivar quiere decidir en su comité de final de mes.',
        discussion: 'María propone adelantar el piloto; tú confirmas el presupuesto de 40.000 euros para el primer trimestre.',
        outcome: 'El piloto se adelanta al 15 de noviembre si el entorno está listo antes del viernes.',
        points: [{ text: 'Valdivar pide el piloto el 15 de noviembre porque su comité se reúne a final de mes.', citations: ['t000002'] },
          { text: 'El presupuesto revisado es de 40.000 euros para el primer trimestre.', citations: ['t000001'] }] },
      { title: 'Infraestructura en Nimbus', context: 'Lucerno condiciona dónde se guardan los datos.',
        discussion: 'Se comparan opciones y se elige Nimbus con Postgres como servicio y bucket en el mismo proyecto.',
        outcome: 'Nimbus queda aprobado; tú montas el entorno antes del viernes.',
        points: [{ text: 'Nimbus se elige porque Lucerno exige que los datos se queden en la UE.', citations: ['t000006'] },
          { text: 'Postgres irá como servicio y el bucket en el mismo proyecto de Nimbus.', citations: ['t000004'] }] },
    ],
    tasks: [
      { key: 't1', title: 'Montar el entorno de Nimbus con Postgres y bucket', owner: 'Tú', owner_evidence: 'autocompromiso',
        due_text: 'antes del viernes', due_date: '2026-10-30', citations: ['t000003', 't000004'], topic: 'Infraestructura en Nimbus' },
      { key: 't2', title: 'Revisar los permisos de las carpetas antes de dar acceso al cliente', owner: 'Ana', owner_evidence: 'explicito',
        due_text: null, due_date: null, citations: ['t000007'], topic: 'Infraestructura en Nimbus' },
    ],
    decisions: [{ key: 'd1', decision: 'Usar Nimbus para el piloto', reason: 'Lucerno exige que los datos se queden en la Unión Europea',
      status: 'acordada', citations: ['t000006'], topic: 'Infraestructura en Nimbus' },
      ...(state()==='mixeddecisions' ? [{key:'d2',decision:'Definir el plazo de retención del audio',reason:null,
        status:'pendiente de validar' as const,citations:['t000008'],topic:'Infraestructura en Nimbus'}] : [])],
    open_questions: [{ text: 'Plazo de retención del audio.', citations: ['t000008'] }],
    continuity: [
      { key: 'P1', kind: 'tarea', text: 'Pedir a Valdivar la fecha de su comité', status: 'hecha', citations: ['t000002'] },
      { key: 'D1', kind: 'decisión', text: 'Alojar el piloto en AWS', status: 'contradicha', citations: ['t000006'] },
      { key: 'P2', kind: 'tarea', text: 'Enviar el contrato marco a Lucerno', status: 'sin mencionar', citations: [] },
    ],
  } };
}

const tasks: TaskRow[] = [
  { id: 'aaaaaaaa-0000-4000-8000-000000000001', meeting_id: MEETING, item_key: 't1', title: 'Montar el entorno de Nimbus con Postgres y bucket',
    description: '', owner_name: 'Tú', owner_principal_id: null, owner_evidence: 'autocompromiso', due_text: 'antes del viernes',
    due_date: '2026-10-30', status: 'abierta', lifecycle: 'nueva', citations: ['t000003', 't000004'], meeting_title: 'Comité Valdivar · piloto', meeting_date: '2026-10-27T10:00:00Z' },
  { id: 'aaaaaaaa-0000-4000-8000-000000000002', meeting_id: MEETING, item_key: 't2', title: 'Revisar los permisos de las carpetas antes de dar acceso al cliente',
    description: '', owner_name: 'Ana', owner_principal_id: null, owner_evidence: 'explicito', due_text: null, due_date: null, status: 'abierta',
    lifecycle: 'nueva', citations: ['t000007'], meeting_title: 'Comité Valdivar · piloto', meeting_date: '2026-10-27T10:00:00Z' },
];

const glossary: GlossaryTerm[] = [
  { id: 'g1', scope: 'workspace', folder_id: null, term: 'Valdivar', kind: 'organizacion', aliases: ['Baldibar'], definition: 'Cliente industrial del piloto de nóminas', status: 'aprobado', origin: 'manual', occurrences: 12, examples: [],
    alias_modes: [{ alias: 'Baldibar', mode: 'corrige', reason: 'inventado' }] },
  { id: 'g9', scope: 'workspace', folder_id: null, term: 'Synth', kind: 'producto', aliases: ['Cinta', 'PH'], definition: 'Producto de Synth para empresas', status: 'aprobado', origin: 'manual', occurrences: 9, examples: [],
    alias_modes: [{ alias: 'Cinta', mode: 'sugiere', reason: 'palabra' }, { alias: 'PH', mode: 'inactivo', reason: 'corto' }] },
  { id: 'g2', scope: 'workspace', folder_id: null, term: 'Luservo', kind: 'concepto', aliases: [], definition: 'Posible alias de 00000000-0000-4000-8000-0000000000c3', status: 'propuesto', origin: 'fonetico', occurrences: 4,
    examples: ['Y luego con Luservo, que pide fecha.', 'Luservo nos manda el contrato el lunes.'] },
  { id: '00000000-0000-4000-8000-0000000000c3', scope: 'workspace', folder_id: null, term: 'Lucerno', kind: 'organizacion', aliases: [], definition: 'Cliente de logística', status: 'aprobado', origin: 'manual', occurrences: 7, examples: [] },
  { id: 'g4', scope: 'workspace', folder_id: null, term: 'Atlántica', kind: 'concepto', aliases: [], definition: '', status: 'propuesto', origin: 'llm', occurrences: 1, examples: ['oído como «Atlantia»'] },
  { id: 'g5', scope: 'workspace', folder_id: null, term: 'Nimbus', kind: 'sistema', aliases: [], definition: 'Plataforma donde se despliegan la API y el worker', status: 'aprobado', origin: 'llm', occurrences: 5, examples: [] },
  { id: 'g6', scope: 'workspace', folder_id: null, term: 'María López', kind: 'persona', aliases: [], definition: 'Responsable de RR. HH. en Valdivar', status: 'aprobado', origin: 'llm', occurrences: 3, examples: [] },
  { id: 'g7', scope: 'workspace', folder_id: null, term: 'Atlas', kind: 'proyecto', aliases: [], definition: 'Migración de nóminas al sistema nuevo', status: 'aprobado', origin: 'llm', occurrences: 6, examples: [] },
  { id: 'g8', scope: 'workspace', folder_id: null, term: 'OKR', kind: 'concepto', aliases: [], definition: 'Objetivos trimestrales del equipo', status: 'aprobado', origin: 'manual', occurrences: 2, examples: [] },
];

const voice: VoiceStatus = { state: 'suggestions', transcript_version: 2, resolved: ['system:S2'], matches: {
  'system:S1': { state: 'ask', reason: 'ask', similarity: 0.64, candidate_name: 'Pablo Ruiz', candidate_employee_id: '44444444-4444-4444-8444-444444444444' },
} };

function delay<T>(value: T, ms = 120): Promise<T> {
  return new Promise(resolve => setTimeout(() => resolve(value), ms));
}

export async function mockRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  const mode = state();
  if (path.startsWith('/v1/calendar')) {
    const today = new Date(); today.setHours(10,0,0,0);
    const items = [1,2,4,5,7].map((offset, index) => {
      const start = new Date(today); start.setDate(today.getDate()+offset);
      return {id: `eeeeeeee-0000-4000-8000-${String(index+1).padStart(12,'0')}`, folder_id:FOLDER, content_hash:'fixture-hash',
        content:{title:index % 2 ? 'Revisión del piloto · Valdivar' : 'Synth · seguimiento de producto',
          description:'Revisar el piloto, los permisos y los próximos pasos.', start:start.toISOString(),
          end:new Date(start.getTime()+1800000).toISOString(), all_day:false,
          attendees:[{name:'María',email:'maria@example.test',response:'accepted'}, {name:'Ana',email:'ana@example.test',response:'needsAction'}]}};
    });
    if (path.endsWith('/preparation') || path.endsWith('/folder')) {
      const selected = items.find(e => path.includes(e.id)) ?? items[0];
      const folderId = method === 'PUT' ? (body as {folder_id:string|null}).folder_id : selected.folder_id;
      return {event_id:selected.id,event_hash:selected.content_hash,event:selected.content,folder_id:folderId,
        folder:folderId ? 'Clientes' : null,fingerprint:'synthetic-context',expected_attendees:selected.content.attendees.map((a,index)=>({...a,
          principal_id:index ? null : MARIA,profile_available:!index})),
        glossary:folderId ? [{id:'term1',term:'Synth',definition:'Producto de Synth'}] : [],
        passages:folderId ? [{evidence_id:MEETING+'@2:t000006',meeting_id:MEETING,transcript_version:2,
          turn_id:'t000006',meeting_title:'Comité Valdivar · piloto',text:'Decidimos usar Nimbus porque Lucerno exige datos en la Unión Europea.',time:'00:38'}] : []} as T;
    }
    return {configured:true,connected:mode !== 'calendar-disconnected' && mode !== 'empty',email:'alex@example.test',
      synced_at:new Date().toISOString(),items:mode === 'empty' ? [] : items,read_only:true} as T;
  }
  if (mode.startsWith('livevoices') && path.startsWith('/v1/voice/captures/')) {
    if (mode.endsWith('error')) throw new Error('Synthetic voice status unavailable');
    liveVoices ??= liveVoiceFixture();
    if (method === 'POST') {
      const decision = body as { action: string; group_id: string; profile_id: string | null; confirmed_by_user: boolean };
      const profile = liveVoices.eligible_profiles?.find(p => p.id === decision.profile_id);
      liveVoices = { ...liveVoices, confirmation_revision: (liveVoices.confirmation_revision ?? 0)+1,
        matches: liveVoices.matches.map(row => row.group_id !== decision.group_id ? row : {
          ...row, state: decision.action === 'clear' ? 'unknown' : 'confirmed', confirmed: decision.action !== 'clear',
          candidate_name: profile?.name ?? null, candidate_employee_id: profile?.employee_id ?? null, profile_id: profile?.id ?? null,
        }) };
    }
    return structuredClone(liveVoices) as T;
  }
  if (mode === 'loading') return new Promise<T>(() => undefined);
  if (mode === 'error' && path !== '/v1/me') throw new Error('No se pudo conectar con el servicio de tu organización. Revisa la conexión y vuelve a intentarlo.');
  const resource = path.split('?')[0];
  const empty = mode === 'empty';
  const processing = mode === 'processing' || mode === 'streaming';
  const meeting = { id: MEETING, title: 'Comité Valdivar · piloto', folder_id: FOLDER, state: processing ? 'processing' : 'ready', revision: 4,
    notes: savedHumanNotes, created_at: '2026-10-27T10:00:00Z', updated_at: '2026-10-27T10:31:00Z' };
  const routes: Record<string, () => unknown> = {
    '/v1/people': () => ({ items: state() === 'empty' ? [] : [{ id: MARIA, name: 'María' }, { id: '44444444-4444-4444-8444-444444444444', name: 'Ana' }] }),
    '/v1/me': () => ({ id: 'me', name: 'Ana García', kind: 'user' }),
    '/v1/folders': () => ({ items: [{ id: FOLDER, name: 'Clientes', can_manage: true, can_edit: true }] }),
    [`/v1/folders/${FOLDER}/members`]: () => ({ items:[{name:'María',role:'editor'},{name:'Ana',role:'reader'}] }),
    '/v1/library': () => ({ items: empty ? [] : mode === 'calendar-long' ? Array.from({length:24}, (_,index) => ({ ...meeting, id:`55555555-5555-4555-8555-${String(index+1).padStart(12,'0')}`, title:`Seguimiento del piloto · ${index+1}` })) : [meeting, { ...meeting, id: '55555555-5555-4555-8555-555555555555', title: 'Synth Infra', folder_id: null }], next_cursor: null }),
    [`/v1/meetings/${MEETING}`]: () => meeting,
    [`/v1/meetings/${MEETING}/transcript`]: transcript,
    [`/v1/meetings/${MEETING}/minutes`]: () => { if (processing) throw new Error('Todavía no hay un acta para la transcripción actual.'); return minutes(); },
    [`/v1/meetings/${MEETING}/generation`]: () => ({ generation: mode === 'streaming' ? {
      id: '77777777-7777-4777-8777-777777777777', job_id: '88888888-8888-4888-8888-888888888888',
      transcript_version: 2, state: 'running', last_sequence: 3,
    } : null }),
    [`/v1/meetings/${MEETING}/tasks`]: () => ({ items: tasks }),
    [`/v1/meetings/${MEETING}/decisions`]: () => ({ items: [{ id: 'bbbbbbbb-0000-4000-8000-000000000001', meeting_id: MEETING, item_key: 'd1', decision: 'Usar Nimbus para el piloto',
      reason: 'Lucerno exige que los datos se queden en la Unión Europea', alternatives: [], status: 'acordada', affects: null, decided_by: null, citations: ['t000006'] }] }),
    '/v1/tasks': () => ({ items: empty ? [] : tasks.filter(t => t.owner_name === 'Tú') }),
    '/v1/glossary': () => ({ items: empty ? [] : glossary }),
    [`/v1/voice/meetings/${MEETING}`]: () => voice,
    '/v1/voice': () => ({ profiles: [], corporate_profiles: [], sync_state: 'local_only' }),
    '/v1/voice/learning': () => ({ enabled:learningEnabled,sync_state:'available',outbox:{},samples:[] }),
    '/v1/auth/state': authState,
    '/v1/status': () => ({ worker: {}, audio_location: 'this_mac', document_route: 'configured_gateway',
      asr_backend: 'qwen3', audio_setup: {state:'ready',step:3,message:'Audio preparado'} }),
    '/v1/tasks/pipeline': () => ({ items: [] }),
  };
  const asked = { id: QUESTION, question: '¿Por qué elegimos Nimbus?', state: 'queued', error_code: null, answer: null };
  if (method === 'POST' && resource === '/v1/voice/learning/settings') learningEnabled=(body as {enabled:boolean}).enabled;
  if (method === 'PUT' && resource === `/v1/meetings/${MEETING}/notes`) { savedHumanNotes=(body as {notes:string}).notes; return delay({revision:5} as T); }
  if (mode === 'streaming' && resource.endsWith('/generations/77777777-7777-4777-8777-777777777777/events')) {
    const after = Number(new URLSearchParams(path.split('?')[1]).get('after') ?? 0);
    const events = [{ sequence: 1, kind: 'started', content: {} }, ...minutes().minutes.topics.map((topic, index) =>
      ({ sequence: index + 2, kind: 'topic', content: { index, topic } }))].filter(event => event.sequence > after)
      .map(event => ({ ...event, generation_id: '77777777-7777-4777-8777-777777777777', transcript_version: 2 }));
    return delay({ events } as T);
  }
  if (method === 'POST' && resource === '/v1/questions') return delay(asked as T);
  if (resource === `/v1/questions/${QUESTION}`) return delay({ ...asked, state: 'answered', answer: { status: 'answered',
    text: 'Porque Lucerno exige que los datos se queden en la Unión Europea; Nimbus permite alojarlos allí.',
    citations: [{ meeting_id: MEETING, turn_id: 't000006', transcript_version: 2, meeting_title: 'Comité Valdivar · piloto', meeting_date: '2026-10-27',
      time: '00:38', speaker: 'María', text: 'Decidimos usar Nimbus porque Lucerno exige que los datos se queden en la Unión Europea.' }] } } as T);
  if (resource === `/v1/meetings/${MEETING}/follow-up-email`) return delay({ subject: 'Resumen y próximos pasos: Comité Valdivar · piloto',
    body: 'Hola:\n\nTe paso el resumen…', sent: false } as T);
  if (method !== 'GET') return delay({ ok: true, transcript_version: 3 } as T);
  const handler = routes[resource];
  if (!handler) throw new Error('Esta operación no está disponible en la vista previa.');
  return delay(handler() as T);
}

if (typeof window !== 'undefined' && process.env.NEXT_PUBLIC_SYNTH_MOCK === '1') installTauriShim();
