import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import re
import json
import io
import hashlib
import google.generativeai as genai

# ---------- File parsing ----------
import pypdf
from docx import Document
from pptx import Presentation
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# =====================================================
# DB
# =====================================================

DB_FILE = "pk_study_log.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS materials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        file_hash TEXT UNIQUE,
        uploaded_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        material_id INTEGER,
        topic TEXT,
        question TEXT,
        choices_json TEXT,
        correct TEXT,
        explanation TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_key TEXT UNIQUE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER,
        question_id INTEGER,
        is_correct INTEGER,
        answered_at TEXT,
        misconception_note TEXT
    )
    """)

    def ensure_misconception_column():
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("PRAGMA table_info(answers)")
        cols = [row[1] for row in c.fetchall()]
        if "misconception_note" not in cols:
            c.execute("ALTER TABLE answers ADD COLUMN misconception_note TEXT")
            conn.commit()
        conn.close()


    conn.commit()
    conn.close()
    ensure_misconception_column()

def calc_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def get_or_create_material(file_name: str, data: bytes):
    file_hash = calc_file_hash(data)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        "SELECT id FROM materials WHERE file_hash = ?",
        (file_hash,)
    )
    row = c.fetchone()

    if row:
        material_id = row[0]
    else:
        c.execute(
            "INSERT INTO materials (title, file_hash, uploaded_at) VALUES (?, ?, ?)",
            (
                file_name,
                file_hash,
                datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
            )
        )
        material_id = c.lastrowid
        conn.commit()

    conn.close()
    return material_id


def log_answer(student_id, question_id, is_correct, misconception_note=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    INSERT INTO answers
    (student_id, question_id, is_correct, answered_at, misconception_note)
    VALUES (?, ?, ?, ?, ?)
    """, (
        student_id,
        question_id,
        int(is_correct),
        datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        misconception_note
    ))

    conn.commit()
    conn.close()

    
def get_stats(student_id):
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql("""
        SELECT
            a.id,
            q.topic,
            a.is_correct
        FROM answers a
        JOIN questions q ON a.question_id = q.id
        WHERE a.student_id = ?
    """, conn, params=(student_id,))
    conn.close()
    return df


# =====================================================
# Gemini
# =====================================================

def configure_gemini():
    api_key = st.secrets.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        st.error("❌ GEMINI_API_KEY が設定されていません")
        return False
    genai.configure(api_key=api_key)
    return True


# =====================================================
# File extraction
# =====================================================
def chunk_text(text, size=500, overlap=100):
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start += size - overlap
    return chunks


def retrieve_relevant_chunks(chunks, query, top_k=3):
    if not chunks:
        return []

    vec = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b", max_df=0.9)
    X = vec.fit_transform(chunks + [query])
    sims = cosine_similarity(X[-1], X[:-1])[0]
    idx = sims.argsort()[-top_k:][::-1]
    return [chunks[i] for i in idx]



def extract_from_pdf(data):
    reader = pypdf.PdfReader(io.BytesIO(data))
    texts = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            texts.append(f"【ページ {i+1}】\n{text}")
    return "\n\n".join(texts)

def extract_from_docx(data):
    doc = Document(io.BytesIO(data))
    texts = []
    for p in doc.paragraphs:
        if p.style.name.startswith("Heading"):
            texts.append(f"\n## {p.text}\n")
        else:
            texts.append(p.text)
    return "\n".join(texts)

def extract_from_xlsx(data):
    xl = pd.ExcelFile(io.BytesIO(data))
    texts = []
    for sheet in xl.sheet_names:
        df = xl.parse(sheet)
        texts.append(f"\n## シート: {sheet}\n")
        texts.append(df.to_csv(index=False))
    return "\n".join(texts)

def extract_from_pptx(data):
    prs = Presentation(io.BytesIO(data))
    texts = []
    for i, slide in enumerate(prs.slides):
        texts.append(f"\n## スライド {i+1}\n")
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                texts.append(shape.text)
    return "\n".join(texts)

def extract_text_from_bytes(data: bytes, filename: str):
    ext = filename.split(".")[-1].lower()

    if ext == "pdf":
        return extract_from_pdf(data)
    if ext == "docx":
        return extract_from_docx(data)
    if ext == "xlsx":
        return extract_from_xlsx(data)
    if ext == "pptx":
        return extract_from_pptx(data)

    raise ValueError("未対応のファイル形式です")



# =====================================================
# AI problem generation
# =====================================================

def safe_json_load(text: str):
    # コードブロックの除去
    text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    # 1. そのまま試行
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 範囲抽出
    start_candidates = [i for i in [text.find("{"), text.find("[")] if i != -1]
    if not start_candidates:
        raise ValueError(f"JSONが見つかりません\n\n--- Gemini出力 ---\n{text}")

    start = min(start_candidates)
    end_obj = text.rfind("}")
    end_arr = text.rfind("]")
    end = max(end_obj, end_arr)

    # 閉じカッコが見つからない場合、文字列の最後までを対象とする
    if end == -1 or end <= start:
        json_text = text[start:].strip()
    else:
        json_text = text[start:end + 1].strip()

    # 3. 解析を試み、失敗したら閉じカッコを補完してリトライ
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        try:
            # 強引に閉じカッコを付け足してみる（単純な生成中断対策）
            return json.loads(json_text + "}")
        except:
            try:
                return json.loads(json_text + "]}") # ネスト対策
            except:
                raise ValueError(f"JSON解析失敗: 構造が壊れています。\n\n--- 抽出JSON ---\n{json_text}")

def generate_one_ai_problem(text, problem_no):
    model = genai.GenerativeModel("gemini-2.5-flash-lite")

    prompt = f"""
以下の資料をもとに、薬剤師国家試験形式の五肢択一問題を1問作成してください。


【重要】
これは【{problem_no}問目】です。
これまでとは異なる論点・概念・知識を使ってください。
同じ問題・類似問題は禁止です。

【条件】※必ず厳守すること
・5択単一正解
・choices は A〜E の5つすべてを含める
・正解は必ず "correct" キーで出力する（A〜E の1文字）
・解説は必ず "explanation" キーで出力する（1〜3文）
・JSON以外の文章は一切出力しない

出力形式:
{{
  "topic": "分野名",
  "question": "問題文",
  "choices": {{
    "A": "選択肢",
    "B": "選択肢",
    "C": "選択肢",
    "D": "選択肢",
    "E": "選択肢"
  }},
  "correct": "A",
  "explanation": "解説"
}}

資料（関連部分のみ）:
{text}
"""

    response = model.generate_content(
        prompt,
        generation_config={
            "temperature": 0.1,
            "max_output_tokens": 500
        }
    )

    if not response.candidates:
        raise ValueError("Geminiが応答を返しませんでした")

    c = response.candidates[0]
    if not c.content or not c.content.parts:
        raise ValueError(f"Gemini出力が空です (finish_reason={c.finish_reason})")

    raw = c.content.parts[0].text
    data = safe_json_load(raw)

    # Geminiが配列で返してきた場合にも対応
    if isinstance(data, list):
        if not data:
            raise ValueError("Geminiが空配列を返しました")
        return data[0]

    # オブジェクトで返してきた場合
    return data

    
def generate_ai_problems(text, n=3):
    problems = []
    for i in range(n):
        p = generate_one_ai_problem(text, i + 1)
        problems.append(p)
    return problems

def generate_misconception_note(
    topic: str,
    question: str,
    choices: dict,
    correct: str,
    selected: str
) -> str | None:
    """
    誤答時の「学問的つまずきの示唆」を1文で生成
    ※ 内部ログ専用（学生非表示）
    """
    model = genai.GenerativeModel("gemini-2.5-flash-lite")

    prompt = f"""
以下は薬剤師国家試験形式の問題です。

分野: {topic}
問題文:
{question}

選択肢:
{json.dumps(choices, ensure_ascii=False)}

正解: {correct}
学生の選択: {selected}

この誤答から考えられる
「学習上のつまずき」を
【1文のみ】で述べてください。

【重要】
・断定は禁止
・「〜の可能性があります」など可能性表現を用いる
・評価・叱責・診断語は禁止
・学問的内容に限定する
"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 100
            }
        )
        text = response.text.strip()
        if text:
            return text
    except Exception:
        pass

    return None

   
def get_ai_coaching_message(df, recent_n=5):
    """
    5問ごとの通常コーチング
    ・よくある誤解
    ・暗記か理解かを明示
    """
    if df.empty:
        return ""

    # --- 累積統計 ---
    total_stats = df.groupby("topic").agg(
        正解数=("is_correct", "sum"),
        回答数=("id", "count")
    )
    total_stats["正答率"] = total_stats["正解数"] / total_stats["回答数"]

    # --- 直近 n 問 ---
    recent_df = df.tail(recent_n)
    recent_stats = recent_df.groupby("topic").agg(
        正解数=("is_correct", "sum"),
        回答数=("id", "count")
    )
    recent_stats["正答率"] = recent_stats["正解数"] / recent_stats["回答数"]

    prompt = f"""
あなたは薬剤師国家試験の学習を支援するコーチです。

以下は【直近{recent_n}問】の分野別成績です。
{recent_stats.to_csv()}

以下は【これまで全体】の分野別成績です。
{total_stats.to_csv()}

この情報をもとに、
・直近で目立った誤解や混同しやすいポイント
・その分野は「暗記重視」か「理解重視」か
を中心に、穏やかなコーチ口調で簡潔に述べてください。

【注意】
・叱責は禁止
・前向きな助言にする
・挨拶文は不要
"""

    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    response = model.generate_content(
        prompt,
        generation_config={"temperature": 0.2, "max_output_tokens": 600}
    )

    return response.text

def get_ai_final_coaching_message(df):
    """
    全問終了時の最終コーチング
    ・数値を明示
    ・成長を言語化
    ・継続の動機づけ
    """
    if df.empty:
        return ""

    total_answered = len(df)
    total_correct = df["is_correct"].sum()
    total_rate = total_correct / total_answered

    stats = df.groupby("topic").agg(
        正解数=("is_correct", "sum"),
        回答数=("id", "count")
    )
    stats["正答率"] = stats["正解数"] / stats["回答数"]

    prompt = f"""
あなたは薬剤師国家試験の学習を支援するコーチです。

以下は、ある学生の今回の学習結果です。

・総回答数: {total_answered}
・正解数: {total_correct}
・正答率: {total_rate:.0%}

分野別成績:
{stats.to_csv()}

この結果をもとに、
・今回しっかり取り組めた点
・理解が定着してきている分野
・努力が成果につながっている点
を具体的に示し、学習継続の意欲が高まるような
前向きで穏やかなコーチングコメントを書いてください。

【注意】
・叱責や否定は禁止
・比較は禁止
・挨拶文は不要
"""

    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    response = model.generate_content(
        prompt,
        generation_config={"temperature": 0.3, "max_output_tokens": 700}
    )

    return response.text

   



# =====================================================
# UI
# =====================================================
student_key = st.text_input("学籍番号またはニックネーム")
def normalize_problem(p: dict) -> dict:
    required = ["topic", "question", "choices", "correct", "explanation"]
    missing = [k for k in required if k not in p]

    if missing:
        raise ValueError(f"必須キー不足: {missing}")

    if not isinstance(p["choices"], dict) or len(p["choices"]) != 5:
        raise ValueError("choices が不正です")

    if p["correct"] not in p["choices"]:
        raise ValueError("correct が choices に含まれていません")

    return p



def get_or_create_student(student_key):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        "SELECT id FROM students WHERE student_key = ?",
        (student_key,)
    )
    row = c.fetchone()

    if row:
        student_id = row[0]
    else:
        c.execute(
            "INSERT INTO students (student_key) VALUES (?)",
            (student_key,)
        )
        student_id = c.lastrowid
        conn.commit()

    conn.close()
    return student_id

def delete_questions_by_material(material_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute(
        "DELETE FROM questions WHERE material_id = ?",
        (material_id,)
    )

    conn.commit()
    conn.close()

    
def save_questions(material_id, problems):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    valid_count = 0

    for p in problems:
        try:
            p = normalize_problem(p)
        except Exception as e:
            st.warning(f"⚠️ 不正な問題を除外しました: {e}")
            continue

        c.execute("""
        INSERT INTO questions
        (material_id, topic, question, choices_json, correct, explanation)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            material_id,
            p["topic"],
            p["question"],
            json.dumps(p["choices"], ensure_ascii=False),
            p["correct"],
            p["explanation"]
        ))
        valid_count += 1

    conn.commit()
    conn.close()

    if valid_count == 0:
        raise ValueError("有効な問題が1問もありませんでした")


    
def main():
    st.set_page_config("AIコーチング学習アプリ", layout="centered")
    st.title("📚 AIコーチング学習アプリ")

    init_db()
    if not configure_gemini():
        return

    if "text" not in st.session_state:
        st.session_state.text = None
    if "problems" not in st.session_state:
        st.session_state.problems = []
    if "idx" not in st.session_state:
        st.session_state.idx = 0
    if "answered_idx" not in st.session_state:
        st.session_state.answered_idx = {}
    if "is_correct_idx" not in st.session_state:
        st.session_state.is_correct_idx = {}


    tab1, tab2, tab3 = st.tabs(["資料", "問題演習", "コーチング"])

    # ---------- 資料 ----------
    with tab1:
        file = st.file_uploader(
            "資料をアップロード",
            type=["pdf", "docx", "xlsx", "pptx"]
        )

        if file:
            with st.spinner("資料解析中..."):
                data = file.read()

                material_id = get_or_create_material(file.name, data)
                st.session_state.material_id = material_id

                st.session_state.text = extract_text_from_bytes(data, file.name)

            st.success("資料を読み込みました")


            if st.button("AI問題を生成"):
                try:
                    with st.spinner("問題生成中..."):
                        if "material_id" not in st.session_state:
                            st.error("資料が読み込まれていません")
                            return

                        chunks = chunk_text(st.session_state.text)

                        retrieved = retrieve_relevant_chunks(
                            chunks,
                            query="薬剤師国家試験の五肢択一問題を作成する"
)

                        context = "\n\n".join(retrieved)

                        problems = generate_ai_problems(context)

                        
                        if not problems:
                            raise ValueError("問題が1問も生成できませんでした（Gemini出力/JSON解析失敗の可能性）")

                       
                        # ① DB保存
                        save_questions(st.session_state.material_id, problems)
                        # ② DBから読み直す
                        conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
                        df = pd.read_sql(
                            """
                            SELECT * FROM questions
                            WHERE material_id = ?
                            ORDER BY id
                            """,
                            conn,
                            params=(st.session_state.material_id,)
                        )
                        conn.close()
                        # ③ session_state に入れる
                        st.session_state.problems = df.to_dict("records")
                                         
                    st.session_state.idx = 0
                    st.session_state.answered = False
                    st.session_state.answered_idx = {}
                    st.session_state.is_correct_idx = {}
                    st.success("問題を生成しました")
                    st.rerun()

                except Exception as e:
                    st.error("❌ 問題生成に失敗しました")
                    st.exception(e)
     
    # ---------- 問題 ----------
    with tab2:
        if not student_key:
            st.warning("学籍番号またはニックネームを入力してください")
            st.stop()

        # --- idx の安全化 ---
        if st.session_state.idx < 0:
            st.session_state.idx = 0

        
        if not st.session_state.problems and "material_id" in st.session_state:
            conn = sqlite3.connect(DB_FILE)
            df = pd.read_sql(
                """
                SELECT * FROM questions
                WHERE material_id = ?
                ORDER BY id
                """,
                conn,
                params=(st.session_state.material_id,)
            )
            conn.close()
            st.session_state.problems = df.to_dict("records")
            
        if not st.session_state.problems:
            st.info("問題がまだありません")
            st.stop()

    # --- 全問終了 ---
        if st.session_state.problems and st.session_state.idx >= len(st.session_state.problems):
            st.success("🎉 すべての問題が終了しました！")
            
            student_id = get_or_create_student(student_key)
            df = get_stats(student_id)
            
            correct = sum(st.session_state.is_correct_idx.values())
            total = len(st.session_state.problems)
            st.write(f"正解数: {correct} / {total}")

            if st.button("もう一度最初から"):
                st.session_state.idx = 0
                st.rerun()
            return
            
         # --- 問題表示 ---
        p = st.session_state.problems[st.session_state.idx]
        st.subheader(f"問題 {st.session_state.idx + 1}")
        st.markdown(p["question"])

        # --- choices を dict に変換（1問分） ---
        choices = json.loads(p["choices_json"])

        choice = st.radio(
            "選択肢",
            options=list(choices.keys()),
            format_func=lambda x: f"{x}: {choices[x]}",
            key=f"choice_{p['id']}"
        )


        # --- 解答する ---
        answered = st.session_state.answered_idx.get(st.session_state.idx, False)
        if not answered:
            if st.button("解答する"):
                st.session_state.answered_idx[st.session_state.idx] = True

                is_correct = (choice == p["correct"])
                st.session_state.is_correct_idx[st.session_state.idx] = is_correct

                student_id = get_or_create_student(student_key)

            # --- 誤答時のみ学問的示唆を生成 ---
                misconception_note = None
                if not is_correct:
                    misconception_note = generate_misconception_note(
                        topic=p["topic"],
                        question=p["question"],
                        choices=json.loads(p["choices_json"]),
                        correct=p["correct"],
                        selected=choice
                    )

                log_answer(
                    student_id,
                    p["id"],
                    is_correct,
                    misconception_note
                )

                st.rerun()




        # --- 解答後表示 ---
        answered = st.session_state.answered_idx.get(st.session_state.idx, False)
        
        if answered:
            is_correct = st.session_state.is_correct_idx.get(st.session_state.idx, False)

            if is_correct:
                st.success("正解です 🎉")
            else:
                st.error(f"不正解です。正解は {p['correct']} です。")
                
            # --- 解説 ---
            st.markdown("### 解説")
            st.markdown(p["explanation"])

            # --- 解答数 ---
            answered_count = len(st.session_state.is_correct_idx)

            student_id = get_or_create_student(student_key)
            df = get_stats(student_id)

            # ===== 5問ごとの通常コーチング =====
            if answered_count > 0 and answered_count % 5 == 0 and answered_count < len(st.session_state.problems):
                st.markdown("---")
                st.markdown("### 🔍 今回の5問の振り返り")
                msg = get_ai_coaching_message(df, recent_n=5)
                st.info(msg)

            # ===== 最後の称賛コーチング =====
            if answered_count == len(st.session_state.problems):
                st.markdown("---")
                st.markdown("### 🎉 今回の学習のまとめ")
                final_msg = get_ai_final_coaching_message(df)
                st.success(final_msg)

            
            # --- 次の問題へ ---
            if st.button("次の問題へ"):
                st.session_state.idx += 1
                st.rerun()


    # ---------- コーチング ----------
    with tab3:
        student_id = get_or_create_student(student_key)
        df = get_stats(student_id)
        if df.empty:
            st.info("学習履歴がありません")
        else:
            st.subheader("分野別 正答率")
            stats = df.groupby("topic").agg(
                正解数=("is_correct", "sum"),
                回答数=("id", "count")
            )
            stats["正答率"] = stats["正解数"] / stats["回答数"]
            st.dataframe(stats, width="stretch")

            if st.button("AIコーチングを更新"):
                with st.spinner("分析中..."):
                    msg = get_ai_coaching_message(df)
                st.info(msg)


if __name__ == "__main__":
    main()
























































































