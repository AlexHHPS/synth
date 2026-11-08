import { afterAll, afterEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';

const original = { ...await import('../../src/synth/api') };
let response: unknown;
const calls: string[] = [];
mock.module('../../src/synth/api', () => ({ ...original, pipelineRequest: async (method: string, path: string) => {
  calls.push(method + ' ' + path);
  return method === 'GET' ? response : {};
} }));
mock.module('../../src/synth/design-system/button', () => ({ Button: ({children, variant, ...props}: any) => <button {...props}>{children}</button> }));
const { AudioPreparation, useAudioSetup } = await import('../../src/synth/AudioPreparation');
function Harness() {
  const { setup, retry } = useAudioSetup();
  return setup.state === 'ready' ? <span>Recording available</span> : <AudioPreparation setup={setup} onRetry={retry}/>;
}
let renderer: ReactTestRenderer;
afterEach(async () => { await act(async () => renderer?.unmount()); calls.length = 0; });
afterAll(() => mock.module('../../src/synth/api', () => original));

test('preparing models keeps recording unavailable and explains the initial download', async () => {
  response = {audio_setup:{state:'preparing',step:2,message:'Downloading models'}};
  await act(async () => { renderer = create(<Harness/>); });
  expect(JSON.stringify(renderer.toJSON())).toContain('5,9 GB');
  expect(JSON.stringify(renderer.toJSON())).not.toContain('Recording available');
  expect(renderer.root.findByType('progress').props.value).toBe(2);
});

test('a failed preparation offers a retry and a verified ready response opens the workspace', async () => {
  response = {audio_setup:{state:'failed',step:2,message:'Connection interrupted'}};
  await act(async () => { renderer = create(<Harness/>); });
  expect(renderer.root.findByProps({role:'alert'}).children).toContain('Connection interrupted');
  response = {audio_setup:{state:'ready',step:3,message:'Audio ready'}};
  await act(async () => { renderer.root.findByType('button').props.onClick(); });
  expect(calls).toContain('POST /v1/audio/setup/retry');
  expect(JSON.stringify(renderer.toJSON())).toContain('Recording available');
});

test('an old service without readiness does not unlock recording', async () => {
  response = {state:'idle'};
  await act(async () => { renderer = create(<Harness/>); });
  expect(JSON.stringify(renderer.toJSON())).not.toContain('Recording available');
  expect(JSON.stringify(renderer.toJSON())).toContain('servicio anterior');
});
