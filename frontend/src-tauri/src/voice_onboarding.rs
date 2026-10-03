//! A bounded, microphone-only enrollment recording. No transcription or network audio.
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::{sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, io::Write};
use tauri::Manager;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static CANCEL: AtomicBool = AtomicBool::new(false);
pub fn active() -> bool { ACTIVE.load(Ordering::SeqCst) }
struct Guard;
impl Drop for Guard { fn drop(&mut self) { ACTIVE.store(false, Ordering::SeqCst); } }
#[tauri::command] pub fn synth_voice_recording_active() -> bool { active() }
#[tauri::command] pub fn synth_voice_cancel() { CANCEL.store(true, Ordering::SeqCst); }
fn build<T>(device: &cpal::Device, config: &cpal::StreamConfig, samples: Arc<Mutex<Vec<i16>>>, failed: Arc<AtomicBool>) -> Result<cpal::Stream, String>
where T: cpal::SizedSample + cpal::Sample, f32: cpal::FromSample<T> {
    let channels = config.channels as usize;
    let limit = config.sample_rate.0 as usize * 32;
    let callback_failed = failed.clone();
    device.build_input_stream(config, move |data: &[T], _| {
        let Ok(mut buffer) = samples.try_lock() else { callback_failed.store(true, Ordering::SeqCst); return; };
        for frame in data.chunks_exact(channels) {
            if buffer.len() >= limit { break; }
            let mono = frame.iter().map(|s| s.to_sample::<f32>()).sum::<f32>() / channels as f32;
            if !mono.is_finite() { callback_failed.store(true, Ordering::SeqCst); break; }
            buffer.push((mono.clamp(-1.0,1.0) * 32767.0).round() as i16);
        }
    }, move |_| { failed.store(true, Ordering::SeqCst); }, None).map_err(|_| "No se puede abrir el micrófono. Revisa el permiso en Ajustes del Sistema.".into())
}
#[tauri::command]
pub async fn synth_voice_record(app: tauri::AppHandle, consent_confirmed: bool) -> Result<String, String> {
    let _lifecycle = crate::audio::common::acquire_engine_lifecycle_lock().await;
    if !consent_confirmed { return Err("Confirma el consentimiento para crear tu perfil de voz.".into()); }
    if crate::is_recording().await || ACTIVE.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() { return Err("Termina la grabación actual antes del onboarding.".into()); }
    CANCEL.store(false, Ordering::SeqCst);
    let root = match app.path().app_data_dir() { Ok(p) => p.join("voice/onboarding/raw"), Err(_) => { ACTIVE.store(false, Ordering::SeqCst); return Err("Directorio de audio no disponible".into()); } };
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = Guard;
        let device = cpal::default_host().default_input_device().ok_or("No hay micrófono disponible.")?;
        let supported = device.default_input_config().map_err(|_| "No se puede consultar el micrófono.")?;
        let config: cpal::StreamConfig = supported.clone().into();
        if config.channels == 0 || config.channels > 16 || !(8000..=192000).contains(&config.sample_rate.0) { return Err("Formato de micrófono no compatible.".into()); }
        let samples = Arc::new(Mutex::new(Vec::with_capacity(config.sample_rate.0 as usize * 32)));
        let failed = Arc::new(AtomicBool::new(false));
        let stream = match supported.sample_format() {
            cpal::SampleFormat::F32 => build::<f32>(&device,&config,samples.clone(),failed.clone()),
            cpal::SampleFormat::I16 => build::<i16>(&device,&config,samples.clone(),failed.clone()),
            cpal::SampleFormat::U16 => build::<u16>(&device,&config,samples.clone(),failed.clone()),
            _ => Err("Formato de micrófono no compatible.".into()),
        }?;
        stream.play().map_err(|_| "No se puede iniciar el micrófono.")?;
        for _ in 0..300 { if CANCEL.load(Ordering::SeqCst) || failed.load(Ordering::SeqCst) { break; } std::thread::sleep(std::time::Duration::from_millis(100)); }
        drop(stream);
        if CANCEL.load(Ordering::SeqCst) { return Err("Grabación de onboarding cancelada.".into()); }
        if failed.load(Ordering::SeqCst) { return Err("La muestra de voz se interrumpió. Vuelve a grabar.".into()); }
        let buffer = samples.lock().map_err(|_| "No se pudo cerrar la muestra.")?;
        if buffer.len() < config.sample_rate.0 as usize * 15 { return Err("La muestra es demasiado corta. Vuelve a grabar.".into()); }
        std::fs::create_dir_all(&root).map_err(|_| "No se puede guardar la muestra.")?;
        #[cfg(unix)] { use std::os::unix::fs::PermissionsExt; std::fs::set_permissions(&root,std::fs::Permissions::from_mode(0o700)).map_err(|_| "No se puede proteger la muestra.")?; }
        let id = uuid::Uuid::new_v4().to_string();
        let path = root.join(format!("{id}.wav"));
        let mut opts = std::fs::OpenOptions::new(); opts.write(true).create_new(true);
        #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; opts.mode(0o600); }
        let mut file = opts.open(&path).map_err(|_| "No se puede guardar la muestra.")?;
        let length = buffer.len() as u32 * 2;
        let mut bytes=Vec::with_capacity(length as usize+44);
        bytes.extend_from_slice(b"RIFF"); bytes.extend_from_slice(&(length+36).to_le_bytes()); bytes.extend_from_slice(b"WAVEfmt "); bytes.extend_from_slice(&16u32.to_le_bytes()); bytes.extend_from_slice(&1u16.to_le_bytes()); bytes.extend_from_slice(&1u16.to_le_bytes()); bytes.extend_from_slice(&config.sample_rate.0.to_le_bytes()); bytes.extend_from_slice(&(config.sample_rate.0*2).to_le_bytes()); bytes.extend_from_slice(&2u16.to_le_bytes()); bytes.extend_from_slice(&16u16.to_le_bytes()); bytes.extend_from_slice(b"data"); bytes.extend_from_slice(&length.to_le_bytes());
        for v in buffer.iter() { bytes.extend_from_slice(&v.to_le_bytes()); }
        if file.write_all(&bytes).and_then(|_| file.sync_all()).is_err() { let _=std::fs::remove_file(&path); return Err("No se pudo cerrar la muestra.".into()); }
        Ok(id)
    }).await.map_err(|_| "No se pudo completar la grabación.")?
}

#[tauri::command] pub fn synth_voice_discard(app: tauri::AppHandle, recording_id: String) -> Result<(), String> {
    let id=uuid::Uuid::parse_str(&recording_id).map_err(|_| "Muestra no válida")?.to_string();
    let root=app.path().app_data_dir().map_err(|_| "Directorio no disponible")?.join("voice/onboarding/raw");
    let path=root.join(format!("{id}.wav"));
    if path.exists() { if path.is_symlink() || path.canonicalize().map_err(|_| "Muestra no válida")?.parent()!=Some(root.canonicalize().map_err(|_| "Muestra no válida")?.as_path()) { return Err("Muestra no válida".into()); } std::fs::remove_file(path).map_err(|_| "No se puede borrar la muestra")?; }
    Ok(())
}
