/** Shapes of the meeting API (minutes v2, turns, speakers, tasks, glossary) and pure helpers. */

export interface Turn {
  id: string; text: string; start_ms: number; end_ms: number;
  speaker_id: string | null; source_id?: string | null; segment_ids: string[]; interjection?: boolean; word_count?: number;
}
export interface SegmentV2 { id: string; text: string; start_ms: number; end_ms: number; speaker_id: string | null; source_id?: string | null }
export interface ResolvedSpeaker { status: 'confirmed' | 'renamed' | 'rejected'; name?: string | null; employee_id?: string | null }
export interface Correction {
  id: string; segment_id: string; original: string; replacement: string; term: string;
  status: 'applied' | 'undone' | 'suggested' | 'rejected'; origin?: 'modelo' | 'alias' | 'contexto' | null;
  start?: number | null; turn_id?: string | null; turn_start?: number | null;
}
export interface TranscriptContent {
  language: string; duration_ms: number; model_fingerprint?: string; segments: SegmentV2[]; turns?: Turn[];
  speakers?: Record<string, ResolvedSpeaker>; corrections?: Correction[]; raw_version?: number;
}
export interface TranscriptVersion { version: number; content_hash?: string; model_fingerprint: string; content: TranscriptContent }

export interface Point { text: string; citations: string[] }
export interface Topic { title: string; context: string; discussion: string; outcome: string; points: Point[] }
export interface MinutesTask {
  key: string; title: string; description?: string; owner: string; owner_evidence: string; due_text?: string | null;
  due_date?: string | null; lifecycle?: string; previous?: string | null; citations: string[]; topic?: string;
}
export interface MinutesDecision {
  key: string; decision: string; reason?: string | null; alternatives?: string[]; status: string; affects?: string | null;
  decided_by?: string | null; citations: string[]; topic?: string;
}
export interface Minutes {
  schema_version: number; converted_from?: number; meeting_type: string | null; tldr: string; topics: Topic[];
  tasks: MinutesTask[]; decisions: MinutesDecision[]; open_questions: Point[]; continuity?: ContinuityItem[];
}
export interface ContinuityItem { key: string; kind: 'tarea' | 'decisión'; text: string; status: 'hecha' | 'en curso' | 'contradicha' | 'sin mencionar'; citations: string[] }
export interface QuestionCitation {
  meeting_id: string; turn_id: string; transcript_version: number; meeting_title: string; meeting_date: string; time: string; speaker: string; text: string;
}
export interface QuestionResult {
  id: string; question: string; state: 'queued' | 'running' | 'answered' | 'failed'; error_code: string | null;
  answer: { status: 'answered' | 'no_consta'; text: string; citations: QuestionCitation[] } | null;
}
export interface MinutesResponse { version: number; transcript_version: number; created_at?: string; minutes: Minutes }

export interface TaskRow {
  id: string; meeting_id: string; item_key: string; title: string; description: string; owner_name: string | null;
  owner_principal_id: string | null; owner_evidence: string; due_text: string | null; due_date: string | null;
  status: 'abierta' | 'hecha' | 'descartada'; lifecycle: string; citations: string[]; meeting_title?: string; meeting_date?: string;
}
export interface DecisionRow {
  id: string; meeting_id: string; item_key: string; decision: string; reason: string | null; alternatives: string[];
  status: 'acordada' | 'pendiente de validar' | 'en desacuerdo' | 'aparcada'; affects: string | null; decided_by: string | null; citations: string[];
}
export interface GlossaryTerm {
  id: string; scope: 'workspace' | 'folder'; folder_id: string | null; term: string; kind: string; aliases: string[];
  definition: string; status: 'propuesto' | 'aprobado' | 'descartado'; origin: string; occurrences: number; examples: string[];
  alias_modes?: AliasMode[];
}
/** What one «suena como» does: rewrites text, suggests the term, or only helps Whisper and the minutes. */
export interface AliasMode {
  alias: string; mode: 'corrige' | 'sugiere' | 'inactivo';
  reason: 'inventado' | 'palabra' | 'aceptado' | 'rechazado' | 'corto' | 'nombre' | 'compartido';
}
export interface VoiceSuggestion {
  state: 'suggested' | 'ask' | 'unknown'; reason: string; similarity: number | null; candidate_name: string | null;
  candidate_employee_id: string | null; clean_seconds?: number;
}
export interface VoiceStatus {
  state: string; reason?: string; cause?: string; transcript_version?: number; matches: Record<string, VoiceSuggestion>; resolved?: string[];
}

export const MEETING_TYPES: Record<string, string> = {
  seguimiento: 'Seguimiento', comite: 'Comité', '1:1': 'Uno a uno', cliente: 'Cliente', kickoff: 'Arranque',
  brainstorming: 'Lluvia de ideas', workshop: 'Taller',
};
export const DECISION_STATES = ['acordada', 'pendiente de validar', 'en desacuerdo', 'aparcada'] as const;

/** Turns when the transcript has them; older transcripts show each segment as a turn. */
export function turnsOf(content: TranscriptContent): Turn[] {
  if (content.turns?.length) return content.turns;
  return content.segments.map(s => ({ id: s.id, text: s.text, start_ms: s.start_ms, end_ms: s.end_ms, speaker_id: s.speaker_id,
    source_id: s.source_id, segment_ids: [s.id] }));
}

/** Same rule as the server: a resolved name, «Tú» for the local microphone, otherwise «Hablante N». */
export function speakerNames(content: TranscriptContent): (speaker: string | null) => string {
  const order = new Map<string, number>();
  for (const turn of turnsOf(content)) {
    const id = turn.speaker_id;
    if (id && !id.endsWith(':local') && !order.has(id)) order.set(id, order.size + 1);
  }
  return speaker => {
    if (!speaker) return 'Voz sin identificar';
    const resolved = content.speakers?.[speaker];
    if (resolved && resolved.status !== 'rejected' && resolved.name) return resolved.name;
    if (speaker.endsWith(':local')) return 'Tú';
    return `Hablante ${order.get(speaker) ?? 1}`;
  };
}

/** Turn ids and segment ids both resolve: v2 minutes cite turns, v1 minutes cite segments. */
export function citationIndex(content: TranscriptContent): Map<string, Turn> {
  const index = new Map<string, Turn>();
  for (const turn of turnsOf(content)) {
    index.set(turn.id, turn);
    for (const segment of turn.segment_ids) if (!index.has(segment)) index.set(segment, turn);
  }
  return index;
}

export function clock(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

export function duration(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 1) return 'menos de un minuto';
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60 ? `${minutes % 60} min` : ''}`.trim();
}

export function participantsOf(content: TranscriptContent): string[] {
  const name = speakerNames(content);
  const seen = new Set<string>();
  for (const turn of turnsOf(content)) if (turn.speaker_id) seen.add(name(turn.speaker_id));
  return [...seen];
}

/** Split a line into plain text and glossary-corrected spans so the UI can mark (and undo) them. */
/** Split a turn into plain text and corrected spans. Positioned corrections mark exactly their occurrence;
 *  older ones without offsets fall back to the first match of their replacement. */
export function markCorrections(turnId: string, text: string, corrections: Correction[]): { text: string; correction?: Correction }[] {
  const spans: { at: number; length: number; correction: Correction }[] = [];
  for (const correction of corrections) {
    if (correction.status !== 'applied' && correction.status !== 'suggested') continue;
    // An applied correction sits in the text as its replacement; a suggestion still as what was heard.
    const shown = correction.status === 'applied' ? correction.replacement : correction.original;
    const at = correction.turn_start != null
      ? (correction.turn_id === turnId && text.startsWith(shown, correction.turn_start) ? correction.turn_start : -1)
      : (correction.turn_id == null ? text.indexOf(shown) : -1);
    if (at >= 0 && !spans.some(s => at < s.at + s.length && s.at < at + shown.length)) spans.push({ at, length: shown.length, correction });
  }
  spans.sort((a, b) => a.at - b.at);
  const parts: { text: string; correction?: Correction }[] = [];
  let cursor = 0;
  for (const { at, length, correction } of spans) {
    if (at > cursor) parts.push({ text: text.slice(cursor, at) });
    parts.push({ text: text.slice(at, at + length), correction });
    cursor = at + length;
  }
  if (cursor < text.length || !parts.length) parts.push({ text: text.slice(cursor) });
  return parts;
}
