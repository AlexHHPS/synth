//! Narrow local API bridge. The desktop credential never enters JavaScript.
use serde_json::Value;
use std::time::Duration;
use tauri::{AppHandle, Manager};
use std::path::{Path, PathBuf};
use std::io::Read;
use sha2::{Digest, Sha256};

pub fn capture_receipt(root: &Path, requested_folder: &Path) -> Result<Value, String> {
    let directory = root.canonicalize().map_err(|_| "No se encuentra el audio local.")?;
    let folder = requested_folder.canonicalize().map_err(|_| "No se encuentra la captura.")?;
    if !folder.starts_with(&directory) { return Err("La captura no pertenece a esta app.".into()); }
    let receipt_path = folder.join("sources/capture.json").canonicalize().map_err(|_| "El audio aún no está finalizado.")?;
    if !receipt_path.starts_with(&folder) { return Err("El comprobante de captura no es válido.".into()); }
    let mut bytes = Vec::new();
    std::fs::File::open(&receipt_path).map_err(|_| "No se pudo leer la captura.")?
        .take(131073).read_to_end(&mut bytes).map_err(|_| "No se pudo leer la captura.")?;
    if bytes.len() > 131072 { return Err("El comprobante de captura supera el límite.".into()); }
    let receipt: Value = serde_json::from_slice(&bytes).map_err(|_| "El comprobante de captura no es válido.")?;
    if receipt["state"] != "closed" || receipt["version"] != 1
        || receipt["sample_format"] != "pcm16"
        || uuid::Uuid::parse_str(receipt["capture_id"].as_str().unwrap_or("")).is_err() {
        return Err("La captura no tiene un cierre válido.".into());
    }
    let descriptors = receipt["sources"].as_object().ok_or("La captura no contiene fuentes de audio.")?;
    if descriptors.is_empty() || descriptors.len() > 2 { return Err("Las fuentes de audio no son válidas.".into()); }
    let mut sources = serde_json::Map::new();
    for (kind, descriptor) in descriptors {
        if !matches!(kind.as_str(), "microphone" | "system") { return Err("La fuente de audio no es válida.".into()); }
        let path = playback_path(&folder, Path::new(descriptor["path"].as_str().ok_or("Falta la ruta de audio.")?))?;
        let mut file = std::fs::File::open(&path).map_err(|_| "No se encuentra el audio finalizado.")?;
        if descriptor["sample_rate"] != 48000 || descriptor["frames"].as_u64().unwrap_or(0) == 0
            || descriptor["bytes"].as_u64() != Some(file.metadata().map_err(|_| "No se pudo validar el audio.")?.len()) {
            return Err("El audio no coincide con su captura.".into());
        }
        let mut hash = Sha256::new();
        let mut buffer = [0u8;65536];
        loop {
            let n = file.read(&mut buffer).map_err(|_| "No se pudo validar el audio.")?;
            if n == 0 { break; }
            hash.update(&buffer[..n]);
        }
        if descriptor["sha256"].as_str() != Some(format!("{:x}",hash.finalize()).as_str()) {
            return Err("El audio cambió después de cerrar la captura.".into());
        }
        sources.insert(kind.clone(), Value::String(path.to_string_lossy().to_string()));
    }
    Ok(serde_json::json!({"sources":sources,"capture_id":receipt["capture_id"]}))
}

#[tauri::command]
pub async fn synth_enqueue_capture(app: AppHandle, folder_path: String, title: String,
    folder_id: Option<String>, consent_confirmed: bool, notes: Option<String>, calendar_event_id: Option<String>) -> Result<Value, String> {
    if !consent_confirmed { return Err("Confirma el consentimiento antes de procesar la reunión.".into()); }
    let root = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?.join("capture/raw");
    assert_capture_owner(&app, Path::new(&folder_path)).await?;
    let mut body = tokio::task::spawn_blocking(move || capture_receipt(&root, Path::new(&folder_path)))
        .await.map_err(|_| "No se pudo validar la captura.")??;
    body["title"] = Value::String(title);
    body["folder_id"] = folder_id.map(Value::String).unwrap_or(Value::Null);
    body["consent_confirmed"] = Value::Bool(true);
    if let Some(notes) = notes { body["notes"] = Value::String(notes); }
    if let Some(id) = calendar_event_id {
        if uuid::Uuid::parse_str(&id).is_err() { return Err("El evento no es válido.".into()); }
        body["calendar_event_id"] = Value::String(id);
    }
    request_local(app, "POST".into(), "/v1/captures".into(), Some(body), 18383).await
}

/// Legacy playback must not become an arbitrary native file read.
pub fn playback_path(root: &Path, requested: &Path) -> Result<PathBuf, String> {
    let directory = root.canonicalize().map_err(|_| "No se encuentra la carpeta de audio local.")?;
    let path = requested.canonicalize().map_err(|_| "No se encuentra la grabación.")?;
    let extension = path.extension().and_then(|v| v.to_str()).unwrap_or("").to_ascii_lowercase();
    if !path.starts_with(&directory) || !path.is_file()
        || !matches!(extension.as_str(), "wav" | "mp3" | "m4a" | "aac" | "flac" | "ogg" | "opus" | "aiff" | "aif" | "mp4" | "webm") {
        return Err("Solo se puede reproducir audio guardado por esta app.".into());
    }
    Ok(path)
}

fn permitted(method: &str, path: &str) -> bool {
    let resource = path.split('?').next().unwrap_or("");
    // Query values may be encoded; resource names must be literal and canonical.
    if !path.starts_with("/v1/") || resource.contains('%') || resource.contains("..")
        || path.contains('#') || path.contains('\\') || path.chars().any(char::is_control) {
        return false;
    }
    let parts: Vec<_> = resource.trim_start_matches('/').split('/').collect();
    if resource == "/v1/calendar" { return method == "GET"; }
    if resource == "/v1/calendar/sync" { return method == "POST"; }
    if resource == "/v1/calendar/connection" { return method == "DELETE"; }
    if parts.len() == 5 && parts[1] == "calendar" && parts[2] == "events" {
        return uuid::Uuid::parse_str(parts[3]).is_ok()
            && ((method == "GET" && parts[4] == "preparation") || (method == "PUT" && parts[4] == "folder"));
    }
    if parts.len() == 2 {
        return match (method, parts[1]) {
            ("GET", "me" | "people" | "library" | "folders" | "search" | "integrations" | "glossary" | "tasks") => true,
            ("POST", "folders" | "meetings" | "integrations" | "glossary" | "questions") => true,
            _ => false,
        };
    }
    if parts.len() == 3 && method == "POST" && parts[1] == "glossary" && parts[2] == "candidates" {
        return true;
    }
    if parts.len() < 3 || uuid::Uuid::parse_str(parts[2]).is_err() {
        return false;
    }
    match parts[1] {
        "integrations" => method == "DELETE" && parts.len() == 3,
        "meetings" => match (method, parts.get(3).copied(), parts.len()) {
            ("GET", None, 3) => true,
            ("GET", Some("transcript" | "document" | "minutes" | "tasks" | "decisions" | "follow-up-email" | "generation"), 4) => true,
            ("GET", Some("generations"), 6) => uuid::Uuid::parse_str(parts[4]).is_ok() && parts[5] == "events",
            ("PUT", Some("notes" | "folder"), 4) => true,
            ("POST", Some("regenerate"), 4) => true,
            // Undo a glossary correction, or accept/reject a suggestion: /v1/meetings/{id}/corrections/c0001/undo
            ("POST", Some("corrections"), 6) => matches!(parts[5], "undo" | "accept" | "reject") && parts[4].len() == 5 && parts[4].starts_with('c')
                && parts[4][1..].chars().all(|c| c.is_ascii_digit()),
            _ => false,
        },
        "tasks" | "decisions" => method == "PATCH" && parts.len() == 3,
        "questions" => method == "GET" && parts.len() == 3,
        "glossary" => match (method, parts.get(3).copied(), parts.len()) {
            ("PATCH", None, 3) => true,
            ("POST", Some("approve" | "discard" | "merge"), 4) => true,
            _ => false,
        },
        "folders" => match (method, parts.get(3).copied(), parts.len()) {
            ("PATCH" | "DELETE", None, 3) => true,
            ("GET" | "PUT", Some("members"), 4) => true,
            ("DELETE", Some("members"), 5) => uuid::Uuid::parse_str(parts[4]).is_ok(),
            _ => false,
        },
        "jobs" => match (method, parts.get(3).copied(), parts.len()) {
            ("GET", None, 3) => true,
            ("POST", Some("retry" | "cancel"), 4) => true,
            _ => false,
        },
        _ => false,
    }
}

#[tauri::command]
pub async fn synth_desktop_request(
    app: AppHandle,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, String> {
    if path.len() > 4000 || !permitted(&method, &path) {
        return Err("Esta operación no está disponible en Synth.".into());
    }
    request_local(app, method, path, body, 18280).await
}

fn permitted_host(method: &str, path: &str) -> bool {
    if path.contains('%') || path.contains('?') || path.contains('#') || path.contains('\\')
        || path.chars().any(char::is_control) {
        return false;
    }
    match (method, path) {
        ("POST", "/v1/audio/setup/retry") => return true,
        ("POST", "/v1/capture/context") => return true,
        ("GET", "/v1/auth/state") | ("POST", "/v1/auth/login" | "/v1/auth/logout" | "/v1/auth/calendar") => return true,
        ("GET", "/v1/status" | "/v1/tasks" | "/v1/voice") | ("POST", "/v1/captures" | "/v1/voice/enroll" | "/v1/voice/test" | "/v1/voice/delete" | "/v1/voice/share") => return true,
        _ => {},
    }
    if (method=="GET" && path=="/v1/voice/learning") || (method=="POST" && matches!(path,"/v1/voice/learning/settings"|"/v1/voice/learning/sync")) { return true; }
    if path.starts_with("/v1/voice/samples/") {
        let parts:Vec<_>=path.split('/').collect();
        return parts.get(4).is_some_and(|id|uuid::Uuid::parse_str(id).is_ok()) &&
            ((method=="POST" && parts.len()==6 && parts[5]=="accept") || (method=="DELETE" && parts.len()==5));
    }
    if method == "POST" && path.starts_with("/v1/voice/meetings/") && path.ends_with("/analyze") {
        let id=path.trim_start_matches("/v1/voice/meetings/").trim_end_matches("/analyze");
        return uuid::Uuid::parse_str(id).is_ok();
    }
    if method == "POST" && path.starts_with("/v1/voice/meetings/") && path.ends_with("/speakers") {
        let id=path.trim_start_matches("/v1/voice/meetings/").trim_end_matches("/speakers");
        return uuid::Uuid::parse_str(id).is_ok();
    }
    if method == "GET" && path.starts_with("/v1/voice/meetings/") { return uuid::Uuid::parse_str(path.trim_start_matches("/v1/voice/meetings/")).is_ok(); }
    if method == "GET" && path.starts_with("/v1/voice/captures/") { return uuid::Uuid::parse_str(path.trim_start_matches("/v1/voice/captures/")).is_ok(); }
    if method == "POST" && path.starts_with("/v1/voice/captures/") && path.ends_with("/speakers") {
        return uuid::Uuid::parse_str(path.trim_start_matches("/v1/voice/captures/").trim_end_matches("/speakers")).is_ok();
    }
    if method == "GET" && path.starts_with("/v1/voice/jobs/") { return uuid::Uuid::parse_str(path.trim_start_matches("/v1/voice/jobs/")).is_ok(); }
    let parts: Vec<_> = path.split('/').collect();
    if parts.len() < 4 || parts[0] != "" || parts[1] != "v1" || parts[2] != "tasks"
        || uuid::Uuid::parse_str(parts[3]).is_err() {
        return false;
    }
    (method == "GET" && parts.len() == 4)
        || (method == "POST" && parts.len() == 5 && matches!(parts[4], "retry" | "cancel"))
}

#[tauri::command]
pub async fn synth_pipeline_request(
    app: AppHandle,
    method: String,
    path: String,
    body: Option<Value>,
) -> Result<Value, String> {
    if path.len() > 4000 || !permitted_host(&method, &path) {
        return Err("Esta operación de audio no está disponible en Synth.".into());
    }
    if method=="POST" && path=="/v1/auth/logout" && crate::audio::recording_commands::is_recording().await {
        let _=crate::audio::recording_commands::stop_recording(app.clone(),crate::audio::recording_commands::RecordingArgs {save_path:String::new()}).await;
    }
    request_local(app, method, path, body, 18383).await
}

fn backend_url(config: Option<&Value>) -> Result<&str, String> {
    let approved = option_env!("SYNTH_API_URL").unwrap_or("http://127.0.0.1:18280");
    let url = config.and_then(|v| v.get("api_url")).and_then(Value::as_str).unwrap_or(approved);
    if url != approved { return Err("El servidor no coincide con el despliegue autorizado.".into()); }
    let parsed = reqwest::Url::parse(url).map_err(|_| "La URL del servidor no es válida.")?;
    if !parsed.username().is_empty() || parsed.password().is_some() || parsed.query().is_some()
        || parsed.fragment().is_some() || parsed.path() != "/"
        || (parsed.scheme() != "https" && url != "http://127.0.0.1:18280") {
        return Err("El servidor requiere un origen HTTPS autorizado.".into());
    }
    Ok(url)
}

async fn request_local<R: tauri::Runtime>(app: AppHandle<R>, method: String, path: String, body: Option<Value>, port: u16) -> Result<Value, String> {
    let encoded = body.as_ref().map(|v| v.to_string()).unwrap_or_default();
    if encoded.len() > 1024 * 1024 {
        return Err("El contenido supera el límite de esta operación.".into());
    }
    let (client, base, token) = product_connection(&app, port, Duration::from_secs(20)).await?;
    let verb = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "Operación no válida.")?;
    let mut request = client.request(verb, format!("{}{}", base, path)).bearer_auth(token);
    if let Some(value) = body { request = request.json(&value); }
    let mut response = request.send().await.map_err(|_| "El servicio local no responde. Revisa su estado y vuelve a intentarlo.")?;
    let status = response.status();
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| "La respuesta del servicio está incompleta.")? {
        if bytes.len() + chunk.len() > 16 * 1024 * 1024 {
            return Err("La respuesta supera el límite de lectura.".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| "El servicio no devolvió una respuesta válida.")?;
    if !status.is_success() {
        return Err(value.get("detail").and_then(Value::as_str).unwrap_or("No se pudo completar la operación.").to_string());
    }
    Ok(value)
}

/// Native-owned authentication shared by ordinary requests and draft streaming.
pub(crate) async fn product_connection<R: tauri::Runtime>(app: &AppHandle<R>, port: u16, timeout: Duration) -> Result<(reqwest::Client, String, String), String> {
    let directory = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?;
    let token = std::fs::read_to_string(directory.join("desktop-api-key"))
        .map_err(|_| "Falta configurar el acceso de esta app al servicio local de Synth.")?;
    let token = token.trim();
    if token.is_empty() {
        return Err("La clave de acceso local está vacía.".into());
    }
    let client = reqwest::Client::builder()
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build().map_err(|_| "No se pudo iniciar la conexión local.")?;
    let config = if port == 18280 {
        let config_path = directory.join("backend-config.json");
        if config_path.exists() {
            Some(serde_json::from_slice::<Value>(&std::fs::read(config_path).map_err(|_| "No se pudo leer el servidor configurado.")?)
                .map_err(|_| "La configuración del servidor no es válida.")?)
        } else { None }
    } else { None };
    let base = if port == 18280 { backend_url(config.as_ref())?.to_owned() } else { format!("http://127.0.0.1:{}", port) };
    let cloud_token = if port == 18280 && config.as_ref().and_then(|v|v.get("auth_mode")).and_then(Value::as_str)==Some("supabase") {
        let response=client.get("http://127.0.0.1:18383/v1/auth/access").bearer_auth(token).send().await
            .map_err(|_|"No se pudo acceder a tu sesión. Vuelve a iniciar sesión.")?;
        if !response.status().is_success() { return Err("Inicia sesión con tu cuenta de Synth.".into()); }
        let session:Value=response.json().await.map_err(|_|"No se pudo leer tu sesión.")?;
        Some(session.get("access_token").and_then(Value::as_str).ok_or("Inicia sesión con tu cuenta de Synth.")?.to_owned())
    } else { None };
    Ok((client, base, cloud_token.unwrap_or_else(|| token.to_owned())))
}

fn capture_identity_value(state: &Value, key: &str) -> Result<String, String> {
    if state["signed_in"] != true { return Err("Inicia sesión para continuar con esta captura.".into()); }
    match state["mode"].as_str() {
        Some("supabase") => uuid::Uuid::parse_str(state["principal_id"].as_str().unwrap_or(""))
            .map(|id| id.to_string()).map_err(|_| "No se pudo verificar quién está grabando.".into()),
        Some("keys") if !key.trim().is_empty() => Ok(format!("keys:{:x}",Sha256::digest(key.trim().as_bytes()))),
        _ => Err("No se pudo verificar la sesión de esta captura.".into()),
    }
}

pub async fn capture_identity<R: tauri::Runtime>(app: &AppHandle<R>) -> Result<String, String> {
    let state = request_local(app.clone(), "GET".into(), "/v1/auth/capture-identity".into(), None, 18383).await?;
    let directory = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?;
    let key = std::fs::read_to_string(directory.join("desktop-api-key")).map_err(|_| "No se pudo verificar la sesión local.")?;
    capture_identity_value(&state, &key)
}

pub(crate) async fn assert_capture_owner<R: tauri::Runtime>(app: &AppHandle<R>, folder: &Path) -> Result<(), String> {
    let root = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?.join("capture/raw")
        .canonicalize().map_err(|_| "No se encuentra la carpeta de captura.")?;
    let folder = folder.canonicalize().map_err(|_| "No se encuentra la captura.")?;
    if !folder.starts_with(root) { return Err("La captura no pertenece a esta app.".into()); }
    // 0.4.4 archives had no live watermark. New captures always bind an owner.
    if !folder.join("sources/progress.json").exists() { return Ok(()); }
    let path = folder.join("capture-owner.json");
    if path.symlink_metadata().map_err(|_| "No se pudo verificar el propietario de la captura.")?.file_type().is_symlink() {
        return Err("El propietario de la captura no es válido.".into());
    }
    let mut bytes = Vec::new();
    std::fs::File::open(path).map_err(|_| "No se pudo verificar el propietario de la captura.")?
        .take(4097).read_to_end(&mut bytes).map_err(|_| "No se pudo verificar el propietario de la captura.")?;
    if bytes.len()>4096 { return Err("El propietario de la captura no es válido.".into()); }
    let binding:Value=serde_json::from_slice(&bytes).map_err(|_| "El propietario de la captura no es válido.")?;
    if binding["identity"].as_str()!=Some(capture_identity(app).await?.as_str()) {
        return Err("Esta captura pertenece a otra sesión.".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{permitted, permitted_host, playback_path, capture_receipt, backend_url};
    #[test]
    fn calendar_bridge_exposes_metadata_but_never_provider_credentials() {
        let id="11111111-1111-4111-8111-111111111111";
        assert!(permitted("GET", "/v1/calendar"));
        assert!(permitted("POST", "/v1/calendar/sync"));
        assert!(permitted("DELETE", "/v1/calendar/connection"));
        assert!(permitted("GET", &format!("/v1/calendar/events/{id}/preparation")));
        assert!(permitted("PUT", &format!("/v1/calendar/events/{id}/folder")));
        assert!(!permitted("POST", "/v1/calendar/connection"));
        assert!(!permitted("GET", "/v1/calendar/events/not-an-id/preparation"));
        assert!(permitted_host("POST", "/v1/auth/calendar"));
        assert!(permitted_host("POST", "/v1/capture/context"));
        assert!(!permitted_host("GET", "/v1/calendar/connection"));
    }
    #[test]
    fn capture_identity_uses_the_authenticated_principal_and_fences_keys_and_logout() {
        let id="11111111-1111-4111-8111-111111111111";
        assert_eq!(super::capture_identity_value(&serde_json::json!({"mode":"supabase","signed_in":true,"principal_id":id}),"unused").unwrap(),id);
        assert!(super::capture_identity_value(&serde_json::json!({"mode":"supabase","signed_in":false,"principal_id":id}),"unused").is_err());
        assert!(super::capture_identity_value(&serde_json::json!({"mode":"supabase","signed_in":true,"email":"example.invalid"}),"unused").is_err());
        let a=super::capture_identity_value(&serde_json::json!({"mode":"keys","signed_in":true}),"test-key-a").unwrap();
        let b=super::capture_identity_value(&serde_json::json!({"mode":"keys","signed_in":true}),"test-key-b").unwrap();
        assert_ne!(a,b); assert!(!a.contains("test-key"));
    }
    #[test]
    fn remote_credentials_only_go_to_the_authorized_https_backend() {
        assert_eq!(backend_url(None).unwrap(), option_env!("SYNTH_API_URL").unwrap_or("http://127.0.0.1:18280"));
        let approved = option_env!("SYNTH_API_URL").unwrap_or("http://127.0.0.1:18280");
        assert!(backend_url(Some(&serde_json::json!({"api_url":approved}))).is_ok());
        if approved != "http://127.0.0.1:18280" {
            assert!(backend_url(Some(&serde_json::json!({"api_url":"http://127.0.0.1:18280"}))).is_err());
        }
        for url in ["http://voice.example.com", "https://evil.invalid", "https://voice.example.com.evil.invalid", "https://voice.example.com@evil.invalid"] {
            assert!(backend_url(Some(&serde_json::json!({"api_url":url}))).is_err());
        }
    }
    #[test]
    fn captured_sources_must_match_the_closed_owned_receipt() {
        use crate::audio::source_archive::SourceArchive;
        use crate::audio::recording_state::{AudioChunk, DeviceType};
        let root = tempfile::tempdir().unwrap();
        let folder = root.path().join("meeting");
        std::fs::create_dir(&folder).unwrap();
        let (tap, archive) = SourceArchive::start(&folder, true, false).unwrap();
        tap.push(&AudioChunk { data:vec![0.25;480],sample_rate:48000,timestamp:0.01,chunk_id:0,device_type:DeviceType::Microphone }).unwrap();
        assert!(capture_receipt(root.path(), &folder).is_err());
        drop(tap); archive.close().unwrap();
        let receipt = capture_receipt(root.path(), &folder).unwrap();
        assert!(receipt["sources"]["microphone"].as_str().unwrap().ends_with("microphone.wav"));
        let foreign = tempfile::tempdir().unwrap();
        assert!(capture_receipt(foreign.path(), &folder).is_err());
        std::fs::write(folder.join("sources/microphone.wav"), b"changed").unwrap();
        assert!(capture_receipt(root.path(), &folder).is_err());
    }
    #[test]
    fn acoustic_bridge_only_exposes_task_control_on_loopback() {
        assert!(permitted_host("POST", "/v1/audio/setup/retry"));
        assert!(permitted_host("POST", "/v1/captures"));
        assert!(permitted_host("GET", "/v1/status"));
        assert!(permitted_host("GET", "/v1/voice/learning"));
        assert!(permitted_host("POST", "/v1/voice/learning/settings"));
        assert!(permitted_host("POST", "/v1/voice/learning/sync"));
        assert!(permitted_host("POST", "/v1/voice/samples/00000000-0000-4000-8000-000000000001/accept"));
        assert!(permitted_host("DELETE", "/v1/voice/samples/00000000-0000-4000-8000-000000000001"));
        assert!(!permitted_host("POST", "/v1/voice/samples"));
        assert!(!permitted_host("POST", "/v1/voice/samples/not-a-uuid/accept"));
        assert!(!permitted_host("GET", "/v1/voice/samples/00000000-0000-4000-8000-000000000001"));
        assert!(permitted_host("GET", "/v1/voice/captures/00000000-0000-4000-8000-000000000001"));
        assert!(!permitted_host("GET", "/v1/voice/captures/not-a-uuid"));
        assert!(!permitted_host("POST", "/v1/voice/captures/00000000-0000-4000-8000-000000000001"));
        assert!(permitted_host("POST", "/v1/voice/captures/00000000-0000-4000-8000-000000000001/speakers"));
        assert!(!permitted_host("POST", "/v1/voice/captures/not-a-uuid/speakers"));
        assert!(permitted_host("GET", "/v1/auth/state"));
        assert!(permitted_host("POST", "/v1/auth/login"));
        assert!(!permitted_host("GET", "/v1/auth/access"));
        assert!(permitted_host("POST", "/v1/tasks/00000000-0000-4000-8000-000000000001/retry"));
        for path in ["https://evil.invalid/v1/tasks", "//evil.invalid/v1/tasks", "/v1/audio", "/v1/keys", "/v1/tasks/not-a-uuid", "/v1/tasks?path=/etc/hosts", "/v1/../tasks"] {
            assert!(!permitted_host("GET", path));
        }
    }
    #[test]
    fn bridge_never_routes_credentials_outside_the_product_api() {
        assert!(permitted("GET", "/v1/search?q=decisi%C3%B3n"));
        assert!(permitted("POST", "/v1/integrations"));
        assert!(permitted("GET", "/v1/integrations"));
        assert!(permitted("DELETE", "/v1/integrations/00000000-0000-4000-8000-000000000001"));
        assert!(!permitted("GET", "/v1/integrations/00000000-0000-4000-8000-000000000001/token"));
        assert!(permitted("PUT", "/v1/meetings/00000000-0000-4000-8000-000000000001/notes"));
        for path in ["https://evil.invalid/v1/library", "//evil.invalid/v1/library", "/v1/../keys", "/v1/%2e%2e/keys", "/v1/keys", "/v1/principals", "/v1/audio"] {
            assert!(!permitted("GET", path));
        }
        assert!(!permitted("POST", "/v1/keys"));
        assert!(!permitted("GET", "/v1/meetings/not-a-uuid/document"));
    }

    #[test]
    fn v2_routes_for_minutes_tasks_glossary_and_speakers_are_bridged() {
        let id = "00000000-0000-4000-8000-000000000001";
        for (method, path) in [("GET", format!("/v1/meetings/{id}/minutes")), ("GET", format!("/v1/meetings/{id}/tasks")),
                               ("GET", format!("/v1/meetings/{id}/decisions")), ("POST", format!("/v1/meetings/{id}/regenerate")),
                               ("POST", format!("/v1/meetings/{id}/corrections/c0001/undo")), ("GET", "/v1/tasks".into()),
                               ("POST", format!("/v1/meetings/{id}/corrections/c0001/accept")),
                               ("POST", format!("/v1/meetings/{id}/corrections/c0001/reject")),
                               ("PATCH", format!("/v1/tasks/{id}")), ("PATCH", format!("/v1/decisions/{id}")),
                               ("GET", "/v1/glossary?status=aprobado".into()), ("POST", "/v1/glossary".into()),
                               ("POST", "/v1/glossary/candidates".into()), ("PATCH", format!("/v1/glossary/{id}")),
                               ("POST", format!("/v1/glossary/{id}/approve")), ("POST", format!("/v1/glossary/{id}/merge"))] {
            assert!(permitted(method, &path), "{method} {path}");
        }
        for (method, path) in [("POST", format!("/v1/meetings/{id}/corrections/../undo")), ("POST", format!("/v1/meetings/{id}/corrections/c01/undo")),
                               ("POST", format!("/v1/meetings/{id}/corrections/c0001/delete")), ("GET", format!("/v1/meetings/{id}/corrections/c0001/accept")),
                               ("DELETE", format!("/v1/glossary/{id}")), ("POST", format!("/v1/glossary/{id}/delete")),
                               ("DELETE", format!("/v1/tasks/{id}")), ("POST", format!("/v1/meetings/{id}/speakers")),
                               ("POST", "/v1/glossary/not-a-uuid/approve".into())] {
            assert!(!permitted(method, &path), "{method} {path}");
        }
        assert!(permitted_host("POST", &format!("/v1/voice/meetings/{id}/speakers")));
        assert!(!permitted_host("POST", "/v1/voice/meetings/not-a-uuid/speakers"));
    }

    #[test]
    fn questions_and_the_email_draft_are_bridged_but_nothing_sends() {
        let id = "00000000-0000-4000-8000-000000000001";
        for (method, path) in [("POST", "/v1/questions".to_string()), ("GET", format!("/v1/questions/{id}")),
                               ("GET", format!("/v1/meetings/{id}/follow-up-email"))] {
            assert!(permitted(method, &path), "{method} {path}");
        }
        for (method, path) in [("GET", "/v1/questions".to_string()), ("DELETE", format!("/v1/questions/{id}")),
                               ("POST", format!("/v1/meetings/{id}/follow-up-email")), ("POST", format!("/v1/meetings/{id}/send")),
                               ("GET", "/v1/questions/not-a-uuid".to_string())] {
            assert!(!permitted(method, &path), "{method} {path}");
        }
    }

    #[test]
    fn draft_routes_are_read_only_and_require_canonical_meeting_and_generation_ids() {
        let id = "00000000-0000-4000-8000-000000000001";
        assert!(permitted("GET", &format!("/v1/meetings/{id}/generation")));
        assert!(permitted("GET", &format!("/v1/meetings/{id}/generations/{id}/events?after=2")));
        for (method, path) in [
            ("POST", format!("/v1/meetings/{id}/generation")),
            ("GET", format!("/v1/meetings/{id}/generations/not-a-uuid/events")),
            ("GET", format!("/v1/meetings/{id}/generations/{id}/stream")),
            ("GET", format!("/v1/meetings/{id}/generations/{id}/responses")),
        ] { assert!(!permitted(method, &path), "{method} {path}"); }
    }

    #[test]
    fn legacy_playback_cannot_read_the_desktop_credential_or_follow_a_symlink() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("capture");
        std::fs::create_dir(&root).unwrap();
        let secret = directory.path().join("desktop-api-key");
        std::fs::write(&secret, "test-credential").unwrap();
        let recording = root.join("recording.wav");
        std::fs::write(&recording, b"RIFF").unwrap();
        assert!(playback_path(&root, &recording).is_ok());
        assert!(playback_path(&root, &secret).is_err());
        let disallowed = root.join("private.txt");
        std::fs::write(&disallowed, "private").unwrap();
        assert!(playback_path(&root, &disallowed).is_err());
        #[cfg(unix)] {
            let alias = root.join("credential.wav");
            std::os::unix::fs::symlink(&secret, &alias).unwrap();
            assert!(playback_path(&root, &alias).is_err());
        }
    }
}
