use std::sync::{Arc, Mutex};
use tokio::sync::Mutex as AsyncMutex;
use anyhow::Result;
use log::{info, warn, error};
use tauri::{AppHandle, Runtime, Emitter};
use tokio::sync::mpsc;
use serde::{Serialize, Deserialize};
use std::path::PathBuf;
use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};

use super::recording_state::AudioChunk;
use super::audio_processing::create_meeting_folder;
use super::incremental_saver::IncrementalAudioSaver;

#[cfg(test)]
mod capture_sink_tests {
    use super::*;
    fn segment(sequence_id: u64, text: &str) -> TranscriptSegment {
        TranscriptSegment { id:format!("seg_{sequence_id}"), text:text.into(), audio_start_time:0.,
            audio_end_time:1., duration:1., display_time:"00:00".into(), confidence:None, sequence_id,
            capture_id:None, source:None, evidence_state:None, block_id:None, revision:None }
    }

    #[test]
    fn tail_survives_manager_ownership_changes_and_duplicates_upsert() {
        let directory = tempfile::tempdir().unwrap();
        let mut saver = RecordingSaver::new();
        saver.meeting_folder = Some(directory.path().to_owned());
        let sink = saver.transcript_sink();
        sink.append(segment(1,"First block")).unwrap();
        drop(saver);
        sink.append(segment(2,"Last block after stop")).unwrap();
        sink.append(segment(2,"Last block after stop")).unwrap();
        let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(directory.path().join("transcripts.json")).unwrap()).unwrap();
        assert_eq!(saved["total_segments"],2);
        assert_eq!(saved["segments"][1]["text"],"Last block after stop");
        assert!(saved["segments"][1].get("confidence").is_none());
        #[cfg(unix)] {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(directory.path().join("transcripts.json")).unwrap().permissions().mode() & 0o777,0o600);
        }
    }

    #[test]
    fn concurrent_updates_produce_one_complete_snapshot_and_close_fences_late_events() {
        let directory = tempfile::tempdir().unwrap();
        let mut saver = RecordingSaver::new();
        saver.meeting_folder = Some(directory.path().to_owned());
        let sink = saver.transcript_sink();
        let mut tasks = Vec::new();
        for sequence in 1..=8 {
            let sink = sink.clone();
            tasks.push(std::thread::spawn(move || sink.append(segment(sequence,"Synthetic tail")).unwrap()));
        }
        for task in tasks { task.join().unwrap(); }
        sink.close();
        assert!(sink.append(segment(9,"Too late")).is_err());
        let saved: serde_json::Value = serde_json::from_slice(&std::fs::read(directory.path().join("transcripts.json")).unwrap()).unwrap();
        assert_eq!(saved["total_segments"],8);
        assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(),1);
    }
}

/// Structured transcript segment for JSON export
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TranscriptSegment {
    pub id: String,
    pub text: String,
    pub audio_start_time: f64, // Seconds from recording start
    pub audio_end_time: f64,   // Seconds from recording start
    pub duration: f64,          // Segment duration in seconds
    pub display_time: String,   // Formatted time for display like "[02:15]"
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f32>,
    pub sequence_id: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capture_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub evidence_state: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub block_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<u8>,
}

impl From<&super::transcription::TranscriptUpdate> for TranscriptSegment {
    fn from(update: &super::transcription::TranscriptUpdate) -> Self {
        Self { id:update.block_id.clone().unwrap_or_else(||format!("seg_{}",update.sequence_id)), text:update.text.clone(),
            audio_start_time:update.audio_start_time, audio_end_time:update.audio_end_time,
            duration:update.duration, display_time:update.timestamp.clone(), confidence:update.confidence,
            sequence_id:update.sequence_id, capture_id:update.capture_id.clone(),
            source:Some(update.source.clone()), evidence_state:update.evidence_state.clone(),
            block_id:update.block_id.clone(), revision:update.revision }
    }
}

/// A capture owns this sink even while its manager is being stopped/moved.
#[derive(Clone)]
pub struct TranscriptSink {
    segments: Arc<Mutex<Vec<TranscriptSegment>>>,
    writer: Arc<Mutex<()>>,
    active: Arc<AtomicBool>,
    folder: Option<PathBuf>,
}

impl TranscriptSink {
    #[cfg(test)]
    pub(crate) fn for_test_folder(folder: PathBuf) -> Self {
        Self { segments:Arc::new(Mutex::new(Vec::new())), writer:Arc::new(Mutex::new(())),
            active:Arc::new(AtomicBool::new(true)), folder:Some(folder) }
    }

    pub fn append(&self, segment: TranscriptSegment) -> Result<()> {
        let _write = self.writer.lock().map_err(|_| anyhow::anyhow!("transcript_writer_unavailable"))?;
        if !self.active.load(Ordering::SeqCst) { return Err(anyhow::anyhow!("transcript_capture_closed")); }
        {
            let mut segments = self.segments.lock().map_err(|_| anyhow::anyhow!("transcript_store_unavailable"))?;
            if let Some(previous) = segments.iter_mut().find(|s| s.capture_id==segment.capture_id &&
                match &segment.block_id {
                    Some(id)=>s.block_id.as_ref()==Some(id),
                    None=>s.block_id.is_none() && s.sequence_id==segment.sequence_id,
                }) {
                // Retain empty final revisions as tombstones: delayed previews
                // must not resurrect a word removed by seam consolidation.
                if (segment.revision.unwrap_or(0),segment.sequence_id)<(previous.revision.unwrap_or(0),previous.sequence_id) {
                    return Ok(());
                }
                *previous = segment;
            } else { segments.push(segment); }
        }
        self.write_locked()
    }

    fn write_locked(&self) -> Result<()> {
        let Some(folder) = &self.folder else { return Ok(()); };
        let segments = self.segments.lock().map_err(|_| anyhow::anyhow!("transcript_store_unavailable"))?.clone();
        let value = serde_json::json!({"version":"1.0","segments":segments,
            "last_updated":chrono::Utc::now().to_rfc3339(),"total_segments":segments.len()});
        let temporary = folder.join(format!(".transcripts-{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| -> Result<()> {
            let mut options = std::fs::OpenOptions::new();
            options.create_new(true).write(true);
            #[cfg(unix)] { use std::os::unix::fs::OpenOptionsExt; options.mode(0o600); }
            let mut file = options.open(&temporary)?;
            file.write_all(&serde_json::to_vec_pretty(&value)?)?;
            file.sync_all()?;
            std::fs::rename(&temporary, folder.join("transcripts.json"))?;
            std::fs::File::open(folder)?.sync_all()?;
            Ok(())
        })();
        if result.is_err() { let _ = std::fs::remove_file(temporary); }
        result
    }

    pub fn snapshot(&self) -> Result<()> {
        let _write = self.writer.lock().map_err(|_| anyhow::anyhow!("transcript_writer_unavailable"))?;
        self.write_locked()
    }

    pub fn close(&self) {
        let _write = self.writer.lock().unwrap();
        self.active.store(false, Ordering::SeqCst);
    }
}

/// Meeting metadata structure
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MeetingMetadata {
    pub version: String,
    pub meeting_id: Option<String>,
    pub meeting_name: Option<String>,
    pub created_at: String,
    pub completed_at: Option<String>,
    pub duration_seconds: Option<f64>,
    pub devices: DeviceInfo,
    pub audio_file: String,
    pub transcript_file: String,
    pub sample_rate: u32,
    pub status: String,  // "recording", "completed", "error"
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeviceInfo {
    pub microphone: Option<String>,
    pub system_audio: Option<String>,
}

/// New recording saver using incremental saving strategy
pub struct RecordingSaver {
    incremental_saver: Option<Arc<AsyncMutex<IncrementalAudioSaver>>>,
    meeting_folder: Option<PathBuf>,
    meeting_name: Option<String>,
    metadata: Option<MeetingMetadata>,
    transcript_segments: Arc<Mutex<Vec<TranscriptSegment>>>,
    transcript_writer: Arc<Mutex<()>>,
    transcript_active: Arc<AtomicBool>,
    is_saving: Arc<Mutex<bool>>,
}

impl RecordingSaver {
    pub fn new() -> Self {
        Self {
            incremental_saver: None,
            meeting_folder: None,
            meeting_name: None,
            metadata: None,
            transcript_segments: Arc::new(Mutex::new(Vec::new())),
            transcript_writer: Arc::new(Mutex::new(())),
            transcript_active: Arc::new(AtomicBool::new(true)),
            is_saving: Arc::new(Mutex::new(false)),
        }
    }

    /// Set the meeting name for this recording session
    pub fn set_meeting_name(&mut self, name: Option<String>) {
        self.meeting_name = name;
    }

    /// Set device information in metadata
    pub fn set_device_info(&mut self, mic_name: Option<String>, sys_name: Option<String>) {
        if let Some(ref mut metadata) = self.metadata {
            metadata.devices.microphone = mic_name;
            metadata.devices.system_audio = sys_name;

            // Write updated metadata to disk if folder exists
            if let Some(folder) = &self.meeting_folder {
                let metadata_clone = metadata.clone();
                if let Err(e) = self.write_metadata(folder, &metadata_clone) {
                    warn!("Failed to update metadata with device info: {}", e);
                }
            }
        }
    }

    /// Add or update a structured transcript segment (upserts based on sequence_id)
    /// Also saves incrementally to disk
    pub fn add_transcript_segment(&self, segment: TranscriptSegment) {
        if self.transcript_sink().append(segment).is_err() { warn!("Could not persist a transcript segment"); }
    }

    pub fn transcript_sink(&self) -> TranscriptSink {
        TranscriptSink { segments:self.transcript_segments.clone(), writer:self.transcript_writer.clone(),
            active:self.transcript_active.clone(), folder:self.meeting_folder.clone() }
    }

    /// Legacy method for backward compatibility - converts text to basic segment
    pub fn add_transcript_chunk(&self, text: String) {
        let segment = TranscriptSegment {
            id: format!("seg_{}", chrono::Utc::now().timestamp_millis()),
            text,
            audio_start_time: 0.0,
            audio_end_time: 0.0,
            duration: 0.0,
            display_time: "[00:00]".to_string(),
            confidence: None,
            sequence_id: 0,
            capture_id: None,
            source: None,
            evidence_state: None,
            block_id: None,
            revision: None,
        };
        self.add_transcript_segment(segment);
    }

    /// Start accumulation with optional incremental saving
    ///
    /// # Arguments
    /// * `auto_save` - If true, creates checkpoints and enables saving. If false, audio chunks are discarded.
    pub fn start_accumulation(
        &mut self,
        auto_save: bool,
        mut receiver: mpsc::UnboundedReceiver<AudioChunk>,
    ) {
        if auto_save {
            info!("Initializing incremental audio saver for recording (auto-save ENABLED)");
        } else {
            info!("Starting recording without audio saving (auto-save DISABLED - transcripts only)");
        }

        // Initialize meeting folder and incremental saver ONLY if auto_save is enabled
        if auto_save {
            if let Some(name) = self.meeting_name.clone() {
                match self.initialize_meeting_folder(&name, true) {
                    Ok(()) => info!("Successfully initialized meeting folder with checkpoints"),
                    Err(e) => {
                        error!("Failed to initialize meeting folder: {}", e);
                        // Continue anyway - will use fallback flat structure
                    }
                }
            }
        } else {
            // When auto_save is false, still create meeting folder for transcripts/metadata
            // but skip .checkpoints directory
            if let Some(name) = self.meeting_name.clone() {
                match self.initialize_meeting_folder(&name, false) {
                    Ok(()) => info!("Successfully initialized meeting folder (transcripts only)"),
                    Err(e) => {
                        error!("Failed to initialize meeting folder: {}", e);
                    }
                }
            }
        }

        // Start accumulation task
        let is_saving_clone = self.is_saving.clone();
        let incremental_saver_arc = self.incremental_saver.clone();
        let save_audio = auto_save;

        tokio::spawn(async move {
            info!("Recording saver accumulation task started (save_audio: {})", save_audio);

            while let Some(chunk) = receiver.recv().await {
                // Check if we should continue
                let should_continue = if let Ok(is_saving) = is_saving_clone.lock() {
                    *is_saving
                } else {
                    false
                };

                if !should_continue {
                    break;
                }

                // Only process audio chunks if auto_save is enabled
                if save_audio {
                    // Add chunk to incremental saver
                    if let Some(saver_arc) = &incremental_saver_arc {
                        let mut saver_guard = saver_arc.lock().await;
                        if let Err(e) = saver_guard.add_chunk(chunk) {
                            error!("Failed to add chunk to incremental saver: {}", e);
                        }
                    } else {
                        error!("Incremental saver not available while accumulating");
                    }
                } else {
                    // auto_save is false: discard audio chunk (no-op)
                    // Transcription already happened in the pipeline before this point
                }
            }

            info!("Recording saver accumulation task ended");
        });

        // Set saving flag
        if let Ok(mut is_saving) = self.is_saving.lock() {
            *is_saving = true;
        }
    }

    /// Initialize meeting folder structure and metadata
    ///
    /// # Arguments
    /// * `meeting_name` - Name of the meeting
    /// * `create_checkpoints` - Whether to create .checkpoints/ directory and IncrementalAudioSaver
    fn initialize_meeting_folder(&mut self, meeting_name: &str, create_checkpoints: bool) -> Result<()> {
        // Load preferences to get base recordings folder
        let base_folder = super::recording_preferences::get_default_recordings_folder();

        // Create meeting folder structure (with or without .checkpoints/ subdirectory)
        let meeting_folder = create_meeting_folder(&base_folder, meeting_name, create_checkpoints)?;

        // Only initialize incremental saver if checkpoints are needed (auto_save is true)
        if create_checkpoints {
            let incremental_saver = IncrementalAudioSaver::new(meeting_folder.clone(), 48000)?;
            self.incremental_saver = Some(Arc::new(AsyncMutex::new(incremental_saver)));
            info!("✅ Incremental audio saver initialized for meeting: {}", meeting_name);
        } else {
            info!("⚠️  Skipped incremental audio saver (auto-save disabled)");
        }

        // Create initial metadata
        let metadata = MeetingMetadata {
            version: "1.0".to_string(),
            meeting_id: None,  // Will be set by backend
            meeting_name: Some(meeting_name.to_string()),
            created_at: chrono::Utc::now().to_rfc3339(),
            completed_at: None,
            duration_seconds: None,
            devices: DeviceInfo {
                microphone: None,  // Could be enhanced to store actual device names
                system_audio: None,
            },
            audio_file: if create_checkpoints { "audio.mp4".to_string() } else { "".to_string() },
            transcript_file: "transcripts.json".to_string(),
            sample_rate: 48000,
            status: "recording".to_string(),
        };

        // Write initial metadata.json
        self.write_metadata(&meeting_folder, &metadata)?;

        self.meeting_folder = Some(meeting_folder);
        self.metadata = Some(metadata);

        Ok(())
    }

    /// Write metadata.json to disk (atomic write with temp file)
    fn write_metadata(&self, folder: &PathBuf, metadata: &MeetingMetadata) -> Result<()> {
        let metadata_path = folder.join("metadata.json");
        let temp_path = folder.join(".metadata.json.tmp");

        let json_string = serde_json::to_string_pretty(metadata)?;
        std::fs::write(&temp_path, json_string)?;
        std::fs::rename(&temp_path, &metadata_path)?;  // Atomic

        Ok(())
    }

    /// Write transcripts.json to disk (atomic write with temp file and validation)
    fn write_transcripts_json(&self, folder: &PathBuf) -> Result<()> {
        let mut sink = self.transcript_sink();
        sink.folder = Some(folder.clone());
        sink.snapshot()
    }

    // in frontend/src-tauri/src/audio/recording_saver.rs
    pub fn get_stats(&self) -> (usize, u32) {
        if let Some(ref saver) = self.incremental_saver {
            if let Ok(guard) = saver.try_lock() {
                (guard.get_checkpoint_count() as usize, 48000)
            } else {
                (0, 48000)
            }
        } else {
            (0, 48000)
        }
    }

    /// Stop and save using incremental saving approach
    ///
    /// # Arguments
    /// * `app` - Tauri app handle for emitting events
    /// * `recording_duration` - Actual recording duration in seconds (from RecordingState)
    pub async fn stop_and_save<R: Runtime>(
        &mut self,
        app: &AppHandle<R>,
        recording_duration: Option<f64>
    ) -> Result<Option<String>, String> {
        info!("Stopping recording saver");

        // Stop accumulation
        if let Ok(mut is_saving) = self.is_saving.lock() {
            *is_saving = false;
        }

        // Give time for final chunks
        tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;

        // Check if incremental saver exists (indicates auto_save was enabled)
        let should_save_audio = self.incremental_saver.is_some();

        if !should_save_audio {
            info!("⚠️  No audio saver initialized (auto-save was disabled) - skipping audio finalization");
            info!("✅ Transcripts and metadata already saved incrementally");
            return Ok(None);
        }

        // Finalize incremental saver (merge checkpoints into final audio.mp4)
        let final_audio_path = if let Some(saver_arc) = &self.incremental_saver {
            let mut saver = saver_arc.lock().await;
            match saver.finalize().await {
                Ok(path) => {
                    info!("✅ Successfully finalized audio: {}", path.display());
                    path
                }
                Err(e) => {
                    error!("❌ Failed to finalize incremental saver: {}", e);
                    return Err(format!("Failed to finalize audio: {}", e));
                }
            }
        } else {
            error!("No incremental saver initialized - cannot save recording");
            return Err("No incremental saver initialized".to_string());
        };

        // Save final transcripts.json with validation
        if let Some(folder) = &self.meeting_folder {
            if let Err(e) = self.write_transcripts_json(folder) {
                error!("❌ Failed to write final transcripts: {}", e);
                return Err(format!("Failed to save transcripts: {}", e));
            }

            // Verify transcripts were written correctly
            let transcript_path = folder.join("transcripts.json");
            if !transcript_path.exists() {
                error!("❌ Transcript file was not created at: {}", transcript_path.display());
                return Err("Transcript file verification failed".to_string());
            }
            info!("✅ Transcripts saved and verified at: {}", transcript_path.display());
        }

        // Update metadata to completed status with actual recording duration
        if let (Some(folder), Some(mut metadata)) = (&self.meeting_folder, self.metadata.clone()) {
            metadata.status = "completed".to_string();
            metadata.completed_at = Some(chrono::Utc::now().to_rfc3339());

            // Use actual recording duration from RecordingState (more accurate than transcript segments)
            // Falls back to last transcript segment if duration not provided
            metadata.duration_seconds = recording_duration.or_else(|| {
                if let Ok(segments) = self.transcript_segments.lock() {
                    segments.last().map(|seg| seg.audio_end_time)
                } else {
                    None
                }
            });

            if let Err(e) = self.write_metadata(folder, &metadata) {
                error!("❌ Failed to update metadata to completed: {}", e);
                return Err(format!("Failed to update metadata: {}", e));
            }

            info!("✅ Metadata updated with duration: {:?}s", metadata.duration_seconds);
        }

        // Emit save event with audio and transcript paths
        let save_event = serde_json::json!({
            "audio_file": final_audio_path.to_string_lossy(),
            "transcript_file": self.meeting_folder.as_ref()
                .map(|f| f.join("transcripts.json").to_string_lossy().to_string()),
            "meeting_name": self.meeting_name,
            "meeting_folder": self.meeting_folder.as_ref()
                .map(|f| f.to_string_lossy().to_string())
        });

        if let Err(e) = app.emit("recording-saved", &save_event) {
            warn!("Failed to emit recording-saved event: {}", e);
        }

        self.transcript_sink().close();
        // Clean up transcript segments
        if let Ok(mut segments) = self.transcript_segments.lock() {
            segments.clear();
        }

        Ok(Some(final_audio_path.to_string_lossy().to_string()))
    }

    /// Get the meeting folder path (for passing to backend)
    pub fn get_meeting_folder(&self) -> Option<&PathBuf> {
        self.meeting_folder.as_ref()
    }

    /// Get accumulated transcript segments (for reload sync)
    pub fn get_transcript_segments(&self) -> Vec<TranscriptSegment> {
        if let Ok(segments) = self.transcript_segments.lock() {
            segments.clone()
        } else {
            Vec::new()
        }
    }

    /// Get meeting name (for reload sync)
    pub fn get_meeting_name(&self) -> Option<String> {
        self.meeting_name.clone()
    }
}

impl Default for RecordingSaver {
    fn default() -> Self {
        Self::new()
    }
}
