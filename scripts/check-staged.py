"""Check the Git index without changing the index or unstaged working files."""

import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'],
                                        text=True).strip())
    os.chdir(root)
    changed = subprocess.check_output([
        'git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z',
    ])
    names = [os.fsdecode(name) for name in changed.split(b'\0') if name]
    private_names = {'.lighting.json', '.lighting-stream.json', '.sync-box.json', '.env'}
    for name in names:
        path = Path(name)
        if (path.name in private_names or name.startswith('bridge/state/')
                or name.endswith(('.pem', '.key'))):
            print('Private configuration cannot be committed: ' + name, file=sys.stderr)
            return 1
    subprocess.run(['git', 'diff', '--cached', '--check'], check=True)
    python_files = [name for name in names if name.endswith('.py')]
    javascript_files = [name for name in names if name.endswith(('.js', '.mjs', '.cjs'))]
    if not python_files and not javascript_files:
        return 0
    with TemporaryDirectory(prefix='tmp_staged_checks_') as temporary:
        snapshot = Path(temporary)
        subprocess.run(['git', 'checkout-index', '--all', '--prefix', str(snapshot) + os.sep],
                       check=True)
        if python_files:
            requirement = (snapshot / 'requirements-dev.txt').read_text().strip()
            if not re.fullmatch(r'ruff==\d+\.\d+\.\d+', requirement):
                raise ValueError('Expected one pinned Ruff development requirement')
            env = dict(os.environ)
            env.setdefault('UV_CACHE_DIR', str(root / '.ruff_cache/uv'))
            env.setdefault('UV_TOOL_DIR', str(root / '.ruff_cache/tools'))
            subprocess.run(['uvx', '--offline', '--from', requirement, 'ruff', 'check',
                            '--force-exclude', '--', *python_files], cwd=snapshot,
                           env=env, check=True)
        for name in javascript_files:
            subprocess.run(['node', '--check', str(snapshot / name)], check=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except FileNotFoundError:
        print('Development tools are missing; run sh scripts/setup-dev.sh.', file=sys.stderr)
        raise SystemExit(1)
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        if not isinstance(error, subprocess.CalledProcessError):
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
