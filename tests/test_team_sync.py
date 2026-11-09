from pathlib import Path
import tempfile
import unittest
from scripts.team_sync import git, read, send


class TeamSyncTests(unittest.TestCase):
    def test_mailboxes_preserve_dirty_worktree_and_record_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote, checkout = root / 'remote.git', root / 'checkout'
            git('init', '--bare', str(remote))
            git('clone', str(remote), str(checkout))
            git('config', 'user.name', 'Test Agent', cwd=checkout)
            git('config', 'user.email', 'test@example.invalid', cwd=checkout)
            (checkout / 'code.txt').write_text('initial', encoding='utf-8')
            git('add', 'code.txt', cwd=checkout)
            git('commit', '-m', 'test fixture', cwd=checkout)
            (checkout / 'code.txt').write_text('valuable uncommitted edit', encoding='utf-8')
            before = git('status', '--porcelain', cwd=checkout)
            head = git('rev-parse', 'HEAD', cwd=checkout)
            first = send('codex', 'Задача: проверить изоляцию', checkout)
            reply = send('roma', f'ACK {first}: принято', checkout)
            second = send('codex', f'ACK {reply}: спасибо', checkout)
            received_head, received_body = read('roma', checkout)
            self.assertEqual(received_head, reply)
            self.assertIn(first, received_body)
            self.assertEqual(read('codex', checkout)[0], second)
            self.assertEqual(git('rev-parse', f'{second}^', cwd=checkout), first)
            self.assertEqual(git('rev-parse', 'HEAD', cwd=checkout), head)
            self.assertEqual(git('status', '--porcelain', cwd=checkout), before)
            self.assertEqual((checkout / 'code.txt').read_text(), 'valuable uncommitted edit')
            self.assertEqual(git('ls-tree', '--name-only', second, cwd=checkout), 'MESSAGE.md')

    def test_invalid_messages_are_rejected_before_git(self):
        for body in (' ', 'x' * 32769):
            with self.assertRaises(ValueError):
                send('codex', body)
