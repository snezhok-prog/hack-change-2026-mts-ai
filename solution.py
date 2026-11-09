"""Platform entrypoint: DATA_DIR/questions.csv -> working-directory submission.csv.

Contract: once the questions are readable, submission.csv is always written with every
input ID and a non-empty answer. Configuration, knowledge-base, provider and per-question
failures degrade to an honest fallback answer instead of aborting the run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import logging
from pathlib import Path
import sys
import tempfile
import time
from zipfile import BadZipFile
from consultant import Consultant
from model_client import ModelClient, Settings, env_number, env_value
from retrieval import SearchIndex, load_archive

LOG = logging.getLogger('submission')
UNAVAILABLE = 'Сейчас не удалось получить ответ сервиса консультации. Попробуйте позже или обратитесь к специалисту EnergyTool.'
EMPTY_QUESTION = 'Уточните, пожалуйста, ваш вопрос: опишите объект и задачу, которую хотите решить.'
QUESTION_FILES = ('questions.csv', 'train.csv')
KB_FILES = ('knowledge-base.zip', 'knowledge_base.zip', 'kb.zip')
MAX_ANSWER = 10000


def find_input(data_dir: Path, names: tuple[str, ...]) -> Path:
    """Look where the organizer sample looks: DATA_DIR, DATA_DIR next to the script, cwd, script dir."""
    here = Path(__file__).resolve().parent
    for root in (data_dir, here / data_dir, Path.cwd(), here):
        for name in names:
            if (root / name).is_file():
                return root / name
    raise FileNotFoundError(f'None of {names} found')


def read_questions(path: Path) -> list[tuple[str, str]]:
    """Tolerates BOM, header case/spaces and extra columns; skips rows without ID; keeps the
    first of duplicate IDs (output IDs must be unique); empty questions are kept and answered."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        header = [h.strip().lower() for h in next(reader, [])]
        if len(header) < 2:
            raise ValueError('Input needs id and question columns')
        id_col = header.index('id') if 'id' in header else 0
        q_col = header.index('question') if 'question' in header else (1 if id_col != 1 else 0)
        rows, seen = [], set()
        for record in reader:
            identifier = record[id_col].strip() if len(record) > id_col else ''
            if not identifier:
                continue
            if identifier in seen:
                LOG.warning('Duplicate input ID skipped')
                continue
            seen.add(identifier)
            rows.append((identifier, record[q_col] if len(record) > q_col else ''))
    if not rows:
        raise ValueError('Input contains no questions')
    return rows


def write_submission(path: Path, rows: list[tuple[str, str]]) -> None:
    if not rows or len({i for i, _ in rows}) != len(rows):
        raise ValueError('Output must contain unique IDs')
    if any(not i.strip() or not a.strip() or len(a) > MAX_ANSWER for i, a in rows):
        raise ValueError('Invalid output value')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.writer(stream)
            writer.writerow(['id', 'answer'])
            writer.writerows(rows)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def clean(answer: str) -> str:
    answer = (answer or '').strip()
    return answer[:MAX_ANSWER].strip() if answer else UNAVAILABLE


def run(data_dir: Path, output: Path, settings: Settings | None, client: ModelClient | None, budget: float = 1100) -> int:
    """Returns the number of questions answered with a fallback. Raises only if questions are unreadable."""
    deadline = time.monotonic() + max(1.0, min(budget, 1150.0))
    questions = read_questions(find_input(data_dir, QUESTION_FILES))
    answers = [EMPTY_QUESTION if not q.strip() else UNAVAILABLE for _, q in questions]
    # Complete file first: a timeout kill or later crash still leaves every ID answered.
    write_submission(output, [(qid, answers[i]) for i, (qid, _) in enumerate(questions)])
    core = None
    if client is not None:
        try:
            core = Consultant(SearchIndex(load_archive(str(find_input(data_dir, KB_FILES)))), client)
        except (OSError, ValueError, BadZipFile) as error:
            LOG.error('Knowledge base unavailable (%s); writing fallback answers', type(error).__name__)
    pending = [i for i, (_, q) in enumerate(questions) if q.strip()]
    failures = len(pending)
    if core is not None and pending:
        failures = 0
        with ThreadPoolExecutor(max_workers=settings.workers if settings else 1) as pool:
            futures = {pool.submit(core.answer, questions[i][1], deadline): i for i in pending}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    result = future.result()
                    answers[index] = clean(result.answer)
                    LOG.info('row=%d elapsed_ms=%d sources=%d', index + 1, result.elapsed_ms, len(result.sources))
                except Exception as error:  # one question must never cost the whole submission
                    failures += 1
                    LOG.error('row=%d %s: %s', index + 1, type(error).__name__, error)
    write_submission(output, [(qid, answers[i]) for i, (qid, _) in enumerate(questions)])
    LOG.info('rows=%d fallback_answers=%d', len(questions), failures)
    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    data_dir = Path(env_value('DATA_DIR') or 'data')
    settings = client = None
    try:
        settings = Settings.from_env()
        client = ModelClient(settings)
    except ValueError as error:
        # Do not include environment values; still produce a complete CSV below.
        LOG.error('Model configuration error (%s): %s', type(error).__name__, error)
    try:
        failures = run(data_dir, Path('submission.csv'), settings, client, env_number('RUN_BUDGET_SECONDS', 1100.0, 1.0, 1150.0, float))
        if failures:
            LOG.error('%d answers are fallbacks, not consultations', failures)
        return 0
    except (ValueError, OSError) as error:
        LOG.error('Cannot read questions (%s); submission.csv not written', type(error).__name__)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
