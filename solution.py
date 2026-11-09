"""Platform entrypoint: DATA_DIR/questions.csv -> working-directory submission.csv.

Contract: once the questions are readable, submission.csv is always written with every
input ID and a non-empty answer. Configuration, knowledge-base, provider and per-question
failures degrade to an honest fallback answer instead of aborting the run.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import io
import logging
from pathlib import Path
import sys
import tempfile
import time
from zipfile import BadZipFile
from consultant import Consultant, extractive_answer
from model_client import ModelClient, ProviderError, Settings, env_number, env_value
from retrieval import HybridIndex, SearchIndex, load_archive

LOG = logging.getLogger('submission')
UNAVAILABLE = 'Сейчас не удалось получить ответ сервиса консультации. Попробуйте позже или обратитесь к специалисту EnergyTool.'
EMPTY_QUESTION = 'Уточните, пожалуйста, ваш вопрос: опишите объект и задачу, которую хотите решить.'
QUESTION_FILES = ('questions.csv', 'train.csv')
KB_FILES = ('knowledge-base.zip', 'knowledge_base.zip', 'kb.zip')
MAX_ANSWER = 10000
CHECKPOINT_SECONDS = 15.0
EMBED_BUILD_SECONDS = 120.0


def find_input(data_dir: Path, names: tuple[str, ...]) -> Path:
    """Look where the organizer sample looks: DATA_DIR, DATA_DIR next to the script, cwd, script dir."""
    here = Path(__file__).resolve().parent
    for root in (data_dir, here / data_dir, Path.cwd(), here):
        for name in names:
            if (root / name).is_file():
                return root / name
    raise FileNotFoundError(f'None of {names} found')


def read_questions(path: Path) -> list[tuple[str, str]]:
    """Tolerates BOM, blank lines before the header, `;`/tab delimiters, header case/spaces,
    column order and extra columns. IDs are echoed exactly as given (the platform matches
    them verbatim); rows without ID are skipped; the first of duplicate IDs is kept (output
    IDs must be unique); empty questions are kept and answered."""
    with path.open(encoding='utf-8-sig', newline='') as stream:
        text = stream.read().lstrip('\r\n')
    first_line = text.split('\n', 1)[0]
    delimiter = max(',;\t', key=first_line.count) if any(d in first_line for d in ',;\t') else ','
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
    header = [h.strip().lower() for h in next(reader, [])]
    if len(header) < 2:
        raise ValueError('Input needs id and question columns')
    id_col = header.index('id') if 'id' in header else 0
    q_col = header.index('question') if 'question' in header else (1 if id_col != 1 else 0)
    rows, seen = [], set()
    for record in reader:
        identifier = record[id_col] if len(record) > id_col else ''
        if not identifier.strip():
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
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:  # Windows: a reader briefly holds the old file
                if attempt == 4:
                    raise
                time.sleep(.05)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def grounded_fallback(lexical: SearchIndex, question: str) -> str:
    try:
        return clean(extractive_answer(lexical, question))
    except Exception as error:  # fallback must never fail the run
        LOG.error('Extractive fallback failed (%s)', type(error).__name__)
        return UNAVAILABLE


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
    core = lexical = None
    try:
        lexical = SearchIndex(load_archive(str(find_input(data_dir, KB_FILES))))
    except (OSError, ValueError, BadZipFile) as error:
        LOG.error('Knowledge base unavailable (%s); writing fallback answers', type(error).__name__)
    if lexical is not None:
        # Base-grounded fallbacks replace 'service unavailable' before any model call.
        answers = [EMPTY_QUESTION if not q.strip() else grounded_fallback(lexical, q) for _, q in questions]
        write_submission(output, [(qid, answers[i]) for i, (qid, _) in enumerate(questions)])
        if client is not None:
            # Dense index build is bounded so a slow embedder cannot eat the answering budget.
            index = HybridIndex.build(lexical, client, min(deadline, time.monotonic() + EMBED_BUILD_SECONDS))
            LOG.info('retrieval=%s rerank=%s chunks=%d', 'hybrid' if index.dense else 'bm25', index.rerank_enabled, len(index.chunks))
            core = Consultant(index, client)
    pending = [i for i, (_, q) in enumerate(questions) if q.strip()]
    failures = len(pending)
    def save():
        write_submission(output, [(qid, answers[i]) for i, (qid, _) in enumerate(questions)])

    if core is not None and pending:
        failures = 0
        # Checkpoint successful answers so a platform timeout keeps finished work.
        every = max(1, len(pending) // 20)
        done = saved_at = 0
        last_save = time.monotonic()
        with ThreadPoolExecutor(max_workers=settings.workers if settings else 1) as pool:
            futures = {pool.submit(core.answer, questions[i][1], deadline): i for i in pending}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    result = future.result()
                    answers[row] = clean(result.answer)
                    LOG.info('row=%d elapsed_ms=%d sources=%d', row + 1, result.elapsed_ms, len(result.sources))
                except Exception as error:  # one question must never cost the whole submission
                    failures += 1  # the row keeps its base-grounded fallback
                    # ProviderError text is designed to be safe; other exceptions may carry payloads.
                    LOG.error('row=%d %s', row + 1, error if isinstance(error, ProviderError) else type(error).__name__)
                done += 1
                if done - saved_at >= every or time.monotonic() - last_save >= CHECKPOINT_SECONDS:
                    save()
                    saved_at, last_save = done, time.monotonic()
    save()
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
