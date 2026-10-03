import React, { useEffect, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { Button } from '@/components/ui/button';
import { OnboardingContainer } from '../OnboardingContainer';
import { useOnboarding } from '@/contexts/OnboardingContext';

type LocalModel = { name: string; status: string | object };

export function DownloadProgressStep() {
  const { goNext, completeOnboarding } = useOnboarding();
  const [isMac, setIsMac] = useState(false);
  const [checking, setChecking] = useState(true);
  const [whisperReady, setWhisperReady] = useState(false);
  const [serviceReady, setServiceReady] = useState(false);
  const [error, setError] = useState('');
  const [completing, setCompleting] = useState(false);

  const check = async () => {
    setChecking(true);
    setError('');
    try {
      await invoke('whisper_init');
      const models = await invoke<LocalModel[]>('whisper_get_available_models');
      const ready = models.some(model => model.name === 'large-v3-turbo-q5_0' &&
        (model.status === 'Available' || (typeof model.status === 'object' && 'Available' in model.status)));
      setWhisperReady(ready);
      const response = await fetch('http://127.0.0.1:18280/health', { signal: AbortSignal.timeout(5000) });
      const health = await response.json();
      setServiceReady(response.ok && health.status === 'ready');
      if (!ready) setError('Falta el modelo local de transcripción. Configura Whisper antes de continuar.');
      else if (!response.ok || health.status !== 'ready') setError('El servicio de actas no está disponible.');
    } catch {
      setServiceReady(false);
      setError('No se pudo comprobar la configuración. Revisa el servicio local y vuelve a intentarlo.');
    } finally {
      setChecking(false);
    }
  };

  useEffect(() => {
    setIsMac(navigator.userAgent.includes('Mac'));
    void check();
  }, []);

  const proceed = async () => {
    setCompleting(true);
    setError('');
    try {
      // Persist the authorized route and Whisper selection before advancing.
      await invoke('configure_synth_pipeline');
      if (isMac) goNext();
      else {
        await completeOnboarding();
        window.location.reload();
      }
    } catch {
      setError('No se pudo guardar la configuración. Vuelve a intentarlo.');
    } finally {
      setCompleting(false);
    }
  };

  return (
    <OnboardingContainer title="Preparar Synth" description="Audio en tu Mac y actas mediante OmniRoute."
      step={3} totalSteps={isMac ? 4 : 3}>
      <div className="w-full max-w-lg space-y-6" aria-busy={checking || completing}>
        <div>
          <h2 className="font-medium">Transcripción local</h2>
          <p className="text-sm text-muted-foreground">Whisper · español · large-v3-turbo-q5_0</p>
          <p className="text-sm mt-2">{checking ? 'Comprobando modelo…' : whisperReady ? 'Modelo disponible en este Mac' : 'Modelo pendiente'}</p>
        </div>
        <div>
          <h2 className="font-medium">Actas en OmniRoute</h2>
          <p className="text-sm text-muted-foreground">local-combo · el texto puede procesarse fuera del Mac</p>
          <p className="text-sm mt-2">{checking ? 'Comprobando servicio…' : serviceReady ? 'Servicio de actas disponible' : 'Servicio no disponible'}</p>
        </div>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex gap-3">
          <Button onClick={proceed} disabled={checking || completing || !whisperReady || !serviceReady}>
            {completing ? 'Guardando configuración…' : 'Continuar'}
          </Button>
          <Button variant="outline" onClick={() => void check()} disabled={checking || completing}>Comprobar de nuevo</Button>
        </div>
      </div>
    </OnboardingContainer>
  );
}
