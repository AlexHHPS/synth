import { useCallback, useEffect, useRef, useState } from 'react';
import { pipelineRequest } from './api';
import { Check, Permission, PermissionState, openPrivacySettings, pendingChecks, requestPermission, runChecks } from './setup';

export interface ChecksState { checks: Check[]; running: boolean; rerun: () => void }

export function useChecks(enabled: boolean): ChecksState {
  const [checks, setChecks] = useState<Check[]>(pendingChecks);
  const [running, setRunning] = useState(false);
  const generation = useRef(0);
  const rerun = useCallback(() => {
    const run = ++generation.current;
    setRunning(true); setChecks(pendingChecks());
    void runChecks().then(result => { if (run === generation.current) setChecks(result); })
      .finally(() => { if (run === generation.current) setRunning(false); });
  }, []);
  useEffect(() => { if (enabled) rerun(); return () => { generation.current++; }; }, [enabled, rerun]);
  return { checks, running, rerun };
}

type VisionStatus = 'checking' | 'ready' | 'missing' | 'preparing' | 'failed' | 'unavailable';

export interface VisionSetup {
  state: VisionStatus;
  refresh: () => void;
  prepare: () => void;
}

export function useVisionSetup(enabled: boolean): VisionSetup {
  const [state, setState] = useState<VisionStatus>('checking');
  const generation = useRef(0);
  const update = useCallback((method: 'GET' | 'POST') => {
    const run = ++generation.current;
    if (method === 'POST') setState('preparing');
    void pipelineRequest<{ state: VisionStatus }>(method, '/v1/vision/setup')
      .then(result => { if (run === generation.current) setState(result.state); })
      .catch(() => { if (run === generation.current) setState(method === 'POST' ? 'failed' : 'unavailable'); });
  }, []);
  const refresh = useCallback(() => update('GET'), [update]);
  const prepare = useCallback(() => update('POST'), [update]);
  useEffect(() => {
    if (enabled) refresh();
    return () => { generation.current++; };
  }, [enabled, refresh]);
  useEffect(() => {
    if (!enabled || state !== 'preparing') return;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [enabled, state, refresh]);
  return { state, refresh, prepare };
}

export interface PermissionsState {
  states: Record<Permission, PermissionState>; busy: Permission | null;
  request: (kind: Permission) => void; openSettings: (kind: Permission) => void;
}

export function usePermissions(): PermissionsState {
  const [states, setStates] = useState<Record<Permission, PermissionState>>({ mic: 'unknown', system: 'unknown', screen: 'unknown' });
  const [busy, setBusy] = useState<Permission | null>(null);
  const request = useCallback((kind: Permission) => {
    setBusy(kind);
    void requestPermission(kind).then(state => setStates(old => ({ ...old, [kind]: state }))).finally(() => setBusy(null));
  }, []);
  const openSettings = useCallback((kind: Permission) => { void openPrivacySettings(kind).catch(() => undefined); }, []);
  return { states, busy, request, openSettings };
}
