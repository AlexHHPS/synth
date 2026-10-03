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
    folder_id: Option<String>, consent_confirmed: bool, notes: Option<String>) -> Result<Value, String> {
    if !consent_confirmed { return Err("Confirma el consentimiento antes de procesar la reunión.".into()); }
    let root = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?.join("capture/raw");
    let mut body = tokio::task::spawn_blocking(move || capture_receipt(&root, Path::new(&folder_path)))
        .await.map_err(|_| "No se pudo validar la captura.")??;
    body["title"] = Value::String(title);
    body["folder_id"] = folder_id.map(Value::String).unwrap_or(Value::Null);
    body["consent_confirmed"] = Value::Bool(true);
    if let Some(notes) = notes { body["notes"] = Value::String(notes); }
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
    if parts.len() == 2 {
        return match (method, parts[1]) {
            ("GET", "me" | "people" | "library" | "folders" | "search" | "integrations") => true,
            ("POST", "folders" | "meetings" | "integrations") => true,
            _ => false,
        };
    }
    if parts.len() < 3 || uuid::Uuid::parse_str(parts[2]).is_err() {
        return false;
    }
    match parts[1] {
        "integrations" => method == "DELETE" && parts.len() == 3,
        "meetings" => match (method, parts.get(3).copied(), parts.len()) {
            ("GET", None, 3) => true,
            ("GET", Some("transcript" | "document"), 4) => true,
            ("PUT", Some("notes" | "folder"), 4) => true,
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
        ("GET", "/v1/auth/state") | ("POST", "/v1/auth/login" | "/v1/auth/logout") => return true,
        ("GET", "/v1/status" | "/v1/tasks" | "/v1/voice") | ("POST", "/v1/captures" | "/v1/voice/enroll" | "/v1/voice/test" | "/v1/voice/delete" | "/v1/voice/share") => return true,
        _ => {},
    }
    if method == "POST" && path.starts_with("/v1/voice/meetings/") && path.ends_with("/analyze") {
        let id=path.trim_start_matches("/v1/voice/meetings/").trim_end_matches("/analyze");
        return uuid::Uuid::parse_str(id).is_ok();
    }
    if method == "GET" && path.starts_with("/v1/voice/meetings/") { return uuid::Uuid::parse_str(path.trim_start_matches("/v1/voice/meetings/")).is_ok(); }
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

async fn request_local(app: AppHandle, method: String, path: String, body: Option<Value>, port: u16) -> Result<Value, String> {
    let encoded = body.as_ref().map(|v| v.to_string()).unwrap_or_default();
    if encoded.len() > 1024 * 1024 {
        return Err("El contenido supera el límite de esta operación.".into());
    }
    let directory = app.path().app_data_dir().map_err(|_| "No se encuentra la configuración local.")?;
    let token = std::fs::read_to_string(directory.join("desktop-api-key"))
        .map_err(|_| "Falta configurar el acceso de esta app al servicio local de Synth.")?;
    let token = token.trim();
    if token.is_empty() {
        return Err("La clave de acceso local está vacía.".into());
    }
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build().map_err(|_| "No se pudo iniciar la conexión local.")?;
    let verb = reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "Operación no válida.")?;
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
    let mut request = client.request(verb, format!("{}{}", base, path)).bearer_auth(cloud_token.as_deref().unwrap_or(token));
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

#[cfg(test)]
mod tests {
    use super::{permitted, permitted_host, playback_path, capture_receipt, backend_url};
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
        assert!(permitted_host("POST", "/v1/captures"));
        assert!(permitted_host("GET", "/v1/status"));
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
