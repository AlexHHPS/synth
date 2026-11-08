import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const originalCore = { ...await import('@tauri-apps/api/core') };
const originalApi = { ...await import('../../src/synth/api') };
const originalMock = { ...await import('../../src/synth/mock') };
afterAll(() => {
  mock.module('@tauri-apps/api/core', () => originalCore);
  mock.module('../../src/synth/api', () => originalApi);
  mock.module('../../src/synth/mock', () => originalMock);
});

class TestChannel { onmessage: (value: any) => void = () => {}; }
const streams: { channel: TestChannel; args: any; finish: () => void }[] = [];
const invoke = mock((_cmd: string, args: any) => new Promise<void>(finish => streams.push({ channel: args.onEvent, args, finish })));
mock.module('@tauri-apps/api/core', () => ({ ...originalCore, Channel: TestChannel, invoke }));
mock.module('../../src/synth/mock', () => ({ ...originalMock, mockEnabled: () => false }));
let generation: any;
let serverNotes = 'Original', serverRevision = 1;
const writes: any[] = [];
const request = mock(async (method: string, path: string, body?: unknown): Promise<unknown> => {
  if (method === 'PUT') { writes.push(body); throw new Error('Conflicto de notas'); }
  if (path.endsWith('/generation')) return { generation };
  if (path.endsWith('/transcript')) return { version: 1, model_fingerprint: 'synthetic', content: { language: 'es', duration_ms: 60000, segments: [] } };
  if (path.endsWith('/minutes')) throw new Error('Acta pendiente');
  if (path.endsWith('/tasks') || path.endsWith('/decisions')) return { items: [] };
  return { id: path.split('/').at(-1), title: 'Synthetic meeting', state: 'processing', notes: serverNotes, revision: serverRevision };
});
mock.module('../../src/synth/api', () => ({ ...originalApi, request, pipelineRequest: async () => ({ matches: {}, state: 'unavailable' }) }));
// Test the draft contract without instantiating the DOM-based rich editor.
mock.module('next/dynamic',()=>({default:()=>({value,onChange}:any)=><textarea value={value} onChange={event=>onChange(event.target.value)} />}));

const { useGeneration, applyGenerationEvent } = await import('../../src/synth/useGeneration');
const { MeetingDetail } = await import('../../src/synth/MeetingDetail');
let current: ReturnType<typeof useGeneration>;
let renderer: ReactTestRenderer | undefined;
const completed = mock(() => {});
function Harness({ id = 'meeting-a', version = 1 }: { id?: string; version?: number }) {
  current = useGeneration(id, version, true, completed);
  return <output>{JSON.stringify(current.draft)}</output>;
}
beforeEach(() => {
  streams.length = 0; writes.length = 0; invoke.mockClear(); request.mockClear(); completed.mockClear();
  generation = { id: 'gen-a', job_id: 'job-a', transcript_version: 1, state: 'running', last_sequence: 0 };
  serverNotes = 'Original'; serverRevision = 1;
});
afterEach(async () => {
  await act(async () => { renderer?.unmount(); for (const stream of streams) stream.finish(); });
  renderer = undefined;
});
const event = (sequence: number, kind: string, content: unknown = {}) => ({
  event: 'generation', data: { generation_id: 'gen-a', transcript_version: 1, sequence, kind, content },
});

test('native reconnection resumes from the committed cursor and ignores duplicate events', async () => {
  await act(async () => { renderer = create(<Harness />); });
  expect(streams[0].args.after).toBe(0);
  await act(async () => {
    streams[0].channel.onmessage(event(1, 'started'));
    streams[0].channel.onmessage(event(2, 'delta', { text: 'Decisión: revisar el viernes.' }));
    streams[0].finish();
  });
  expect(streams[1].args.after).toBe(2);
  await act(async () => {
    streams[1].channel.onmessage(event(2, 'delta', { text: 'Decisión: revisar el viernes.' }));
  });
  expect(current.draft?.text).toBe('Decisión: revisar el viernes.');
  expect(request.mock.calls.every(([method]) => method === 'GET')).toBe(true);
  await act(async () => { streams[1].channel.onmessage(event(3, 'complete')); streams[1].finish(); });
  expect(completed).toHaveBeenCalledTimes(1);
});

test('late delivery from a previous meeting cannot overwrite the newly selected meeting', async () => {
  await act(async () => { renderer = create(<Harness />); });
  const old = streams[0];
  generation = { ...generation, id: 'gen-b' };
  await act(async () => { renderer!.update(<Harness id="meeting-b" />); });
  await act(async () => { old.channel.onmessage(event(1, 'delta', { text: 'Old meeting' })); old.finish(); });
  expect(current.draft?.id).toBe('gen-b');
  expect(current.draft?.text).toBe('');
});

test('an event gap never advances the cursor or silently loses text', () => {
  const draft = { id: 'gen-a', version: 1, cursor: 0, topics: [], text: '', phase: 'running' as const };
  expect(() => applyGenerationEvent(draft, event(2, 'delta', { text: 'Missing prefix' }).data as any)).toThrow('generation_sequence_gap');
  expect(draft.cursor).toBe(0);
});

test('draft from a different transcript is hidden', async () => {
  await act(async () => { renderer = create(<Harness version={2} />); });
  expect(current.draft).toBeNull();
  expect(streams.length).toBe(0);
});

test('context revocation clears already displayed draft before the stream closes', async () => {
  await act(async () => { renderer=create(<Harness />); });
  await act(async () => {
    streams[0].channel.onmessage(event(1,'delta',{text:'Synthetic previous folder context'}));
  });
  expect(current.draft?.text).toContain('previous folder');
  await act(async () => {
    streams[0].channel.onmessage({event:'error',data:{code:'generation_context_changed',status:409}});
  });
  expect(current.draft).toBeNull();
  await act(async () => { streams[0].finish(); });
  expect(completed).toHaveBeenCalledTimes(1);
  expect(streams).toHaveLength(1);
});

test('a withdrawn current generation clears the previous draft instead of retaining old context', async () => {
  await act(async () => { renderer=create(<Harness />); });
  await act(async () => { streams[0].channel.onmessage(event(1,'delta',{text:'Old draft'})); });
  generation=null;
  await act(async () => { streams[0].finish(); });
  expect(current.draft).toBeNull();
});

async function meeting(refreshToken = 0) {
  await act(async () => {
    const view = <MeetingDetail id="meeting-a" refreshToken={refreshToken} folders={[]} onBack={() => {}} onChanged={async () => {}} />;
    if (renderer) renderer.update(view); else renderer = create(view);
  });
}
async function openNotes() {
  const tab = renderer!.root.findAllByType('button').find(button => button.children.some(child=>typeof child==='string'&&child.trim()==='Mis notas'))!;
  await act(async () => { tab.props.onClick(); });
}
test('refresh during generation keeps unsaved notes and their original revision for conflict detection', async () => {
  await meeting(); await openNotes();
  await act(async () => { renderer!.root.findByType('textarea').props.onChange({ target: { value: 'Mis notas nuevas' } }); });
  serverNotes = 'Notas remotas'; serverRevision = 2;
  await meeting(1);
  expect(renderer!.root.findByType('textarea').props.value).toBe('Mis notas nuevas');
  await act(async () => { renderer!.root.findAllByType('button').find(button=>button.children.includes('Guardar notas'))!.props.onClick(); });
  expect(writes[0]).toEqual({ notes: 'Mis notas nuevas', expected_revision: 1 });
  expect(renderer!.root.findByType('textarea').props.value).toBe('Mis notas nuevas');
});

test('clearing the notes stays empty while generation refreshes', async () => {
  await meeting(); await openNotes();
  await act(async () => { renderer!.root.findByType('textarea').props.onChange({ target: { value: '' } }); });
  await meeting(1);
  expect(renderer!.root.findByType('textarea').props.value).toBe('');
});
