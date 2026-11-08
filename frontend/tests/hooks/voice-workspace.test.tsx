import { afterAll, afterEach, beforeEach, describe, expect, mock, test } from 'bun:test';
import { createContext, forwardRef } from 'react';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const originalAPI = { ...await import('../../src/synth/api') };
type Call = { method: string; path: string; body?: unknown };
let handle: (call: Call) => Promise<unknown>;
const request = mock((method: string, path: string, body?: unknown) => handle({ method, path, body }));
mock.module('../../src/synth/api', () => ({ ...originalAPI, request,
  pipelineRequest: async () => ({ state: 'unavailable', matches: {} }) }));
mock.module('next/image', () => ({ default: () => null }));
mock.module('../../src/synth/design-system/button', () => ({ Button: forwardRef(({ children, loading, variant, size, ...props }: any,ref:any) => <button ref={ref} {...props}>{children}</button>) }));
// Rich editor and portal behavior need a real browser and are checked manually;
// this suite isolates request ownership, revisions and preservation of drafts.
mock.module('next/dynamic',()=>({default:()=>({value,onChange}:any)=><textarea value={value} onChange={event=>onChange(event.target.value)} />}));
mock.module('../../src/components/ui/popover',()=>({Popover:({children}:any)=>children,PopoverTrigger:({children}:any)=>children,PopoverContent:({children}:any)=><div>{children}</div>}));
mock.module('../../src/synth/MeetingPanel',()=>({MeetingPanel:({open,children}:any)=>open?<section>{children}</section>:null}));
mock.module('../../src/synth/design-system/form-field', () => ({ FormField: ({ children }: any) => <div>{children}</div> }));
mock.module('../../src/synth/design-system/tooltip', () => ({ TooltipProvider: ({ children }: any) => children }));
mock.module('@base-ui/react/field', () => ({ Field: { Control: ({ render, ...props }: any) => render?.type === 'textarea' ? <textarea {...props} /> : <input {...props} /> } }));
mock.module('../../src/synth/CorporateSession', () => ({ CorporateSession: ({ children }: any) => children, SessionEmail: createContext(undefined) }));
const originalSetup = { ...await import('../../src/synth/setup') };
mock.module('../../src/synth/setup', () => ({ ...originalSetup, setupCompleted: async () => true }));
mock.module('../../src/synth/AudioPermissions', () => ({ AudioPermissions: () => null }));
mock.module('../../src/synth/FolderManagement', () => ({ FolderManagement: () => null }));
mock.module('../../src/synth/OperationsView', () => ({ OperationsView: () => null }));
mock.module('../../src/synth/CaptureView', () => ({ CaptureView: () => null }));
mock.module('../../src/synth/IntegrationsView', () => ({ IntegrationsView: () => null }));
mock.module('../../src/synth/MeetingDetection', () => ({ MeetingDetection: () => null }));
mock.module('../../src/synth/VoiceOnboarding', () => ({ VoiceOnboarding: () => null }));
mock.module('../../src/synth/CalendarHome', () => ({ CalendarHome: () => <div>Inicio agenda</div> }));
const { VoiceWorkspace } = await import('../../src/synth/VoiceWorkspace');
const { MeetingDetail: MeetingView } = await import('../../src/synth/MeetingDetail');
afterAll(() => mock.module('../../src/synth/api', () => originalAPI));

let renderer: ReactTestRenderer | undefined;
const originalWindow = globalThis.window;
beforeEach(() => {
  request.mockClear();
  globalThis.window = { location: { search: '' } } as Window & typeof globalThis;
  handle = async ({ method, path }) => {
    if (path === '/v1/me') return { name: 'Synthetic owner' };
    if (path === '/v1/folders') return { items: [] };
    if (path === '/v1/library?limit=100') return { items: [{ id: 'initial', title: 'Initial meeting', folder_id: null, state: 'ready' }], next_cursor: 'page-2' };
    if (method === 'GET' && path === '/v1/meetings/A') return { id: 'A', title: 'Meeting A', notes: 'Original notes', revision: 1, state: 'ready', folder_id: null };
    if (path.endsWith('/transcript')) return { version: 1, content: { segments: [], duration_ms: 1, language: 'es' } };
    throw new Error('synthetic request: ' + path);
  };
});
afterEach(async () => {
  if (renderer) await act(async () => renderer!.unmount());
  renderer = undefined;
  globalThis.window = originalWindow;
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
const text = () => JSON.stringify(renderer!.toJSON());
const button = (label: string) => renderer!.root.findAllByType('button').find(node => node.children.filter(child => typeof child === 'string').join('').includes(label))!;
async function workspace() {
  await act(async () => { renderer = create(<VoiceWorkspace />); });
  await act(async () => { button('Biblioteca').props.onClick(); });
}

describe('Synth workspace request and draft ownership', () => {
  test('opening published schema 2 minutes preserves its content and tasks in the v2 client', async () => {
    const base = handle;
    handle = async call => call.path.endsWith('/minutes') ? {
      version: 1, transcript_version: 1, model_fingerprint: 'synthetic:minutes-v2',
      markdown: '# Acta de reunión\n\nResumen de la prueba\n\n## Despliegue\n\nConfirmamos el piloto con Edu.\n\n## Tareas\n\n- Revisar el despliegue',
      minutes: { schema_version: 2, language: 'es', tldr: 'Resumen de la prueba',
        topics: [{ title: 'Despliegue', context: 'Confirmamos el piloto con Edu.', points: [] }],
        tasks: [{ title: 'Revisar el despliegue' }], decisions: [], open_questions: [] },
    } : call.path.endsWith('/tasks') ? { items: [{id:'task-1',title:'Revisar el despliegue',status:'abierta',citations:[]}] } : base(call);
    await act(async () => { renderer = create(<MeetingView id="A" refreshToken={0} folders={[{id:'folder',name:'Shared folder',can_edit:true} as any]} onBack={() => {}} onChanged={async () => {}} />); });
    expect(text()).toContain('Confirmamos el piloto con Edu.');
    expect(text()).toContain('v1 · transcripción v1');
    await act(async () => { button('Tareas y decisiones').props.onClick(); });
    expect(text()).toContain('Revisar el despliegue');
  });

  test('moving a meeting preserves an unsaved draft and its original revision', async () => {
    const base = handle;
    let moved = false;
    handle = async call => {
      if (call.method === 'PUT' && call.path.endsWith('/folder')) { moved = true; return {}; }
      if (moved && call.method === 'GET' && call.path === '/v1/meetings/A') return { id: 'A', title: 'Meeting A', notes: 'Someone else edited', revision: 3, state: 'ready', folder_id: 'folder' };
      if (call.method === 'PUT' && call.path.endsWith('/notes')) throw new Error('meeting_revision_conflict');
      return base(call);
    };
    await act(async () => { renderer = create(<MeetingView id="A" refreshToken={0} folders={[{id:'folder',name:'Shared folder',can_edit:true} as any]} onBack={() => {}} onChanged={async () => {}} />); });
    await act(async () => { button('Mis notas').props.onClick(); });
    await act(async () => { renderer!.root.findByType('textarea').props.onChange({ target: { value: 'My unsaved draft' } }); });
    await act(async () => { await renderer!.root.findByProps({'aria-label':'Carpeta de la reunión'}).props.onChange({ target: { value: 'folder' } }); });
    await act(async () => { await button('Guardar notas').props.onClick(); });
    expect(request.mock.calls.find(([method, path]) => method === 'PUT' && path.endsWith('/notes'))?.[2]).toEqual({ notes: 'My unsaved draft', expected_revision: 1 });
    expect(renderer!.root.findByType('textarea').props.value).toBe('My unsaved draft');
    expect(text()).toContain('meeting_revision_conflict');
  });

  test('clearing a pending search cannot restore its stale results', async () => {
    const pending = deferred<unknown>(); const base = handle;
    handle = call => call.path.startsWith('/v1/search?') ? pending.promise : base(call);
    await workspace();
    const input = () => renderer!.root.findByProps({ 'aria-label': 'Buscar en transcripciones' });
    await act(async () => { input().props.onChange({ target: { value: 'old query' } }); });
    await act(async () => { void renderer!.root.findByProps({ role: 'search' }).props.onSubmit({ preventDefault() {} }); });
    await act(async () => { input().props.onChange({ target: { value: '' } }); });
    await act(async () => { pending.resolve({ items: [{ meeting_id: 'stale', transcript_version: 1, evidence: [{ segment_id: 'x', quote: 'Stale search evidence' }] }] }); });
    expect(text()).not.toContain('Stale search evidence');
    expect(text()).toContain('Initial meeting');
  });

  test('moving without a concurrent notes edit still allows saving the draft', async () => {
    const base = handle; let moved = false;
    handle = async call => {
      if (call.method === 'PUT' && call.path.endsWith('/folder')) { moved = true; return {}; }
      if (moved && call.method === 'GET' && call.path === '/v1/meetings/A') return { id: 'A', title: 'Meeting A', notes: 'Original notes', revision: 2, state: 'ready', folder_id: 'folder' };
      if (call.method === 'PUT' && call.path.endsWith('/notes')) return { revision: 3 };
      return base(call);
    };
    await act(async () => { renderer = create(<MeetingView id="A" refreshToken={0} folders={[{id:'folder',name:'Shared folder',can_edit:true} as any]} onBack={() => {}} onChanged={async () => {}} />); });
    await act(async () => { button('Mis notas').props.onClick(); });
    await act(async () => { renderer!.root.findByType('textarea').props.onChange({ target: { value: 'My draft' } }); });
    await act(async () => { await renderer!.root.findByProps({'aria-label':'Carpeta de la reunión'}).props.onChange({ target: { value: 'folder' } }); });
    await act(async () => { await button('Guardar notas').props.onClick(); });
    expect(request.mock.calls.find(([method, path]) => method === 'PUT' && path.endsWith('/notes'))?.[2]).toEqual({ notes: 'My draft', expected_revision: 2 });
    expect(text()).toContain('Notas guardadas.');
  });

  test('a stale next page cannot overwrite the cursor or loading state of a refresh', async () => {
    const page = deferred<unknown>(), refreshed = deferred<unknown>(); const base = handle;
    let reads = 0;
    handle = call => {
      if (call.path.includes('&after=')) return page.promise;
      if (call.path === '/v1/library?limit=100' && ++reads > 1) return refreshed.promise;
      return base(call);
    };
    await workspace();
    await act(async () => { void button('Cargar más reuniones').props.onClick(); });
    await act(async () => { button('Actualizar').props.onClick(); });
    await act(async () => { page.resolve({ items: [{ id: 'stale', title: 'Stale next page', folder_id: null, state: 'ready' }], next_cursor: null }); });
    expect(text()).not.toContain('Stale next page');
    await act(async () => { refreshed.resolve({ items: [{ id: 'fresh', title: 'Fresh meeting', folder_id: null, state: 'ready' }], next_cursor: 'fresh-cursor' }); });
    expect(text()).toContain('Fresh meeting');
    expect(text()).not.toContain('Initial meeting');
    expect(button('Cargar más reuniones')).toBeDefined();
  });
});
