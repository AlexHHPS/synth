use crate::synth_desktop::{
    assert_capture_owner, capture_identity, product_connection, request_local,
};
use serde_json::{json, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;
use uuid::Uuid;

const VIDEOS: &[&str] = &["mp4", "mov", "m4v", "webm", "mkv"];
const ATTACHMENTS: &[&str] = &["pdf", "png", "jpg", "jpeg", "txt", "md", "csv"];
const VIDEO_LIMIT: u64 = 2 * 1024 * 1024 * 1024;
const ATTACHMENT_LIMIT: u64 = 20 * 1024 * 1024;

fn raw_root(app: &AppHandle) -> Result<PathBuf, String> {
    let root = app
        .path()
        .app_data_dir()
        .map_err(|_| "No se encuentra la configuración local.")?
        .join("capture/raw");
    fs::create_dir_all(&root).map_err(|_| "No se encuentra la carpeta local de capturas.")?;
    Ok(root)
}

fn file_kind(path: &Path) -> Result<(&str, bool), String> {
    let extension = path
        .extension()
        .and_then(|v| v.to_str())
        .ok_or("El archivo no tiene formato reconocido.")?;
    if VIDEOS.iter().any(|v| extension.eq_ignore_ascii_case(v)) {
        Ok((extension, true))
    } else if ATTACHMENTS
        .iter()
        .any(|v| extension.eq_ignore_ascii_case(v))
    {
        Ok((extension, false))
    } else {
        Err("El formato del archivo no es compatible.".into())
    }
}

fn bind(folder: &Path, id: Uuid, owner: &str) -> Result<(), String> {
    fs::write(
        folder.join("capture-owner.json"),
        json!({"version": 1, "capture_id": id, "identity": owner}).to_string(),
    )
    .map_err(|_| "No se pudo preparar el archivo local.".into())
}

fn new_private_folder(root: &Path, name: &str) -> Result<tempfile::TempDir, String> {
    tempfile::Builder::new()
        .prefix(name)
        .tempdir_in(root)
        .map_err(|_| "No se pudo preparar el archivo local.".into())
}

async fn draft(app: &AppHandle, root: &Path, id: Uuid) -> Result<PathBuf, String> {
    let folder = root.join(format!("media-draft-{id}"));
    if folder.is_symlink() {
        return Err("El archivo seleccionado no está disponible.".into());
    }
    assert_capture_owner(app, &folder).await?;
    if !folder.join("capture-owner.json").is_file() {
        return Err("Selecciona el archivo de nuevo.".into());
    }
    let mut entries =
        fs::read_dir(&folder).map_err(|_| "El archivo seleccionado no está disponible.")?;
    let path = entries
        .find_map(|entry| {
            let path = entry.ok()?.path();
            (file_kind(&path).is_ok() && !path.is_symlink() && path.is_file()).then_some(path)
        })
        .ok_or("Selecciona el archivo de nuevo.")?;
    Ok(path)
}

#[tauri::command]
pub async fn synth_pick_media(app: AppHandle, video: bool) -> Result<Option<Value>, String> {
    let owner = capture_identity(&app).await?;
    let root = raw_root(&app)?;
    let limit = if video { VIDEO_LIMIT } else { ATTACHMENT_LIMIT };
    let dialog = app.clone();
    let result = tokio::task::spawn_blocking(move || -> Result<Option<Value>, String> {
        let (label, extensions) = if video {
            ("Vídeos", VIDEOS)
        } else {
            ("Archivos de contexto", ATTACHMENTS)
        };
        let Some(selection) = dialog
            .dialog()
            .file()
            .add_filter(label, extensions)
            .blocking_pick_file()
        else {
            return Ok(None);
        };
        let source = selection
            .into_path()
            .map_err(|_| "Selecciona un archivo local o sincronizado.")?;
        if file_kind(&source)?.1 != video {
            return Err("El formato del archivo no es compatible con esta selección.".into());
        }
        let original = source
            .file_name()
            .and_then(|v| v.to_str())
            .ok_or("El nombre del archivo no es válido.")?;
        let name = original.trim_start_matches('.');
        let file = File::open(&source).map_err(|_| "No se puede leer el archivo seleccionado.")?;
        let metadata = file
            .metadata()
            .map_err(|_| "No se puede leer el archivo seleccionado.")?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > limit {
            return Err("El archivo está vacío o supera el tamaño permitido.".into());
        }
        let id = Uuid::new_v4();
        let temp = new_private_folder(&root, "media-copy-")?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(temp.path().join(name))
            .map_err(|_| "No se pudo copiar el archivo.")?;
        if io::copy(&mut file.take(limit + 1), &mut output)
            .map_err(|_| "No se pudo copiar el archivo.")?
            != metadata.len()
        {
            return Err("El archivo cambió durante la importación.".into());
        }
        output
            .sync_all()
            .map_err(|_| "No se pudo guardar el archivo.")?;
        bind(temp.path(), id, &owner)?;
        fs::rename(temp.path(), root.join(format!("media-draft-{id}")))
            .map_err(|_| "No se pudo guardar el archivo.")?;
        Ok(Some(json!({"id": id, "name": name, "video": video})))
    })
    .await
    .map_err(|_| "No se pudo completar la selección.")??;
    Ok(result)
}

#[tauri::command]
pub async fn synth_preview_video(app: AppHandle, id: Uuid) -> Result<(), String> {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, id);
        return Err("La reproducción previa está disponible en macOS.".into());
    }
    #[cfg(target_os = "macos")]
    {
        let path = draft(&app, &raw_root(&app)?, id).await?;
        if !file_kind(&path)?.1 {
            return Err("Selecciona un vídeo.".into());
        }
        tokio::process::Command::new("/usr/bin/open")
            .arg(path)
            .status()
            .await
            .ok()
            .filter(|status| status.success())
            .ok_or("No se pudo abrir el vídeo.")?;
        Ok(())
    }
}

#[tauri::command]
pub async fn synth_enqueue_video(
    app: AppHandle,
    id: Uuid,
    video_id: Uuid,
    attachment_ids: Vec<Uuid>,
    title: String,
    folder_id: Option<Uuid>,
    notes: String,
    consent_confirmed: bool,
) -> Result<Value, String> {
    if !consent_confirmed
        || title.trim().is_empty()
        || title.chars().count() > 300
        || notes.chars().count() > 200_000
        || attachment_ids.len() > 6
    {
        return Err("Revisa el título, los adjuntos y el consentimiento.".into());
    }
    let owner = capture_identity(&app).await?;
    let root = raw_root(&app)?;
    let video = draft(&app, &root, video_id).await?;
    let (extension, is_video) = file_kind(&video)?;
    if !is_video {
        return Err("Selecciona un vídeo para generar el acta.".into());
    }
    let filename = format!("video.{}", extension.to_ascii_lowercase());
    let mut attachments = Vec::new();
    for attachment_id in attachment_ids {
        let path = draft(&app, &root, attachment_id).await?;
        if file_kind(&path)?.1 || attachments.contains(&path) {
            return Err("Los adjuntos deben ser archivos de contexto distintos.".into());
        }
        attachments.push(path);
    }

    let folder = root.join(format!("media-{id}"));
    let body = json!({"sources": {"import": folder.join(&filename)}, "capture_id": id,
        "title": title, "folder_id": folder_id, "notes": notes, "consent_confirmed": true});
    let receipt = json!({"video": video_id, "body": body, "attachments": attachments});
    if !folder.exists() {
        let temp = new_private_folder(&root, "media-copy-")?;
        fs::hard_link(video, temp.path().join(filename))
            .map_err(|_| "No se pudo preparar el vídeo.")?;
        let evidence = temp.path().join("evidence");
        fs::create_dir(&evidence).map_err(|_| "No se pudo preparar la carpeta de adjuntos.")?;
        for (index, path) in attachments.iter().enumerate() {
            let name = path
                .file_name()
                .ok_or("El adjunto no tiene nombre.")?
                .to_string_lossy();
            let mut target = evidence.join(name.as_ref());
            if target.exists() || name.starts_with("screen-") {
                target = evidence.join(format!(
                    "Adjunto {} - {}",
                    index + 1,
                    name.chars().take(70).collect::<String>()
                ));
                target.set_extension(path.extension().ok_or("El adjunto no tiene formato.")?);
            }
            fs::hard_link(path, target).map_err(|_| "No se pudo preparar un adjunto.")?;
        }
        bind(temp.path(), id, &owner)?;
        fs::write(temp.path().join("selection.json"), receipt.to_string())
            .map_err(|_| "No se pudo guardar el comprobante de importación.")?;
        if let Err(error) = fs::rename(temp.path(), &folder) {
            if !folder.exists() {
                return Err(format!("No se pudo guardar la importación: {error}"));
            }
        }
    }
    assert_capture_owner(&app, &folder).await?;
    let stored = fs::read(folder.join("selection.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    if stored.as_ref() != Some(&receipt) {
        return Err("La importación ha cambiado. Vuelve a seleccionar el vídeo.".into());
    }
    request_local(app, "POST".into(), "/v1/captures".into(), Some(body), 18383).await
}

#[tauri::command]
pub async fn synth_download_drive_media(
    app: AppHandle,
    link: String,
    video: bool,
) -> Result<Value, String> {
    let (client, base, token) = product_connection(&app, 18383, Duration::from_secs(310)).await?;
    let response = client
        .post(format!("{base}/v1/drive/media"))
        .bearer_auth(token)
        .json(&json!({"link": link, "video": video}))
        .send()
        .await
        .map_err(|_| "No se pudo descargar el archivo de Google Drive.")?;
    let status = response.status();
    let body = response
        .json::<Value>()
        .await
        .map_err(|_| "No se pudo recibir el archivo de Google Drive.")?;
    if status.is_success() {
        Ok(body)
    } else {
        Err(body["detail"]
            .as_str()
            .unwrap_or("drive_download_failed")
            .to_owned())
    }
}

#[tauri::command]
pub async fn synth_request_screen_permission(app: AppHandle) -> Result<bool, String> {
    #[cfg(target_os = "macos")]
    {
        let (send, receive) = tokio::sync::oneshot::channel();
        app.run_on_main_thread(move || {
            let access = core_graphics::access::ScreenCaptureAccess::default();
            let _ = send.send(access.preflight() || access.request());
        })
        .map_err(|_| "No se pudo comprobar el permiso de pantalla.")?;
        receive
            .await
            .map_err(|_| "No se pudo comprobar el permiso de pantalla.".into())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Ok(false)
    }
}

#[tauri::command]
pub async fn synth_take_screenshot(app: AppHandle) -> Result<Option<u64>, String> {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        return Err("La captura está disponible en macOS.".into());
    }
    #[cfg(target_os = "macos")]
    {
        use crate::audio::recording_commands::{active_media_folder, commit_capture_media};

        let folder = active_media_folder()
            .ok_or("Empieza o reanuda la grabación para capturar la pantalla.")?;
        assert_capture_owner(&app, &folder).await?;
        if !synth_request_screen_permission(app.clone()).await? {
            return Err(
                "Autoriza la grabación de pantalla de Synth en los ajustes de macOS.".into(),
            );
        }
        let temp = new_private_folder(&folder, "screen-pending-")?;
        let path = temp.path().join("selection.jpg");
        let mut command = tokio::process::Command::new("/usr/sbin/screencapture");
        command
            .args(["-i", "-x", "-t", "jpg"])
            .arg(&path)
            .kill_on_drop(true);
        let result = tokio::time::timeout(Duration::from_secs(90), command.output())
            .await
            .map_err(|_| "La selección de pantalla ha caducado.")?
            .map_err(|_| "No se pudo abrir la herramienta de captura.")?;
        if !path.is_file() {
            return if result.stderr.is_empty() {
                Ok(None)
            } else {
                Err("No se pudo capturar la región seleccionada.".into())
            };
        }
        let size = fs::metadata(&path)
            .map_err(|_| "No se pudo leer la captura.")?
            .len();
        if size == 0 || size > ATTACHMENT_LIMIT {
            return Err("La captura está vacía o supera 20 MB. Selecciona un área menor.".into());
        }
        assert_capture_owner(&app, &folder).await?;
        commit_capture_media(&folder, &path, true).map(Some)
    }
}

#[tauri::command]
pub async fn synth_attach_capture_media(
    app: AppHandle,
    id: Uuid,
    capture_id: Uuid,
) -> Result<(), String> {
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, id, capture_id);
        return Err("Los adjuntos de grabación están disponibles en macOS.".into());
    }
    #[cfg(target_os = "macos")]
    {
        use crate::audio::recording_commands::{active_media_folder, commit_capture_media};
        let folder = active_media_folder()
            .ok_or("Empieza o reanuda la grabación para adjuntar el archivo.")?;
        let receipt = fs::read(folder.join("capture-owner.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        if receipt
            .as_ref()
            .and_then(|value| value["capture_id"].as_str())
            != Some(capture_id.to_string().as_str())
        {
            return Err("La grabación ha cambiado.".into());
        }
        let path = draft(&app, &raw_root(&app)?, id).await?;
        if file_kind(&path)?.1 {
            return Err("Selecciona una imagen, un PDF o un archivo de texto.".into());
        }
        assert_capture_owner(&app, &folder).await?;
        commit_capture_media(&folder, &path, false)?;
        Ok(())
    }
}
