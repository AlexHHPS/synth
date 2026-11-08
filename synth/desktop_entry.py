"""Self-contained pilot host entry point: no checkout, Xcode or system Python."""
import json
import os
from pathlib import Path
import secrets
import sys
import plistlib
import subprocess
import hashlib


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
            import urllib.request
            build=Path(sys._MEIPASS)/'host-build.json'
            try:
                request=urllib.request.Request('http://127.0.0.1:18383/v1/status',headers={'Authorization':'Bearer '+(appdata/'desktop-api-key').read_text().strip()})
                with urllib.request.urlopen(request,timeout=3) as response:running=json.load(response)
                with Path(sys.executable).open('rb') as binary:
                    package_sha256 = hashlib.file_digest(binary, 'sha256').hexdigest()
                if (running.get('host_build')==json.loads(build.read_text())['id']
                        and running.get('host_binary_sha256')==package_sha256):return
            except (OSError,ValueError):pass
            database=appdata/'pipeline/queue.sqlite3'
            if database.is_file():
                import sqlite3
                with sqlite3.connect(f'file:{database}?mode=ro',uri=True) as db:
                    if db.execute("SELECT count(*) FROM desktop_tasks WHERE state IN ('running','cancel_requested')").fetchone()[0]:
                        raise SystemExit('Espera a que termine el procesamiento y vuelve a abrir Synth.')
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
    elif "--verify-first-run" in sys.argv:
        from synth.server import audio_setup
        directory = Path(sys.argv[sys.argv.index('--verify-first-run')+1]).expanduser().resolve()
        audio_setup.start(directory)
        audio_setup._THREAD.join()
        result = audio_setup.status()
        print(json.dumps({'first_run':result,'isolated_app_data':str(directory)}))
        sys.exit(0 if result['state']=='ready' else 1)
    elif "--setup-audio" in sys.argv:
        from synth.server.audio_setup import load_installer
        sys.exit(load_installer().main([argument for argument in sys.argv[1:] if argument != '--setup-audio']))
    elif "--self-check" in sys.argv:
        from synth.speakers.native import verify_models, BINARY
        import subprocess
        verify_models()
        from synth.speakers.matching import THRESHOLDS
        if not isinstance(THRESHOLDS, dict) or not THRESHOLDS.get('id'):
            raise SystemExit('Bundled voice thresholds missing')
        # Exercise the final-text path in the actual frozen interpreter, not
        # just the external Qwen runtime. Missing numpy must fail packaging.
        from synth.worker.desktop_pipeline import canonical_transcript
        from synth.worker.transcript_quality import batch_quality_report
        sample = {'language':'es','duration_ms':2000,'model_fingerprint':'packaged-self-check',
                  'segments':[{'id':'s0','start_ms':0,'end_ms':1000,'text':'Prueba local.','source_id':'microphone'}]}
        final = canonical_transcript({'microphone':sample},
            {'microphone':{'speech_state':'detected','segments':[{'start_ms':0,'end_ms':1000,'speaker_id':'one'}]}}, {}, [])
        if len(final['segments']) != 1 or batch_quality_report({'microphone':sample},final)['selected'] != 'batch':
            raise SystemExit('Bundled final transcription check failed')
        from synth.worker.live_bundle import verify as verify_live_bundle
        verify_live_bundle(Path(sys._MEIPASS)/'synth/worker')
        if not BINARY.is_file() or not (Path(sys._MEIPASS)/'synth/worker/qwen_live_runner.py').is_file():
            raise SystemExit("Bundled audio runtime verification failed")
        for binary, arguments in [("ffmpeg",["-version"]),("uv",["--version"])]:
            result = subprocess.run([binary,*arguments], capture_output=True, timeout=20)
            if result.returncode:
                raise SystemExit("Bundled tool failed: " + binary)
        print("SYNTH_HOST_SELF_CHECK_PASS")
    elif "--echo-smoke" in sys.argv:
        import tempfile
        from synth.worker.acoustic import prepare_acoustic_sources
        from synth.worker.journal import file_hash
        index = sys.argv.index('--echo-smoke')
        paths = [Path(p).resolve(strict=True) for p in sys.argv[index+1:index+3]]
        if len(paths) != 2:
            raise SystemExit('Expected microphone and system WAV files')
        sources = {name: {'path': str(path), 'sha256': file_hash(path)}
                   for name, path in zip(('microphone', 'system'), paths)}
        with tempfile.TemporaryDirectory(prefix='synth-echo-check-') as directory:
            prepared = prepare_acoustic_sources(sources, directory)
            print(json.dumps({'status': 'PASS', 'echo': prepared['echo']}))
    elif "--voice-smoke" in sys.argv:
        import tempfile
        from synth.speakers.native import infer, project_embeddings
        from synth.worker.desktop_pipeline import inference_lock, STATE
        source = Path(sys.argv[sys.argv.index('--voice-smoke') + 1]).resolve(strict=True)
        with inference_lock(STATE) as fd, tempfile.TemporaryDirectory(prefix='synth-voice-check-') as directory:
            vector = infer('embed',source,directory,lock_fd=fd)['embedding']
            projected = project_embeddings([vector],directory,lock_fd=fd)[0]
            print(json.dumps({'status':'PASS','raw_dimensions':len(vector),'projected_dimensions':len(projected),
                              'embedding_space':'plda_rho128_v1'}))
    elif "--asr-smoke" in sys.argv:
        import tempfile
        from synth.worker.asr import transcribe
        source = Path(sys.argv[sys.argv.index('--asr-smoke') + 1]).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='synth-asr-check-') as directory:
            transcript, raw = transcribe(source, directory)
            print(json.dumps({'status': 'PASS', 'model_fingerprint': transcript['model_fingerprint'],
                              'segments': len(transcript['segments']), 'words': len(raw.get('words', []))}))
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
