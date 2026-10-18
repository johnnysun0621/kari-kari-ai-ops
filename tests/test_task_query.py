"""小律的自然語言查任務（M6-03）：判斷、日期區間、篩選，以及 cog 實際回答路徑。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

import config
from cogs import course_qa as course_qa_cog
from services import notion_links, repositories, task_query

TZ = config.TZ
NOW = datetime(2026, 10, 18, 15, 0, tzinfo=TZ)  # 週日


def make_task(name="交 cover 素材", *, end=None, start=None, status="進行中", open_submit=False, page_id=None):
    return repositories.TaskItem(
        page_id=page_id or name, name=name, status=status, task_type="創作交付",
        artist_page_ids=["a1"], end=end, start=start, discord_visible=True,
        open_for_submission=open_submit,
    )


@pytest.mark.parametrize("question", [
    "這週有什麼任務", "我還有哪些沒交", "10/25 要交什麼", "下週行程", "最近有什麼要做的",
    "作業什麼時候要交", "我的 deadline 是哪天",
])
def test_任務問題會被認出來(question):
    assert task_query.is_task_query(question)


@pytest.mark.parametrize("question", [
    "上次會議討論了什麼", "幫我整理 9 月上課的重點", "編曲課教了什麼", "會議提到的任務有哪些",
])
def test_會議內容的問題不當成任務(question):
    assert not task_query.is_task_query(question)


@pytest.mark.parametrize("question,expected", [
    ("今天有什麼任務", ("2026-10-18", "2026-10-18")),
    ("明天要交什麼", ("2026-10-19", "2026-10-19")),
    ("這週有什麼任務", ("2026-10-12", "2026-10-18")),
    ("下週行程", ("2026-10-19", "2026-10-25")),
    ("這個月的任務", ("2026-10-01", "2026-10-31")),
    ("下個月有什麼通告", ("2026-11-01", "2026-11-30")),
    ("11月的任務", ("2026-11-01", "2026-11-30")),
    ("10/25 要交什麼", ("2026-10-25", "2026-10-25")),
    ("1月有什麼任務", ("2027-01-01", "2027-01-31")),
    ("最近有什麼任務", ("2026-10-18", "2026-10-31")),
])
def test_日期區間(question, expected):
    rng = task_query.parse_range(question, NOW)
    assert (rng.start.isoformat(), rng.end.isoformat()) == expected


def test_沒提到日期就是全部未結案():
    assert task_query.parse_range("我有哪些任務", NOW) is None


def test_篩選區間_排除已結案_依期限排序():
    tasks = [
        make_task("下週二截止", end=datetime(2026, 10, 20, tzinfo=TZ)),
        make_task("下週一開始", start=datetime(2026, 10, 19, 14, tzinfo=TZ), end=datetime(2026, 11, 2, tzinfo=TZ)),
        make_task("這週已結案", end=datetime(2026, 10, 21, tzinfo=TZ), status="已完成"),
        make_task("月底", end=datetime(2026, 10, 31, tzinfo=TZ)),
    ]
    rng = task_query.parse_range("下週", NOW)

    assert [t.name for t in task_query.select_tasks(tasks, rng)] == ["下週二截止", "下週一開始"]


def test_還沒交只列可提交的():
    tasks = [
        make_task("可交", end=datetime(2026, 10, 25, tzinfo=TZ), open_submit=True, status="待提交"),
        make_task("還沒開放", end=datetime(2026, 10, 25, tzinfo=TZ)),
        make_task("已交待審", end=datetime(2026, 10, 25, tzinfo=TZ), open_submit=True, status="待審核"),
    ]

    picked = task_query.select_tasks(tasks, None, pending_only=task_query.wants_pending_only("還有哪些沒交"))

    assert [t.name for t in picked] == ["可交"]


@pytest.fixture
def cog(monkeypatch):
    artist = repositories.Artist(page_id="a1", name="Johnny", discord_user_id="1")
    calls = {"meetings": 0}

    async def find_artist(_uid):
        return artist

    async def artist_tasks(page_id):
        assert page_id == "a1", "只能查自己的任務"
        return [
            make_task("交 cover 素材 https://www.notion.so/abc123", end=datetime(2026, 10, 22, tzinfo=TZ)),
            make_task("錄音", start=datetime(2026, 10, 30, 14, tzinfo=TZ), end=datetime(2026, 10, 30, 17, tzinfo=TZ)),
        ]

    async def meetings(_page_id):
        calls["meetings"] += 1
        return []

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(repositories, "find_artist_by_discord_id", find_artist)
    monkeypatch.setattr(repositories, "get_discord_tasks_for_artist", artist_tasks)
    monkeypatch.setattr(repositories, "get_course_meetings_for_artist", meetings)
    monkeypatch.setattr(course_qa_cog, "datetime", FakeDatetime)
    instance = course_qa_cog.CourseQACog.__new__(course_qa_cog.CourseQACog)
    instance._answer_locks = {}
    instance._records_cache = {}
    instance._memory_by_user = {}
    instance._save_memory_state = lambda: None  # 不可寫到正式 .state
    return SimpleNamespace(cog=instance, calls=calls)


def ask(env, question):
    return asyncio.run(env.cog._answer_for_user(SimpleNamespace(id=1), question))


def test_小律回答下週任務_不經會議紀錄_不帶_Notion_網址(cog):
    answer = ask(cog, "下週有什麼任務")

    assert "交 cover 素材" in answer and "錄音" not in answer
    assert not notion_links.contains_notion_link(answer)
    assert "/我的任務" in answer
    assert cog.calls["meetings"] == 0, "查任務不該去讀會議紀錄（也不該用到 LLM）"


def test_沒有符合的任務會說清楚(cog):
    answer = ask(cog, "明天有什麼任務")

    assert "明天（10/19）沒有任務" in answer


def test_會議問題仍走會議紀錄(cog):
    answer = ask(cog, "上次會議討論了什麼")

    assert cog.calls["meetings"] == 1
    assert "這週有什麼任務" in answer  # 沒有會議紀錄時提示可以改問任務


# ---------- 任務詳細內容（2026-10-18 QA：藝人要看任務內容，小律只會列清單） ----------

def _block(kind, text):
    return {"type": kind, kind: {"rich_text": [{"plain_text": text}]}}


def test_內頁全文略過_bot_寫的段落與任務說明標題():
    blocks = [
        _block("paragraph", "控方直接寫在內頁的說明"),
        _block("heading_2", "任務說明"),
        _block("paragraph", "第二段"),
        _block("heading_2", "藝人回覆"),
        _block("paragraph", "藝人回報的內容不該出現"),
        _block("heading_2", "附註"),
        _block("paragraph", "附註內容"),
    ]

    assert repositories.task_page_body_text(blocks) == "控方直接寫在內頁的說明\n第二段\n附註\n附註內容"


def test_用名稱點名任務():
    tasks = [make_task("Johnny 測試(課程時間&作業繳交)"), make_task("測試GOGO")]

    assert [t.name for t in task_query.match_tasks_by_name("Johnny 測試的詳細內容", tasks)] == [
        "Johnny 測試(課程時間&作業繳交)"
    ]


def test_日期數字不算點名():
    tasks = [make_task("10/25（日）week4 聽力、歌唱")]

    assert task_query.match_tasks_by_name("10/25 要交什麼", tasks) == []


def test_第N個():
    assert task_query.index_reference("第2個的詳細內容") == 2
    assert task_query.index_reference("第三筆") == 3
    assert task_query.index_reference("這週有什麼任務") is None


@pytest.fixture
def detail_cog(cog, monkeypatch):
    async def page_body(page_id):
        return f"{page_id} 的內頁內容 https://www.notion.so/secret"

    monkeypatch.setattr(repositories, "get_task_page_body", page_body)
    cog.cog._save_memory_state = lambda: None
    return cog


def test_先列清單再問第N個的詳細(detail_cog):
    listing = ask(detail_cog, "我有哪些任務")
    assert listing.splitlines()[3].startswith("1. ")

    detail = ask(detail_cog, "第2個的詳細內容")

    assert "📌 **錄音**" in detail and "錄音 的內頁內容" in detail
    assert not notion_links.contains_notion_link(detail)


def test_直接講任務名稱看詳細(detail_cog):
    detail = ask(detail_cog, "交 cover 素材的要求是什麼")

    assert "📌 **交 cover 素材" in detail and "**任務內容**" in detail


def test_想看內容但對不到任務就交回會議紀錄(detail_cog):
    ask(detail_cog, "那份企劃的內容")

    assert detail_cog.calls["meetings"] == 1


# ---------- 任務問答（LLM）：資料整理與路由 ----------

def test_時間池草稿的控方內部字樣不給藝人():
    draft = (
        "安排來源：/從時間池建立任務\n課程類別：吉他課\n負責老師：Tony\n需要時長：2 小時\n"
        "候選時段（由營運最後確認）：\n2026-09-16 15:00~17:00\n\n"
        "此 Task 同時管理課程場次與課後作業；正式時間請由營運直接填寫開始日。"
    )
    assert task_query.clean_description(draft) == "課程類別：吉他課\n負責老師：Tony"
    assert task_query.clean_description("此為控方草稿；補齊內容後勾選 Discord 顯示=true") == ""


def test_舊格式的藝人回報不當成任務內容():
    text = "交 demo\n\n【Discord 任務回饋｜2026-09-15 10:28】\n藝人：Johnny\n回饋：測試\n\n補充：要 wav"
    assert task_query.clean_description(text) == "交 demo\n\n補充：要 wav"


@pytest.mark.parametrize("question,expected", [
    ("聽力要做幾題", True), ("要用什麼格式交", True), ("發布前要準備什麼", True),
    ("這週有什麼任務", False), ("第2個的詳細內容", False), ("我還有哪些沒交", False),
])
def test_具體問題才交給模型(question, expected):
    assert task_query.is_specific_question(question) is expected


def test_給模型的任務資料_不含網址與_page_id():
    task = make_task("交 cover https://www.notion.so/x", end=datetime(2026, 10, 25, tzinfo=TZ), page_id="secret-page-id")
    context = task_query.build_task_context(
        task, now=NOW, deadline_text="截止 10/25（剩 7 天）", project_name="方晨2610 (發文）",
        teacher_names=["Tony"], body="參考 https://app.notion.com/p/abc\n• 聽力：50題／截圖",
    )
    assert "secret-page-id" not in context and not notion_links.contains_notion_link(context)
    assert "所屬專案：方晨2610 (發文）" in context and "負責老師：Tony" in context
    assert "• 聽力：50題／截圖" in context


@pytest.fixture
def llm_cog(detail_cog, monkeypatch):
    sent = {}

    async def fake_answer(artist_name, question, task_context, usage_key=None):
        sent["context"] = task_context
        return "要做 50 題音程，做完交截圖。"

    async def no_projects():
        return {}

    async def no_people(ids):
        return []

    tenant = config.get_tenant()
    monkeypatch.setattr(config, "get_tenant", lambda: __import__("dataclasses").replace(tenant, llm_api_key="k"))
    monkeypatch.setattr(course_qa_cog.course_qa, "answer_task_question", fake_answer)
    monkeypatch.setattr(repositories, "get_project_map", no_projects)
    monkeypatch.setattr(repositories, "get_people_items_by_ids", no_people)
    detail_cog.sent = sent
    return detail_cog


def test_點名任務的具體問題交給模型_只帶那一筆(llm_cog):
    answer = ask(llm_cog, "交 cover 素材要用什麼格式交")

    assert "50 題音程" in answer and "**參考任務**" in answer
    assert "交 cover 素材" in llm_cog.sent["context"] and "錄音" not in llm_cog.sent["context"]


def test_模型失敗時退回任務詳細(llm_cog, monkeypatch):
    async def boom(*args, **kwargs):
        raise course_qa_cog.course_qa.CourseQAError("x", "⚠️ 暫時無法回應")

    monkeypatch.setattr(course_qa_cog.course_qa, "answer_task_question", boom)

    answer = ask(llm_cog, "交 cover 素材要用什麼格式交")

    assert "📌 **交 cover 素材" in answer


# ---------- 會議紀錄查詢封存（MEETING_QA_ENABLED=false，2026-10-18） ----------

@pytest.fixture
def task_only(cog, monkeypatch):
    import dataclasses

    tenant = dataclasses.replace(config.get_tenant(), meeting_qa_enabled=False)
    monkeypatch.setattr(config, "get_tenant", lambda: tenant)
    cog.cog._save_memory_state = lambda: None
    return cog


def test_封存時問會議只給查任務的說明_不讀會議紀錄(task_only):
    answer = ask(task_only, "上次會議討論了什麼")

    assert answer == course_qa_cog.TASK_ONLY_HELP
    assert task_only.calls["meetings"] == 0


def test_封存時任務照常查(task_only):
    answer = ask(task_only, "下週有什麼任務")

    assert "交 cover 素材" in answer and task_only.calls["meetings"] == 0


def test_封存時啟動檢查略過會議紀錄欄位(monkeypatch):
    import dataclasses

    from services import notion_api, schema_check

    tenant = dataclasses.replace(config.get_tenant(), meeting_qa_enabled=False)
    monkeypatch.setattr(config, "get_tenant", lambda: tenant)
    asked = []

    async def fake_retrieve(ds):
        asked.append(ds)
        return {"properties": {}}

    monkeypatch.setattr(notion_api, "retrieve_data_source", fake_retrieve)
    problems = asyncio.run(schema_check.find_problems())

    assert config.DS_COURSE_MEETINGS not in asked
    assert not any("藝人可見" in line or "會議類型" in line for line in problems)
