import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const original = { ...await import('../../src/synth/api') };
const originalButton = { ...await import('../../src/synth/design-system/button') };
let handle: (method: string, path: string, body?: any) => Promise<any>;
const calls: { method: string; path: string; body?: any }[] = [];
mock.module('../../src/synth/api', () => ({ ...original, pipelineRequest: async (method: string, path: string, body?: any) => {
  calls.push({ method, path, body }); return handle(method, path, body);
} }));
mock.module('../../src/synth/design-system/button', () => ({ Button: ({children, variant, ...props}: any) => <button {...props}>{children}</button> }));
const { useLiveVoices } = await import('../../src/synth/useLiveVoices');
const { LiveVoices, voiceForSegment } = await import('../../src/synth/LiveVoices');
let current: ReturnType<typeof useLiveVoices>;
let renderer: ReactTestRenderer | undefined;
const profile = '33333333-3333-4333-8333-333333333333';
function status(id = 'a') {
  return { capture_id: id, state: 'collecting', confirmation_revision: 0,
    eligible_profiles: [{id: profile, name: 'María', employee_id: profile}],
    matches: [{ group_id: 'system:0:S1', source: 'system', state: 'suggested', confirmed: false,
      candidate_name: 'María', candidate_employee_id: profile, profile_id: profile, can_identify: true,
      evidence_ids: ['1','2','3'].map(s => s.repeat(64)), speech_spans_ms: [[0,10000]], source_ranges_ms: [[0,10000]], start_ms: 0, end_ms: 30000 }] };
}
function Harness({ id = 'a' }: {id?: string}) {
  current = useLiveVoices(id);
  return <LiveVoices {...current} transcripts={[]} />;
}
async function mount(id = 'a') {
  await act(async () => {
    if (renderer) renderer.update(<Harness id={id}/>); else renderer = create(<Harness id={id}/>);
  });
}
afterEach(async () => { await act(async () => renderer?.unmount()); renderer = undefined; calls.length = 0; });
afterAll(() => { mock.module('../../src/synth/api', () => original); mock.module('../../src/synth/design-system/button', () => originalButton); });

test('a human submit sends exact evidence and only then displays a confirmed identity', async () => {
  handle = async (method, path, body) => method === 'POST' ? { ...status(), confirmation_revision: 1,
    matches: [{...status().matches[0], state: 'confirmed', confirmed: true}] } : status();
  await mount();
  expect(current.status?.matches[0].confirmed).toBe(false);
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(0);
  const form = renderer!.root.findByType('form');
  await act(async () => { form.props.onSubmit({preventDefault() {}}); });
  const body = calls.find(c => c.method === 'POST')!.body;
  expect(body.confirmed_by_user).toBe(true);
  expect(body.evidence_ids).toEqual(status().matches[0].evidence_ids);
  expect(body.expected_revision).toBe(0);
  expect(body.profile_id).toBe(profile);
  expect(current.status?.matches[0].confirmed).toBe(true);
  expect(JSON.stringify(body)).not.toContain('embedding');
});

test('an older GET cannot overwrite the result of a human confirmation', async () => {
  handle = async () => status();
  await mount();
  let resolve!: (value: any) => void;
  handle = async method => method === 'GET' ? new Promise(done => {resolve = done;}) :
    {...status(),confirmation_revision: 1,matches:[{...status().matches[0],confirmed:true,state:'confirmed'}]};
  let refreshing!: Promise<void>;
  await act(async () => { refreshing = current.refresh(); });
  await act(async () => { await current.decide(current.status!.matches[0], profile); });
  await act(async () => { resolve(status()); await refreshing; });
  expect(current.status?.confirmation_revision).toBe(1);
  expect(current.status?.matches[0].confirmed).toBe(true);
});

test('late confirmation from another capture cannot expose its name in the next capture', async () => {
  handle = async () => status();
  await mount();
  let resolve!: (value: any) => void;
  handle = async method => method === 'POST' ? new Promise(done => { resolve = done; }) : {...status('b'),matches:[]};
  let confirmation!: Promise<void>;
  await act(async () => { confirmation = current.decide(current.status!.matches[0],profile); });
  await mount('b');
  await act(async () => { resolve({...status(),matches:[{...status().matches[0],confirmed:true,state:'confirmed'}]}); await confirmation; });
  expect(current.status?.capture_id).toBe('b');
  expect(current.status?.matches).toEqual([]);
  expect(current.saving).toBeNull();
});

test('failed permission refresh removes old names and recovery can show the revoked catalog', async () => {
  handle = async () => status();
  await mount();
  handle = async () => { throw new Error('permission unavailable'); };
  await act(async () => { await current.refresh(); });
  expect(current.status).toBeNull();
  expect(current.error).not.toBe('');
  handle = async () => ({...status(),eligible_profiles:[],matches:[{...status().matches[0],state:'unknown',candidate_name:null,profile_id:null}]});
  await act(async () => { await current.refresh(); });
  expect(current.status?.matches[0].candidate_name).toBeNull();
});

test('mixed, overlapping and duplicate time spans cannot label a whole transcript block', () => {
  const segment = {id:'s1',text:'Texto',timestamp:'10:00',source:'system',audio_start_time:0,audio_end_time:10};
  const match = status().matches[0] as any;
  expect(voiceForSegment(segment,[match])?.candidate_name).toBe('María');
  expect(voiceForSegment(segment,[match,{...match,group_id:'system:0:S2',speech_spans_ms:[[6000,10000]]}])).toBeNull();
  expect(voiceForSegment(segment,[{...match,speech_spans_ms:[[0,3000],[0,3000]]}])).toBeNull();
  expect(voiceForSegment({...segment,source:'microphone'},[match])).toBeNull();
});

test('adjacent windows preserve a whole-block identity only for the same human-confirmed person', () => {
  const segment = {id:'s1',text:'La frase cruza el corte de audio',timestamp:'10:00',source:'system',audio_start_time:0,audio_end_time:10};
  const match = {...status().matches[0],state:'confirmed',confirmed:true} as any;
  const first={...match,speech_spans_ms:[[0,5000]]};
  const second={...match,group_id:'system:30000:S1',speech_spans_ms:[[5000,10000]]};
  expect(voiceForSegment(segment,[first,second])?.candidate_employee_id).toBe(profile);
  for(const change of [{candidate_employee_id:'different'}, {confirmed:false}, {candidate_employee_id:null},
    {speech_spans_ms:[[4000,10000]]}, {speech_spans_ms:[[6000,10000]]}]) {
    expect(voiceForSegment(segment,[first,{...second,...change}])).toBeNull();
  }
  expect(voiceForSegment(segment,[first])).toBeNull();
  expect(voiceForSegment(segment,[{...first,speech_spans_ms:[[0,5000],[0,5000]]}])).toBeNull();
});
