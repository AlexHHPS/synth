import { invoke } from '@tauri-apps/api/core';

/** The native layer owns the credential and approved library backend URL. */
export function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  return invoke<T>('synth_desktop_request', { method, path, body: body ?? null });
}

export function pipelineRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  return invoke<T>('synth_pipeline_request', { method, path, body: body ?? null });
}

export interface CaptureTask {
  id: string; title?: string; folder_id?: string | null; capture_id?: string | null; state: string; stage: string; attempts: number; error_code: string | null;
  created_at: number; updated_at: number; meeting_id: string | null; job_id: string | null;
  identity: { state: string; reason?: string } | null;
  transcript_version?: number; capture_state?: string; can_retry?: boolean;
}

export interface Integration {
  id: string; name: string; created_at: string; revoked_at: string | null;
  folders: Folder[]; read_only: true;
}

export interface Folder { id: string; name: string; can_manage?: boolean; can_edit?: boolean }
export interface Meeting {
  id: string; title: string; folder_id: string | null; state: string; revision: number;
  notes?: string; created_at?: string; updated_at?: string;
}
export interface Page<T> { items: T[]; next_cursor?: string | null }
export interface Segment {
  id: string; text: string; start_ms: number; end_ms: number;
  speaker_id: string | null; employee_id: string | null; source_id: string;
}
export interface Transcript {
  version: number; content_hash: string; model_fingerprint: string;
  content: { language: string; duration_ms: number; segments: Segment[] };
}
export interface Evidence { segment_id: string; quote: string }
export interface Statement {
  text: string; evidence: Evidence[]; owner?: string | null; due_date?: string | null;
}
export interface Document {
  version: number; transcript_version: number; content_hash: string;
  model_fingerprint: string; markdown: string;
  content: { summary: Statement[]; decisions: Statement[]; actions: Statement[]; open_questions: Statement[] };
}
export interface SearchHit { meeting_id: string; transcript_version: number; evidence: Evidence[] }
export function message(error: unknown): string {
  const value = error instanceof Error ? error.message : String(error);
  if (['auth_unavailable', 'api_unavailable'].includes(value)) return 'No se puede conectar con la biblioteca. Comprueba la conexión y la configuración del servicio de tu organización.';
  if (value === 'auth_login_required') return 'Inicia sesión para acceder a tu biblioteca.';
  if (value === 'unapproved_product_backend') return 'El servidor configurado no coincide con el autorizado para esta app. Contacta con tu administrador.';
  return value;
}
export function time(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
