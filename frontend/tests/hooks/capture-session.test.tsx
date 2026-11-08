import { afterEach, expect, mock, spyOn, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

let getState: () => Promise<any>;
let post: () => Promise<any> = async () => ({});
mock.module('../../src/synth/api', () => ({
  pipelineRequest: (method: string) => method === 'GET' ? getState() : post(),
  message: (error: any) => String(error.message),
}));
mock.module('next/image', () => ({ default: () => null }));
mock.module('../../src/synth/AudioPreparation', () => ({
  useAudioSetup: () => ({ setup: { state: 'ready' }, retry: () => {} }), AudioPreparation: () => null,
}));
mock.module('../../src/synth/design-system/button', () => ({ Button: ({ children, variant, size, loading, ...props }: any) => <button {...props}>{children}</button> }));
const listeners = new Map<string, () => void>();
Object.defineProperty(globalThis, 'window', { configurable: true, value: {
  addEventListener: (event: string, fn: () => void) => listeners.set(event, fn),
  removeEventListener: (event: string) => listeners.delete(event),
} });
const { CorporateSession } = await import('../../src/synth/CorporateSession');
const { captureScope } = await import('../../src/synth/capture');
let renderer: ReactTestRenderer;
let restoreTimers = () => {};
const session = (id: string) => ({ mode: 'supabase', signed_in: true, principal_id: id, email: `${id}@example.invalid` });
afterEach(async () => { await act(async () => renderer?.unmount()); restoreTimers(); restoreTimers = () => {}; listeners.clear(); post = async () => ({}); });

function pollTimer() {
  const callbacks: (() => void)[] = [];
  const start = spyOn(globalThis, 'setInterval').mockImplementation(((callback: () => void) => {
    callbacks.push(callback); return callbacks.length;
  }) as any);
  const stop = spyOn(globalThis, 'clearInterval').mockImplementation(() => {});
  restoreTimers = () => {start.mockRestore();stop.mockRestore();};
  return () => callbacks.at(-1)?.();
}

test('initial service failure recovers automatically when the host becomes ready', async () => {
  const poll = pollTimer();
  getState = async () => {throw new Error('Host starting');};
  await act(async () => {renderer = create(<CorporateSession><span>Private meeting ready</span></CorporateSession>);});
  expect(JSON.stringify(renderer.toJSON())).not.toContain('Private meeting ready');
  getState = async () => session('user-a');
  await act(async () => poll());
  expect(JSON.stringify(renderer.toJSON())).toContain('Private meeting ready');
  expect(captureScope()?.startsWith('user-a:')).toBe(true);
});

test('automatic startup polls do not invalidate an auth request that is still pending', async () => {
  const poll = pollTimer();
  let calls = 0;
  let resolve!: (value: any) => void;
  getState = () => {calls++;return new Promise(done => {resolve = done;});};
  await act(async () => {renderer = create(<CorporateSession><span>Ready after slow auth</span></CorporateSession>);});
  await act(async () => {poll();poll();poll();});
  expect(calls).toBe(1);
  await act(async () => resolve(session('user-a')));
  expect(JSON.stringify(renderer.toJSON())).toContain('Ready after slow auth');
});

test('local logout hides private views even when the following cloud state refresh fails', async () => {
  getState = async () => session('user-a');
  await act(async () => { renderer = create(<CorporateSession><span>Private meeting A</span></CorporateSession>); });
  expect(JSON.stringify(renderer.toJSON())).toContain('Private meeting A');
  getState = async () => { throw new Error('Cloud unavailable'); };
  await act(async () => { await renderer.root.findByType('button').props.onClick(); });
  expect(JSON.stringify(renderer.toJSON())).not.toContain('Private meeting A');
  expect(captureScope()).toBeNull();
});

test('an older auth response cannot replace a newer identity returned on focus', async () => {
  let resolve!: (value: any) => void;
  getState = () => new Promise(done => { resolve = done; });
  await act(async () => { renderer = create(<CorporateSession><span>Private meeting</span></CorporateSession>); });
  getState = async () => session('user-b');
  await act(async () => { listeners.get('focus')?.(); });
  await act(async () => { resolve(session('user-a')); });
  expect(JSON.stringify(renderer.toJSON())).toContain('user-b@example.invalid');
  expect(JSON.stringify(renderer.toJSON())).not.toContain('user-a@example.invalid');
  expect(captureScope()?.startsWith('user-b:')).toBe(true);
});
