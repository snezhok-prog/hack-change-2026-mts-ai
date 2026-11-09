"""Local quality evaluation of the consultant (dev only, not part of the submission ZIP).

Mirrors the organizer's LLM-as-judge scheme: every question is judged twice with the
answer order swapped; per-question score is the mean of both rounds on the
1 / .75 / .5 / .25 / 0 scale; Score = 0.7 * Match Rate + 0.3 * Win Rate.

    python eval.py run   --out work/eval/v1.csv
    python eval.py judge --answers work/eval/v1.csv [--baseline work/eval/v0.csv]

Without --baseline the team answer is compared with the golden answer itself, which
gives an absolute "how close to the reference" estimate. With --baseline two bot
versions are compared against the golden reference, exactly like the platform does.
Every model call is paid: run only with the project lead's permission.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import dataclasses
import json
import os
from pathlib import Path
import re
import sys
import time
from consultant import Consultant
from model_client import ModelClient, ProviderError, Settings
from retrieval import SearchIndex, load_archive
from solution import read_questions

DEFAULT_DATA = Path('data/org')
RUN_BUDGET = 1100.0
VERDICTS = ('A>>B', 'A>B', 'A=B', 'B>A', 'B>>A')
# Score of answer B for each verdict; for answer A the scale is mirrored.
B_SCORE = {'A>>B': 0.0, 'A>B': 0.25, 'A=B': 0.5, 'B>A': 0.75, 'B>>A': 1.0}
THINK = re.compile(r'<think>.*?(</think>|$)', re.S | re.I)
CITATION = re.compile(r'\[\d+(?:\s*,\s*\d+)*\]')

JUDGE_SYSTEM = '''Ты — строгий эксперт, оценивающий ответы ИИ-консультанта МТС EnergyTool
для клиентов сайта. Тебе дан вопрос (или история диалога), эталонный ответ и два ответа: A и B.
Эталон — образец верных фактов и желаемого поведения консультанта.

Сравни A и B по критериям, в порядке важности:
1. Фактическая точность относительно эталона: нет выдуманных цен, сроков, обещаний и
   возможностей продукта; нет противоречий эталону.
2. Полезность: прямой ответ на последнюю реплику пользователя с учётом его ситуации.
3. Поведение консультанта: уместный уточняющий вопрос, честный отказ, если информации
   нет; не навязывает контакты, если клиент не хочет их оставлять.
4. Ясность и краткость: простой язык, обычно до 150 слов, без лишнего.

Не отдавай предпочтение ответу из-за его позиции или длины.
Выведи ровно одну метку и ничего больше:
A>>B — A значительно лучше; A>B — A немного лучше; A=B — сопоставимы;
B>A — B немного лучше; B>>A — B значительно лучше.'''


def load_dotenv(path: Path = Path('.env')) -> None:
    """Minimal .env reader for local runs; never overrides real environment, never prints values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


def read_answers(path: Path) -> dict[str, str]:
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or reader.fieldnames[:2] != ['id', 'answer']:
            raise ValueError(f'{path}: expected columns id,answer')
        return {row['id']: row['answer'] for row in reader}


def strip_think(text: str) -> str:
    return THINK.sub('', text).strip()


def static_checks(answer: str) -> dict:
    """Cheap format signals that matter to the judge; no model calls."""
    visible = strip_think(answer)
    return {
        'words': len(re.findall(r'\w+', visible)),
        'over_150_words': len(re.findall(r'\w+', visible)) > 150,
        'think_leak': '<think' in answer.lower(),
        'citations': bool(CITATION.search(visible)),
        'questions': visible.count('?'),
        'markdown': bool(re.search(r'(^|\n)\s*(#{1,6}\s|[-*]\s|\d+\.\s)|\*\*', visible)),
    }


def parse_verdict(text: str) -> str | None:
    """Exactly one label must be present; none of the labels is a substring of another."""
    visible = strip_think(text).replace(' ', '').replace('＞', '>')
    found = [v for v in VERDICTS if v in visible]
    return found[0] if len(found) == 1 else None


def round_scores(first: str, second: str) -> float:
    """first: round 1 (A=baseline, B=team); second: round 2 (A=team, B=baseline)."""
    return (B_SCORE[first] + (1.0 - B_SCORE[second])) / 2


def metrics(scores: list[float]) -> dict:
    if not scores:
        return {'judged': 0, 'match_rate': 0.0, 'win_rate': 0.5, 'score': 0.0, 'wins': 0, 'losses': 0, 'ties': 0}
    wins = sum(s > 0.5 for s in scores)
    losses = sum(s < 0.5 for s in scores)
    match = sum(scores) / len(scores)
    win = wins / (wins + losses) if wins + losses else 0.5
    return {'judged': len(scores), 'match_rate': round(match, 4), 'win_rate': round(win, 4),
            'score': round(0.7 * match + 0.3 * win, 4), 'wins': wins, 'losses': losses,
            'ties': len(scores) - wins - losses}


def judge_once(client: ModelClient, question: str, golden: str, a: str, b: str, deadline: float) -> str | None:
    content = (f'ВОПРОС ИЛИ ДИАЛОГ:\n{question}\n\nЭТАЛОННЫЙ ОТВЕТ:\n{golden}\n\n'
               f'ОТВЕТ A:\n{strip_think(a)}\n\nОТВЕТ B:\n{strip_think(b)}\n\nМетка:')
    raw = client.generate([{'role': 'system', 'content': JUDGE_SYSTEM}, {'role': 'user', 'content': content}], deadline)
    return parse_verdict(raw)


def judge(questions: list[tuple[str, str]], team: dict[str, str], golden: dict[str, str],
          baseline: dict[str, str], client: ModelClient, deadline: float) -> list[dict]:
    def one(item):
        qid, question = item
        row = {'id': qid, 'checks': static_checks(team.get(qid, ''))}
        if not team.get(qid, '').strip() or qid not in golden or qid not in baseline:
            return {**row, 'error': 'missing answer, golden or baseline'}
        try:
            first = judge_once(client, question, golden[qid], baseline[qid], team[qid], deadline)
            second = judge_once(client, question, golden[qid], team[qid], baseline[qid], deadline)
        except ProviderError as error:
            return {**row, 'error': str(error)}
        if first is None or second is None:
            return {**row, 'error': 'unparseable verdict', 'verdicts': [first, second]}
        return {**row, 'verdicts': [first, second], 'score': round_scores(first, second)}

    with ThreadPoolExecutor(max_workers=client.settings.workers) as pool:
        return list(pool.map(one, questions))


def run_answers(questions: list[tuple[str, str]], core: Consultant, workers: int, deadline: float) -> list[dict]:
    def one(item):
        qid, question = item
        try:
            result = core.answer(question, deadline)
        except ProviderError as error:
            return {'id': qid, 'answer': '', 'error': str(error)}
        return {'id': qid, 'answer': result.answer, 'elapsed_ms': result.elapsed_ms,
                'sources': [f"{s['document']} :: {s['heading']}" for s in result.sources]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, questions))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'answer'])
        writer.writerows((r['id'], r['answer']) for r in rows)


def summarize(rows: list[dict]) -> dict:
    checks = [r['checks'] for r in rows]
    result = metrics([r['score'] for r in rows if 'score' in r])
    result['errors'] = sum('error' in r for r in rows)
    for key in ('over_150_words', 'think_leak', 'citations', 'markdown'):
        result[key] = sum(c[key] for c in checks)
    result['avg_words'] = round(sum(c['words'] for c in checks) / len(checks), 1) if checks else 0
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    run_p = sub.add_parser('run', help='answer demo questions with the current core')
    run_p.add_argument('--questions', type=Path, default=DEFAULT_DATA / 'demo_questions.csv')
    run_p.add_argument('--kb', type=Path, default=DEFAULT_DATA / 'knowledge-base.zip')
    run_p.add_argument('--out', type=Path, required=True)
    judge_p = sub.add_parser('judge', help='pairwise LLM judge, organizer methodology')
    judge_p.add_argument('--answers', type=Path, required=True)
    judge_p.add_argument('--baseline', type=Path, help='previous version answers; default: golden')
    judge_p.add_argument('--questions', type=Path, default=DEFAULT_DATA / 'demo_questions.csv')
    judge_p.add_argument('--golden', type=Path, default=DEFAULT_DATA / 'demo_output.csv')
    judge_p.add_argument('--report', type=Path, help='JSON report path; default: next to answers')
    args = parser.parse_args(argv)

    load_dotenv()
    settings = Settings.from_env()
    deadline = time.monotonic() + RUN_BUDGET
    questions = read_questions(args.questions)

    if args.command == 'run':
        core_client = ModelClient(settings)
        try:
            core = Consultant(SearchIndex(load_archive(str(args.kb))), core_client)
            started = time.monotonic()
            rows = run_answers(questions, core, settings.workers, deadline)
        finally:
            core_client.close()
        write_csv(args.out, rows)
        details = args.out.with_suffix('.details.json')
        details.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        times = sorted(r['elapsed_ms'] for r in rows if 'elapsed_ms' in r)
        print(json.dumps({'answers': len(rows), 'errors': sum('error' in r for r in rows),
                          'wall_s': round(time.monotonic() - started, 1),
                          'p50_ms': times[len(times) // 2] if times else None,
                          'max_ms': times[-1] if times else None,
                          'out': str(args.out), 'details': str(details)}, ensure_ascii=False, indent=1))
        return 0

    team, golden = read_answers(args.answers), read_answers(args.golden)
    baseline = read_answers(args.baseline) if args.baseline else golden
    judge_model = os.environ.get('JUDGE_MODEL', '').strip()
    judge_client = ModelClient(dataclasses.replace(settings, model=judge_model) if judge_model else settings)
    try:
        rows = judge(questions, team, golden, baseline, judge_client, deadline)
    finally:
        judge_client.close()
    summary = summarize(rows)
    summary['baseline'] = str(args.baseline) if args.baseline else 'golden'
    report = args.report or args.answers.with_suffix('.judge.json')
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({'summary': summary, 'rows': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
    for r in rows:
        mark = r.get('error') or f"{r['score']:.3f} {'/'.join(r['verdicts'])}"
        print(f"{r['id']:>4}  {mark}  words={r['checks']['words']}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f'report: {report}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
