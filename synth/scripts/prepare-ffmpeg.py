"""Build a pinned, audio-only LGPL FFmpeg sidecar with corresponding source."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'synth/.runtime'
RUNTIME.mkdir(parents=True, exist_ok=True)
archive = RUNTIME / 'ffmpeg-8.0.tar.xz'
expected = 'b2751fccb6cc4c77708113cd78b561059b6fa904b24162fa0be2d60273d27b8e'
if not archive.exists():
    urllib.request.urlretrieve('https://ffmpeg.org/releases/ffmpeg-8.0.tar.xz', archive)
if hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() != expected:
    raise ValueError('FFmpeg source checksum mismatch')
source = RUNTIME / 'ffmpeg-8.0'
if not source.exists():
    with tarfile.open(archive) as handle:
        handle.extractall(RUNTIME, filter='data')
flags = ['--extra-cflags=-mmacosx-version-min=14.0', '--extra-ldflags=-mmacosx-version-min=14.0', '--disable-everything', '--disable-autodetect', '--disable-network',
         '--disable-doc', '--disable-debug', '--disable-ffplay', '--disable-ffprobe',
         '--enable-ffmpeg', '--enable-protocol=file,pipe',
         '--enable-demuxer=wav,aiff,flac,mp3,mov,matroska,ogg,aac',
         '--enable-muxer=wav,flac,adts,mp4',
         '--enable-decoder=pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le,pcm_s16be,pcm_s24be,pcm_s32be,pcm_f32be,pcm_f64be,aac,alac,mp3,flac,opus,vorbis',
         '--enable-encoder=pcm_s16le,aac,flac',
         '--enable-parser=aac,mpegaudio,flac,opus,vorbis',
         '--enable-filter=aresample,aformat,anull,volume', '--enable-swresample']
subprocess.run(['rtk', 'proxy', './configure', *flags], cwd=source, check=True)
subprocess.run(['rtk', 'proxy', 'make', '-j4'], cwd=source, check=True)
binary = ROOT / 'frontend/src-tauri/binaries/ffmpeg-aarch64-apple-darwin'
binary.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source/'ffmpeg', binary)
output = subprocess.check_output([str(binary), '-L'], stderr=subprocess.STDOUT).decode()
if 'GNU Lesser General Public License' not in ' '.join(output.split()):
    raise ValueError('Expected LGPL-only FFmpeg build')
(RUNTIME/'ffmpeg-build.json').write_text(json.dumps({
    'source': archive.name, 'sha256': expected, 'configure': flags,
    'patches': [], 'license': 'LGPL-2.1-or-later',
    'binary_sha256': hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest()}, indent=2))
print('FFMPEG_SOURCE_BUILD_PASS')
