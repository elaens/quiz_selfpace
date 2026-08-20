"""
Self-Paced Timed Quiz App (PowerApps-style) — Streamlit + SQLite.
Supports two question types: multiple-choice (MCQ) and free text.

Run with:
    streamlit run app.py

Model
-----
- An ADMIN prepares a quiz (title, questions, and a turnaround time in
  minutes — default 60) and gets back a short quiz CODE. Each question is
  either MCQ (with a correct option) or FREE TEXT (with optional expected
  answers for auto-grading; if left blank, the admin grades it manually).
- The admin shares the app link + code with the team.
- Each PARTICIPANT opens the link, enters the code + their name, and clicks
  "Start". That click stamps THEIR OWN start_time in the database. From
  that moment, they have `duration_minutes` (default 60) to finish, at
  their own convenience — they can close the tab and come back; the timer
  keeps counting from their original start_time, not from when they reopen.
- Answers are saved the instant they're entered (no "lost progress" risk).
- When a participant's personal timer hits zero, their quiz is locked and
  auto-submitted, whatever they'd answered so far.
- The admin gets a live dashboard: who has started, time remaining / status
  for each person, auto-graded scores, free-text answers to review, and a
  CSV export.

Deployment note: state lives in a local SQLite file (quiz.db), which is
fine for one running app instance (one `streamlit run` process, or one
Streamlit Community Cloud app). See README.md for hosting options.
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


# --------------------------------------------------------------------------
# SCORING
# --------------------------------------------------------------------------

def normalize(s):
    return (s or "").strip().lower()


def grade_one(q, ans):
    """
    Returns one of: True (correct), False (incorrect), None (needs manual
    review — free text question with no expected answers on file).
    `ans` is {'answer_index':.., 'answer_text':..} or None if unanswered.
    """
    if q["type"] == "mcq":
        if ans is None or ans.get("answer_index") is None:
            return False
        return ans["answer_index"] == q["correct_index"]

    # free text
    expected = q.get("expected_answers") or []
    if not expected:
        return None  # manual review only
    if ans is None or not (ans.get("answer_text") or "").strip():
        return False
    given = normalize(ans["answer_text"])
    return any(normalize(e) == given for e in expected)


def score_attempt(questions, answers_dict):
    """Returns (correct, auto_gradable_total, manual_review_count)."""
    correct = 0
    auto_total = 0
    manual = 0
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
        min_value=5, max_value=480, value=60, step=5,
    )
    show_score = st.checkbox("Show participants their score immediately after they submit", value=False)

    q_type = st.radio("Question type", ["Multiple choice", "Free text"], horizontal=True)

    with st.form("add_question_form", clear_on_submit=True):
        st.markdown("**Add a question**")
        q_text = st.text_area("Question text")

        if q_type == "Multiple choice":
            opt_a = st.text_input("Option A")
            opt_b = st.text_input("Option B")
            opt_c = st.text_input("Option C (optional)")
            opt_d = st.text_input("Option D (optional)")
            correct = st.selectbox("Correct answer", ["A", "B", "C", "D"])
        else:
            expected_raw = st.text_input(
                "Expected answer(s) for auto-grading — comma-separated, case-insensitive. "
                "Leave blank to grade this one manually."
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
                        st.session_state.draft_questions.append({
                            "type": "mcq",
                            "text": q_text,
                            "options": options,
                            "correct_index": correct_index,
                        })
                        st.success("Question added.")
            else:
                expected = [e.strip() for e in expected_raw.split(",") if e.strip()]
                st.session_state.draft_questions.append({
                    "type": "text",
                    "text": q_text,
                    "expected_answers": expected,
                })
                st.success("Question added." + ("" if expected else " (marked for manual grading)"))

    if st.session_state.draft_questions:
        st.markdown(f"**Questions so far ({len(st.session_state.draft_questions)}):**")
        for i, q in enumerate(st.session_state.draft_questions, 1):
            if q["type"] == "mcq":
                st.write(f"{i}. [MCQ] {q['text']}  —  ✅ {q['options'][q['correct_index']]}")
            else:
                tag = "auto-graded" if q["expected_answers"] else "manual review"
                st.write(f"{i}. [Free text — {tag}] {q['text']}")

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


def admin_dashboard():
    st.subheader("📊 Dashboard")
    quizzes = list_quizzes()
    if not quizzes:
        st.info("No quizzes created yet.")
        return

    options = {f"{q['title']}  ({q['code']})": q["code"] for q in quizzes}
    choice = st.selectbox("Select a quiz", list(options.keys()))
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

        # Detailed per-question export, including free-text responses for manual grading
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

    autorefresh(5000, key="admin_refresh")


def admin_view():
    tab1, tab2 = st.tabs(["➕ Create quiz", "📊 Dashboard"])
    with tab1:
        admin_create_quiz()
    with tab2:
        admin_dashboard()


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

    # Auto-submit if time is up
    if rem <= 0 and not already_submitted:
        mark_submitted(code, name)
        already_submitted = True

    st.subheader(quiz["title"])

    if already_submitted:
        st.success("✅ Submitted. Thanks!")
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

    q_labels = [f"Q{i+1}" + (" ✓" if i in answers else "") for i in range(len(questions))]
    q_idx = st.radio("Jump to question:", list(range(len(questions))),
                      format_func=lambda i: q_labels[i], horizontal=True)

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

    st.caption(f"Answered {len(answers)} of {len(questions)} questions.")

    st.divider()
    if st.button("✅ Submit final answers now", type="primary"):
        mark_submitted(code, name)
        st.rerun()

    autorefresh(3000, key="participant_refresh")


def participant_view():
    if "p_code" in st.session_state and "p_name" in st.session_state:
        participant_quiz(st.session_state.p_code, st.session_state.p_name)
    else:
        participant_entry()


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Timed Quiz", page_icon="⏱️", layout="centered")
    init_db()

    st.title("⏱️ Team Quiz")

    role = st.sidebar.radio("I am a...", ["Participant", "Admin"])

    if role == "Admin":
        admin_view()
    else:
        participant_view()


if __name__ == "__main__":
    main()
