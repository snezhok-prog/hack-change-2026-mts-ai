"""Offline regression probes; all HTTP uses MockTransport and synthetic KB only.
Run: .venv/Scripts/python.exe docs/notes/reproduce-runtime-findings.py
Exit 0: probes executed (PASS/XFAIL); 1: unexpected failure; 2: harness error.
"""
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from consultant import Consultant
from model_client import ModelClient, ProviderError, Settings
from retrieval import Chunk, HybridIndex, SearchIndex
import solution

RESULTS = []

def reply(text, reason='stop'):
    return httpx.Response(200, json={'choices': [{'finish_reason': reason, 'message': {'content': text}}]})

def settings(**kwargs):
    return Settings('https://synthetic.invalid/v1', 'synthetic-only-key', 'synthetic-model', **kwargs)

def record(name, probe, known_defect=False):
    started = time.monotonic()
    try:
        checks, evidence = probe()
        passed = all(checks.values())
        status = ('XPASS' if known_defect else 'PASS') if passed else ('XFAIL' if known_defect else 'FAIL')
        item = dict(name=name, status=status, checks=checks, evidence=evidence)
    except Exception as error:
        item = dict(name=name, status='HARNESS_ERROR', error_type=type(error).__name__, error=str(error))
    item['elapsed_ms'] = round((time.monotonic()-started)*1000)
    RESULTS.append(item)
    print(name, item['status'])


def truncated_final():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return reply('Стоимость действует только при', 'length') if len(calls) == 1 else reply('Полный безопасный ответ.')
    client = ModelClient(settings(), transport=httpx.MockTransport(handler))
    try:
        answer = client.generate([{'role':'user','content':'Синтетический вопрос'}], time.monotonic()+3)
        return {'complete_recovery_answer': answer == 'Полный безопасный ответ.', 'exactly_one_recovery': len(calls) == 2}, {'answer':answer,'calls':len(calls),'tokens':[x['max_tokens'] for x in calls]}
    finally:
        client.close()


def reasoning_recovery(reject_extra=False):
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if reject_extra and len(calls) == 1:
            return httpx.Response(422, json={'detail':'synthetic unsupported field'})
        if len(calls) == (2 if reject_extra else 1):
            return reply('<think>Незаконченное рассуждение', 'length')
        return reply('<think>Готово</think>Проверенный синтетический ответ.')
    client = ModelClient(settings(max_tokens=300), transport=httpx.MockTransport(handler))
    try:
        answer = client.generate([{'role':'user','content':'Синтетический вопрос'}],time.monotonic()+3)
        return {'recovered_final':answer=='Проверенный синтетический ответ.', 'bounded_calls':len(calls)==(3 if reject_extra else 2), 'grew_budget':calls[-1]['max_tokens']>calls[-2]['max_tokens']}, {'answer':answer,'calls':len(calls),'tokens':[x['max_tokens'] for x in calls],'extra_fields':['chat_template_kwargs' in x for x in calls]}
    finally:
        client.close()


def optional_deadline(stage):
    calls=[]
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/embeddings'):
            if stage=='embedding':
                await asyncio.sleep(2)
            return httpx.Response(200,json={'data':[{'index':0,'embedding':[1.0]}]})
        if request.url.path.endswith('/rerank'):
            await asyncio.sleep(2)
            return httpx.Response(200,json={'results':[{'index':0,'relevance_score':1.0}]})
        await asyncio.sleep(.05)  # nonzero generation latency avoids sub-tick Windows timer artifacts
        return reply('Синтетический ответ модели.')
    client=ModelClient(settings(emb_model='fixture',reranker_model='fixture' if stage=='rerank' else '',timeout=10),transport=httpx.MockTransport(handler))
    lexical=SearchIndex([Chunk('fixture.md','Синтетический прибор','Синтетический прибор измеряет воду.')])
    index=HybridIndex(lexical,client,[[1.0]],rerank=stage=='rerank')
    try:
        error=None
        try:
            answer=Consultant(index,client).answer('Синтетический прибор?',time.monotonic()+.3).answer
        except ProviderError as exc:
            answer=None
            error=str(exc)
        actual_calls=list(calls)
        control=Consultant(lexical,client).answer('Синтетический прибор?',time.monotonic()+1).answer
        return {'generation_reached':any(x.endswith('/chat/completions') for x in actual_calls),'answer_obtained':answer=='Синтетический ответ модели.','lexical_control_passed':control=='Синтетический ответ модели.'}, {'answer':answer,'provider_error':error,'calls_before_control':actual_calls,'control_answer':control,'budget_seconds':.3,'mock_optional_delay_seconds':2,'mock_generation_delay_seconds':.05}
    finally:
        client.close()


def fallback_csv(mode):
    # Fictional facts deliberately put the mandatory price condition in sentence 3.
    kb=('# Синтетический прибор Альфа\n'
        'Синтетический прибор Альфа измеряет воду. Стоимость составляет 123 рубля. '
        'Эта стоимость действует только при уже установленных совместимых приборах.')
    question=('Пользователь: Сколько стоит синтетический прибор Альфа?\n'
              'Пользователь: Контакты оставлять не буду, заявку не предлагайте. Ответьте здесь.')
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401,json={'detail':'synthetic unauthorized'})
    config=settings() if mode=='http401' else None
    client=ModelClient(config,transport=httpx.MockTransport(handler)) if config else None
    try:
        with tempfile.TemporaryDirectory(prefix='runtime-regression-') as tmp:
            folder=Path(tmp)
            with ZipFile(folder/'knowledge-base.zip','w') as archive:
                archive.writestr('synthetic.md',kb)
            with (folder/'questions.csv').open('w',encoding='utf-8',newline='') as stream:
                csv.writer(stream).writerows([['id','question'],['007',question],['empty',' ']])
            failures=solution.run(folder,folder/'submission.csv',config,client,budget=2)
            with (folder/'submission.csv').open(encoding='utf-8',newline='') as stream:
                reader=csv.DictReader(stream)
                headers=reader.fieldnames
                rows=list(reader)
            answer=rows[0]['answer']
        return {'csv_contract':headers==['id','answer'] and [r['id'] for r in rows]==['007','empty'] and all(r['answer'].strip() for r in rows), 'fallback_count_honest':failures==1, 'price_condition_preserved_or_price_omitted':'123' not in answer or 'только при уже установленных совместимых приборах' in answer, 'known_application_offer_absent':'оставьте заявку' not in answer.lower()}, {'mode':mode,'answer':answer,'rows':rows,'fallback_count':failures,'calls':calls,'fixture_price_condition_sentence':3}
    finally:
        if client:
            client.close()


def main():
    (ROOT / 'work').mkdir(exist_ok=True)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    core_commit=subprocess.check_output(['git','log','-1','--format=%H','--','consultant.py','model_client.py','solution.py','retrieval.py'],cwd=ROOT,text=True).strip()
    record('length_partial_final',truncated_final,known_defect=True)
    record('reasoning_recovery',reasoning_recovery)
    record('rejected_parameter_then_reasoning_recovery',lambda:reasoning_recovery(True))
    record('embedding_generation_reserve',lambda:optional_deadline('embedding'),known_defect=True)
    record('reranker_generation_reserve',lambda:optional_deadline('rerank'),known_defect=True)
    record('fallback_csv_no_api_key',lambda:fallback_csv('no_api_key'),known_defect=True)
    record('fallback_csv_http401',lambda:fallback_csv('http401'),known_defect=True)
    report={'created_utc':datetime.now(timezone.utc).isoformat(),'head':head,'latest_core_commit':core_commit,'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['solution.py','model_client.py','consultant.py','retrieval.py']},'network':'httpx.MockTransport only; no remote calls; no .env access','command':'.venv/Scripts/python.exe docs/notes/reproduce-runtime-findings.py','status_meanings':{'PASS':'Expected healthy behavior observed','XFAIL':'Known product defect reproduced; probe itself executed successfully','XPASS':'Previously known defect no longer reproduced','FAIL':'Unexpected failed behavior check','HARNESS_ERROR':'Probe execution error; not evidence of a product defect'},'cases':RESULTS}
    (ROOT/'work/runtime-review-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# Offline runtime regression results', '', f'HEAD: `{head}`; latest core commit: `{core_commit}`.', '', 'Run: `.venv/Scripts/python.exe docs/notes/reproduce-runtime-findings.py`.', '', 'All data and responses are synthetic. No .env read, remote request, core edit, submission or container mutation.', '', '| Scenario | Status | Failed checks |', '|---|---|---|']
    for result in RESULTS:
        failed=', '.join(k for k,v in result.get('checks',{}).items() if not v)
        lines.append(f"| {result['name']} | {result['status']} | {failed or result.get('error','none')} |")
    lines.extend(['', 'XFAIL denotes a reproduced defect, not a harness failure. XPASS indicates recovery of a previously failing scenario. No real-model quality is measured.'])
    (ROOT/'work/runtime-review-results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    if any(x['status']=='HARNESS_ERROR' for x in RESULTS):return 2
    return int(any(x['status']=='FAIL' for x in RESULTS))

if __name__=='__main__':
    raise SystemExit(main())

