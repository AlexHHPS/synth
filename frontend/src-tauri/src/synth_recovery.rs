//! Captures that were never handed to the pipeline: the app died while
//! recording, or the close failed. They are sealed from the audio on disk and
//! offered to the person, who decides whether to process them.
use serde_json::{json, Value};
use std::path::Path;
use tauri::{AppHandle, Manager};

const ENQUEUED: &str = "sources/recovery-enqueued";

/// Seal every open capture under `root` and list those still waiting.
pub fn pending(root: &Path) -> Vec<Value> {
    let Ok(entries) = std::fs::read_dir(root) else {
        return Vec::new();
    };
    let mut found = Vec::new();
    for entry in entries.flatten() {
        let folder = entry.path();
        if !folder.is_dir()
            || entry
                .file_type()
                .map(|kind| kind.is_symlink())
                .unwrap_or(true)
        {
            continue;
        }
        if let Err(error) = crate::audio::source_archive::recover(&folder) {
            log::warn!(
                "capture recovery skipped {:?}: {}",
                folder.file_name(),
                error
            );
            continue;
        }
        if let Some(item) = describe(&folder) {
            found.push(item);
        }
    }
    found.sort_by(|a, b| b["started_at"].as_str().cmp(&a["started_at"].as_str()));
    found
}

fn describe(folder: &Path) -> Option<Value> {
    if folder.join(ENQUEUED).exists() {
        return None;
    }
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(folder.join("sources/capture.json")).ok()?).ok()?;
    if manifest["recovered"] != true {
        return None;
    }
    let sources = manifest["sources"].as_object()?;
    // Processed captures keep their manifest but no longer hold audio.
    if !sources.values().all(|source| {
        source["path"]
            .as_str()
            .map(|path| Path::new(path).is_file())
            .unwrap_or(false)
    }) {
        return None;
    }
    let frames = sources
        .values()
        .filter_map(|source| source["frames"].as_u64())
        .max()?;
    let name = folder.file_name()?.to_str()?;
    // Folders are named "<title>_<YYYY-MM-DD>_<HH-MM>_<id>".
    let mut parts = name.rsplitn(4, '_');
    let (_, time, date, title) = (
        parts.next()?,
        parts.next()?,
        parts.next()?,
        parts.next().unwrap_or(name),
    );
    Some(
        json!({"capture_id": manifest["capture_id"], "folder_path": folder, "title": title,
        "started_at": format!("{date}T{}", time.replace('-', ":")),
        "duration_seconds": frames / 48000}),
    )
}

pub fn mark_enqueued(folder: &Path) {
    if folder.join("sources/capture.json").exists() {
        let _ = std::fs::write(folder.join(ENQUEUED), b"");
    }
}

#[tauri::command]
pub async fn synth_list_recovered_captures(app: AppHandle) -> Result<Vec<Value>, String> {
    // Never seal the capture that is being written right now.
    if crate::audio::recording_commands::is_recording().await {
        return Ok(Vec::new());
    }
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "No se encuentra la configuración local.")?
        .join("capture/raw");
    tokio::task::spawn_blocking(move || pending(&root))
        .await
        .map_err(|_| "No se pudieron revisar las capturas.".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::audio::{
        recording_state::{AudioChunk, DeviceType},
        source_archive::SourceArchive,
    };

    #[test]
    fn an_interrupted_capture_is_offered_once_and_not_after_it_is_enqueued() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("Daily Synth_2026-11-10_09-00_abc123");
        std::fs::create_dir(&folder).unwrap();
        let (tap, archive) = SourceArchive::start(&folder, true, false).unwrap();
        tap.push(&AudioChunk {
            data: vec![0.1; 96000],
            sample_rate: 48000,
            timestamp: 2.0,
            chunk_id: 0,
            device_type: DeviceType::Microphone,
        })
        .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(1600));
        std::mem::forget(tap);
        std::mem::forget(archive);

        let listed = pending(root.path());
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0]["title"], "Daily Synth");
        assert_eq!(listed[0]["started_at"], "2026-11-10T09:00");
        assert_eq!(listed[0]["duration_seconds"], 2);
        assert_eq!(pending(root.path()).len(), 1);

        mark_enqueued(&folder);
        assert!(pending(root.path()).is_empty());
    }

    #[test]
    fn a_capture_closed_normally_is_never_listed() {
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("Nota_2026-11-10_10-00_def456");
        std::fs::create_dir(&folder).unwrap();
        let (tap, archive) = SourceArchive::start(&folder, true, false).unwrap();
        tap.push(&AudioChunk {
            data: vec![0.1; 4800],
            sample_rate: 48000,
            timestamp: 0.1,
            chunk_id: 0,
            device_type: DeviceType::Microphone,
        })
        .unwrap();
        drop(tap);
        archive.close().unwrap();
        assert!(pending(root.path()).is_empty());
    }
}
