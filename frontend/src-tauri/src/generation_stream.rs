//! A bounded SSE decoder and a native channel; credentials never reach JavaScript.
use serde_json::{json, Value};
use std::time::Duration;
use tauri::{ipc::Channel, AppHandle};

#[derive(Default)]
struct Decoder {
    pending: Vec<u8>,
    data: Vec<u8>,
    event: String,
}

impl Decoder {
    fn feed(&mut self, bytes: &[u8]) -> Result<Vec<Value>, String> {
        self.pending.extend_from_slice(bytes);
        let mut output = Vec::new();
        while let Some(end) = self.pending.iter().position(|byte| *byte == b'\n') {
            let mut line: Vec<u8> = self.pending.drain(..=end).collect();
            line.pop();
            if line.last() == Some(&b'\r') { line.pop(); }
            if line.is_empty() {
                if !self.data.is_empty() {
                    let value: Value = serde_json::from_slice(&self.data)
                        .map_err(|_| "generation_stream_invalid")?;
                    output.push(json!({"event": self.event, "data": value}));
                }
                self.data.clear();
                self.event.clear();
            } else if let Some(data) = line.strip_prefix(b"data:") {
                if !self.data.is_empty() { self.data.push(b'\n'); }
                self.data.extend_from_slice(data.strip_prefix(b" ").unwrap_or(data));
            } else if let Some(event) = line.strip_prefix(b"event:") {
                self.event = std::str::from_utf8(event).map_err(|_| "generation_stream_invalid")?.trim().to_owned();
            }
            if self.data.len() > 128 * 1024 { return Err("generation_event_too_large".into()); }
        }
        if self.pending.len() > 128 * 1024 { return Err("generation_event_too_large".into()); }
        Ok(output)
    }

    fn complete(&self) -> bool { self.pending.is_empty() && self.data.is_empty() }
}

#[tauri::command]
pub async fn synth_generation_stream(app: AppHandle, meeting_id: String, generation_id: String,
                                     after: u64, on_event: Channel<Value>) -> Result<(), String> {
    let meeting = uuid::Uuid::parse_str(&meeting_id).map_err(|_| "generation_id_invalid")?;
    let generation = uuid::Uuid::parse_str(&generation_id).map_err(|_| "generation_id_invalid")?;
    if after > i64::MAX as u64 { return Err("generation_cursor_invalid".into()); }
    let (client, base, token) = crate::synth_desktop::product_connection(&app, 18280, Duration::from_secs(30)).await?;
    let mut response = client.get(format!("{base}/v1/meetings/{meeting}/generations/{generation}/stream?after={after}"))
        .bearer_auth(token).header("Accept", "text/event-stream").send().await
        .map_err(|_| "generation_stream_unavailable")?;
    if !response.status().is_success() {
        return Err(match response.status().as_u16() {
            401 | 403 | 404 => "generation_access_denied",
            409 => "generation_superseded",
            _ => "generation_stream_unavailable",
        }.into());
    }
    if !response.headers().get("content-type").and_then(|value| value.to_str().ok())
        .unwrap_or("").starts_with("text/event-stream") {
        return Err("generation_stream_invalid".into());
    }
    let mut decoder = Decoder::default();
    while let Some(chunk) = response.chunk().await.map_err(|_| "generation_stream_incomplete")? {
        for event in decoder.feed(&chunk)? {
            // A closed reader drops this HTTP stream; the worker owns inference.
            on_event.send(event).map_err(|_| "generation_reader_closed")?;
        }
    }
    if !decoder.complete() { return Err("generation_stream_incomplete".into()); }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::Decoder;
    #[test]
    fn unicode_and_frames_survive_arbitrary_network_boundaries() {
        let wire = "event: generation\r\ndata: {\"sequence\":1,\"text\":\"decisión\"}\r\n\r\nevent: status\ndata: {\"state\":\"complete\"}\n\n";
        for size in [1, 2, 7, 31, 1024] {
            let mut decoder = Decoder::default();
            let mut events = Vec::new();
            for chunk in wire.as_bytes().chunks(size) { events.extend(decoder.feed(chunk).unwrap()); }
            assert_eq!(events.len(), 2);
            assert_eq!(events[0]["data"]["text"], "decisión");
            assert_eq!(events[1]["data"]["state"], "complete");
            assert!(decoder.complete());
        }
    }
    #[test]
    fn first_event_is_returned_before_the_stream_finishes() {
        let mut decoder = Decoder::default();
        assert_eq!(decoder.feed(b"event: generation\ndata: {\"sequence\":1}\n\n").unwrap().len(), 1);
        assert!(decoder.feed(b"event: generation\ndata: {\"sequ").unwrap().is_empty());
        assert!(!decoder.complete());
    }
    #[test]
    fn comments_are_not_content_and_oversized_or_invalid_events_fail() {
        assert!(Decoder::default().feed(b": keepalive\n\n").unwrap().is_empty());
        assert!(Decoder::default().feed(&vec![b'x'; 129 * 1024]).is_err());
        assert!(Decoder::default().feed(b"data: not-json\n\n").is_err());
    }
}
