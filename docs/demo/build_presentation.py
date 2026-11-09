"""Build the editable demo deck. Requires reportlab and DejaVu Sans fonts.
Run: python docs/demo/build_presentation.py --output work/presentation-draft.pdf
This document contains no organizer data, model responses or credentials.
"""
import argparse
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

W, H = 1280, 720
BG, INK, MUTED, RED, LINE = "#F5F3EF", "#172127", "#52616B", "#D92D3B", "#D7DCD9"
REPO = "https://github.com/snezhok-prog/hack-change-2026-mts-ai"
CI = REPO + "/actions/runs/42684281251"
BASE = "edfba527c396abac007454236ee9df920058e89d"

def build(output, font_dir):
    pdfmetrics.registerFont(TTFont("Body", str(font_dir / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("Bold", str(font_dir / "DejaVuSans-Bold.ttf")))
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output), pagesize=(W, H), invariant=1)
    c.setTitle("EnergyTool — демонстрационная презентация / черновик")
    c.setAuthor("Hack&Change 2026 team")
    def rect(x,y,w,h,color):
        c.setFillColor(HexColor(color)); c.rect(x,H-y-h,w,h,fill=1,stroke=0)
    def text(x,y,t,size=22,color=INK,bold=False):
        c.setFillColor(HexColor(color)); c.setFont("Bold" if bold else "Body",size)
        c.drawString(x,H-y-size,t)
    def wrap(x,y,t,width,size=22,color=INK,bold=False):
        font = "Bold" if bold else "Body"
        words=t.split(); line=""; step=size*1.42
        for word in words:
            attempt=(line+" "+word).strip()
            if pdfmetrics.stringWidth(attempt,font,size)>width and line:
                text(x,y,line,size,color,bold); y+=step; line=word
            else: line=attempt
        if line: text(x,y,line,size,color,bold); y+=step
        return y
    def start(n,eyebrow,title,sub):
        rect(0,0,W,H,BG); rect(48,45,32,6,RED)
        text(96,33,"ENERGYTOOL / HACK&CHANGE 2026",14,MUTED,True)
        text(978,33,"МАКЕТ · 09.11.2026",14,RED,True)
        text(48,94,eyebrow.upper(),15,RED,True)
        text(48,135,title,42,INK,True)
        wrap(48,201,sub,1160,21,MUTED)
        rect(48,665,1184,1,LINE)
        text(48,680,"ПРОТОТИП / факты о продуктах — только из базы знаний",12,MUTED)
        text(1160,679,f"{n:02d} / 08",13,MUTED)
    def card(x,y,w,num,title,body):
        rect(x,y,w,240,"#FFFFFF")
        text(x+24,y+20,num,16,RED,True)
        yy=wrap(x+24,y+58,title,w-48,26,INK,True)
        wrap(x+24,yy+16,body,w-48,20,MUTED)
    def note(t):
        rect(48,585,1184,55,"#E9ECE8")
        wrap(66,599,t,1140,16,MUTED)
    def end(): c.showPage()

    start(1,"ИИ-консультант для сайта","От бизнес-вопроса к заявке","EnergyTool · кейс МТС · начальный прототип команды")
    for x,num,title,body in [
        (48,"01","Описать задачу","Клиент формулирует ситуацию своими словами."),
        (450,"02","Разобраться","Поиск материалов и консультация с учётом диалога."),
        (852,"03","Продолжить","Локальная заявка с контекстом разговора.")]:
        card(x,285,380,num,title,body)
    text(48,553,"Репозиторий проекта",16,RED,True)
    text(48,579,REPO,18,INK)
    c.linkURL(REPO,(48,H-608,1000,H-575),relative=0)
    end()

    start(2,"Пользовательский путь","Пять шагов в одном интерфейсе","Консультация и заявка связаны одной сессией.")
    rows=[
      ("01","Задача","Описать проблему бизнеса без технической анкеты."),
      ("02","Уточнение","Добавить исходные данные по вопросу консультанта."),
      ("03","Ответ","Получить объяснение; открыть источники и время."),
      ("04","Контакты","По желанию заполнить отдельную форму заявки."),
      ("05","Результат","Увидеть локально сохранённую заявку и резюме.")]
    y=280
    for num,title,body in rows:
        text(50,y,num,22,RED,True); text(115,y,title,23,INK,True)
        wrap(355,y,body,835,22,MUTED); y+=56
    note("В учебном прототипе используются только выдуманные контакты. Внешней отправки нет.")
    end()

    start(3,"Архитектура","Одно ядро. Два способа запуска.","Локальный поиск BM25 и вызов API модели используются в вебе и CSV-сабмите.")
    for x,title,body in [
      (48,"База знаний","ZIP → Markdown → секции"),
      (450,"Поиск BM25","Релевантные фрагменты"),
      (852,"Консультант","Контекст + диалог → модель")]:
        rect(x,285,380,110,"#FFFFFF"); text(x+24,303,title,25,INK,True)
        wrap(x+24,348,body,340,19,MUTED)
    rect(48,430,580,115,"#172127"); text(72,448,"WEB",16,"#FF8890",True)
    wrap(72,480,"FastAPI · SQLite · HTML/CSS/JS",530,23,"#FFFFFF")
    rect(652,430,580,115,"#172127"); text(676,448,"BATCH",16,"#FF8890",True)
    wrap(676,480,"solution.py → submission.csv",530,23,"#FFFFFF")
    note("Docker Compose — локальный запуск. Плотный поиск и реранкер пока относятся к плану.")
    end()

    start(4,"Обоснованность","Источники видны. Факты проверяются.","Продуктовые факты ограничены предоставленными материалами.")
    card(48,285,380,"01","Контекст","В модель передаются найденные фрагменты и текущий диалог.")
    card(450,285,380,"02","Отладка","Интерфейс показывает найденные источники и время ответа.")
    card(852,285,380,"03","Границы","Промпт требует не выдумывать условия и предлагать специалиста.")
    note("Найденный источник и системный промпт не гарантируют достоверность каждого утверждения.")
    end()

    start(5,"Проверки","Что подтверждено сейчас","Синтетические тесты проверяют работу кода; качество модели измеряется отдельно.")
    y=284
    for label,body,color in [
      ("КОД","Поиск, CSV, веб-чат, история, заявки и сборщик ZIP присутствуют.",INK),
      ("CI / УСПЕХ","Тесты, ZIP, контейнер, HTTP и сохранение SQLite после перезапуска.",INK),
      ("ЖИВАЯ МОДЕЛЬ","Качество, факты и скорость пока не подтверждены в этой работе.",RED),
      ("ПЛАТФОРМА","Первая попытка: «Не хватает данных в решении». Балла нет.",RED)]:
        text(48,y,label,17,color,True); wrap(300,y,body,910,21,MUTED); y+=68
    text(48,565,"Источник: GitHub Actions · run 42684281251 · commit 2ed811b",16,RED)
    c.linkURL(CI,(48,H-592,1100,H-560),relative=0)
    note("Результаты CI указаны для конкретного коммита. Это не оценка LLM-судьи организаторов.")
    end()

    start(6,"Демонстрация","От вопроса до истории диалога","План показа на 60–90 секунд; полный сценарий — 3–5 минут.")
    card(48,285,380,"01","Консультация","Задача трёх складов → уточнение → ответ с источниками.")
    card(450,285,380,"02","Заявка","Выдуманные контакты → сохранение → резюме диалога.")
    card(852,285,380,"03","Новая сессия","Очистка контекста → просмотр сохранённой истории.")
    note("Живой прогон ещё не выполнен. При сбое API показываем ошибку, без заготовленного ответа.")
    end()

    start(7,"Надёжность и ограничения","Локальное демо с сохранением истории","Текущая версия предназначена для localhost и тестовых данных.")
    rect(48,280,580,270,"#FFFFFF"); text(72,304,"Реализовано",27,INK,True)
    y=356
    for t in ["Изолированные сессии и SQLite","Обработка ошибок API","Заявка без внешней отправки","Ключ просмотра списка архива"]:
        y=wrap(72,y,"• "+t,520,22,MUTED)+10
    rect(652,280,580,270,"#FFFFFF"); text(676,304,"Ограничения",27,RED,True)
    y=356
    for t in ["Один процесс приложения","Архив: последние 100 сессий","Резюме собирается из переписки","Публичный запуск требует защиты"]:
        y=wrap(676,y,"• "+t,520,22,MUTED)+10
    note("Секреты — через окружение. Материалы организаторов и реальные контакты в git не входят.")
    end()

    start(8,"Перед защитой","Три шага до подтверждённой готовности","Следующий этап — реальные проверки и финальная репетиция.")
    card(48,285,380,"01","Проверить модель","Демо-вопросы, достоверность фактов, качество и задержка.")
    card(450,285,380,"02","Проверить платформу","Временной бюджет, ZIP, принятый сабмит и результат.")
    card(852,285,380,"03","Провести репетицию","Полный веб-сценарий, разрешённые кадры и финальный PDF.")
    note("Основание макета: main "+BASE[:7]+". Срок и комплект сдачи сверить на платформе.")
    end()
    c.save()

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--output",type=Path,default=Path("work/presentation-draft.pdf"))
    p.add_argument("--font-dir",type=Path,default=Path("/usr/share/fonts/truetype/dejavu"))
    a=p.parse_args()
    build(a.output,a.font_dir)

