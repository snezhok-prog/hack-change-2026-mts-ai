"""Build a deterministic, code-only ZIP for the organizer's root-file contract."""
import argparse
import hashlib
from pathlib import Path
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = ('solution.py', 'consultant.py', 'model_client.py', 'retrieval.py', 'requirements.txt')


def build(destination: Path, root: Path = ROOT) -> str:
    if destination.suffix.lower() != '.zip':
        raise ValueError('Destination must end in .zip')
    sources = []
    for name in FILES:
        source = root / name
        if source.is_symlink() or source.resolve().parent != root.resolve():
            raise ValueError(f'Unexpected source location: {name}')
        content = source.read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        if name.endswith('.py'):
            compile(content, name, 'exec')
        sources.append((name, content.encode('utf-8')))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.zip', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as archive:
            for name, content in sources:
                info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, content)
        if temporary.stat().st_size > 128 * 1024 * 1024:
            raise ValueError('Submission exceeds 128 MiB')
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return digest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/submission.zip')
    args = parser.parse_args()
    digest = build(args.output)
    print(f'ZIP: {args.output.resolve()}\nSHA256: {digest}')
