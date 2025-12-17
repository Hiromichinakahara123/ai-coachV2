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
        answered_at TEXT
    )
    """)

    conn.commit()
    conn.close()

def calc_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def get_or_create_material(file):
    data = file.read()
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
                file.name,
                file_hash,
                datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
            )
        )
        material_id = c.lastrowid
        conn.commit()

    conn.close()
    return material_id, data

def log_answer(student_id, question_id, is_correct):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
    INSERT INTO answers
    (student_id, question_id, is_correct, answered_at)
    VALUES (?, ?, ?, ?)
    """, (
        student_id,
        question_id,
        int(is_correct),
        datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
    ))

    conn.commit()
    conn.close()
    
def get_stats():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql("""
        SELECT
            a.id,
            q.topic,
            a.is_correct
        FROM answers a
        JOIN questions q ON a.question_id = q.id
    """, conn)
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
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks

def retrieve_relevant_chunks(chunks, query, top_k=3):
    vec = TfidfVectorizer(
        token_pattern=r"(?u)\b\w+\b",
        max_df=0.9
    )
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

def extract_text(uploaded_file):
    data = uploaded_file.read()
    ext = uploaded_file.name.split(".")[-1].lower()

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

   
def get_ai_coaching_message(df):
    if df.empty:
        return "まだ学習履歴がありません。"

    # 分野別統計
    stats = df.groupby("topic").agg(
        正解数=("is_correct", "sum"),
        回答数=("id", "count")
    )
    stats["正答率"] = stats["正解数"] / stats["回答数"]
    stats_csv = stats.sort_values("正答率").to_csv()

    # --- RAG: 教材から学習指導に関連する部分を抽出 ---
    if "text" in st.session_state and st.session_state.text:
        chunks = chunk_text(st.session_state.text)
        retrieved = retrieve_relevant_chunks(
            chunks,
            query="薬剤師国家試験 分野別 学習指導 弱点"
        )
        context = "\n\n".join(retrieved)
    else:
        context = ""

    model = genai.GenerativeModel("gemini-2.5-flash-lite")

    prompt = f"""
あなたは【薬学教育・国家試験指導を専門とする大学教員】です。

以下は、ある学生の分野別成績です。
{stats_csv}

以下は、対応する教材の抜粋です。
{context}

この情報をもとに、
・つまずきやすい概念
・混同しやすいポイント
・理解を深めるための学習の工夫
をそれぞれ簡潔かつ具体的に述べてください。

【重要】
・前置きや挨拶は禁止
・分析から書き始める
"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 1000
            }
        )
        return response.text

    except Exception as e:
        return f"❌ AIコーチング生成エラー: {e}"



# =====================================================
# UI
# =====================================================
student_key = st.text_input("学籍番号またはニックネーム")
def normalize_problem(p: dict) -> dict:
    # --- correct の揺れ対応 ---
    if "correct" not in p:
        for k in ["answer", "correct_answer", "正解"]:
            if k in p:
                p["correct"] = p[k]
                break

     # --- ★ correct が無い場合の最終救済 ---
    if "correct" not in p:
        # choices がある場合のみ救済
        if "choices" in p and isinstance(p["choices"], dict):
            # 仮で A を正解にする（ログ用途）
            p["correct"] = list(p["choices"].keys())[0]
            p["_warning"] = "correct が Gemini 出力に存在しなかったため自動補完"
        else:
            raise ValueError("❌ correct も choices も存在しません")
            
    # --- explanation が無い場合の補完 ---
    if "explanation" not in p:
        p["explanation"] = "解説はAIによって自動生成されました。"

    # --- 最終チェック ---
    required = ["topic", "question", "choices", "correct", "explanation"]
    missing = [k for k in required if k not in p]

    if missing:
        raise ValueError(
            f"❌ 問題データに必須キーが不足しています: {missing}\n\n{p}"
        )

    # --- correct が choices に存在するか ---
    if p["correct"] not in p["choices"]:
        raise ValueError(
            f"❌ correct が choices に含まれていません: {p['correct']}\n\n{p}"
        )

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
    
def save_questions(material_id, problems):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    for p in problems:
        p = normalize_problem(p)   # ← ★ この1行を追加

        if "_warning" in p:
            st.warning(f"⚠️ 問題生成警告: {p['_warning']}")
            
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

    conn.commit()
    conn.close()

    
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
    if "answered" not in st.session_state:
        st.session_state.answered = False
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
                material_id, _ = get_or_create_material(file)
                st.session_state.material_id = material_id
                file.seek(0)
                st.session_state.text = extract_text(file)
            st.success("資料を読み込みました")

            if st.button("AI問題を生成"):
                try:
                    with st.spinner("問題生成中..."):
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
                        # ③ session_state に入れる
                        st.session_state.problems = df.to_dict("records")
                                         
                    st.session_state.idx = 0
                    st.session_state.answered = False
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

        if st.session_state.idx >= len(st.session_state.problems):
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

            df = get_stats()
            correct = df["is_correct"].sum() if not df.empty else 0
            st.write(f"正解数: {correct} / {len(st.session_state.problems)}")

            if st.button("もう一度最初から"):
                st.session_state.idx = 0
                st.session_state.answered = False
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
            key=f"choice_{st.session_state.idx}"
        )


        # --- 解答する ---
        answered = st.session_state.answered_idx.get(st.session_state.idx, False)
        if not answered:
            if st.button("解答する"):
                st.session_state.answered_idx[st.session_state.idx] = True

                # ★ 正誤判定を変数に保持
                is_correct = (choice == p["correct"])
                st.session_state.is_correct_idx[st.session_state.idx] = is_correct

                student_id = get_or_create_student(student_key)

                # ★ 修正ポイント：存在しない is_correct を参照しない
                log_answer(student_id, p["id"], is_correct)



        # --- 解答後表示 ---
        is_correct = st.session_state.is_correct_idx.get(st.session_state.idx, False)
        if is_correct:
            st.success("正解です 🎉")
        else:
            st.error(f"不正解です。正解は {p['correct']} です。")

            st.markdown("### 解説")
            st.markdown(p["explanation"])

            # --- 次の問題へ ---
            if st.button("次の問題へ"):
                st.session_state.idx += 1
                st.rerun()






    # ---------- コーチング ----------
    with tab3:
        df = get_stats()
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













































































