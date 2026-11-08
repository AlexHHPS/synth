//! Packaged Synth always uses the pinned local Qwen runtime for live speech.
use super::provider::{TranscriptionError, TranscriptionProvider, TranscriptResult};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::{path::PathBuf, process::Stdio, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};
use tauri::{AppHandle, Manager, Runtime, Emitter};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, process::{Child, ChildStdin, ChildStdout, Command}, sync::Mutex};

static ENGINE: Mutex<Option<Arc<QwenProvider>>> = Mutex::const_new(None);
static ACTIVE: AtomicBool = AtomicBool::new(false);
static WAITING: AtomicBool = AtomicBool::new(false);
const MODEL: &str = "Qwen3-ASR-1.7B";
pub const AUDIO_BUSY: &str = "Hay otro trabajo de audio en curso. Espera a que termine antes de grabar.";

pub fn packaged<R: Runtime>(app: &AppHandle<R>) -> bool {
    app.path().resource_dir().map(|p| p.join("synth-host/synth-voice-host").is_file()).unwrap_or(false)
}

pub fn active() -> bool { ACTIVE.load(Ordering::SeqCst) }
pub fn waiting() -> bool { WAITING.load(Ordering::SeqCst) }

fn start_readiness(result: Result<(), String>) -> Result<bool, String> {
    match result {
        Ok(()) => Ok(false),
        Err(error) if error == AUDIO_BUSY => Ok(true),
        Err(error) => Err(error),
    }
}

pub async fn prepare_capture<R:Runtime>(app:&AppHandle<R>) -> Result<(),String> {
    let queued = start_readiness(get_or_init(app).await.map(|_| ()))?;
    WAITING.store(queued,Ordering::SeqCst);
    Ok(())
}

/// The durable source archive owns audio; drain mixed notifications while waiting.
/// Closing capture cancels initialization and returns immediately to the final queue.
async fn wait_for_engine<T,P,F,Fut,W>(receiver:&mut tokio::sync::mpsc::UnboundedReceiver<T>,
    mut load:F,mut on_wait:W,retry:Duration) -> Result<Option<P>,String>
where F:FnMut()->Fut,Fut:std::future::Future<Output=Result<P,String>>,W:FnMut() {
    loop {
        if receiver.is_closed() { return Ok(None); }
        let loading=load();tokio::pin!(loading);
        let result=loop {
            if receiver.is_closed() { return Ok(None); }
            tokio::select! {
                result=&mut loading=>break result,
                chunk=receiver.recv()=>if chunk.is_none() {return Ok(None);},
            }
        };
        match result {
            Ok(provider)=>return Ok(Some(provider)),
            Err(error) if error==AUDIO_BUSY=>on_wait(),
            Err(error)=>return Err(error),
        }
        let pause=tokio::time::sleep(retry);tokio::pin!(pause);
        loop {
            if receiver.is_closed() { return Ok(None); }
            tokio::select! {
                _=&mut pause=>break,
                chunk=receiver.recv()=>if chunk.is_none() {return Ok(None);},
            }
        }
    }
}

struct Session { child: Child, input: ChildStdin, output: BufReader<ChildStdout> }
impl Session {
    async fn exchange(&mut self, request: Value) -> Result<Value, String> {
        let operation = async {
            let mut bytes = serde_json::to_vec(&request).map_err(|_| "No se pudo preparar el audio para Qwen.")?;
            bytes.push(b'\n');
            self.input.write_all(&bytes).await.map_err(|_| "Se interrumpió el proceso local de Qwen.")?;
            self.input.flush().await.map_err(|_| "Se interrumpió el proceso local de Qwen.")?;
            let mut line = String::new();
            let read = self.output.read_line(&mut line).await.map_err(|_| "Qwen no devolvió una respuesta válida.")?;
            if read == 0 || read > 1024*1024 { return Err("Qwen no devolvió una respuesta válida.".into()); }
            let value: Value = serde_json::from_str(&line).map_err(|_| "Qwen no devolvió una respuesta válida.")?;
            if let Some(code) = value.get("error_code").and_then(Value::as_str) {
                return Err(if code == "acoustic_worker_already_running" {
                    AUDIO_BUSY.into()
                } else { "No se pudo iniciar o ejecutar Qwen. Comprueba la preparación del audio y vuelve a intentarlo.".into() });
            }
            Ok(value)
        };
        tokio::time::timeout(Duration::from_secs(120), operation).await
            .map_err(|_| "Qwen no respondió dentro del plazo. Vuelve a intentar la grabación.".to_string())?
    }
}

pub struct QwenProvider { session: Mutex<Option<Session>> }

pub async fn get_or_init<R: Runtime>(app: &AppHandle<R>) -> Result<Arc<QwenProvider>, String> {
    let mut engine = ENGINE.lock().await;
    if let Some(provider) = engine.as_ref() {
        if provider.is_model_loaded().await { return Ok(provider.clone()); }
    }
    let root = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración del audio.")?;
    let resources = app.path().resource_dir().map_err(|_| "No se encuentran los recursos de la app.")?;
    let provider = QwenProvider::start(root, resources).await?;
    let provider = Arc::new(provider);
    *engine = Some(provider.clone());
    ACTIVE.store(true, Ordering::SeqCst);
    WAITING.store(false, Ordering::SeqCst);
    Ok(provider)
}

impl QwenProvider {
    pub async fn start(root: PathBuf, resources: PathBuf) -> Result<Self, String> {
        let bytes = std::fs::read(root.join("asr-config.json")).map_err(|_| "Espera a que termine la preparación automática de Qwen.")?;
        let config: Value = serde_json::from_slice(&bytes).map_err(|_| "La configuración de Qwen no es válida.")?;
        if config["backend"] != "qwen3" { return Err("Espera a que termine la preparación automática de Qwen.".into()); }
        let python = config["python"].as_str().ok_or("Falta el runtime de Qwen.")?;
        let model = config["asr"].as_str().ok_or("Falta el modelo de Qwen.")?;
        let aligner = config["aligner"].as_str().ok_or("Falta el alineador de Qwen.")?;
        let runner = resources.join("synth-host/_internal/synth/worker/qwen_live_runner.py");
        if !runner.is_file() || !std::path::Path::new(python).is_file() { return Err("Reinstala la app para reparar el runtime de Qwen.".into()); }
        let mut child = Command::new(python).arg("-I").arg(runner)
            .env("HF_HUB_OFFLINE", "1").env("HF_HUB_DISABLE_TELEMETRY", "1")
            .env("SSL_CERT_FILE", "/etc/ssl/cert.pem")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null())
            .kill_on_drop(true).spawn().map_err(|_| "No se pudo abrir el runtime local de Qwen.")?;
        let input = child.stdin.take().ok_or("No se pudo conectar con Qwen.")?;
        let output = BufReader::new(child.stdout.take().ok_or("No se pudo conectar con Qwen.")?);
        let mut session = Session {child,input,output};
        let internal = resources.join("synth-host/_internal");
        let response = session.exchange(json!({"command":"load","model":model,"aligner":aligner,"state":root.join("pipeline"),
            "speakers":{"binary":internal.join("synth/.runtime/swift-build/release/synth-speakers"),
                "models":internal.join("synth/.runtime/models/speakers-community1"),
                "lock":internal.join("synth/resources/speaker-model-lock.json")}})).await?;
        if response["ready"] != true { return Err("Qwen todavía no está preparado.".into()); }
        if response["protocol_version"] != 3 { return Err("El componente de audio necesita actualizarse. Reinstala esta versión de la app.".into()); }
        Ok(Self {session:Mutex::new(Some(session))})
    }

    async fn stop(&self) {
        if let Some(mut session) = self.session.lock().await.take() {
            let _ = session.child.start_kill();
            let _ = tokio::time::timeout(Duration::from_secs(5),session.child.wait()).await;
        }
    }

    async fn attach(&self, capture: &super::capture_stream::LiveCapture) -> Result<(),String> {
        let mut guard=self.session.lock().await;
        let session=guard.as_mut().ok_or("Qwen no está disponible.")?;
        let value=session.exchange(json!({"command":"capture","folder":capture.folder,
            "echo_cancellation":"aec3","speech_detection":"webrtc_rnn","consolidation":"context3"})).await?;
        if value["attached"]!=true || value["capture_id"].as_str()!=Some(capture.id.as_str()) {
            return Err("Qwen no confirmó esta captura.".into());
        }
        Ok(())
    }

    async fn poll_capture(&self, capture_id: &str, after: u64) -> Result<super::capture_stream::CaptureReply,String> {
        let mut guard=self.session.lock().await;
        let session=guard.as_mut().ok_or("Qwen no está disponible.")?;
        let value=session.exchange(json!({"command":"poll","capture_id":capture_id,"after":after})).await?;
        let reply:super::capture_stream::CaptureReply=serde_json::from_value(value).map_err(|_| "Qwen devolvió un bloque no válido.")?;
        reply.validate(capture_id,after)?;
        Ok(reply)
    }
}

pub fn start_capture_task<R:Runtime>(app:AppHandle<R>, mut receiver:tokio::sync::mpsc::UnboundedReceiver<crate::audio::AudioChunk>,
    capture:super::capture_stream::LiveCapture) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let result=run_capture(&app,&mut receiver,&capture).await;
        if result.is_err() {
            shutdown().await;
            let _=app.emit("transcription-error",json!({"error":"incremental_capture_interrupted",
                "userMessage":"Se interrumpió la transcripción en directo. El audio sigue guardado para recuperarlo al finalizar.",
                "actionable":true,"capture_id":capture.id,"phase":"active"}));
        }
    })
}

async fn run_capture<R:Runtime>(app:&AppHandle<R>, receiver:&mut tokio::sync::mpsc::UnboundedReceiver<crate::audio::AudioChunk>,
    capture:&super::capture_stream::LiveCapture) -> Result<(),String> {
    if crate::synth_desktop::capture_identity(app).await?!=capture.identity {
        capture.recording.stop_recording(); return Err("La sesión de la captura ha cambiado.".into());
    }
    let mut announced=false;
    let provider=wait_for_engine(receiver,||get_or_init(app),|| {
        WAITING.store(true,Ordering::SeqCst);
        if !announced {
            announced=true;
            let _=app.emit("transcription-state",json!({"capture_id":capture.id,"state":"waiting"}));
        }
    },Duration::from_secs(3)).await?;
    let Some(mut provider)=provider else {
        WAITING.store(false,Ordering::SeqCst);
        let _=app.emit("transcription-queue-complete",json!({"capture_id":capture.id,"mode":"deferred"}));
        return Ok(());
    };
    let _=app.emit("transcription-state",json!({"capture_id":capture.id,"state":"active"}));
    provider.attach(capture).await?;
    let (mut cursor,mut restarts,mut first_speech)=(0u64,0u8,false);
    let mut input_closed=None;
    loop {
        if input_closed.is_none() {
            tokio::select! {
                chunk=receiver.recv()=> { if chunk.is_none() { input_closed=Some(std::time::Instant::now()); } },
                _=tokio::time::sleep(Duration::from_millis(400))=>{},
            }
        }
        if crate::synth_desktop::capture_identity(app).await?!=capture.identity {
            capture.recording.stop_recording(); return Err("La sesión de la captura ha cambiado.".into());
        }
        let reply=match provider.poll_capture(&capture.id,cursor).await {
            Ok(reply)=>reply,
            Err(error) if restarts==0=> {
                // Stop/wait before loading the replacement: the file journal
                // rebuilds AEC/VAD and reuses completed ASR under the same UUID.
                restarts+=1; provider.stop().await;
                provider=get_or_init(app).await?; provider.attach(capture).await?;
                log::warn!("Restarted local capture transcription from its journal");
                let _=error; continue;
            },
            Err(error)=>return Err(error),
        };
        if crate::synth_desktop::capture_identity(app).await?!=capture.identity {
            capture.recording.stop_recording(); return Err("La sesión de la captura ha cambiado.".into());
        }
        for event in reply.events {
            if let Some(update)=event.update(&capture.id) {
                super::capture_stream::persist(&capture.sink,&update)?;
                if !first_speech && update.evidence_state.as_deref()==Some("provisional") {
                    first_speech=true;
                    let _=app.emit("speech-detected",json!({"message":"Speech activity detected","capture_id":capture.id}));
                }
                app.emit("transcript-update",&update).map_err(|_| "No se pudo entregar la transcripción en directo.")?;
            }
            cursor=event.sequence;
        }
        if reply.state=="drained" {
            let _=app.emit("transcription-queue-complete",json!({"capture_id":capture.id,"blocks":cursor,"restarts":restarts}));
            return Ok(());
        }
        if let Some(closed)=input_closed {
            if reply.state=="waiting" {
                if closed.elapsed()>Duration::from_secs(60) { return Err("El audio no confirmó su cierre.".into()); }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        }
    }
}

pub async fn shutdown() {
    let mut engine = ENGINE.lock().await;
    if let Some(provider) = engine.take() { provider.stop().await; }
    ACTIVE.store(false, Ordering::SeqCst);
    WAITING.store(false, Ordering::SeqCst);
}

#[async_trait]
impl TranscriptionProvider for QwenProvider {
    async fn transcribe(&self, audio: Vec<f32>, _language: Option<String>) -> Result<TranscriptResult, TranscriptionError> {
        if audio.len() < 1600 { return Err(TranscriptionError::AudioTooShort {samples:audio.len(),minimum:1600}); }
        let mut session = self.session.lock().await;
        let current = session.as_mut().ok_or(TranscriptionError::ModelNotLoaded)?;
        let mut texts = Vec::new();
        // Legacy/import entry points may hand over longer utterances.
        for chunk in audio.chunks(30*16000) {
            let mut samples: Vec<f32> = chunk.iter().map(|v| v.clamp(-1.0,1.0)).collect();
            if samples.len() < 1600 { samples.resize(1600,0.0); }
            match current.exchange(json!({"samples":samples})).await {
                Ok(value) => texts.push(value["text"].as_str().unwrap_or("").to_owned()),
                Err(error) => { *session=None; return Err(TranscriptionError::EngineFailed(error)); }
            }
        }
        Ok(TranscriptResult {text:texts.join(" ").trim().to_owned(),confidence:None,is_partial:false})
    }
    async fn is_model_loaded(&self) -> bool {
        self.session.lock().await.as_mut().map(|s|matches!(s.child.try_wait(),Ok(None))).unwrap_or(false)
    }
    async fn get_current_model(&self) -> Option<String> { if self.is_model_loaded().await {Some(MODEL.into())} else {None} }
    fn provider_name(&self) -> &'static str { "Qwen3-ASR" }
}

/// Exercise native source persistence, bounded VAD and the exact live provider
/// using an explicitly supplied file, without accessing recording devices.
pub fn verify_audio_path(root: PathBuf, resources: PathBuf, fixture: PathBuf) -> Result<Value, String> {
    let decoded = crate::audio::decode_audio_file(&fixture).map_err(|e|e.to_string())?;
    let samples = decoded.to_whisper_format();
    let duration = samples.len() as f64 / 16000.0;
    let mut vad = crate::audio::vad::ContinuousVadProcessor::new_live(16000,500).map_err(|e|e.to_string())?;
    let mut segments = Vec::new();
    for chunk in samples.chunks(480) { segments.extend(vad.process_audio(chunk).map_err(|e|e.to_string())?); }
    segments.extend(vad.flush().map_err(|e|e.to_string())?);
    let mut end = 0.0;
    let mut max_seconds = 0.0_f64;
    for segment in &segments {
        let seconds = segment.samples.len() as f64/16000.0;
        if segment.start_timestamp_ms + 0.2 < end || segment.end_timestamp_ms > duration*1000.0+30.0 || seconds > 8.04 {
            return Err(format!("Live VAD clock/bound failure: {}..{} ({} seconds)",segment.start_timestamp_ms,segment.end_timestamp_ms,seconds));
        }
        end = segment.end_timestamp_ms;
        max_seconds = max_seconds.max(seconds);
    }
    if segments.is_empty() { return Err("No speech detected in the supplied fixture.".into()); }
    let directory = tempfile::tempdir().map_err(|e|e.to_string())?;
    let folder = directory.path().join("capture");
    std::fs::create_dir(&folder).map_err(|e|e.to_string())?;
    let (tap, archive) = crate::audio::source_archive::SourceArchive::start(&folder,true,false).map_err(|e|e.to_string())?;
    let pcm = crate::audio::audio_processing::resample_audio(&samples,16000,48000);
    for (index,chunk) in pcm.chunks(8*48000).enumerate() {
        tap.push(&crate::audio::AudioChunk {data:chunk.to_vec(),sample_rate:48000,
            timestamp:(index*8*48000+chunk.len()) as f64/48000.0,chunk_id:index as u64,
            device_type:crate::audio::recording_state::DeviceType::Microphone}).map_err(|e|e.to_string())?;
    }
    drop(tap); archive.close().map_err(|e|e.to_string())?;
    crate::synth_desktop::capture_receipt(directory.path(),&folder)?;
    tokio::runtime::Runtime::new().map_err(|e|e.to_string())?.block_on(async {
        let provider = QwenProvider::start(root,resources).await?;
        let started = std::time::Instant::now();
        let mut words = 0;
        for segment in &segments {
            let result = provider.transcribe(segment.samples.clone(),None).await.map_err(|e|e.to_string())?;
            words += result.text.split_whitespace().count();
        }
        provider.stop().await;
        if words == 0 { return Err("Qwen returned no text for the supplied speech fixture.".into()); }
        Ok(json!({"status":"PASS","provider":"qwen3","whisper_required":false,
            "live_segments":segments.len(),"max_segment_seconds":max_seconds,
            "audio_seconds":duration,"decode_seconds":started.elapsed().as_secs_f64(),
            "words":words,"source_archive":"closed_and_hash_verified","microphone_accessed":false}))
    })
}

#[cfg(test)]
mod waiting_tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn only_acoustic_contention_defers_capture_start() {
        assert_eq!(start_readiness(Ok(())),Ok(false));
        assert_eq!(start_readiness(Err(AUDIO_BUSY.into())),Ok(true));
        assert_eq!(start_readiness(Err("missing runtime".into())),Err("missing runtime".into()));
    }

    #[tokio::test]
    async fn waiting_drains_notifications_and_resumes_with_one_engine() {
        let (sender,mut receiver)=tokio::sync::mpsc::unbounded_channel();
        for n in 0..512 {sender.send(n).unwrap();}
        let calls=AtomicUsize::new(0);
        let result=wait_for_engine(&mut receiver,|| {
            let attempt=calls.fetch_add(1,Ordering::SeqCst);
            async move {if attempt==0 {Err(AUDIO_BUSY.to_string())} else {Ok(42)}}
        },||{},Duration::from_millis(10)).await.unwrap();
        assert_eq!(result,Some(42));
        assert_eq!(calls.load(Ordering::SeqCst),2);
        assert!(receiver.is_empty());
        drop(sender);
    }

    #[tokio::test]
    async fn notifications_do_not_restart_pending_model_initialization() {
        let (sender,mut receiver)=tokio::sync::mpsc::unbounded_channel();
        for n in 0..512 {sender.send(n).unwrap();}
        let calls=AtomicUsize::new(0);
        let result=wait_for_engine(&mut receiver,|| {
            calls.fetch_add(1,Ordering::SeqCst);
            async {tokio::time::sleep(Duration::from_millis(20)).await;Ok(7)}
        },||{},Duration::from_millis(10)).await.unwrap();
        assert_eq!(result,Some(7));
        assert_eq!(calls.load(Ordering::SeqCst),1);
        assert!(receiver.is_empty());
        drop(sender);
    }

    #[tokio::test]
    async fn closing_capture_cancels_initialization_without_waiting_for_the_other_job() {
        struct DropNotice(Arc<AtomicBool>);
        impl Drop for DropNotice {fn drop(&mut self){self.0.store(true,Ordering::SeqCst);}}
        let (sender,mut receiver)=tokio::sync::mpsc::unbounded_channel::<u8>();
        let dropped=Arc::new(AtomicBool::new(false));let observed=dropped.clone();
        let (started,ready)=tokio::sync::oneshot::channel();let mut started=Some(started);
        let task=tokio::spawn(async move {
            wait_for_engine(&mut receiver,|| {
                let notice=DropNotice(observed.clone());let signal=started.take();
                async move {
                    let _notice=notice;if let Some(signal)=signal {let _=signal.send(());}
                    std::future::pending::<Result<usize,String>>().await
                }
            },||{},Duration::from_secs(60)).await
        });
        ready.await.unwrap();sender.send(1).unwrap();drop(sender);
        assert_eq!(tokio::time::timeout(Duration::from_secs(1),task).await.unwrap().unwrap().unwrap(),None);
        assert!(dropped.load(Ordering::SeqCst));
    }

    #[tokio::test]
    async fn a_closed_capture_never_starts_a_model_even_with_buffered_notifications() {
        let (sender,mut receiver)=tokio::sync::mpsc::unbounded_channel();sender.send(1).unwrap();drop(sender);
        let result=wait_for_engine(&mut receiver,||async {panic!("Must not initialize after close"); #[allow(unreachable_code)] Ok::<_,String>(1)},||{},Duration::from_millis(1)).await.unwrap();
        assert_eq!(result,None);
    }

    #[tokio::test]
    async fn model_failures_are_not_misreported_as_queue_contention() {
        let (_sender,mut receiver)=tokio::sync::mpsc::unbounded_channel::<u8>();
        let result=wait_for_engine(&mut receiver,||async {Err::<u8,_>("broken runtime".to_owned())},||panic!("Not a busy engine"),Duration::from_millis(1)).await;
        assert_eq!(result,Err("broken runtime".into()));
    }
}

#[cfg(test)]
mod capture_integration_tests {
    use super::*;
    use crate::audio::{recording_saver::TranscriptSink, recording_state::{RecordingState, AudioChunk, DeviceType},
        source_archive::SourceArchive, transcription::capture_stream::{LiveCapture, persist}};

    /// Explicit public fixtures only. Reuses installed model bytes offline and
    /// hard-links the real acoustic lease so it cannot overlap the installed app.
    #[tokio::test]
    #[ignore = "requires installed Qwen and explicit public microphone/system fixtures"]
    async fn real_qwen_capture_roundtrip() -> Result<(),String> {
        let installed=PathBuf::from(std::env::var("SYNTH_QWEN_INSTALLED_ROOT").map_err(|_|"Missing installed root")?);
        let resources=PathBuf::from(std::env::var("SYNTH_QWEN_TEST_RESOURCES").map_err(|_|"Missing resources")?);
        let fixture=PathBuf::from(std::env::var("SYNTH_QWEN_PUBLIC_CAPTURE").map_err(|_|"Missing public fixture")?);
        let root=tempfile::tempdir().map_err(|e|e.to_string())?;
        std::fs::copy(installed.join("asr-config.json"),root.path().join("asr-config.json")).map_err(|e|e.to_string())?;
        std::fs::create_dir(root.path().join("pipeline")).map_err(|e|e.to_string())?;
        // Never replace/create the installed lease under a competing worker.
        std::fs::hard_link(installed.join("pipeline/acoustic-worker.lock"),root.path().join("pipeline/acoustic-worker.lock")).map_err(|e|e.to_string())?;
        let folder=root.path().join("capture");
        std::fs::create_dir(&folder).map_err(|e|e.to_string())?;
        let (tap,archive)=SourceArchive::start(&folder,true,true).map_err(|e|e.to_string())?;
        let mut frames=Vec::new();
        for (source,device_type) in [("microphone",DeviceType::Microphone),("system",DeviceType::System)] {
            let decoded=crate::audio::decode_audio_file(&fixture.join(format!("{source}.wav"))).map_err(|e|e.to_string())?;
            if decoded.sample_rate!=48000 || decoded.channels!=1 || decoded.duration_seconds>180.01 {
                return Err("Supply mono 48 kHz public fixtures up to three minutes".into());
            }
            frames.push(decoded.samples.len());
            // At most 46 chunks total: less than the native callback queue cap.
            for (index,chunk) in decoded.samples.chunks(8*48000).enumerate() {
                tap.push(&AudioChunk {data:chunk.to_vec(),sample_rate:48000,
                    timestamp:(index*8*48000+chunk.len()) as f64/48000.,chunk_id:index as u64,
                    device_type:device_type.clone()}).map_err(|e|e.to_string())?;
            }
        }
        drop(tap);
        let manifest=archive.close().map_err(|e|e.to_string())?;
        crate::synth_desktop::capture_receipt(root.path(),&folder)?;
        let sink=TranscriptSink::for_test_folder(folder.clone());
        let capture=LiveCapture::bind(folder.clone(),uuid::Uuid::new_v4().to_string(),sink.clone(),RecordingState::new())?;
        let began=std::time::Instant::now();
        let mut provider=QwenProvider::start(root.path().to_owned(),resources.clone()).await?;
        provider.attach(&capture).await?;
        let (mut cursor,mut events,mut restarts,mut drains)=(0,0,0,0);
        let (mut reused,mut previous_decodes,mut decodes,mut peak_mlx_gb)=(0u64,0u64,0u64,0f64);
        let mut source_counts=std::collections::BTreeMap::new();
        for _ in 0..100 {
            let reply=provider.poll_capture(&capture.id,cursor).await?;
            reused+=reply.reused_sources.unwrap_or(0);
            decodes=reply.decoded_blocks.unwrap_or(0);
            peak_mlx_gb=peak_mlx_gb.max(reply.peak_mlx_gb.unwrap_or(0.));
            for event in &reply.events {
                if let Some(update)=event.update(&capture.id) { persist(&sink,&update)?; }
                *source_counts.entry(event.source.clone()).or_insert(0)+=1;
                events+=1; cursor=event.sequence;
            }
            if restarts==0 && events>=8 {
                previous_decodes=decodes;
                provider.stop().await;
                provider=QwenProvider::start(root.path().to_owned(),resources.clone()).await?;
                provider.attach(&capture).await?;
                restarts+=1;
            } else if reply.state=="drained" { drains+=1; break; }
        }
        provider.stop().await;
        assert_eq!(drains,1,"Native consumer did not drain the closed archive");
        assert_eq!(restarts,1);
        let cores:usize=frames.iter().map(|n|n.div_ceil(12*48000)).sum();
        let expected=cores*2;
        assert_eq!(events,expected,"Missing or duplicated source events after restart");
        assert_eq!(cursor,expected as u64);
        let saved:Value=serde_json::from_slice(&std::fs::read(folder.join("transcripts.json")).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        let segments=saved["segments"].as_array().unwrap();
        assert_eq!(segments.len(),cores,"Final revisions must replace previews, including empty blocks");
        assert!(reused>0,"Restart must reuse committed source windows");
        let ids:std::collections::BTreeSet<_>=segments.iter().map(|s|s["sequence_id"].as_u64().unwrap()).collect();
        assert_eq!(ids.len(),segments.len());
        assert!(segments.iter().all(|s|s["capture_id"]==capture.id && s.get("confidence").is_none()
            && matches!(s["source"].as_str(),Some("microphone"|"system"))
            && s["revision"]==2 && matches!(s["evidence_state"].as_str(),Some("aligned"|"needs_review"))));
        let receipt:Value=serde_json::from_slice(&std::fs::read(folder.join("consolidated-capture.json")).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
        assert_eq!(receipt["capture_id"],capture.id);
        assert_eq!(receipt["asr_evidence"].as_array().unwrap().len(),cores);
        for source in ["microphone","system"] {
            use sha2::{Sha256,Digest};
            let descriptor=&receipt["prepared_sources"][source];
            let path=folder.join(descriptor["path"].as_str().unwrap());
            let bytes=std::fs::read(path).map_err(|e|e.to_string())?;
            assert_eq!(format!("{:x}",Sha256::digest(bytes)),descriptor["sha256"]);
            let mut rows:Vec<_>=segments.iter().filter(|s|s["source"]==source).collect();
            rows.sort_by_key(|s|s["block_id"].as_str().unwrap().rsplit(':').next().unwrap().parse::<u64>().unwrap());
            let visible=rows.iter().map(|s|s["text"].as_str().unwrap()).collect::<Vec<_>>().join(" ");
            let final_text=receipt["transcripts"][source]["segments"].as_array().unwrap().iter()
                .map(|s|s["text"].as_str().unwrap()).collect::<Vec<_>>().join(" ");
            assert_eq!(visible.split_whitespace().collect::<Vec<_>>(),final_text.split_whitespace().collect::<Vec<_>>());
        }
        let voice = if std::env::var("SYNTH_QWEN_TEST_VOICE").as_deref()==Ok("1") {
            let path=root.path().join("pipeline/incremental").join(&capture.id).join("voice-capture.json");
            let value:Value=serde_json::from_slice(&std::fs::read(path).map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
            assert_eq!(value["state"],"drained","Voice collector failed: {:?} phase={:?}",value["error_code"],value["phase"]);
            assert_eq!(value["through_frame"].as_u64(),Some(*frames.iter().max().unwrap() as u64/3));
            assert!(value["diarization_calls"].as_u64().unwrap()>0);
            json!({"state":value["state"],"completed_windows":value["completed_windows"],
                "reused_windows":value["reused_windows"],"new_embedding_calls_after_restart":value["embedding_calls"],
                "new_diarization_calls_after_restart":value["diarization_calls"]})
        } else {Value::Null};
        println!("NATIVE_QWEN_CAPTURE_PROOF {}",json!({"status":"PASS","protocol":3,"source_frames":frames,"live_voice":voice,
            "source_events":source_counts,"events":events,"persisted_segments":segments.len(),"restarts":restarts,
            "elapsed_seconds":began.elapsed().as_secs_f64(),"model_downloads":0,"api_requests":0,
            "asr_calls":previous_decodes+decodes,"reused_source_windows":reused,"peak_mlx_gb":peak_mlx_gb,
            "final_receipt":"published_raw_and_prepared_hash_bound","final_text_equals_native_history":true,
            "source_hashes":manifest["sources"].as_object().unwrap().iter().map(|(k,v)|(k.clone(),v["sha256"].clone())).collect::<serde_json::Map<String,Value>>(),
            "microphone_accessed":false,"input":"explicit_public_files","scope":"native_source_archive_qwen_protocol_and_transcript_sink"}));
        Ok(())
    }
}
