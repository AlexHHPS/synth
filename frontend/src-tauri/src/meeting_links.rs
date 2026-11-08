//! Links select a meeting; its data still passes the signed-in user's API permissions.
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Runtime, WebviewWindow};

static PENDING: Mutex<Option<String>> = Mutex::new(None);

fn meeting_id(value: &str) -> Option<String> {
    let url = reqwest::Url::parse(value).ok()?;
    if url.scheme() != "synth-voice" || url.host_str() != Some("meeting")
        || url.query().is_some() || url.fragment().is_some() || url.port().is_some()
        || !url.username().is_empty() || url.password().is_some() {
        return None;
    }
    uuid::Uuid::parse_str(url.path().strip_prefix('/')?).ok().map(|id| id.to_string())
}

pub fn receive<R: Runtime>(app: &AppHandle<R>, value: &str) {
    if let Some(id) = meeting_id(value) {
        if let Ok(mut pending) = PENDING.lock() { *pending = Some(id.clone()); }
        let _ = app.emit_to("main", "synth-open-meeting", id);
        crate::tray::focus_main_window(app);
    }
}

#[tauri::command]
pub fn take_meeting_link(window: WebviewWindow) -> Result<Option<String>, String> {
    if window.label() != "main" { return Err("meeting_link_window_denied".into()); }
    PENDING.lock().map(|mut value| value.take()).map_err(|_| "meeting_link_unavailable".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn links_only_select_one_valid_meeting_without_credentials_or_extra_paths() {
        let id = "11111111-1111-4111-8111-111111111111";
        assert_eq!(meeting_id(&format!("synth-voice://meeting/{id}")), Some(id.into()));
        for value in [format!("https://meeting/{id}"), format!("synth-voice://other/{id}"),
            format!("synth-voice://meeting/{id}/notes"), format!("synth-voice://meeting/{id}?admin=1"),
            format!("synth-voice://user@meeting/{id}"), format!("synth-voice://meeting/{id}#notes"),
            "synth-voice://meeting/invalid".into()] {
            assert_eq!(meeting_id(&value), None);
        }
    }
}
