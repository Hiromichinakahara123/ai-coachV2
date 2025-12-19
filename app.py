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
import requests

# ---------- File parsing ----------
import pypdf
from docx import Document
from pptx import Presentation
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


# =====================================================
# Hugging Face / Gemma API
# =====================================================

def hf_generate(prompt: str, max_tokens=500, temperature=0.1) -> str:
    hf_token = st.secrets.get("HF_TOKEN") or os.getenv("HF_TOKEN")
    if not hf_token:
        raise RuntimeError("HF_TOKEN が設定されていません")

    API_URL = "https://api-inference.huggingface.co/models/google/gemma-3-4b-it"
    headers = {
        "Authorization": f"Bearer {hf_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": max_tokens,
            "temperature": temperature,
            "return_full_text": False
        }
    }

    r = requests.post(API_URL, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list) and data and "generated_text" in data[0]:
        return data[0]["generated_text"]

    raise ValueError(f"Unexpected HF response: {data}")


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

    conn.commit()
    conn.close()


def calc_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_or_create_material(file_name: str, data: bytes):
    file_hash = calc_file_hash(data)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("SELECT id FROM materials WHERE file_hash = ?", (file_hash,))
    row = c.fetchone()

    if row:
        material_id = row[0]
    else:
        c.execute(
            "INSERT INTO materials (title, file_hash, uploaded_at) VALUES (?, ?, ?)",
            (file_name, file_hash, datetime.now(ZoneInfo("Asia/Tokyo")).isoformat())
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
# JSON safety
# =====================================================

def safe_json_load(text: str):
    text = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = min(i for i in [text.find("{"), text.find("[")] if i != -1)
    end = max(text.rfind("}"), text.rfind("]"))

    json_text = text[start:end + 1] if end > start else text[start:]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        raise ValueError(f"JSON解析失敗\n---\n{json_text}")


# =====================================================
# AI generation (Gemma)
# =====================================================

def generate_one_ai_problem(text, problem_no):
    prompt = f"""
以下の資料をもとに、薬剤師国家試験形式の五肢択一問題を1問作成してください。

【重要】
これは【{problem_no}問目】です。
これまでとは異なる論点・概念・知識を使ってください。

【条件】※必ず厳守
・5択単一正解
・choices は A〜E
・correct は A〜E
・JSON以外出力禁止

出力形式:
{{
  "topic": "...",
  "question": "...",
  "choices": {{
    "A": "...",
    "B": "...",
    "C": "...",
    "D": "...",
    "E": "..."
  }},
  "correct": "A",
  "explanation": "..."
}}

資料:
{text}
"""
    raw = hf_generate(prompt, max_tokens=500, temperature=0.1)
    data = safe_json_load(raw)
    return data[0] if isinstance(data, list) else data


def generate_misconception_note(topic, question, choices, correct, selected):
    prompt = f"""
以下は薬剤師国家試験形式の問題です。

分野: {topic}
問題文:
{question}

選択肢:
{json.dumps(choices, ensure_ascii=False)}

正解: {correct}
学生の選択: {selected}

この誤答から考えられる学習上のつまずきを
【1文のみ】で述べてください。
"""
    try:
        return hf_generate(prompt, max_tokens=100, temperature=0.2).strip()
    except Exception:
        return None


def get_ai_coaching_message(df, recent_n=5):
    if df.empty:
        return ""

    total = df.groupby("topic").agg(正解数=("is_correct", "sum"), 回答数=("id", "count"))
    recent = df.tail(recent_n).groupby("topic").agg(正解数=("is_correct", "sum"), 回答数=("id", "count"))

    prompt = f"""
あなたは薬剤師国家試験の学習コーチです。

直近{recent_n}問:
{recent.to_csv()}

全体:
{total.to_csv()}

誤解しやすい点と、
暗記重視か理解重視かを
穏やかに述べてください。
"""
    return hf_generate(prompt, max_tokens=600, temperature=0.2)


def get_ai_final_coaching_message(df):
    total = len(df)
    correct = df["is_correct"].sum()
    rate = correct / total

    stats = df.groupby("topic").agg(正解数=("is_correct", "sum"), 回答数=("id", "count"))

    prompt = f"""
以下は学習結果です。

総回答数: {total}
正解数: {correct}
正答率: {rate:.0%}

分野別:
{stats.to_csv()}

前向きで継続意欲が高まる
コーチングコメントを書いてください。
"""
    return hf_generate(prompt, max_tokens=700, temperature=0.3)


# =====================================================
# UI / main（変更なし）
# =====================================================

def main():
    st.set_page_config("AIコーチング学習アプリ")
    st.title("📚 AIコーチング学習アプリ")
    init_db()
    st.info("Gemma (Hugging Face Inference API) 使用中")

if __name__ == "__main__":
    main()
