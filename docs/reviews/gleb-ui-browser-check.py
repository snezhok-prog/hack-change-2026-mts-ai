import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
out=Path(__file__).resolve().parent
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True, executable_path="/usr/bin/chromium")
 for width,height in [(1440,1000),(768,1024),(390,844)]:
  page=browser.new_page(viewport={"width":width,"height":height})
  state={"mode":"fail","session":0}
  errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
  def route(r):
   path=r.request.url.split("8765")[-1]
   if path=="/api/health": data={"ready":False}
   elif path=="/api/sessions":
    state["session"]+=1;data={"id":"synthetic-"+str(state["session"])}
   elif path.endswith("/messages"):
    if state["mode"]=="fail":
     return r.fulfill(status=503,json={"detail":"Синтетический сбой API"})
    if state["mode"]=="network":return r.abort()
    if state["mode"]=="html":return r.fulfill(status=502,content_type="text/html",body="<html>bad gateway</html>")
    data={"answer":"Синтетический ответ <img src=x onerror=window.injected=1>",
          "elapsed_ms":17,"sources":[{"id":1,"document":"synthetic.md","heading":"Проверка","text":"<script>window.injected=1</script>"}]}
   elif path.endswith("/lead"):data={"id":"lead-1","summary":"Синтетическое резюме"}
   elif path=="/api/admin/sessions":data=[]
   else:data={}
   r.fulfill(json=data)
  page.route("**/api/**",route)
  page.goto("http://127.0.0.1:8765/static/index.html")
  expect(page.locator("#question")).to_be_enabled()
  expect(page.locator("#save-lead")).to_be_disabled()
  assert "базу знаний" not in page.locator("#connection").inner_text()
  page.locator("#question").fill("Синтетический вопрос")
  page.locator("#question").press("Control+Enter")
  expect(page.locator("#error")).to_contain_text("Синтетический сбой")
  expect(page.locator("#question")).to_have_value("Синтетический вопрос")
  assert page.locator(".message.assistant").count()==0
  expect(page.locator(".message-status")).to_be_visible()
  page.screenshot(path=str(out/f"ui-{width}-error.png"),full_page=True)
  for mode,message in [("network","Нет соединения"),("html","некорректный ответ")]:
   state["mode"]=mode;page.locator("#send").click()
   expect(page.locator("#error")).to_contain_text(message)
   expect(page.locator("#question")).to_have_value("Синтетический вопрос")
  state["mode"]="success";page.locator("#send").click()
  expect(page.locator(".message.assistant")).to_be_visible()
  expect(page.locator("#save-lead")).to_be_enabled()
  assert page.locator("#messages img").count()==0
  assert page.evaluate("window.injected || 0")==0
  page.locator("#debug").check()
  page.locator(".debug summary").click()
  expect(page.locator(".debug")).to_contain_text("17 мс")
  expect(page.locator(".debug")).to_contain_text("synthetic.md")
  page.locator("#name").fill("Тестов Тест Тестович")
  page.locator("#phone").fill("+7 (000) 000-00-00")
  page.locator("#company").fill("Учебная компания")
  page.locator("#test-data").check()
  page.locator("#save-lead").click()
  expect(page.locator("#lead-status")).to_contain_text("Заявка сохранена")
  expect(page.locator("#save-lead")).to_be_disabled()
  expect(page.locator("#name")).to_have_attribute("readonly","")
  assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"),width
  page.screenshot(path=str(out/f"ui-{width}-success.png"),full_page=True)
  page.locator(".review-panel summary").click()
  page.locator("#admin-token").fill("synthetic-not-a-secret")
  page.locator("#new-chat").click()
  expect(page.locator("#question")).to_be_enabled()
  expect(page.locator("#messages")).to_be_empty()
  expect(page.locator("#name")).to_have_value("")
  expect(page.locator("#admin-token")).to_have_value("")
  expect(page.locator("#lead-summary")).to_be_hidden()
  expect(page.locator("#lead-summary")).to_be_empty()
  expect(page.locator("#debug")).not_to_be_checked()
  expect(page.locator("#save-lead")).to_be_disabled()
  assert not page.locator(".review-panel").evaluate("(e)=>e.open")
  assert not errors,errors
  print(f"PASS {width}x{height}: retry/network/non-JSON/debug/XSS/lead/reset/no-overflow; no JS errors")
  page.close()
 for health, message in [
  ({"ready":False},"Консультант пока недоступен"),
  ({"ready":False,"knowledge_base_ready":False,"model_configured":True},"База знаний пока недоступна"),
  ({"ready":False,"knowledge_base_ready":True,"model_configured":False},"Модель пока не подключена"),
  ({"ready":True,"provider_verified":False},"при первом вопросе"),
  ({"ready":True,"provider_verified":True,"model":"synthetic-model"},"synthetic-model")
 ]:
  page=browser.new_page()
  def health_route(r):
   r.fulfill(json=health if r.request.url.endswith('/health') else {"id":"synthetic-session"})
  page.route('**/api/**',health_route)
  page.goto('http://127.0.0.1:8765/static/index.html')
  expect(page.locator('#connection')).to_contain_text(message)
  page.close()
 for width,height in [(1440,1000),(768,1024),(390,844)]:
  page=browser.new_page(viewport={"width":width,"height":height})
  page.route('**/api/**',lambda r:r.fulfill(json={"ready":True,"provider_verified":False} if r.request.url.endswith('/health') else {"id":"synthetic-session"}))
  page.goto('http://127.0.0.1:8765/static/index.html')
  expect(page.locator('#connection')).to_contain_text('при первом вопросе')
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  assert page.evaluate('scrollY')==0
  page.screenshot(path=str(out/f'home-{width}.png'),full_page=True)
  page.close()
 print('PASS: five health states, three initial layouts; no overflow or initial scroll')
 browser.close()

