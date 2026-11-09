"""SSH-only team mailbox. Never switches branches or touches the worktree/index."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys


def git(*args: str, text: str | None = None, cwd=None) -> str:
    result = subprocess.run(['git', *args], input=text.encode('utf-8') if text is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    if result.returncode:
        raise RuntimeError(f'git {args[0]} failed: {result.stderr.decode("utf-8", errors="replace").strip()}')
    return result.stdout.decode('utf-8').strip()


def fetch_head(sender: str, cwd=None) -> str | None:
    ref = f'refs/heads/coordination/{sender}'
    remote = git('ls-remote', '--heads', 'origin', ref, cwd=cwd)
    if not remote:
        return None
    tracking = f'refs/remotes/origin/coordination/{sender}'
    git('fetch', '--quiet', 'origin', f'{ref}:{tracking}', cwd=cwd)
    return git('rev-parse', tracking, cwd=cwd)


def read(sender: str, cwd=None) -> tuple[str, str]:
    head = fetch_head(sender, cwd)
    if head is None:
        raise RuntimeError(f'No message from {sender} yet')
    return head, git('show', f'{head}:MESSAGE.md', cwd=cwd)


def send(sender: str, body: str, cwd=None) -> str:
    if not body.strip() or len(body.encode('utf-8')) > 32768:
        raise ValueError('Message must be nonempty and at most 32 KiB')
    previous = fetch_head(sender, cwd)
    message = f'# From: {sender}\n\nUTC: {datetime.now(timezone.utc).isoformat()}\n\n{body.strip()}\n'
    blob = git('hash-object', '-w', '--stdin', text=message, cwd=cwd)
    tree = git('mktree', text=f'100644 blob {blob}\tMESSAGE.md\n', cwd=cwd)
    parent = ['-p', previous] if previous else []
    commit = git('commit-tree', tree, *parent, '-m', f'coordination: message from {sender}', cwd=cwd)
    # A concurrent writer causes a non-fast-forward rejection; never force-push.
    git('push', 'origin', f'{commit}:refs/heads/coordination/{sender}', cwd=cwd)
    return commit


def main() -> int:
    # Keep Russian messages readable when redirected by Windows terminals/tools.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    reader = sub.add_parser('read')
    reader.add_argument('sender', choices=['codex', 'roma'])
    writer = sub.add_parser('send')
    writer.add_argument('sender', choices=['codex', 'roma'])
    writer.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'read':
            head, message = read(args.sender)
            print(f'Commit: {head}\n\n{message}')
        else:
            print('Published:', send(args.sender, args.file.read_text(encoding='utf-8-sig')))
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
