"""Independent web API QA. Synthetic cores only; never contacts an LLM."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import subprocess
import sys
import json
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from consultant import Consultation
from model_client import ProviderError
from web import create_app


LEAD = {"name": "Тестов Тест Тестович", "phone": "+7 (000) 000-00-00",
        "company": "ООО Учебный склад", "test_data_confirmed": True}


class ProbeCore:
    def __init__(self):
        self.dialogues = []
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.three_entered = threading.Event()
        self.active = self.peak = 0
        self.block = False
        self.fail = False

    def answer(self, dialogue, deadline):
        with self.lock:
            self.dialogues.append(dialogue)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.entered.set()
            if self.active == 3:
                self.three_entered.set()
        try:
            if self.block and not self.release.wait(5):
                raise RuntimeError("Synthetic test gate timed out")
            if self.fail:
                raise ProviderError("synthetic-private-provider-detail")
            return Consultation("Синтетический ответ.", (), 7)
        finally:
            with self.lock:
                self.active -= 1


class WebIndependentQA(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "state.sqlite"
        self.core = ProbeCore()
        self.client = self.enterContext(TestClient(create_app(self.core, self.db, "qa-admin")))

    def session(self):
        response = self.client.post("/api/sessions")
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def send(self, sid, text):
        return self.client.post(f"/api/sessions/{sid}/messages", json={"text": text})

    def detail(self, sid):
        response = self.client.get(f"/api/sessions/{sid}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_interleaved_sessions_never_share_dialogue(self):
        a, b = self.session(), self.session()
        for sid, text in [(a,"ALPHA-private"),(b,"BETA-private"),(a,"ALPHA-next"),(b,"BETA-next")]:
            self.assertEqual(self.send(sid,text).status_code,200)
        self.assertNotIn("BETA-private", self.core.dialogues[2])
        self.assertNotIn("ALPHA-private", self.core.dialogues[3])
        self.assertIn("ALPHA-private", self.core.dialogues[2])
        self.assertIn("BETA-private", self.core.dialogues[3])
        self.assertEqual(len(self.detail(a)["messages"]),4)
        self.assertEqual(len(self.detail(b)["messages"]),4)

    def test_same_session_parallel_turns_are_serialized(self):
        sid = self.session()
        self.core.block = True
        second_arrived=threading.Event()
        arrived=0
        app=create_app(self.core,self.db,"qa-admin")
        @app.middleware("http")
        async def observe_arrival(request,call_next):
            nonlocal arrived
            if request.url.path.endswith("/messages"):
                arrived+=1
                if arrived==2:
                    second_arrived.set()
            return await call_next(request)
        with TestClient(app) as concurrent_client:
            def post(text):
                return concurrent_client.post(f"/api/sessions/{sid}/messages",json={"text":text})
            with ThreadPoolExecutor(max_workers=2) as pool:
                first=pool.submit(post,"first-turn")
                try:
                    self.assertTrue(self.core.entered.wait(2))
                    second=pool.submit(post,"second-turn")
                    self.assertTrue(second_arrived.wait(3))
                finally:
                    self.core.release.set()
                self.assertEqual(first.result(timeout=5).status_code,200)
                self.assertEqual(second.result(timeout=5).status_code,200)
        self.assertEqual(self.core.peak,1)
        self.assertIn("first-turn",self.core.dialogues[1])
        messages=self.detail(sid)["messages"]
        self.assertEqual([m["role"] for m in messages],["user","assistant","user","assistant"])
        self.assertEqual([m["text"] for m in messages if m["role"]=="user"],["first-turn","second-turn"])

    def test_three_parallel_sessions_reject_fourth_then_recover(self):
        # Avoid documented lock striping collisions when testing independent parallelism.
        sessions=[]; stripes=set()
        for _ in range(1000):
            sid=self.session()
            if hash(sid)%64 not in stripes:
                sessions.append(sid); stripes.add(hash(sid)%64)
            if len(sessions)==4: break
        self.assertEqual(len(sessions),4)
        self.core.block=True
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures=[pool.submit(self.send,s,"parallel") for s in sessions[:3]]
            try:
                self.assertTrue(self.core.three_entered.wait(3))
                self.assertEqual(self.send(sessions[3],"overflow").status_code,429)
                self.assertEqual(self.detail(sessions[3])["messages"],[])
            finally:
                self.core.release.set()
            self.assertTrue(all(f.result(timeout=5).status_code==200 for f in futures))
        self.assertEqual(self.send(sessions[3],"recovered").status_code,200)

    def test_provider_error_keeps_history_blocks_lead_and_recovers(self):
        sid=self.session()
        self.core.fail=True
        for i in range(4):  # More than the semaphore capacity: all failures must release slots.
            response=self.send(sid,f"failed-{i}")
            self.assertEqual(response.status_code,503)
            self.assertNotIn("synthetic-private",response.text)
        detail=self.detail(sid)
        self.assertFalse(any(m["role"]=="assistant" for m in detail["messages"]))
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/lead",json=LEAD).status_code,409)
        self.core.fail=False
        self.assertEqual(self.send(sid,"retry").status_code,200)
        self.assertNotIn("временно недоступен",self.core.dialogues[-1])
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/lead",json=LEAD).status_code,201)

    def test_parallel_identical_leads_have_one_persistent_id(self):
        sid=self.session()
        self.assertEqual(self.send(sid,"synthetic-task").status_code,200)
        path=f"/api/sessions/{sid}/lead"
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses=list(pool.map(lambda _:self.client.post(path,json=LEAD),range(6)))
        self.assertTrue(all(r.status_code==201 for r in responses))
        self.assertEqual(len({r.json()["id"] for r in responses}),1)
        self.assertEqual(self.detail(sid)["lead"],responses[0].json())
        changed=self.client.post(path,json={**LEAD,"company":"Другая учебная компания"})
        self.assertEqual(changed.status_code,409)
        self.assertEqual(self.detail(sid)["lead"],responses[0].json())

    def test_restart_preserves_both_lead_and_no_lead_history(self):
        # Seed data in a separate interpreter, then read after that process exits.
        seed = r'''import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from consultant import Consultation
from web import create_app
class Core:
    def answer(self, dialogue, deadline):
        return Consultation("Synthetic subprocess reply", (), 7)
with TestClient(create_app(Core(), Path(sys.argv[1]), "qa-admin")) as client:
    a=client.post("/api/sessions").json()["id"]
    b=client.post("/api/sessions").json()["id"]
    for sid in (a,b):
        assert client.post(f"/api/sessions/{sid}/messages",json={"text":sid}).status_code==200
    body=json.loads(sys.argv[2])
    response=client.post(f"/api/sessions/{a}/lead",json=body)
    assert response.status_code==201
    print(json.dumps({"a":client.get(f"/api/sessions/{a}").json(),
                      "b":client.get(f"/api/sessions/{b}").json()}))
'''
        result=subprocess.run([sys.executable,"-c",seed,str(self.db),json.dumps(LEAD)],
                              cwd=Path(__file__).resolve().parents[1],
                              capture_output=True,text=True,timeout=15,check=True)
        snapshot=json.loads(result.stdout)
        snapshot_a,snapshot_b=snapshot["a"],snapshot["b"]
        a,b=snapshot_a["id"],snapshot_b["id"]
        lead=snapshot_a["lead"]
        with TestClient(create_app(ProbeCore(),self.db,"qa-admin")) as after:
            self.assertEqual(after.get(f"/api/sessions/{a}").json(),snapshot_a)
            self.assertEqual(after.get(f"/api/sessions/{b}").json(),snapshot_b)
            archive=after.get("/api/admin/sessions",headers={"Authorization":"Bearer qa-admin"}).json()
            by_id={s["id"]:s for s in archive}
            self.assertEqual(by_id[a]["has_lead"],1)
            self.assertEqual(by_id[b]["has_lead"],0)
            self.assertEqual(lead["session_id"],a)
            new=after.post("/api/sessions").json()["id"]
            self.assertNotIn(new,(a,b))
            self.assertEqual(after.get(f"/api/sessions/{new}").json()["messages"],[])

    def test_invalid_and_unknown_requests_leave_other_session_unchanged(self):
        sid=self.session(); before=self.detail(sid)
        for text in [" ","x"*4001]:
            self.assertEqual(self.send(sid,text).status_code,422)
        self.assertEqual(self.send("missing","question").status_code,404)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/lead",json={**LEAD,"test_data_confirmed":False}).status_code,422)
        self.assertEqual(self.detail(sid),before)
        self.assertEqual(self.send(sid,"valid").status_code,200)

    def test_missing_model_rejects_messages_and_leads_after_restart(self):
        sid=self.session()
        with patch.dict("os.environ",{},clear=True):
            with TestClient(create_app(db_path=self.db,admin_token="qa-admin")) as unconfigured:
                self.assertFalse(unconfigured.get("/api/health").json()["ready"])
                self.assertEqual(unconfigured.post(f"/api/sessions/{sid}/messages",json={"text":"question"}).status_code,503)
                self.assertEqual(unconfigured.post(f"/api/sessions/{sid}/lead",json=LEAD).status_code,409)
                roles=[m["role"] for m in unconfigured.get(f"/api/sessions/{sid}").json()["messages"]]
                self.assertEqual(roles,["user","error"])


if __name__ == "__main__":
    unittest.main()

