//! One event tells the person what is happening to the capture in progress.
//! The recording continues in every case; the message says what to expect.
use serde_json::json;
use tauri::{AppHandle, Emitter, Runtime};

pub const EVENT: &str = "synth-capture-health";

pub fn message(kind: &str, source: Option<&str>) -> &'static str {
    match (kind, source) {
        ("archive_degraded", _) => "El disco va lento y parte del audio puede no guardarse. La grabación continúa.",
        ("source_stalled", Some("system")) => "Se ha dejado de recibir el audio de la llamada. Tu micrófono se sigue grabando.",
        ("source_stalled", _) => "Se ha dejado de recibir tu micrófono. El audio de la llamada se sigue grabando.",
        ("live_transcription_stopped", _) => "La transcripción en directo se ha detenido. El audio se sigue grabando y se transcribirá al terminar.",
        ("recovered", _) => "La grabación vuelve a funcionar con normalidad.",
        _ => "La grabación continúa con un problema.",
    }
}

pub fn emit<R: Runtime>(app: &AppHandle<R>, kind: &str, source: Option<&str>) {
    log::warn!("capture health: {} {:?}", kind, source);
    let _ = app.emit(
        EVENT,
        json!({"kind": kind, "source": source, "message": message(kind, source)}),
    );
}
