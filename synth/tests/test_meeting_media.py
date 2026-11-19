from contextlib import nullcontext
from io import BytesIO
import json
import subprocess
import time
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import wave

import pytest

from synth.contracts.transcript import Transcript
from synth.server import desktop_auth, drive_media
from synth.server.capture_owner import validate_sources
from synth.worker.asr import normalize_audio
from synth.worker.desktop_pipeline import delete_processed_audio
from synth.worker.desktop_queue import DesktopQueue
from synth.worker.document_context import MeetingContext, render_lines
from synth.worker.media import evidence_files, frame_times, notes_with_media, observations, screen_references
from synth.worker.minutes_single import window_prompt


def test_video_import_transcribes_the_audio_track_and_adds_media_as_separate_context(tmp_path):
    receipt_id = str(uuid4())
    owner = str(uuid4())
    folder = tmp_path / f"media-{receipt_id}"
    evidence = folder / "evidence"
    evidence.mkdir(parents=True)
    video = folder / "video.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "color=white:size=320x240:rate=10", "-f", "lavfi", "-i", "sine=frequency=440",
                    "-t", "2", "-c:v", "mpeg4", "-c:a", "aac", str(video)], check=True)
    (folder / "capture-owner.json").write_text(json.dumps({
        "version": 1, "capture_id": receipt_id, "identity": owner,
    }))
    (evidence / "agenda.txt").write_text("Migración de inventario")
    sources = {"import": str(video)}
    validate_sources(sources, receipt_id, owner, tmp_path)
    with pytest.raises(ValueError, match="capture_owner_mismatch"):
        validate_sources(sources, receipt_id, str(uuid4()), tmp_path)

    queue = DesktopQueue(tmp_path / "queue.sqlite", tmp_path)
    try:
        task = queue.enqueue(sources, "Revisión", consent_confirmed=True, capture_id=receipt_id, owner_id=owner)
        normalized = tmp_path / "audio.wav"
        normalize_audio(video, normalized, input_format="mov")
        with wave.open(str(normalized)) as audio:
            assert audio.getframerate() == 16000
            assert audio.getnchannels() == 1
            assert 31000 < audio.getnframes() < 34000

        def ocr(path, directory, lock_fd):
            if path.suffix == ".jpg":
                assert path.read_bytes().startswith(b"\xff\xd8")
                return "Pantalla: coste estimado 350 €"
            return path.read_text()

        with patch("synth.worker.media.read_text", side_effect=ocr):
            items = observations(task, tmp_path, tmp_path, lambda: None, None)
        assert [item["kind"] for item in items] == ["video_frame", "attachment"]
        assert items[0]["at_ms"] == 1000

        claimed = queue.claim()
        token = claimed["lease_token"]
        queue.checkpoint(task["id"], token, "media_observed", {"items": items})
        queue.checkpoint(task["id"], token, "media_delivery", {"structured": False})
        queue.finish(task["id"], token, error_code="api_http_422")
        resumed = queue.retry(task["id"])
        assert resumed["checkpoints"]["media_observed"]["items"] == items
        assert resumed["checkpoints"]["media_delivery"] == {"structured": False}

        transcript = Transcript.model_validate({
            "model_fingerprint": "test",
            "duration_ms": 2000,
            "segments": [{"id": "s000001", "text": "Revisemos el coste.", "start_ms": 500, "end_ms": 1800}],
            "media_observations": items,
        }).model_dump()
        lines = render_lines(transcript)
        assert [line["text"] for line in lines] == ["Revisemos el coste."]
        prompt = window_prompt(MeetingContext(media=tuple(transcript["media_observations"])), lines,
                               first=True, last=True, covered=[])
        assert "coste estimado 350 €" in prompt
        assert "Migración de inventario" in prompt
        assert "requieren evidencia de la conversación" in prompt

        assert delete_processed_audio(task, tmp_path) == 2
        assert not video.exists()
        retry = queue.enqueue(sources, "Revisión", consent_confirmed=True, capture_id=receipt_id, owner_id=owner)
        assert retry["id"] == task["id"]
    finally:
        queue.close()


def test_drive_import_uses_google_endpoint_and_binds_download_to_the_current_account(tmp_path):
    metadata = {"name": "Plan", "mimeType": "application/vnd.google-apps.document",
                "capabilities": {"canDownload": True}}
    session = {"principal_id": str(uuid4()), "token": "fake-access-token"}
    opener = Mock()
    opener.open.side_effect = [BytesIO(json.dumps(metadata).encode()), BytesIO(b"%PDF-test")]
    with patch.object(drive_media.desktop_auth, "drive_access", return_value=session), \
         patch.object(drive_media.urllib.request, "build_opener", return_value=opener):
        result = drive_media.download("https://docs.google.com/document/d/valid-file-id/edit", False, tmp_path)

    assert result["name"] == "Plan.pdf"
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://www.googleapis.com/drive/v3/files/valid-file-id/export?mimeType=application%2Fpdf"
    assert request.get_header("Authorization") == "Bearer fake-access-token"
    folder = tmp_path / ("media-draft-" + result["id"])
    assert (folder / result["name"]).read_bytes() == b"%PDF-test"
    assert json.loads((folder / "capture-owner.json").read_text())["identity"] == session["principal_id"]
    assert "token" not in result

    for link in ["http://drive.google.com/open?id=valid-id", "https://drive.google.com.example.org/file/d/valid-id",
                 "https://drive.google.com/open?id=../valid-id", "https://drive.google.com/drive/folders/valid-id"]:
        with pytest.raises(ValueError, match="drive_link_invalid"):
            drive_media.parse_link(link)


def test_drive_browser_preserves_folder_access_and_filters_unavailable_media():
    opener = Mock()
    response = {"nextPageToken": "next-page", "files": [
        {"id": "nested-folder", "name": "Grabaciones", "mimeType": drive_media.FOLDER_MIME,
         "resourceKey": "folder-access"},
        {"id": "video-shortcut", "name": "Revisión del piloto", "mimeType": drive_media.SHORTCUT_MIME,
         "driveId": "shared-drive", "shortcutDetails": {"targetId": "real-video", "targetMimeType": "video/mp4",
                                                       "targetResourceKey": "real-access"}},
        {"id": "video-big", "name": "Llamada larga", "mimeType": "video/mp4",
         "size": str(drive_media.MAX_VIDEO_BYTES + 1), "capabilities": {"canDownload": True}},
        {"id": "video-hidden", "name": "Restringido", "mimeType": "video/mp4",
         "capabilities": {"canDownload": False}},
        {"id": "another-doc", "name": "Agenda", "mimeType": "application/pdf",
         "capabilities": {"canDownload": True}},
    ]}
    opener.open.return_value = BytesIO(json.dumps(response).encode())
    with patch.object(drive_media.desktop_auth, "drive_access", return_value={"principal_id": "owner", "token": "secret"}), \
         patch.object(drive_media, "drive_opener", return_value=opener):
        page = drive_media.browse({"location": "folder", "video": True, "folder_id": "parent-folder",
                                   "drive_id": "shared-drive", "resource_key": "parent-access", "cursor": "previous-page"})

    assert page["next_cursor"] == "next-page"
    folder, shortcut, large, inaccessible = page["items"]
    assert folder["folder"] and folder["resource_key"] == "folder-access" and folder["drive_id"] == "shared-drive"
    assert shortcut["id"] == "real-video" and shortcut["resource_key"] == "real-access" and not shortcut["disabled"]
    assert large["disabled"] and inaccessible["disabled"]
    request = opener.open.call_args.args[0]
    parsed = urlsplit(request.full_url)
    args = parse_qs(parsed.query)
    assert parsed.netloc == "www.googleapis.com"
    assert "'parent-folder' in parents" in args["q"][0] and "application/pdf" not in args["q"][0]
    assert args["pageToken"] == ["previous-page"]
    assert args["corpora"] == ["drive"] and args["driveId"] == ["shared-drive"]
    assert request.get_header("X-goog-drive-resource-keys") == "parent-folder/parent-access"
    assert all("token" not in entry for entry in page["items"])

    with pytest.raises(ValueError, match="drive_browser_invalid"):
        drive_media.browse({"location": "folder", "video": True, "folder_id": "folder' or trashed = false"})


def test_screen_captures_keep_recording_time_and_detect_modified_content(tmp_path):
    screenshots = tmp_path / "evidence"
    screenshots.mkdir()
    for at_ms in (25001, 9000):
        (screenshots / f"screen-{at_ms}-{uuid4()}.jpg").write_bytes(b"synthetic")
    descriptors = evidence_files(tmp_path)
    assert [item["at_ms"] for item in descriptors] == [9000, 25001]

    task = {"metadata": {"sources": {}, "media": descriptors}}
    with patch("synth.worker.media.read_text", return_value="Pantalla de inventario"):
        assert observations(task, tmp_path, tmp_path, lambda: None, None)[0]["kind"] == "screenshot"
    next(screenshots.iterdir()).write_bytes(b"modified")
    with pytest.raises(ValueError, match="capture_media_changed"):
        observations(task, tmp_path, tmp_path, lambda: None, None)


def test_drive_oauth_keeps_provider_token_local_and_requires_same_principal():
    flow = {"state": "waiting", "nonce": "drive-nonce", "verifier": "verifier",
            "expires_at": time.time() + 60, "config": {"publishable_key": "test"},
            "drive": True, "principal_id": "alice"}
    callback = "/auth/callback/drive-nonce?code=test"
    exchange = {"access_token": "synth-token", "provider_token": "drive-token"}
    with patch.object(desktop_auth, "FLOW", flow.copy()), patch.object(desktop_auth, "DRIVE", None), \
         patch.object(desktop_auth, "session_lock", side_effect=nullcontext), \
         patch.object(desktop_auth, "read_session", return_value={"principal_id": "alice"}), \
         patch.object(desktop_auth, "capture_identity", return_value={"signed_in": True, "principal_id": "alice"}), \
         patch.object(desktop_auth, "store_session") as store:
        with patch.object(desktop_auth, "fetch", side_effect=[exchange, {"id": "bob"}]):
            with pytest.raises(ValueError, match="auth_identity_changed"):
                desktop_auth.callback(callback)
        assert desktop_auth.DRIVE is None
        desktop_auth.FLOW = flow.copy()
        with patch.object(desktop_auth, "fetch", side_effect=[exchange, {"id": "alice"}]):
            desktop_auth.callback(callback)
        store.assert_not_called()
        assert desktop_auth.drive_state() == {"state": "connected"}
        assert desktop_auth.drive_access()["token"] == "drive-token"


def test_media_fallback_for_an_older_service_keeps_human_notes_and_timestamps():
    media = [
        {"kind": "video_frame", "label": "Fotograma", "at_ms": 2000,
         "status": "read", "text": "Inventario\nPresupuesto de 350 EUR"},
        {"kind": "video_frame", "label": "Fotograma", "at_ms": 16000,
         "status": "read", "text": "Inventario\n42 pedidos"},
    ]
    human = "Mi nota sobre el piloto"
    notes = notes_with_media(human, media)
    assert "Fotograma 00:02" in notes and "Fotograma 00:16" in notes
    assert notes.count("Inventario") == 1
    assert "350 EUR" in notes and "42 pedidos" in notes
    assert notes.endswith(human)
    assert notes_with_media("a" * 200_000, media) == "a" * 200_000


def test_drive_folder_paginates_and_lists_nested_videos_only():
    opener = Mock()
    folder = {"id": "root-folder", "name": "Main", "mimeType": "application/vnd.google-apps.folder"}
    child = {"id": "child-folder", "name": "Sub", "mimeType": folder["mimeType"], "resourceKey": "child-key"}
    video = {"id": "first-video", "name": "First.mp4", "mimeType": "video/mp4"}
    hidden = {"id": "text-bytes", "name": "Other.pdf", "mimeType": "application/pdf"}
    opener.open.side_effect = [BytesIO(json.dumps(item).encode()) for item in [
        folder, {"files": [video], "nextPageToken": "next"}, {"files": [child, hidden]},
        {"files": [{**video, "id": "second-video", "name": "Second.mp4"}]},
    ]]
    with patch.object(desktop_auth, "drive_access", return_value={"principal_id": "user", "token": "token"}), \
         patch.object(drive_media, "drive_opener", return_value=opener):
        catalog = drive_media.folder_videos("https://drive.google.com/drive/folders/root-folder")
    assert [entry["id"] for entry in catalog["videos"]] == ["first-video", "second-video"]
    assert catalog["videos"][1]["parents"] == ["Sub"]
    assert "pageToken=next" in opener.open.call_args_list[2].args[0].full_url
    assert opener.open.call_args.args[0].get_header("X-goog-drive-resource-keys") == "child-folder/child-key"


def test_visual_sampling_uses_an_aligned_here_cue_and_the_change_nearby():
    words = [{"text": "Como", "start_ms": 8900}, {"text": "veis", "start_ms": 9040},
             {"text": "aquí,", "start_ms": 9200}, {"text": "hay", "start_ms": 9420}]
    references = screen_references([], words)
    assert references == [(9040, 2)]
    choices = frame_times(22, [(9500, 10000, 0.20), (17000, 17500, 0.95)], references)
    assert (9500, 10000, 9040) in choices
    assert choices[0][1] < 9040 < choices[-1][1]
    assert screen_references([{"text": "Aquí se ve", "start_ms": 1000, "end_ms": 2000}]) == [(1500, 1)]


def test_visual_context_works_when_there_is_no_written_text_in_the_frame():
    media = {"kind": "video_frame", "label": "Fotograma", "at_ms": 12000, "status": "no_text", "text": "",
             "visual": {"model": "qwen3-vl-4b", "scene": "Panel con un formulario.",
                        "change": "Aparece un modal centrado.", "compared_to_ms": 11000, "speech_reference_ms": 11600}}
    notes = notes_with_media("", [media])
    assert "Pantalla 00:11–00:12 (voz 00:11)" in notes
    assert "Aparece un modal centrado." in notes
    context = MeetingContext(media=(media,))
    assert "Aparece un modal centrado." in context.media_prompt([{"start_ms": 9000, "end_ms": 15000}])
    saved = Transcript.model_validate({"model_fingerprint": "test", "duration_ms": 18000,
        "segments": [{"id": "s0", "text": "Aquí", "start_ms": 11600, "end_ms": 11900}],
        "media_observations": [media]}).model_dump()
    assert saved["media_observations"] == [media]
