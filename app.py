"""
Self-Paced Timed Quiz App (PowerApps-style) — Streamlit + SQLite.
Supports MCQ + free-text questions, per-participant timed turnaround,
password-gated admin area, quiz editing, and results reset.

Run with:
    streamlit run app.py

Participant link (what you share with the team):
    <your app URL>                      e.g. https://your-quiz.streamlit.app
    or with the code prefilled:
    <your app URL>/?code=12345

Admin link (keep this one to yourself):
    <your app URL>/?admin=1
    You'll be asked for the admin password (see ADMIN_PASSWORD below, or
    set it via Streamlit secrets — see README.md).

Model
-----
- Only people who open the app with ?admin=1 AND enter the correct password
  ever see admin controls. The normal participant link never shows an
  "Admin" option — there is no sidebar role switcher.
- An ADMIN prepares a quiz (title, questions, turnaround time in minutes —
  default 60) and gets a short quiz CODE to share.
- Each PARTICIPANT opens the link, enters the code + their name, clicks
  "Start" — that stamps THEIR OWN start_time. From that moment they have
  `duration_minutes` to finish, at their own convenience; closing the tab
  and coming back does not reset the clock.
- Answers save the instant they're entered. When a participant's personal
  timer hits zero, their quiz auto-submits whatever they'd answered.
- Admin dashboard: live status per participant, auto-graded scores, a
  full answer detail view (including free-text responses to grade
  manually), CSV export, and a "clear results" reset per quiz.
- Admin can also edit a published quiz: title, duration, show-score
  setting, and add/remove questions.

Deployment note: state lives in a local SQLite file (quiz.db), fine for one
running app instance. See README.md for hosting + securing the admin page.
"""

import sqlite3
import json
import random
import string
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

DB_PATH = "quiz.db"

# Change this before sharing the app, or set ADMIN_PASSWORD in
# .streamlit/secrets.toml to avoid hardcoding it in code.
try:
    ADMIN_PASSWORD = st.secrets["ADMIN_PASSWORD"]
except Exception:
    ADMIN_PASSWORD = "admin123"  # <-- CHANGE ME

# --------------------------------------------------------------------------
# DATA LAYER
# --------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            code TEXT PRIMARY KEY,
            title TEXT,
            questions_json TEXT,
            duration_minutes INTEGER,
            show_score INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            quiz_code TEXT,
            participant_name TEXT,
            start_time TEXT,
            submitted INTEGER DEFAULT 0,
            submit_time TEXT,
            PRIMARY KEY (quiz_code, participant_name)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            quiz_code TEXT,
            participant_name TEXT,
            question_index INTEGER,
            answer_index INTEGER,
            answer_text TEXT,
            answered_at TEXT,
            PRIMARY KEY (quiz_code, participant_name, question_index)
        )
    """)
    conn.commit()
    conn.close()


def create_quiz(title, questions, duration_minutes, show_score):
    code = "".join(random.choices(string.digits, k=5))
    conn = get_conn()
    conn.execute(
        "INSERT INTO quizzes (code, title, questions_json, duration_minutes, show_score, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (code, title, json.dumps(questions), duration_minutes, int(show_score),
         datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return code


def update_quiz(code, title, questions, duration_minutes, show_score):
    conn = get_conn()
    conn.execute(
        "UPDATE quizzes SET title = ?, questions_json = ?, duration_minutes = ?, show_score = ? "
        "WHERE code = ?",
        (title, json.dumps(questions), duration_minutes, int(show_score), code),
    )
    conn.commit()
    conn.close()


def delete_results(code):
    """Clears all attempts and answers for a quiz — the quiz itself stays."""
    conn = get_conn()
    conn.execute("DELETE FROM attempts WHERE quiz_code = ?", (code,))
    conn.execute("DELETE FROM answers WHERE quiz_code = ?", (code,))
    conn.commit()
    conn.close()


def get_quiz(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM quizzes WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row


def list_quizzes():
    conn = get_conn()
    rows = conn.execute("SELECT code, title, created_at FROM quizzes ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def start_attempt(code, name):
    """Idempotent: only sets start_time the FIRST time this person starts."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM attempts WHERE quiz_code = ? AND participant_name = ?", (code, name)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO attempts (quiz_code, participant_name, start_time, submitted) "
            "VALUES (?, ?, ?, 0)",
            (code, name, datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def get_attempt(code, name):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM attempts WHERE quiz_code = ? AND participant_name = ?", (code, name)
    ).fetchone()
    conn.close()
    return row


def mark_submitted(code, name):
    conn = get_conn()
    conn.execute(
        "UPDATE attempts SET submitted = 1, submit_time = ? "
        "WHERE quiz_code = ? AND participant_name = ? AND submitted = 0",
        (datetime.utcnow().isoformat(), code, name),
    )
    conn.commit()
    conn.close()


def save_mcq_answer(code, name, question_index, answer_index):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO answers "
        "(quiz_code, participant_name, question_index, answer_index, answer_text, answered_at) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (code, name, question_index, answer_index, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def save_text_answer(code, name, question_index, answer_text):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO answers "
        "(quiz_code, participant_name, question_index, answer_index, answer_text, answered_at) "
        "VALUES (?, ?, ?, NULL, ?, ?)",
        (code, name, question_index, answer_text, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_answers(code, name):
    """Returns {question_index: {'answer_index': int|None, 'answer_text': str|None}}"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM answers WHERE quiz_code = ? AND participant_name = ?", (code, name)
    ).fetchall()
    conn.close()
    return {
        r["question_index"]: {"answer_index": r["answer_index"], "answer_text": r["answer_text"]}
        for r in rows
    }


def get_all_attempts(code):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM attempts WHERE quiz_code = ? ORDER BY start_time", (code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_answers(code):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM answers WHERE quiz_code = ?", (code,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# TIME HELPERS
# --------------------------------------------------------------------------

def remaining_seconds(attempt_row, duration_minutes):
    start = datetime.fromisoformat(attempt_row["start_time"])
    deadline = start + timedelta(minutes=duration_minutes)
    remaining = (deadline - datetime.utcnow()).total_seconds()
    return max(0, remaining)


def fmt_mmss(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


def autorefresh(interval_ms, key):
    if HAS_AUTOREFRESH:
        st_autorefresh(interval=interval_ms, key=key)
    else:
        st.button("🔄 Refresh timer", key=key + "_btn")


def hide_streamlit_chrome():
    """Hides the default Streamlit hamburger menu, footer, and the
    top-right toolbar (which includes the GitHub / 'view source' icon on
    apps deployed from a public repo). Covers selectors across recent
    Streamlit versions since the internal class names shift between them."""
    st.markdown("""
        <style>
        #MainMenu {visibility: hidden;}
        footer {visibility: hidden;}
        header {visibility: hidden;}
        [data-testid="stToolbar"] {visibility: hidden !important; height: 0 !important;}
        [data-testid="stDecoration"] {display: none !important;}
        [data-testid="stStatusWidget"] {visibility: hidden !important;}
        a[href*="github.com"] {display: none !important;}
        </style>
    """, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# SCORING
# --------------------------------------------------------------------------

def normalize(s):
    return (s or "").strip().lower()


def grade_one(q, ans):
    """
    Returns True (correct), False (incorrect), or None (needs manual
    review — free text question with no expected answers on file).
    """
    if q["type"] == "mcq":
        if ans is None or ans.get("answer_index") is None:
            return False
        return ans["answer_index"] == q["correct_index"]

    expected = q.get("expected_answers") or []
    if not expected:
        return None
    if ans is None or not (ans.get("answer_text") or "").strip():
        return False
    given = normalize(ans["answer_text"])
    return any(normalize(e) == given for e in expected)


def score_attempt(questions, answers_dict):
    """Returns (correct, auto_gradable_total, manual_review_count)."""
    correct, auto_total, manual = 0, 0, 0
    for i, q in enumerate(questions):
        result = grade_one(q, answers_dict.get(i))
        if result is None:
            manual += 1
        else:
            auto_total += 1
            if result:
                correct += 1
    return correct, auto_total, manual


# --------------------------------------------------------------------------
# SHARED QUESTION BUILDER WIDGET (used by both Create and Edit tabs)
# --------------------------------------------------------------------------

def question_builder(state_key, form_key_suffix):
    """Renders the 'type + fields' form to append one question to
    st.session_state[state_key] (a list)."""
    q_type = st.radio("Question type", ["Multiple choice", "Free text"],
                       horizontal=True, key=f"qtype_{form_key_suffix}")

    with st.form(f"add_question_form_{form_key_suffix}", clear_on_submit=True):
        st.markdown("**Add a question**")
        q_text = st.text_area("Question text", key=f"qtext_{form_key_suffix}")

        if q_type == "Multiple choice":
            opt_a = st.text_input("Option A", key=f"a_{form_key_suffix}")
            opt_b = st.text_input("Option B", key=f"b_{form_key_suffix}")
            opt_c = st.text_input("Option C (optional)", key=f"c_{form_key_suffix}")
            opt_d = st.text_input("Option D (optional)", key=f"d_{form_key_suffix}")
            correct = st.selectbox("Correct answer", ["A", "B", "C", "D"], key=f"corr_{form_key_suffix}")
        else:
            expected_raw = st.text_input(
                "Expected answer(s) for auto-grading — comma-separated, case-insensitive. "
                "Leave blank to grade this one manually.",
                key=f"exp_{form_key_suffix}",
            )

        add = st.form_submit_button("Add question")
        if add:
            if not q_text.strip():
                st.error("Give the question text.")
            elif q_type == "Multiple choice":
                options = [o for o in [opt_a, opt_b, opt_c, opt_d] if o.strip()]
                if len(options) < 2:
                    st.error("Give at least 2 options.")
                else:
                    correct_index = "ABCD".index(correct)
                    if correct_index >= len(options):
                        st.error(f"Option {correct} is empty — pick a correct answer that has text.")
                    else:
                        st.session_state[state_key].append({
                            "type": "mcq",
                            "text": q_text,
                            "options": options,
                            "correct_index": correct_index,
                        })
                        st.success("Question added.")
            else:
                expected = [e.strip() for e in expected_raw.split(",") if e.strip()]
                st.session_state[state_key].append({
                    "type": "text",
                    "text": q_text,
                    "expected_answers": expected,
                })
                st.success("Question added." + ("" if expected else " (marked for manual grading)"))

    if st.session_state[state_key]:
        st.markdown(f"**Questions so far ({len(st.session_state[state_key])}):**")
        for i, q in enumerate(st.session_state[state_key]):
            cols = st.columns([8, 1])
            with cols[0]:
                if q["type"] == "mcq":
                    st.write(f"{i+1}. [MCQ] {q['text']}  —  ✅ {q['options'][q['correct_index']]}")
                else:
                    tag = "auto-graded" if q["expected_answers"] else "manual review"
                    st.write(f"{i+1}. [Free text — {tag}] {q['text']}")
            with cols[1]:
                if st.button("🗑️", key=f"del_{form_key_suffix}_{i}"):
                    st.session_state[state_key].pop(i)
                    st.rerun()


# --------------------------------------------------------------------------
# ADMIN UI
# --------------------------------------------------------------------------

def admin_create_quiz():
    st.subheader("Create a new quiz")

    if "draft_questions" not in st.session_state:
        st.session_state.draft_questions = []

    title = st.text_input("Quiz title", value=st.session_state.get("draft_title", ""))
    st.session_state.draft_title = title

    duration = st.number_input(
        "Turnaround time — minutes each person gets once THEY start",
        min_value=5, max_value=480, value=60, step=5, key="create_duration",
    )
    show_score = st.checkbox("Show participants their score immediately after they submit",
                              value=False, key="create_show_score")

    question_builder("draft_questions", "create")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ Clear questions"):
            st.session_state.draft_questions = []
            st.rerun()
    with col2:
        if st.button("🚀 Publish quiz", type="primary",
                      disabled=not (title and st.session_state.draft_questions)):
            code = create_quiz(title, st.session_state.draft_questions, duration, show_score)
            st.session_state.last_created_code = code
            st.session_state.draft_questions = []
            st.session_state.draft_title = ""
            st.rerun()

    if "last_created_code" in st.session_state:
        code = st.session_state.last_created_code
        st.success(f"Quiz published! Join code: **{code}**")
        st.code(f"Share this with your team:\n\n"
                 f"1. App link: <your deployed app URL>\n"
                 f"2. Join code: {code}\n"
                 f"3. They have {duration} minutes from the moment THEY click Start.")


def admin_edit_quiz():
    st.subheader("Edit an existing quiz")
    quizzes = list_quizzes()
    if not quizzes:
        st.info("No quizzes to edit yet — create one first.")
        return

    options = {f"{q['title']}  ({q['code']})": q["code"] for q in quizzes}
    choice = st.selectbox("Select a quiz to edit", list(options.keys()), key="edit_select")
    code = options[choice]

    # (Re)load into session state whenever the selected quiz changes
    if st.session_state.get("editing_code") != code:
        quiz = get_quiz(code)
        st.session_state.editing_code = code
        st.session_state.edit_title = quiz["title"]
        st.session_state.edit_duration = quiz["duration_minutes"]
        st.session_state.edit_show_score = bool(quiz["show_score"])
        st.session_state.edit_questions = json.loads(quiz["questions_json"])

    attempts = get_all_attempts(code)
    if attempts:
        st.warning(
            f"⚠️ {len(attempts)} participant(s) have already started this quiz. "
            f"Removing or reordering questions can make their saved answers line up "
            f"with the wrong question. Adding new questions or fixing typos is safe."
        )

    st.session_state.edit_title = st.text_input("Quiz title", value=st.session_state.edit_title)
    st.session_state.edit_duration = st.number_input(
        "Turnaround time (minutes)", min_value=5, max_value=480,
        value=st.session_state.edit_duration, step=5,
    )
    st.session_state.edit_show_score = st.checkbox(
        "Show participants their score immediately after they submit",
        value=st.session_state.edit_show_score,
    )

    question_builder("edit_questions", "edit")

    if st.button("💾 Save changes", type="primary"):
        update_quiz(
            code,
            st.session_state.edit_title,
            st.session_state.edit_questions,
            st.session_state.edit_duration,
            st.session_state.edit_show_score,
        )
        st.success("Quiz updated.")


def admin_dashboard():
    st.subheader("📊 Dashboard")
    quizzes = list_quizzes()
    if not quizzes:
        st.info("No quizzes created yet.")
        return

    options = {f"{q['title']}  ({q['code']})": q["code"] for q in quizzes}
    choice = st.selectbox("Select a quiz", list(options.keys()), key="dash_select")
    code = options[choice]
    quiz = get_quiz(code)
    questions = json.loads(quiz["questions_json"])
    duration = quiz["duration_minutes"]

    attempts = get_all_attempts(code)
    all_answers = get_all_answers(code)

    answers_by_participant = {}
    for a in all_answers:
        answers_by_participant.setdefault(a["participant_name"], {})[a["question_index"]] = {
            "answer_index": a["answer_index"], "answer_text": a["answer_text"]
        }

    rows = []
    for att in attempts:
        name = att["participant_name"]
        ans = answers_by_participant.get(name, {})
        correct, auto_total, manual = score_attempt(questions, ans)
        rem = remaining_seconds(att, duration) if not att["submitted"] else 0
        if att["submitted"]:
            status = "✅ Submitted"
        elif rem <= 0:
            status = "⏰ Time expired (not yet auto-closed)"
        else:
            status = f"🟡 In progress — {fmt_mmss(rem)} left"
        score_display = f"{correct}/{auto_total}" + (f"  (+{manual} to review)" if manual else "")
        rows.append({
            "participant": name,
            "status": status,
            "answered": f"{len(ans)}/{len(questions)}",
            "score": score_display,
            "started_at": att["start_time"],
            "submitted_at": att["submit_time"] or "",
        })

    if not rows:
        st.info("No one has started this quiz yet.")
    else:
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "⬇️ Download summary as CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"quiz_{code}_summary.csv",
            mime="text/csv",
        )

        detail_rows = []
        for name, ans in answers_by_participant.items():
            for i, q in enumerate(questions):
                a = ans.get(i)
                if q["type"] == "mcq":
                    given = q["options"][a["answer_index"]] if a and a["answer_index"] is not None else ""
                else:
                    given = a["answer_text"] if a else ""
                result = grade_one(q, a)
                result_label = "correct" if result is True else ("incorrect" if result is False else "needs review")
                detail_rows.append({
                    "participant": name,
                    "question_no": i + 1,
                    "type": q["type"],
                    "question": q["text"],
                    "answer_given": given,
                    "result": result_label,
                })
        detail_df = pd.DataFrame(detail_rows)
        with st.expander("🔍 Full answer detail (incl. free-text responses to review)"):
            st.dataframe(detail_df, use_container_width=True)
            st.download_button(
                "⬇️ Download full detail as CSV",
                data=detail_df.to_csv(index=False).encode("utf-8"),
                file_name=f"quiz_{code}_detail.csv",
                mime="text/csv",
            )

    st.divider()
    with st.expander("⚠️ Danger zone"):
        st.write(f"This clears **all attempts and answers** for *{quiz['title']}* ({code}). "
                 f"The quiz and its questions stay intact — only results are wiped. "
                 f"This cannot be undone.")
        confirm = st.checkbox("I understand this permanently deletes all results for this quiz.",
                               key="confirm_clear")
        if st.button("🗑️ Clear all results for this quiz", disabled=not confirm):
            delete_results(code)
            st.success("Results cleared.")
            st.rerun()

    autorefresh(5000, key="admin_refresh")


def admin_view():
    # Deliberately NOT using st.tabs() here: Streamlit executes the code
    # behind every tab on every rerun (it only hides the inactive ones with
    # CSS), which meant the Dashboard's auto-refresh kept firing in the
    # background even while you were on the Edit tab — forcing a rerun
    # mid-keystroke and making text inputs feel like they were rejecting
    # input. A plain section picker only runs the code for the section
    # you're actually looking at.
    section = st.radio(
        "Section",
        ["➕ Create quiz", "✏️ Edit quiz", "📊 Dashboard"],
        horizontal=True,
        key="admin_section",
    )
    st.divider()
    if section == "➕ Create quiz":
        admin_create_quiz()
    elif section == "✏️ Edit quiz":
        admin_edit_quiz()
    else:
        admin_dashboard()


def admin_gate():
    st.title("⏱️ Team Quiz — Admin")
    if st.session_state.get("is_admin_authed"):
        admin_view()
        return

    pw = st.text_input("Admin password", type="password")
    if st.button("Login", type="primary"):
        if pw == ADMIN_PASSWORD:
            st.session_state.is_admin_authed = True
            st.rerun()
        else:
            st.error("Incorrect password.")


# --------------------------------------------------------------------------
# PARTICIPANT UI
# --------------------------------------------------------------------------

def participant_entry():
    st.subheader("Start your quiz")
    default_code = st.query_params.get("code", "")
    code = st.text_input("Quiz code", value=default_code, max_chars=5)
    name = st.text_input("Your name (or email)")

    if code and name:
        quiz = get_quiz(code)
        if quiz is None:
            st.error("No quiz found with that code.")
            return
        attempt = get_attempt(code, name)
        label = "▶️ Resume quiz" if attempt else "▶️ Start quiz"
        if not attempt:
            st.warning(
                f"Once you click Start, your {quiz['duration_minutes']}-minute clock begins "
                f"— it will keep running even if you close this tab."
            )
        if st.button(label, type="primary"):
            start_attempt(code, name)
            st.session_state.p_code = code
            st.session_state.p_name = name
            st.rerun()


def participant_quiz(code, name):
    quiz = get_quiz(code)
    if quiz is None:
        st.error("Quiz not found.")
        return

    questions = json.loads(quiz["questions_json"])
    duration = quiz["duration_minutes"]
    attempt = get_attempt(code, name)
    if attempt is None:
        st.error("No attempt found — please start again.")
        return

    rem = remaining_seconds(attempt, duration)
    already_submitted = bool(attempt["submitted"])

    if rem <= 0 and not already_submitted:
        mark_submitted(code, name)
        already_submitted = True

    st.subheader(quiz["title"])

    if already_submitted:
        st.success("✅ Your quiz has been submitted. Thanks!")
        if quiz["show_score"]:
            answers = get_answers(code, name)
            correct, auto_total, manual = score_attempt(questions, answers)
            st.metric("Your score", f"{correct} / {auto_total}")
            if manual:
                st.caption(f"{manual} free-text question(s) will be reviewed manually.")
        return

    colA, colB = st.columns([3, 1])
    with colA:
        st.progress(min(1.0, max(0.0, 1 - rem / (duration * 60))))
    with colB:
        st.markdown(f"### ⏳ {fmt_mmss(rem)}")

    answers = get_answers(code, name)

    # Sequential navigation state, kept per participant
    nav_key = f"qidx_{code}_{name}"
    if nav_key not in st.session_state:
        st.session_state[nav_key] = 0
    st.session_state[nav_key] = max(0, min(st.session_state[nav_key], len(questions) - 1))
    q_idx = st.session_state[nav_key]
    is_last_question = q_idx == len(questions) - 1

    st.caption(f"Question {q_idx + 1} of {len(questions)}  ·  {len(answers)} answered so far")

    q = questions[q_idx]
    st.markdown(f"#### {q['text']}")

    existing = answers.get(q_idx)

    if q["type"] == "mcq":
        current_index = existing["answer_index"] if existing else None
        choice = st.radio(
            "Your answer:",
            q["options"],
            index=current_index if current_index is not None else None,
            key=f"mcq_{code}_{name}_{q_idx}",
        )
        if choice is not None:
            chosen_index = q["options"].index(choice)
            if chosen_index != current_index:
                save_mcq_answer(code, name, q_idx, chosen_index)
                st.rerun()
    else:
        current_text = existing["answer_text"] if existing else ""
        text_val = st.text_area(
            "Your answer:",
            value=current_text or "",
            key=f"text_{code}_{name}_{q_idx}",
            height=120,
        )
        if text_val != (current_text or ""):
            save_text_answer(code, name, q_idx, text_val)
            st.rerun()

    st.divider()
    col_prev, col_next = st.columns(2)
    with col_prev:
        if st.button("⬅️ Previous", disabled=(q_idx == 0), use_container_width=True):
            st.session_state[nav_key] = q_idx - 1
            st.rerun()
    with col_next:
        if not is_last_question:
            if st.button("Next ➡️", type="primary", use_container_width=True):
                st.session_state[nav_key] = q_idx + 1
                st.rerun()
        else:
            if st.button("✅ Final Submit", type="primary", use_container_width=True):
                mark_submitted(code, name)
                st.rerun()

    if is_last_question and len(answers) < len(questions):
        st.caption(f"⚠️ {len(questions) - len(answers)} question(s) still unanswered — "
                    f"Final Submit will send the quiz as-is.")

    autorefresh(3000, key="participant_refresh")


def participant_view():
    st.title("⏱️ Team Quiz")
    if "p_code" in st.session_state and "p_name" in st.session_state:
        participant_quiz(st.session_state.p_code, st.session_state.p_name)
    else:
        participant_entry()


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Timed Quiz", page_icon="⏱️", layout="centered")
    hide_streamlit_chrome()
    init_db()

    is_admin_request = st.query_params.get("admin", "") == "1"
    if is_admin_request:
        admin_gate()
    else:
        participant_view()


if __name__ == "__main__":
    main()
