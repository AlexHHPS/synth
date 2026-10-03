#!/usr/bin/env python3
"""Generate a branded build from operator-owned configuration, without credentials."""
import argparse
import json
from pathlib import Path
import re
import sys
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from synth.config import origin

parser = argparse.ArgumentParser()
parser.add_argument('--name', default='Synth')
parser.add_argument('--tagline', default='Meeting notes, on your terms')
parser.add_argument('--identifier', default='dev.synth.voice')
parser.add_argument('--accent', default='221 83% 53%')
parser.add_argument('--logo', default='/synth-mark.png')
parser.add_argument('--api-url', default='http://127.0.0.1:18280')
parser.add_argument('--supabase-url', default='')
parser.add_argument('--generate-icons', action='store_true', help='Generate native icons and favicon from the supplied logo')
args = parser.parse_args()
if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', args.name):
    parser.error('Use a plain product name without path separators.')
if not re.fullmatch(r'[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*){2,}', args.identifier):
    parser.error('Use a reverse-domain application identifier.')
if not re.fullmatch(r'\d{1,3} \d{1,3}% \d{1,3}%', args.accent):
    parser.error('Accent must be an HSL triplet, for example 221 83% 53%.')
if not args.logo.startswith('/') or '..' in args.logo or not (ROOT/'frontend/public'/args.logo[1:]).is_file():
    parser.error('Logo must name an existing file in frontend/public.')
api = origin(args.api_url, loopback=True)
supabase = origin(args.supabase_url) if args.supabase_url else ''
if supabase and not re.fullmatch(r'https://[a-z0-9]+\.supabase\.co', supabase):
    parser.error('Select the hosted Supabase project origin.')
if args.generate_icons:
    executable = ROOT/'frontend/node_modules/.bin/tauri'
    if not executable.exists(): parser.error('Install frontend dependencies before generating native icons.')
    output = ROOT/'synth/.runtime/brand-icons'
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(executable), 'icon', str(ROOT/'frontend/public'/args.logo[1:]), '--output', str(output)], check=True, capture_output=True)
    for name in ['icon.png', '32x32.png', '128x128.png', '128x128@2x.png']:
        shutil.copy2(output/name, ROOT/'frontend/src-tauri/icons'/name)
    for name, destination in [('icon.icns','app_icon.icns'),('icon.ico','app_icon.ico')]:
        shutil.copy2(output/name, ROOT/'frontend/src-tauri/icons'/destination)
    shutil.copy2(output/'icon.ico', ROOT/'frontend/src/app/favicon.ico')
old = json.loads((ROOT/'branding.json').read_text())
# Keep all native paths and Keychain namespaces aligned with the bundle identity.
for directory in ['synth', 'frontend/src-tauri/src']:
    for path in (ROOT/directory).rglob('*'):
        if not path.is_file() or '.runtime' in path.parts or '.toolchains' in path.parts or 'tests' in path.parts:
            continue
        try: text = path.read_text()
        except UnicodeDecodeError: continue
        updated = text.replace(old['identifier'], args.identifier)
        if updated != text: path.write_text(updated)
brand = {'name': args.name, 'tagline': args.tagline, 'identifier': args.identifier,
         'accent': args.accent, 'logo': args.logo}
(ROOT/'branding.json').write_text(json.dumps(brand, indent=2)+'\n')
config_path = ROOT/'frontend/src-tauri/tauri.conf.json'
config = json.loads(config_path.read_text())
config.update(productName=args.name, identifier=args.identifier)
config['app']['windows'][0]['title'] = args.name
config_path.write_text(json.dumps(config, indent=2)+'\n')
for filename in ['metadata.ts','metadata.tsx']:
    (ROOT/'frontend/src/app'/filename).write_text(
        "import type { Metadata } from 'next';\nimport { brand } from '@/synth/brand';\n"
        "export const metadata: Metadata = {title: brand.name, description: brand.tagline};\n")
deployment = {'api_url': api, 'auth_mode': 'supabase' if supabase else 'keys', 'supabase_url': supabase}
(ROOT/'synth/deployment.json').write_text(json.dumps(deployment, indent=2)+'\n')
cargo = ROOT/'.cargo'; cargo.mkdir(exist_ok=True)
# This is a build-time destination allowlist. JS cannot redirect credentials.
(cargo/'config.toml').write_text('[env]\nSYNTH_API_URL = '+json.dumps(api)+'\n')
tokens = ROOT/'frontend/src/synth/design-system/tokens.css'
tokens.write_text(re.sub(r'--brand: [^;]+;', '--brand: '+args.accent+';', tokens.read_text(), count=1))
print('Brand and trusted deployment configured. Rebuild the desktop and packaged host together.')
