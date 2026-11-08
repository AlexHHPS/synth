import { afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const handlers = new Map<string, (event: { payload: any }) => void>();
let captureId = 'capture-a';
let transcriptionState: 'waiting'|'active' = 'active';
let history: () => Promise<any[]> = async () => [];
mock.module('@tauri-apps/api/event', () => ({ listen: async (name: string, callback: any) => {
  handlers.set(name, callback);
  return () => { if (handlers.get(name) === callback) handlers.delete(name); };
} }));
mock.module('@tauri-apps/api/core', () => ({ invoke: async (name: string) => {
  if (name === 'get_recording_state') return { capture_id: captureId,transcription_state:transcriptionState };
  if (name === 'get_transcript_history') return history();
  throw new Error(`Unexpected command ${name}`);
} }));
const { useLiveCapture } = await import('../../src/synth/useLiveCapture');
let current: ReturnType<typeof useLiveCapture>;
function Harness() { current = useLiveCapture(); return <span>{current.transcripts.map(row => row.text).join('|')}</span>; }
let renderer: ReactTestRenderer;
function update(capture_id = 'capture-a', sequence_id = 1, text = 'Texto provisional') {
  return { capture_id, sequence_id, text, source: 'system', timestamp: '10:00:00',
    chunk_start_time: 0, audio_start_time: 0, audio_end_time: 12, duration: 12,
    is_partial: true, evidence_state: 'provisional_low_voice_evidence' };
}
function emit(name: string, payload: any) { handlers.get(name)?.({ payload }); }
afterEach(async () => { await act(async () => renderer?.unmount()); handlers.clear(); captureId = 'capture-a'; transcriptionState='active'; history = async () => []; });

test('reload preserves source and uncertainty; duplicate events upsert and other captures stay out', async () => {
  history = async () => [{ ...update(), id: 'seg_1', display_time: '10:00:00' }];
  await act(async () => { renderer = create(<Harness/>); });
  expect(current!.transcripts[0].source).toBe('system');
  expect(current!.transcripts[0].is_partial).toBe(true);
  expect(current!.transcripts[0].confidence).toBeUndefined();
  await act(async () => { emit('transcript-update', update('capture-a', 1, 'Revisión')); emit('transcript-update', update('capture-b', 2, 'Otra reunión')); });
  expect(current!.transcripts.map(row => row.text)).toEqual(['Revisión']);
});

test('an event arriving during history recovery wins over the older snapshot', async () => {
  let resolve!: (rows: any[]) => void;
  history = () => new Promise(done => { resolve = done; });
  await act(async () => { renderer = create(<Harness/>); });
  await act(async () => {
    emit('transcript-update', update('capture-a', 1, 'Último bloque'));
    resolve([{ ...update(), id: 'seg_1', display_time: '10:00:00' }]);
  });
  expect(current!.transcripts.map(row => row.text)).toEqual(['Último bloque']);
});

test('a late recovery cannot replace the next capture and unmount removes listeners', async () => {
  let resolve!: (rows: any[]) => void;
  history = () => new Promise(done => { resolve = done; });
  await act(async () => { renderer = create(<Harness/>); });
  captureId = 'capture-b';
  history = async () => [{ ...update('capture-b', 1, 'Reunión B'), id: 'seg_1', display_time: '11:00:00' }];
  await act(async () => { emit('recording-started', { capture_id: captureId }); });
  await act(async () => { resolve([{ ...update(), id: 'seg_1', display_time: '10:00:00' }]); });
  expect(current!.transcripts.map(row => row.text)).toEqual(['Reunión B']);
  await act(async () => { renderer.unmount(); });
  expect(handlers.size).toBe(0);
});

test('consolidated revisions replace a preview without duplicating the block', async () => {
  history = async () => [{ ...update(), block_id: 'system:0', revision: 1,
    id: 'system:0', display_time: '10:00:00' }];
  await act(async () => { renderer = create(<Harness/>); });
  const recoveredId = current!.transcripts[0].id;
  const revision = (sequence: number, version: number, text: string) => ({ ...update('capture-a', sequence, text),
    block_id: 'system:0', revision: version, evidence_state: version === 1 ? 'provisional' : 'aligned', is_partial: version === 1 });
  await act(async () => { emit('transcript-update', revision(1, 1, 'Está aprobado')); });
  const id = current!.transcripts[0].id;
  expect(id).toBe(recoveredId);
  await act(async () => {
    emit('transcript-update', revision(3, 2, 'No está aprobado'));
    emit('transcript-update', revision(4, 1, 'Está aprobado'));
  });
  expect(current!.transcripts.map(row => row.text)).toEqual(['No está aprobado']);
  expect(current!.transcripts[0].id).toBe(id);
  expect(current!.transcripts[0].is_partial).toBe(false);
});

test('an empty consolidated block stays removed after reload and delayed previews', async () => {
  history = async () => [{ ...update('capture-a', 5, ''), block_id: 'system:0', revision: 2,
    evidence_state: 'aligned', id: 'system:0', display_time: '10:00:00' }];
  await act(async () => { renderer = create(<Harness/>); });
  await act(async () => {
    emit('transcript-update', { ...update('capture-a', 6, 'Texto descartado'), block_id: 'system:0', revision: 1 });
    emit('transcript-update', { ...update('capture-a', 4, 'Final antiguo'), block_id: 'system:0', revision: 2 });
  });
  expect(current!.transcripts).toEqual([]);
});

test('waiting state survives reopening and clears when the same capture starts transcribing', async () => {
  transcriptionState='waiting';
  await act(async () => {renderer=create(<Harness/>);});
  expect(current.waiting).toBe(true);
  await act(async () => {emit('transcription-state',{capture_id:'capture-b',state:'active'});});
  expect(current.waiting).toBe(true);
  await act(async () => {emit('transcription-state',{capture_id:'capture-a',state:'active'});});
  expect(current.waiting).toBe(false);
});

test('a waiting notice received while history loads is retained for its capture', async () => {
  let resolve!:(rows:any[])=>void;history=()=>new Promise(done=>{resolve=done;});
  await act(async () => {renderer=create(<Harness/>);});
  await act(async () => {emit('transcription-state',{capture_id:'capture-a',state:'waiting'});resolve([]);});
  expect(current.waiting).toBe(true);
  await act(async () => {current.clear();});
  expect(current.waiting).toBe(false);
});
