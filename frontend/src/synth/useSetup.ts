import { useCallback, useEffect, useRef, useState } from 'react';
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

export interface PermissionsState {
  states: Record<Permission, PermissionState>; busy: Permission | null;
  request: (kind: Permission) => void; openSettings: () => void;
}

export function usePermissions(): PermissionsState {
  const [states, setStates] = useState<Record<Permission, PermissionState>>({ mic: 'unknown', system: 'unknown' });
  const [busy, setBusy] = useState<Permission | null>(null);
  const request = useCallback((kind: Permission) => {
    setBusy(kind);
    void requestPermission(kind).then(state => setStates(old => ({ ...old, [kind]: state }))).finally(() => setBusy(null));
  }, []);
  const openSettings = useCallback(() => { void openPrivacySettings(); }, []);
  return { states, busy, request, openSettings };
}
