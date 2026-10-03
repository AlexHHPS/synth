"""Self-contained pilot host entry point: no checkout, Xcode or system Python."""
import json
import os
from pathlib import Path
import secrets
import sys
import plistlib
import subprocess


def configure_environment():
    if getattr(sys, "frozen", False):
        resources = Path(sys._MEIPASS)
        os.environ["PATH"] = str(resources / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"
        os.environ["SYNTH_ASR_MODEL"] = str(resources / "models/ggml-large-v3-turbo-q5_0.bin")
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"


def configure():
    appdata = Path.home() / "Library/Application Support/dev.synth.voice"
    appdata.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential = appdata / "desktop-api-key"
    if not credential.exists():
        fd = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    config = appdata / "backend-config.json"
    if not config.exists():
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            from synth.config import API_URL, AUTH_MODE, SUPABASE_URL
            json.dump({"api_url": API_URL, "auth_mode": AUTH_MODE, "supabase_url": SUPABASE_URL}, stream)



def install_agent():
    appdata = Path.home() / "Library/Application Support/dev.synth.voice"
    logs = appdata / "logs"; logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path.home() / "Library/LaunchAgents/dev.synth.voice.pipeline.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    value = {"Label":"dev.synth.voice.pipeline", "ProgramArguments":[sys.executable],
        "RunAtLoad":True, "KeepAlive":True, "ProcessType":"Interactive", "Umask":0o077,
        "WorkingDirectory":str(appdata), "EnvironmentVariables":{"SSL_CERT_FILE":"/etc/ssl/cert.pem"},
        "StandardOutPath":str(logs / "host.log"), "StandardErrorPath":str(logs / "host-error.log")}
    if target.exists():
        old = plistlib.loads(target.read_bytes())
        arguments = old.get("ProgramArguments", [])
        if not any("synth-voice-host" in p or p == "synth.server.desktop_host" for p in arguments):
            raise SystemExit("Existing host service belongs to another application")
        if arguments == value["ProgramArguments"]:
            return
    part = target.with_suffix(".plist.part"); part.write_bytes(plistlib.dumps(value)); part.chmod(0o600); part.replace(target)
    domain = "gui/" + str(os.getuid())
    subprocess.run(["/bin/launchctl","bootout",domain+"/dev.synth.voice.pipeline"],capture_output=True)
    subprocess.run(["/bin/launchctl","bootstrap",domain,str(target)],capture_output=True,check=True)


if __name__ == "__main__":
    configure_environment()
    if "--self-check" not in sys.argv:
        configure()
    if "--install-agent" in sys.argv:
        if not getattr(sys, "frozen", False):
            raise SystemExit("Install the packaged host")
        install_agent()
    elif "--self-check" in sys.argv:
        from synth.speakers.native import verify_models, BINARY
        from synth.worker.asr import MODEL, MODEL_SHA
        from synth.worker.journal import file_hash
        import subprocess
        verify_models()
        if file_hash(MODEL) != MODEL_SHA or not BINARY.is_file():
            raise SystemExit("Bundled model verification failed")
        for binary, arguments in [("ffmpeg",["-version"]),("whisper-cli",["--help"])]:
            result = subprocess.run([binary,*arguments], capture_output=True, timeout=20)
            if result.returncode:
                raise SystemExit("Bundled tool failed: " + binary)
        print("SYNTH_HOST_SELF_CHECK_PASS")
    elif "--acoustic-smoke" in sys.argv:
        import tempfile
        from synth.worker.asr import normalize_audio, transcribe
        from synth.speakers.native import infer
        raw = Path.home() / "Library/Application Support/dev.synth.voice/capture/raw"
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="self-test-", dir=raw) as directory:
            folder = Path(directory)
            source = folder / "synthetic.aiff"; wav = folder / "synthetic.wav"
            subprocess.run(["/usr/bin/say", "-v", "Mónica", "-o", str(source),
                "Esta es una prueba sintética del piloto de Synth. Estamos comprobando que la transcripción y los perfiles de voz se procesan en el ordenador. No se está grabando ninguna persona."],
                capture_output=True, timeout=30, check=True)
            normalize_audio(source, wav)
            transcript, _ = transcribe(wav, folder / "asr")
            diarization = infer("diarize", wav, folder)
            vector = infer("embed", wav, folder)["embedding"]
            result = {"status":"PASS", "synthetic_only":True, "transcript_segments":len(transcript["segments"]),
                      "speaker_turns":len(diarization["segments"]), "embedding_dimension":len(vector)}
        result["audio_deleted"] = not folder.exists()
        print(json.dumps(result))
    else:
        import threading
        import time
        from synth.scripts.retention import main as cleanup
        def retention_loop():
            while True:
                try:
                    cleanup()
                except (OSError, ValueError):
                    print("Audio cleanup requires attention", file=sys.stderr)
                time.sleep(30)
        threading.Thread(target=retention_loop, daemon=True).start()
        from synth.server.desktop_host import main
        main()
