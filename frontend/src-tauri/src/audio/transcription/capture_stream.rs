//! Per-capture ownership, cursor validation and durable live transcript delivery.
use std::{path::PathBuf, sync::Arc, io::{Read, Write}};
use serde::Deserialize;
use serde_json::{json, Value};
use crate::audio::{recording_state::RecordingState, recording_saver::{TranscriptSink, TranscriptSegment}};
use super::worker::TranscriptUpdate;

#[derive(Clone)]
pub struct LiveCapture {
    pub folder: PathBuf,
    pub id: String,
    pub identity: String,
    pub sink: TranscriptSink,
    pub recording: Arc<RecordingState>,
}

impl LiveCapture {
    pub fn bind(folder: PathBuf, identity: String, sink: TranscriptSink, recording: Arc<RecordingState>) -> Result<Self,String> {
        let mut bytes = Vec::new();
        std::fs::File::open(folder.join("sources/progress.json")).map_err(|_| "No se encuentra el progreso de la captura.")?
            .take(65537).read_to_end(&mut bytes).map_err(|_| "No se pudo leer el progreso de la captura.")?;
        if bytes.len()>65536 { return Err("El progreso de captura no es válido.".into()); }
        let progress:Value=serde_json::from_slice(&bytes).map_err(|_| "El progreso de captura no es válido.")?;
        let id=uuid::Uuid::parse_str(progress["capture_id"].as_str().unwrap_or("")).map_err(|_| "La captura no tiene identificador.")?.to_string();
        let path=folder.join("capture-owner.json");
        let owner=json!({"version":1,"capture_id":id,"identity":identity});
        let mut options=std::fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
        let mut file=options.open(path).map_err(|_| "No se pudo guardar el propietario de la captura.")?;
        file.write_all(&serde_json::to_vec(&owner).map_err(|_| "No se pudo preparar la captura.")?).map_err(|_| "No se pudo guardar la captura.")?;
        file.sync_all().map_err(|_| "No se pudo confirmar el propietario de la captura.")?;
        std::fs::File::open(&folder).and_then(|f|f.sync_all()).map_err(|_| "No se pudo confirmar la captura.")?;
        Ok(Self {folder,id,identity,sink,recording})
    }
}

#[derive(Debug,Deserialize)]
pub struct BlockEvent {
    pub sequence: u64,
    pub source: String,
    pub start_frame: u64,
    pub end_frame: u64,
    pub result: BlockText,
}

#[derive(Debug,Deserialize)]
pub struct BlockText {
    pub text: String,
    pub source: String,
    pub state: String,
    pub block_id: Option<String>,
    pub revision: Option<u8>,
    pub audio_start_time: Option<f64>,
    pub audio_end_time: Option<f64>,
    pub validated_through_frame: Option<u64>,
}

#[derive(Debug,Deserialize)]
pub struct CaptureReply {
    pub capture_id: String,
    pub state: String,
    pub events: Vec<BlockEvent>,
    pub decoded_blocks: Option<u64>,
    pub reused_sources: Option<u64>,
    pub peak_mlx_gb: Option<f64>,
}

impl CaptureReply {
    pub fn validate(&self, capture_id: &str, after: u64) -> Result<(),String> {
        if self.capture_id!=capture_id || !matches!(self.state.as_str(),"waiting"|"processed"|"drained") || self.events.len()>64 {
            return Err("La respuesta no pertenece a esta captura.".into());
        }
        let mut cursor=after;
        for event in &self.events {
            if cursor.checked_add(1)!=Some(event.sequence) || !matches!(event.source.as_str(),"microphone"|"system")
                || event.result.source!=event.source || event.start_frame>=event.end_frame
                || event.end_frame-event.start_frame>12*48000 || event.end_frame>86400*48000
                || event.result.text.len()>80000
                || !matches!(event.result.state.as_str(),"provisional"|"provisional_low_voice_evidence"|"no_speech_detected"|"aligned"|"needs_review") {
                return Err("La secuencia de audio está incompleta o no es válida.".into());
            }
            if event.result.state=="no_speech_detected" && !event.result.text.is_empty() {
                return Err("El resultado de voz no es coherente.".into());
            }
            let value=&event.result;
            match (&value.block_id,value.revision,value.audio_start_time,value.audio_end_time,value.validated_through_frame) {
                (Some(id),Some(revision),Some(start),Some(end),Some(ready))=> {
                    let partial=matches!(value.state.as_str(),"provisional"|"provisional_low_voice_evidence");
                    if id!=&format!("{}:{}",event.source,event.start_frame)
                        || event.start_frame%(12*48000)!=0 || !matches!(revision,1|2)
                        || (revision==1)!=partial || value.state=="no_speech_detected"
                        || ready<event.end_frame || ready>86400*48000
                        || !start.is_finite() || !end.is_finite() || start<0. || start>end
                        || start+3.001<event.start_frame as f64/48000.
                        || end>event.end_frame as f64/48000.+3.001 || end>ready as f64/48000.+0.001 {
                        return Err("La revisión del bloque de audio no es válida.".into());
                    }
                },
                (None,None,None,None,None) if !matches!(value.state.as_str(),"aligned"|"needs_review")=>{},
                _=>return Err("La revisión del bloque de audio está incompleta.".into()),
            }
            cursor=event.sequence;
        }
        Ok(())
    }
}

impl BlockEvent {
    pub fn update(&self, capture_id: &str) -> Option<TranscriptUpdate> {
        if self.result.text.trim().is_empty() && self.result.revision!=Some(2) { return None; }
        let start=self.result.audio_start_time.unwrap_or(self.start_frame as f64/48000.);
        let end=self.result.audio_end_time.unwrap_or(self.end_frame as f64/48000.);
        Some(TranscriptUpdate {text:self.result.text.clone(),timestamp:chrono::Local::now().format("%H:%M:%S").to_string(),
            source:self.source.clone(),sequence_id:self.sequence,chunk_start_time:start,
            is_partial:self.result.state.starts_with("provisional"),confidence:None,
            audio_start_time:start,audio_end_time:end,duration:end-start,capture_id:Some(capture_id.into()),
            evidence_state:Some(self.result.state.clone()),block_id:self.result.block_id.clone(),revision:self.result.revision})
    }
}

pub fn persist(sink: &TranscriptSink, update: &TranscriptUpdate) -> Result<(),String> {
    sink.append(TranscriptSegment::from(update))
        .map_err(|_| "No se pudo guardar el bloque transcrito; el audio sigue conservado.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn reply()->CaptureReply { serde_json::from_value(json!({"capture_id":"capture-a","state":"processed","events":[
        {"sequence":1,"source":"system","start_frame":0,"end_frame":576000,
         "result":{"text":"Public synthetic text","source":"system","state":"provisional"}}]})).unwrap() }
    #[test]
    fn source_and_cursor_are_fenced_and_qwen_does_not_invent_a_confidence() {
        let value=reply(); value.validate("capture-a",0).unwrap();
        assert!(value.validate("capture-b",0).is_err());
        assert!(value.validate("capture-a",1).is_err());
        let update=value.events[0].update("capture-a").unwrap();
        assert_eq!(update.source,"system"); assert_eq!(update.audio_end_time,12.);
        assert!(serde_json::to_value(update).unwrap().get("confidence").is_none());
    }
    #[test]
    fn silence_advances_the_cursor_without_a_fake_text_event() {
        let mut value=reply(); value.events[0].result.text="".into();
        value.events[0].result.state="no_speech_detected".into();
        value.validate("capture-a",0).unwrap(); assert!(value.events[0].update("capture-a").is_none());
        value.events[0].result.text="invented".into(); assert!(value.validate("capture-a",0).is_err());
    }
    #[test]
    fn mismatched_sources_and_unbounded_times_are_rejected() {
        let mut value=reply(); value.events[0].result.source="microphone".into();
        assert!(value.validate("capture-a",0).is_err()); value.events[0].result.source="system".into();
        value.events[0].end_frame=576001; assert!(value.validate("capture-a",0).is_err());
    }

    #[test]
    fn persisted_history_keeps_capture_source_and_uncertainty() {
        let directory=tempfile::tempdir().unwrap();
        let sink=TranscriptSink::for_test_folder(directory.path().to_owned());
        let mut value=reply();
        value.events[0].result.state="provisional_low_voice_evidence".into();
        persist(&sink,&value.events[0].update("capture-a").unwrap()).unwrap();
        let saved:Value=serde_json::from_slice(&std::fs::read(directory.path().join("transcripts.json")).unwrap()).unwrap();
        assert_eq!(saved["segments"][0]["capture_id"],"capture-a");
        assert_eq!(saved["segments"][0]["source"],"system");
        assert_eq!(saved["segments"][0]["evidence_state"],"provisional_low_voice_evidence");
        assert!(saved["segments"][0].get("confidence").is_none());
    }

    fn revision(sequence:u64,revision:u8,text:&str)->CaptureReply {
        serde_json::from_value(json!({"capture_id":"capture-a","state":"processed","events":[
            {"sequence":sequence,"source":"system","start_frame":0,"end_frame":576000,
             "result":{"text":text,"source":"system","state":if revision==1 {"provisional"} else {"aligned"},
                "block_id":"system:0","revision":revision,"audio_start_time":11.92,"audio_end_time":12.08,
                "validated_through_frame":720000}}]})).unwrap()
    }

    #[test]
    fn aligned_revisions_use_word_times_and_empty_final_blocks_are_tombstones() {
        let value=revision(2,2,"");
        value.validate("capture-a",1).unwrap();
        let update=value.events[0].update("capture-a").unwrap();
        assert!(!update.is_partial); assert_eq!(update.audio_start_time,11.92);
        assert_eq!(update.audio_end_time,12.08); assert_eq!(update.revision,Some(2));
        assert_eq!(update.block_id.as_deref(),Some("system:0"));
        assert!(revision(1,1,"").events[0].update("capture-a").is_none());
        let mut invalid=revision(2,2,"No");
        invalid.events[0].result.block_id=Some("microphone:0".into());
        assert!(invalid.validate("capture-a",1).is_err());
        invalid=revision(2,2,"No"); invalid.events[0].result.state="provisional".into();
        assert!(invalid.validate("capture-a",1).is_err());
        invalid=revision(2,2,"No"); invalid.events[0].result.validated_through_frame=Some(576000);
        assert!(invalid.validate("capture-a",1).is_err());
        invalid=revision(2,2,"No"); invalid.events[0].result.revision=None;
        assert!(invalid.validate("capture-a",1).is_err());
    }

    #[test]
    fn final_replaces_preview_durably_and_late_preview_cannot_resurrect_it() {
        let directory=tempfile::tempdir().unwrap();
        let sink=TranscriptSink::for_test_folder(directory.path().to_owned());
        for value in [revision(1,1,"No"),revision(2,2,""),revision(1,1,"No"),revision(3,1,"No")] {
            persist(&sink,&value.events[0].update("capture-a").unwrap()).unwrap();
        }
        let saved:Value=serde_json::from_slice(&std::fs::read(directory.path().join("transcripts.json")).unwrap()).unwrap();
        assert_eq!(saved["total_segments"],1);
        assert_eq!(saved["segments"][0]["text"],"");
        assert_eq!(saved["segments"][0]["revision"],2);
        assert_eq!(saved["segments"][0]["sequence_id"],2);
    }
}
