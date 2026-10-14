"""Portable launcher. --check prints non-secret configuration without launching."""
import argparse
import json
import subprocess
import sys

from sp_variant_runtime import REVIEW_SCOPE, bootstrap_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description='SP-first / HOLD-only permit review')
    parser.add_argument('--port', type=int, default=8507)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    root = bootstrap_runtime()
    command = [sys.executable, '-m', 'streamlit', 'run', str(root / 'sp_app.py'),
               '--server.address=127.0.0.1', f'--server.port={args.port}', '--server.headless=true']
    if args.check:
        print(json.dumps({'root': str(root), 'scope': REVIEW_SCOPE, 'command': command}))
        return 0
    return subprocess.call(command, cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
