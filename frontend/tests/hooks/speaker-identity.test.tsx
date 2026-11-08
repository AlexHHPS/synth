import { afterAll, afterEach, beforeEach, expect, mock, test } from 'bun:test';
import { act, create, type ReactTestRenderer } from 'react-test-renderer';
import type { SpeakerAction } from '../../src/synth/TranscriptScript';

const originalAPI = { ...await import('../../src/synth/api') };
let roster: () => Promise<unknown>;
mock.module('../../src/synth/api', () => ({ ...originalAPI, request: (_method: string, path: string) => {
  expect(path).toBe('/v1/people'); return roster();
} }));
const { SpeakerIdentityPicker } = await import('../../src/synth/SpeakerIdentityPicker');
let renderer: ReactTestRenderer;
const save = mock((_action: SpeakerAction) => {}), cancel = mock(() => {});
const member = { id: '22222222-2222-4222-8222-222222222222', name: 'Miembro sin perfil de voz' };
beforeEach(() => { save.mockClear(); cancel.mockClear(); roster = async () => ({ items: [member] }); });
afterEach(async () => { await act(async () => renderer?.unmount()); });
afterAll(() => mock.module('../../src/synth/api', () => originalAPI));
async function mount(busy = false) { await act(async () => { renderer = create(<SpeakerIdentityPicker label="system:S1" name="Hablante 1" busy={busy} onSpeaker={save} onCancel={cancel} />); }); }
async function choose(value: string) { await act(async () => { renderer.root.findByType('select').props.onChange({ target: { value } }); }); }
async function submit() { await act(async () => { renderer.root.findByType('form').props.onSubmit({ preventDefault() {} }); }); }
const button = (text: string) => renderer.root.findAllByType('button').find(node => node.children.includes(text))!;

test('an organization member is confirmed with their stable ID even without a voice profile', async () => {
  await mount(); expect(button('Guardar').props.disabled).toBe(true);
  await choose(member.id); await submit();
  expect(save).toHaveBeenCalledWith({ action: 'confirm', speaker_id: 'system:S1', name: member.name, employee_id: member.id });
});
test('an external name is trimmed and saved without an organization identity', async () => {
  await mount(); await choose('external'); await submit(); expect(save).not.toHaveBeenCalled();
  await act(async () => { renderer.root.findByType('input').props.onChange({ target: { value: '  Cliente externo  ' } }); });
  await submit(); expect(save).toHaveBeenCalledWith({ action: 'rename', speaker_id: 'system:S1', name: 'Cliente externo' });
  await choose(member.id); await submit();
  expect(save.mock.calls.at(-1)).toEqual([{ action: 'confirm', speaker_id: 'system:S1', name: member.name, employee_id: member.id }]);
});
test('a failed directory read can retry while keeping the external option usable', async () => {
  roster = async () => { throw new Error('synthetic unavailable'); }; await mount();
  expect(renderer.root.findByProps({ role: 'alert' }).children).toContain('No se pudieron cargar los miembros.');
  await choose('external');
  await act(async () => { renderer.root.findByType('input').props.onChange({ target: { value: 'Invitada' } }); });
  roster = async () => ({ items: [member] });
  await act(async () => { button('Reintentar').props.onClick(); });
  expect(renderer.root.findByType('input').props.value).toBe('Invitada');
  await submit(); expect(save).toHaveBeenCalledWith({ action: 'rename', speaker_id: 'system:S1', name: 'Invitada' });
});
test('directory loading and an empty roster never block identifying an external person', async () => {
  let resolve!: (value: unknown) => void;
  roster = () => new Promise(done => { resolve = done; }); await mount();
  await choose('external');
  await act(async () => { renderer.root.findByType('input').props.onChange({ target: { value: 'Invitado' } }); });
  await submit(); expect(save).toHaveBeenCalledTimes(1);
  await act(async () => { resolve({ items: [] }); });
  expect(JSON.stringify(renderer.toJSON())).toContain('No hay miembros disponibles.');
});
test('pending mutations prevent duplicate submission and cancellation never saves', async () => {
  await mount(true); await choose(member.id); await submit(); expect(save).not.toHaveBeenCalled();
  await act(async () => { renderer.update(<SpeakerIdentityPicker label="system:S1" name="Hablante 1" busy={false} onSpeaker={save} onCancel={cancel} />); });
  await act(async () => { button('Cancelar').props.onClick(); });
  expect(cancel).toHaveBeenCalledTimes(1); expect(save).not.toHaveBeenCalled();
});
