"""Platform entrypoint: DATA_DIR/questions.csv -> working-directory submission.csv."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from zipfile import BadZipFile
from consultant import Consultant
from model_client import ModelClient, ProviderError, Settings
from retrieval import SearchIndex, load_archive

LOG = logging.getLogger('submission')
UNAVAILABLE = 'Сейчас не удалось получить ответ сервиса консультации. Попробуйте позже или обратитесь к специалисту EnergyTool.'


def read_questions(path: Path) -> list[tuple[str, str]]:
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['id', 'question']:
            raise ValueError('Input columns must be id,question')
        rows = []
        seen = set()
        for row in reader:
            identifier, question = row.get('id'), row.get('question')
            if None in row or not identifier or not identifier.strip() or not question or not question.strip():
                raise ValueError('Malformed or empty input row')
            if identifier in seen:
                raise ValueError('Duplicate input ID')
            if len(question) > 60000:
                raise ValueError('Question exceeds supported length')
            rows.append((identifier, question))
            seen.add(identifier)
    if not rows:
        raise ValueError('Input contains no questions')
    return rows


def write_submission(path: Path, rows: list[tuple[str, str]]) -> None:
    if not rows or len({i for i, _ in rows}) != len(rows):
        raise ValueError('Output must contain unique IDs')
    if any(not i.strip() or not a.strip() or len(a) > 10000 for i, a in rows):
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


def run(data_dir: Path, output: Path, settings: Settings, client: ModelClient, budget: float = 1100) -> int:
    if not math.isfinite(budget) or not 0 < budget <= 1150:
        raise ValueError('RUN_BUDGET_SECONDS must be between 0 and 1150')
    deadline = time.monotonic() + budget
    questions = read_questions(data_dir / 'questions.csv')
    core = Consultant(SearchIndex(load_archive(str(data_dir / 'knowledge-base.zip'))), client)
    answers = [''] * len(questions)
    failures = 0
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        futures = {pool.submit(core.answer, question, deadline): i for i, (_, question) in enumerate(questions)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
                answers[index] = result.answer
                LOG.info('row=%d elapsed_ms=%d sources=%d', index + 1, result.elapsed_ms, len(result.sources))
            except ProviderError as error:
                failures += 1
                LOG.error('row=%d %s', index + 1, error)
                answers[index] = UNAVAILABLE
    write_submission(output, [(qid, answers[i]) for i, (qid, _) in enumerate(questions)])
    LOG.info('rows=%d provider_failures=%d', len(questions), failures)
    return failures


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    client = None
    try:
        settings = Settings.from_env()
        client = ModelClient(settings)
        failures = run(Path(os.environ.get('DATA_DIR', 'data')), Path('submission.csv'), settings, client,
                       float(os.environ.get('RUN_BUDGET_SECONDS', '1100')))
        return 2 if failures else 0
    except (ValueError, OSError, BadZipFile) as error:
        # Do not include arbitrary file contents, request URLs or environment values.
        LOG.error('Input/configuration error (%s). Check README and local inputs.', type(error).__name__)
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == '__main__':
    sys.exit(main())
