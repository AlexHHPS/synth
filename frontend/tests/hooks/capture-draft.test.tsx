import { beforeEach, expect, test } from 'bun:test';
import { activeCapture, clearCapture, setCaptureIdentity, writeCapture } from '../../src/synth/capture';
import { liveNotesEnabled, nextLines, setLiveNotesEnabled, type LiveNote } from '../../src/synth/useLiveNotes';

const values = new Map<string, string>();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); },
  removeItem: (key: string) => { values.delete(key); }, clear: () => values.clear(),
} });

beforeEach(() => { values.clear(); setCaptureIdentity('alice'); });

test('a start that never recorded does not name the next note', () => {
  writeCapture({ title: 'Daily Synth 6 Nov', folder_id: null, consent_confirmed: true });
  expect(activeCapture(false)).toBeNull();
  // The same options are the capture while it is recording.
  expect(activeCapture(true)?.title).toBe('Daily Synth 6 Nov');
});

test('a closed capture waiting to be processed keeps its title until it is sent', () => {
  writeCapture({ title: 'Comité', folder_id: null, consent_confirmed: true, folder_path: '/captures/1' });
  expect(activeCapture(false)?.title).toBe('Comité');
  clearCapture();
  expect(activeCapture(true)).toBeNull();
});

test('live notes send new lines plus the earlier ones the notes rest on', () => {
  const all = ['l1', 'l2', 'l3', 'l4'].map(id => ({ id, speaker: 'Tú', text: id }));
  const note: LiveNote = { kind: 'decision', text: 'x', status: 'confirmed', owner: null, due: null, citations: ['l1'] };
  const { lines, fresh } = nextLines(all, new Set(['l1', 'l2']), [note]);
  expect(fresh.map(line => line.id)).toEqual(['l3', 'l4']);
  expect(lines.map(line => line.id)).toEqual(['l1', 'l3', 'l4']);
});

test('live notes are on by default and stay off once turned off', () => {
  expect(liveNotesEnabled()).toBe(true);
  setLiveNotesEnabled(false);
  expect(liveNotesEnabled()).toBe(false);
});
