import re
import sys
import glob
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_EXTS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.txt', '.md',
             '.bat', '.cmd', '.ps1', '.sh', '.yml', '.yaml', '.toml', '.ini',
             '.cfg', '.env', '.example', '.html', '.xml', '.php', '.gitignore',
             '.properties', '.config'}

PATTERNS = {
    'google_api_key': r'AIza[0-9A-Za-z_-]{35}',
    'aws_key_id': r'AKIA[0-9A-Z]{16}',
    'aws_tmp_key': r'(?:ASIA|A3T)[0-9A-Z]{14,16}',
    'telegram_bot_token': r'[0-9]{9,10}:[A-Za-z0-9_-]{35}',
    'stripe': r'sk_(?:live|test)_[0-9A-Za-z]{20,}',
    'github_token': r'gh[pousr]_[0-9A-Za-z]{36,}',
    'sendgrid': r'SG\.[0-9A-Za-z_-]{20,}',
    'slack': r'xox[baprs]-[0-9A-Za-z-]{10,}',
    'private_key': r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
}

EXCLUDE_DIRS = {'.git', '__pycache__', '.codegraph', 'data', '.atl', 'node_modules', 'venv'}

RED = '\033[91m'
GREEN = '\033[92m'
RESET = '\033[0m'


def main():
    hits = []
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            ext = os.path.splitext(name)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except OSError:
                continue
            for label, pattern in PATTERNS.items():
                for m in re.finditer(pattern, content):
                    line = content[:m.start()].count('\n') + 1
                    rel = os.path.relpath(path, BASE)
                    hits.append((rel, line, label))

    if not hits:
        print(f'{GREEN}[scan_secrets] Suelo limpio: 0 coincidencias de credenciales{RESET}')
        return 0

    print(f'{RED}[scan_secrets] {len(hits)} COINCIDENCIAS DE CREDENCIALES ENCONTRADAS{RESET}')
    for rel, line, label in hits:
        print(f'{RED}  {rel}:{line} -> {label}{RESET}')
    print(f'{RED}Corrige esos valores antes de commitear/pushear.{RESET}')
    return 1


if __name__ == '__main__':
    sys.exit(main())