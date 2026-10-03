//! Durable pre-mix source tracks. Bounded callback queue; no silent data loss.
use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc::{sync_channel, SyncSender}};
use std::thread::JoinHandle;
use anyhow::{Result, anyhow};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use super::recording_state::{AudioChunk, DeviceType};

#[derive(Clone)]
pub struct SourceTap {
    sender: SyncSender<AudioChunk>,
    failed: Arc<AtomicBool>,
}

impl SourceTap {
    pub fn push(&self, chunk: &AudioChunk) -> Result<()> {
        if self.failed.load(Ordering::SeqCst) { return Err(anyhow!("source_archive_failed")); }
        self.sender.try_send(chunk.clone()).map_err(|_| {
            self.failed.store(true, Ordering::SeqCst);
            anyhow!("source_archive_queue_failed")
        })
    }
}

struct Track {
    file: File,
    path: PathBuf,
    frames: u64,
    chunks: u64,
    silence_frames: u64,
}

fn private_file(path: &Path) -> Result<File> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create_new(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}

impl Track {
    fn new(path: PathBuf) -> Result<Self> {
        let file = private_file(&path)?;
        let mut track = Self { file, path, frames: 0, chunks: 0, silence_frames: 0 };
        track.flush()?;
        Ok(track)
    }

    fn flush(&mut self) -> Result<()> {
        let bytes = u32::try_from(self.frames.checked_mul(2).ok_or_else(|| anyhow!("source_size_overflow"))?)?;
        let size = bytes.checked_add(36).ok_or_else(|| anyhow!("source_size_overflow"))?;
        let mut header = Vec::with_capacity(44);
        header.extend_from_slice(b"RIFF"); header.extend_from_slice(&size.to_le_bytes());
        header.extend_from_slice(b"WAVEfmt "); header.extend_from_slice(&16u32.to_le_bytes());
        header.extend_from_slice(&1u16.to_le_bytes()); header.extend_from_slice(&1u16.to_le_bytes());
        header.extend_from_slice(&48000u32.to_le_bytes()); header.extend_from_slice(&96000u32.to_le_bytes());
        header.extend_from_slice(&2u16.to_le_bytes()); header.extend_from_slice(&16u16.to_le_bytes());
        header.extend_from_slice(b"data"); header.extend_from_slice(&bytes.to_le_bytes());
        self.file.seek(SeekFrom::Start(0))?;
        self.file.write_all(&header)?;
        self.file.sync_data()?;
        self.file.seek(SeekFrom::End(0))?;
        Ok(())
    }

    fn append(&mut self, chunk: &AudioChunk) -> Result<()> {
        if chunk.sample_rate != 48000 || !chunk.timestamp.is_finite() || chunk.timestamp < 0.0
            || chunk.data.is_empty() || chunk.data.len() > 480000
            || chunk.data.iter().any(|sample| !sample.is_finite()) {
            return Err(anyhow!("source_chunk_invalid"));
        }
        let end = (chunk.timestamp * 48000.0).round() as u64;
        let start = end.saturating_sub(chunk.data.len() as u64);
        let prospective_frames = start.max(self.frames).checked_add(chunk.data.len() as u64)
            .ok_or_else(|| anyhow!("source_size_overflow"))?;
        if prospective_frames > (u32::MAX as u64 - 36) / 2 { return Err(anyhow!("source_size_overflow")); }
        // Callback timing can jitter by one buffer. Preserve every sample; gaps
        // greater than 50ms (startup, pause, disconnect) get explicit silence.
        if start > self.frames + 2400 {
            let gap = start - self.frames;
            if gap > 48000 * 86400 { return Err(anyhow!("source_timeline_invalid")); }
            let zeroes = [0u8; 8192];
            let mut remaining = gap * 2;
            while remaining > 0 {
                let length = remaining.min(zeroes.len() as u64) as usize;
                self.file.write_all(&zeroes[..length])?;
                remaining -= length as u64;
            }
            self.frames += gap;
            self.silence_frames += gap;
        }
        let mut bytes = Vec::with_capacity(chunk.data.len() * 2);
        for sample in &chunk.data {
            let pcm = (sample.clamp(-1.0, 1.0) * 32767.0).round() as i16;
            bytes.extend_from_slice(&pcm.to_le_bytes());
        }
        self.file.write_all(&bytes)?;
        self.frames += chunk.data.len() as u64;
        self.chunks += 1;
        if self.chunks % 50 == 0 { self.flush()?; }
        Ok(())
    }

    fn close(mut self) -> Result<Value> {
        if self.frames == 0 { return Err(anyhow!("source_track_empty")); }
        self.flush()?;
        let final_path = self.path.with_extension("");
        self.file.seek(SeekFrom::Start(0))?;
        let mut hash = Sha256::new();
        let mut buffer = [0u8; 65536];
        loop {
            let n = self.file.read(&mut buffer)?;
            if n == 0 { break; }
            hash.update(&buffer[..n]);
        }
        std::fs::rename(&self.path, &final_path)?;
        Ok(json!({"path":final_path,"sha256":format!("{:x}",hash.finalize()),
            "sample_rate":48000,"frames":self.frames,"chunks":self.chunks,
            "silence_frames":self.silence_frames,"bytes":44+self.frames*2}))
    }
}

pub struct SourceArchive { thread: JoinHandle<Result<Value>> }

impl SourceArchive {
    pub fn start(folder: &Path, microphone: bool, system: bool) -> Result<(SourceTap, Self)> {
        let directory = folder.join("sources");
        std::fs::create_dir(&directory)?;
        #[cfg(unix)] {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))?;
        }
        let mut tracks = BTreeMap::new();
        if microphone { tracks.insert("microphone", Track::new(directory.join("microphone.wav.part"))?); }
        if system { tracks.insert("system", Track::new(directory.join("system.wav.part"))?); }
        if tracks.is_empty() { return Err(anyhow!("source_devices_missing")); }
        let (sender, receiver) = sync_channel::<AudioChunk>(128);
        let failed = Arc::new(AtomicBool::new(false));
        let worker_failed = failed.clone();
        let thread = std::thread::spawn(move || -> Result<Value> {
            let result = (|| {
                while let Ok(chunk) = receiver.recv() {
                    let key = match chunk.device_type { DeviceType::Microphone => "microphone", DeviceType::System => "system" };
                    tracks.get_mut(key).ok_or_else(|| anyhow!("source_device_unexpected"))?.append(&chunk)?;
                }
                if worker_failed.load(Ordering::SeqCst) { return Err(anyhow!("source_archive_data_loss")); }
                let mut sources = BTreeMap::new();
                for (key, track) in tracks { sources.insert(key, track.close()?); }
                let manifest = json!({"version":1,"capture_id":uuid::Uuid::new_v4(),"state":"closed",
                    "clock":"recording_elapsed","sample_format":"pcm16","sources":sources});
                let temporary = directory.join("capture.json.part");
                let mut file = private_file(&temporary)?;
                file.write_all(serde_json::to_vec_pretty(&manifest)?.as_slice())?;
                file.sync_all()?;
                std::fs::rename(temporary, directory.join("capture.json"))?;
                File::open(&directory)?.sync_all()?;
                Ok(manifest)
            })();
            if result.is_err() { worker_failed.store(true, Ordering::SeqCst); }
            result
        });
        Ok((SourceTap { sender, failed }, Self { thread }))
    }

    pub fn close(self) -> Result<Value> {
        self.thread.join().map_err(|_| anyhow!("source_archive_thread_failed"))?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sources_are_distinct_pcm_tracks_and_only_closed_after_flush() {
        let root = tempfile::tempdir().unwrap();
        let (tap, archive) = SourceArchive::start(root.path(), true, true).unwrap();
        for (kind, value) in [(DeviceType::Microphone, 0.25), (DeviceType::System, -0.5)] {
            tap.push(&AudioChunk { data:vec![value;4800], sample_rate:48000, timestamp:0.1,chunk_id:0,device_type:kind }).unwrap();
        }
        assert!(!root.path().join("sources/capture.json").exists());
        drop(tap);
        let manifest = archive.close().unwrap();
        assert_eq!(manifest["state"], "closed");
        let mic = std::fs::read(root.path().join("sources/microphone.wav")).unwrap();
        let system = std::fs::read(root.path().join("sources/system.wav")).unwrap();
        assert_eq!(&mic[..4], b"RIFF");
        assert_eq!(mic.len(), 44+9600);
        assert_ne!(&mic[44..], &system[44..]);
        assert_ne!(manifest["sources"]["microphone"]["sha256"], manifest["sources"]["system"]["sha256"]);
    }
    #[test]
    fn missing_expected_source_cannot_produce_a_closed_manifest() {
        let root = tempfile::tempdir().unwrap();
        let (tap, archive) = SourceArchive::start(root.path(), true, true).unwrap();
        tap.push(&AudioChunk { data:vec![0.1;480],sample_rate:48000,timestamp:0.01,chunk_id:0,device_type:DeviceType::Microphone }).unwrap();
        drop(tap);
        assert!(archive.close().is_err());
        assert!(!root.path().join("sources/capture.json").exists());
    }
    #[test]
    fn bounded_queue_failure_is_sticky_instead_of_dropping_audio_silently() {
        let (sender, _receiver) = sync_channel(1);
        let tap = SourceTap { sender, failed:Arc::new(AtomicBool::new(false)) };
        let chunk = AudioChunk { data:vec![0.1;480],sample_rate:48000,timestamp:0.01,chunk_id:0,device_type:DeviceType::Microphone };
        tap.push(&chunk).unwrap();
        assert!(tap.push(&chunk).is_err());
        assert!(tap.failed.load(Ordering::SeqCst));
        assert!(tap.push(&chunk).is_err());
    }
    #[test]
    fn pause_gap_preserves_the_recording_clock_in_each_source() {
        let root = tempfile::tempdir().unwrap();
        let (tap, archive) = SourceArchive::start(root.path(), true, false).unwrap();
        for timestamp in [0.1,1.2] {
            tap.push(&AudioChunk { data:vec![0.25;4800],sample_rate:48000,timestamp,chunk_id:0,device_type:DeviceType::Microphone }).unwrap();
        }
        drop(tap);
        let manifest = archive.close().unwrap();
        assert_eq!(manifest["sources"]["microphone"]["frames"], 57600);
        assert_eq!(manifest["sources"]["microphone"]["silence_frames"], 48000);
    }
}
