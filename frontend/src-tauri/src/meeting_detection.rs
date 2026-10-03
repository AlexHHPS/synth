//! Metadata-only CoreAudio call hints. This module never starts or stops capture.
use serde::{Deserialize, Serialize};
use std::{collections::{BTreeMap, BTreeSet}, sync::Mutex, time::{SystemTime, UNIX_EPOCH}};
use tauri::{Manager, Emitter};
use tauri_plugin_notification::NotificationExt;

#[derive(Clone, Serialize, Deserialize)]
pub struct Settings { pub enabled: bool, pub excluded_apps: BTreeSet<String> }
impl Default for Settings { fn default() -> Self { Self { enabled: true, excluded_apps: BTreeSet::new() } } }
#[derive(Clone, Serialize)]
pub struct Hint { pub id: String, pub app_id: String, pub label: String }
struct Session { hint: Hint, seen: u64, samples: u32, handled: bool, snoozed_until: u64 }
#[derive(Default)]
pub struct Detector { settings: Settings, sessions: BTreeMap<String, Session>, error: Option<String> }
pub type DetectionState = Mutex<Detector>;
#[derive(Serialize)]
pub struct Status { settings: Settings, available: bool, error: Option<String>, pending: Vec<Hint> }
fn now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }
fn candidate(id: &str, input: bool, output: bool) -> Option<String> {
    if !input || id.is_empty() || id.len() > 256 { return None; }
    let lower = id.to_lowercase();
    if ["ai.synth.", "meetily", "wispr", "dictation", "com.apple.speech", "com.apple.audio"].iter().any(|x| lower.contains(x)) { return None; }
    for (prefix, label) in [("us.zoom.xos", "Zoom"), ("com.microsoft.teams", "Teams"), ("com.microsoft.teams2", "Teams"), ("com.tinyspeck.slackmacgap", "Slack"), ("com.hnc.discord", "Discord"), ("com.apple.facetime", "FaceTime"), ("com.cisco.webex", "Webex")] {
        if lower == prefix || lower.starts_with(&format!("{prefix}.")) { return Some(label.into()); }
    }
    // Browsers and unknown apps need simultaneous input/output. No title/URL inspection.
    if !output { return None; }
    for (prefix, label) in [("com.google.chrome", "Chrome"), ("com.microsoft.edgemac", "Edge"), ("org.mozilla.firefox", "Firefox"), ("com.apple.safari", "Safari"), ("com.apple.webkit", "Navegador"), ("ai.perplexity.comet", "Comet")] {
        if lower == prefix || lower.starts_with(&format!("{prefix}.")) { return Some(label.into()); }
    }
    Some(id.to_owned())
}
impl Detector {
    fn observe(&mut self, apps: Vec<(String, bool, bool)>, t: u64, recording: bool) -> Vec<Hint> {
        self.sessions.retain(|_, s| t.saturating_sub(s.seen) <= 45);
        if !self.settings.enabled { self.sessions.clear(); return vec![]; }
        if recording { for s in self.sessions.values_mut() { s.handled = true; } }
        let mut alerts = vec![];
        for (id, input, output) in apps {
            if self.settings.excluded_apps.contains(&id) { continue; }
            let Some(label) = candidate(&id, input, output) else { continue; };
            let s = self.sessions.entry(id.clone()).or_insert_with(|| Session { hint: Hint { id: uuid::Uuid::new_v4().to_string(), app_id: id, label }, seen: t, samples: 0, handled: false, snoozed_until: 0 });
            s.samples = if t.saturating_sub(s.seen) <= 5 { s.samples.saturating_add(1) } else { 1 };
            s.seen = t;
            if recording { s.handled = true; }
            if !s.handled && s.samples >= 2 && t >= s.snoozed_until {
                // Only one OS alert per session; pending banner remains until an explicit action.
                if s.samples == 2 || s.snoozed_until != 0 { alerts.push(s.hint.clone()); s.snoozed_until = 0; }
            }
        }
        alerts
    }
    fn status(&self, t: u64) -> Status {
        Status { settings: self.settings.clone(), available: self.error.is_none(), error: self.error.clone(), pending: if !self.settings.enabled || self.error.is_some() { vec![] } else { self.sessions.values().filter(|s| !s.handled && s.samples >= 2 && t >= s.snoozed_until && t.saturating_sub(s.seen) <= 5).map(|s| s.hint.clone()).collect() } }
    }
    fn action(&mut self, id: &str, action: &str, t: u64) -> Result<(), String> {
        let s = self.sessions.values_mut().find(|s| s.hint.id == id && t.saturating_sub(s.seen) <= 5).ok_or("La llamada ya no está activa.")?;
        match action { "prepare" | "dismiss" => s.handled = true, "snooze" => s.snoozed_until = t + 60, _ => return Err("Acción no válida.".into()) }
        Ok(())
    }
}
#[tauri::command]
pub fn synth_detection_status(state: tauri::State<'_, DetectionState>) -> Result<Status, String> {
    Ok(state.lock().map_err(|_| "Detector no disponible")?.status(now()))
}
#[tauri::command]
pub fn synth_detection_action(state: tauri::State<'_, DetectionState>, id: String, action: String) -> Result<(), String> {
    state.lock().map_err(|_| "Detector no disponible")?.action(&id, &action, now())
}
#[tauri::command]
pub fn synth_detection_settings(app: tauri::AppHandle, state: tauri::State<'_, DetectionState>, settings: Settings) -> Result<(), String> {
    if settings.excluded_apps.len() > 100 || settings.excluded_apps.iter().any(|s| s.is_empty() || s.len() > 256 || !s.bytes().all(|b| b.is_ascii_alphanumeric() || b".-_".contains(&b))) { return Err("Lista de exclusiones no válida.".into()); }
    let mut detector = state.lock().map_err(|_| "Detector no disponible")?;
    let path = app.path().app_data_dir().map_err(|_| "Directorio no disponible")?.join("meeting_detection.json");
    let temp = path.with_extension("json.tmp");
    std::fs::write(&temp, serde_json::to_vec(&settings).map_err(|_| "No se pueden guardar los ajustes")?).map_err(|_| "No se pueden guardar los ajustes")?;
    std::fs::rename(temp, path).map_err(|_| "No se pueden guardar los ajustes")?;
    detector.settings = settings;
    detector.sessions.clear();
    Ok(())
}
pub fn start(app: tauri::AppHandle) {
    let state = app.state::<DetectionState>();
    if let Ok(path) = app.path().app_data_dir() {
        if let Ok(bytes) = std::fs::read(path.join("meeting_detection.json")) {
            match serde_json::from_slice::<Settings>(&bytes) {
                Ok(settings) => { if let Ok(mut d) = state.lock() { d.settings = settings; } }
                Err(_) => { if let Ok(mut d) = state.lock() { d.settings.enabled = false; d.error = Some("Ajustes del detector inválidos; revisa la configuración.".into()); } }
            }
        }
    }
    tauri::async_runtime::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_secs(2)).await;
            let enabled = app.state::<DetectionState>().lock().map(|d| d.settings.enabled).unwrap_or(false);
            if !enabled { continue; }
            let snapshot = tauri::async_runtime::spawn_blocking(probe).await;
            let recording = crate::is_recording().await || crate::voice_onboarding::active();
            let alerts = {
                let state = app.state::<DetectionState>();
                let Ok(mut d) = state.lock() else { continue; };
                match snapshot {
                    Ok(Ok(apps)) => { d.error = None; d.observe(apps, now(), recording) }
                    _ => { d.error = Some("No se puede consultar la actividad de audio. Puedes iniciar una nota manualmente.".into()); vec![] }
                }
            };
            for hint in alerts {
                let _ = app.emit("synth-possible-call", &hint);
                // Successful submission is not proof of OS notification visibility/permission.
                let _ = app.notification().builder().title("Synth · Posible llamada").body(format!("{} usa el micrófono. Abre Synth para preparar una nota.", hint.label)).show();
            }
        }
    });
}
#[cfg(not(target_os = "macos"))]
fn probe() -> Result<Vec<(String, bool, bool)>, String> { Err("Plataforma no compatible".into()) }
#[cfg(target_os = "macos")]
fn probe() -> Result<Vec<(String, bool, bool)>, String> { mac::probe() }
#[cfg(target_os = "macos")]
mod mac {
    use std::ffi::{c_void, c_char};
    #[repr(C)] struct Address { selector: u32, scope: u32, element: u32 }
    fn addr(s: &[u8; 4]) -> Address { Address { selector: u32::from_be_bytes(*s), scope: u32::from_be_bytes(*b"glob"), element: 0 } }
    #[link(name="CoreAudio", kind="framework")]
    extern "C" {
        fn AudioObjectHasProperty(o: u32, a: *const Address) -> u8;
        fn AudioObjectGetPropertyDataSize(o: u32, a: *const Address, qs: u32, q: *const c_void, size: *mut u32) -> i32;
        fn AudioObjectGetPropertyData(o: u32, a: *const Address, qs: u32, q: *const c_void, size: *mut u32, data: *mut c_void) -> i32;
    }
    #[link(name="CoreFoundation", kind="framework")]
    extern "C" {
        fn CFStringGetCString(s: *const c_void, out: *mut c_char, size: isize, encoding: u32) -> u8;
        fn CFRelease(s: *const c_void);
    }
    unsafe fn scalar<T: Default>(o: u32, s: &[u8;4]) -> Result<T, String> {
        let mut value = T::default(); let mut size = std::mem::size_of::<T>() as u32;
        let code = AudioObjectGetPropertyData(o, &addr(s), 0, std::ptr::null(), &mut size, &mut value as *mut T as *mut c_void);
        if code != 0 || size as usize != std::mem::size_of::<T>() { return Err("CoreAudio property unavailable".into()); }
        Ok(value)
    }
    pub fn probe() -> Result<Vec<(String, bool, bool)>, String> {
        unsafe {
            let a = addr(b"prs#");
            if AudioObjectHasProperty(1, &a) == 0 { return Err("CoreAudio process API requires macOS 14.2+".into()); }
            let mut size = 0;
            if AudioObjectGetPropertyDataSize(1, &a, 0, std::ptr::null(), &mut size) != 0 || size > 16384 || size % 4 != 0 { return Err("Invalid CoreAudio process list".into()); }
            let mut objects = vec![0u32; size as usize / 4];
            if AudioObjectGetPropertyData(1, &a, 0, std::ptr::null(), &mut size, objects.as_mut_ptr() as *mut c_void) != 0 { return Err("CoreAudio list unavailable".into()); }
            objects.truncate(size as usize / 4);
            let mut apps = std::collections::BTreeMap::<String, (bool,bool)>::new();
            for o in objects {
                let Ok(s) = scalar::<*const c_void>(o, b"pbid") else { continue; };
                if s.is_null() { continue; }
                let mut buf = [0u8; 1024];
                let ok = CFStringGetCString(s, buf.as_mut_ptr() as *mut c_char, 1024, 0x08000100);
                CFRelease(s);
                if ok == 0 { continue; }
                let end = buf.iter().position(|b| *b == 0).unwrap_or(buf.len());
                let Ok(id) = std::str::from_utf8(&buf[..end]) else { continue; };
                let input = scalar::<u32>(o, b"piri").unwrap_or(0) != 0;
                let output = scalar::<u32>(o, b"piro").unwrap_or(0) != 0;
                let id = id.split(".helper").next().unwrap_or(id).to_owned();
                let pair = apps.entry(id).or_default(); pair.0 |= input; pair.1 |= output;
            }
            Ok(apps.into_iter().map(|(id,(i,o))| (id,i,o)).collect())
        }
    }
}
#[cfg(test)] mod tests {
    use super::*;
    fn zoom() -> Vec<(String,bool,bool)> { vec![("us.zoom.xos".into(),true,false)] }
    #[test] fn debounce_dismiss_reconnect_and_expiry() {
        let mut d = Detector::default(); assert!(d.observe(zoom(),100,false).is_empty());
        assert_eq!(d.observe(zoom(),102,false).len(),1); assert!(d.observe(zoom(),104,false).is_empty());
        let id=d.status(104).pending[0].id.clone(); d.action(&id,"dismiss",104).unwrap();
        d.observe(vec![],110,false); assert!(d.observe(zoom(),120,false).is_empty()); assert!(d.status(120).pending.is_empty());
        d.observe(vec![],170,false); assert!(d.action(&id,"prepare",170).is_err());
        d.observe(zoom(),172,false); assert_eq!(d.observe(zoom(),174,false).len(),1);
    }
    #[test] fn snooze_requires_still_active_call() { let mut d=Detector::default(); d.observe(zoom(),100,false); d.observe(zoom(),102,false); let id=d.status(102).pending[0].id.clone(); d.action(&id,"snooze",102).unwrap(); assert!(d.status(104).pending.is_empty()); for t in (104..162).step_by(2) { assert!(d.observe(zoom(),t,false).is_empty()); } assert_eq!(d.observe(zoom(),162,false).len(),1); }
    #[test] fn active_recording_and_disabled_never_prompt() { let mut d=Detector::default(); d.observe(zoom(),100,true); assert!(d.observe(zoom(),102,false).is_empty()); assert!(d.status(102).pending.is_empty()); d.settings.enabled=false; d.observe(zoom(),200,false); assert!(d.status(200).pending.is_empty()); }
    #[test] fn exclusions_and_non_call_input() { let mut d=Detector::default(); d.settings.excluded_apps.insert("us.zoom.xos".into()); d.observe(zoom(),100,false); d.observe(zoom(),102,false); assert!(d.status(102).pending.is_empty()); assert!(candidate("com.open-wispr.app",true,true).is_none()); assert!(candidate("com.google.Chrome",true,false).is_none()); assert!(candidate("com.microsoft.teams2",true,false).is_some()); assert!(candidate("org.unknown.call",true,true).is_some()); }
    #[test] fn sleep_gap_requires_fresh_debounce() { let mut d=Detector::default(); d.observe(zoom(),100,false); assert!(d.observe(zoom(),120,false).is_empty()); assert_eq!(d.observe(zoom(),122,false).len(),1); }
}
