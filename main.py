import os
import json
import time
import re
import arxiv
import requests
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 1. 初期設定と認証
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
MODEL_CANDIDATES = [
    "gemini-3.5-flash"
]


def build_model():
    if not GEMINI_API_KEY:
        print("警告: GEMINI/GOOGLE API キーが設定されていません。環境変数 GEMINI_API_KEY または GOOGLE_API_KEY を設定してください。")
        return None

    genai.configure(api_key=GEMINI_API_KEY)

    last_error = None
    for model_name in MODEL_CANDIDATES:
        try:
            return genai.GenerativeModel(model_name)
        except Exception as e:
            last_error = e
            print(f"モデル '{model_name}' の初期化に失敗: {e}")

    if last_error is not None:
        print(f"利用可能なGeminiモデルの初期化に失敗しました: {last_error}")
    return None


model = build_model()

# ==========================================
# 2. arXivから論文を取得する関数
# ==========================================
def fetch_recent_papers(query, max_results=10):
    """Fetch recent papers using the arxiv package with a requests XML fallback.
    Returns list of dicts: {title, abstract, url}.
    """
    papers = []

    # Primary: use arxiv.Client() (if available and working)
    try:
        client = arxiv.Client()
        search = arxiv.Search(
            query=query,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate
        )

        for result in client.results(search):
            papers.append({
                "title": result.title,
                "abstract": getattr(result, 'summary', '') or getattr(result, 'abstract', ''),
                "url": getattr(result, 'entry_id', getattr(result, 'id', ''))
            })

        if papers:
            return papers
    except Exception as e:
        print(f"arxiv.Client() failed: {e}")

    # Fallback: call export.arxiv.org API directly using requests
    try:
        q = requests.utils.requote_uri(
            f"https://export.arxiv.org/api/query?search_query={query}&start=0&max_results={max_results}"
        )
        headers = {"User-Agent": "NewsAI/1.0 (+https://example.org)"}
        resp = requests.get(q, headers=headers, timeout=15)
        resp.raise_for_status()

        import xml.etree.ElementTree as ET
        ns = {'atom': 'http://www.w3.org/2005/Atom'}
        root = ET.fromstring(resp.text)
        entries = root.findall('atom:entry', ns)
        for e in entries:
            title = (e.find('atom:title', ns).text or '').strip()
            summary = (e.find('atom:summary', ns).text or '').strip()
            id_ = (e.find('atom:id', ns).text or '').strip()
            papers.append({
                'title': title,
                'abstract': summary,
                'url': id_
            })
        return papers
    except Exception as e:
        print(f"Fallback arXiv HTTP fetch failed: {e}")

    return []


# ==========================================
# 3. LLMでスコアリングと3行要約を行う関数
# ==========================================
def extract_json_array(raw_text):
    text = (raw_text or '').strip()
    if not text:
        raise ValueError("LLMから空の応答を受け取りました")

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.IGNORECASE)
    text = text.strip()

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError("LLM出力がJSON配列ではありません")
    return parsed


def extract_response_text(response):
    if hasattr(response, "text") and response.text:
        return response.text

    try:
        candidates = getattr(response, "candidates", [])
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", []) if content else []
            for part in parts:
                if hasattr(part, "text") and part.text:
                    return part.text
    except Exception:
        pass

    return str(response)


def evaluate_and_summarize(papers, evaluation_criteria, batch_size=5):
    """LLMをバッチで呼んで、レート制限に対してリトライを行う。"""
    if not papers:
        return []

    if model is None:
        return [
            {
                "title": paper.get("title", "Unknown"),
                "url": paper.get("url", ""),
                "score": 0,
                "summary": ["⚠️ Gemini APIキーが未設定のため要約をスキップしました", "-", "-"]
            }
            for paper in papers
        ]

    evaluated_papers = []

    def build_batch_prompt(batch):
        lines = [
            "あなたは最先端論文を評価するリサーチエージェントです。",
            "以下の複数の論文を読み、各論文について基準に従って評価と3行要約を行ってください。",
            "1行目: 要点、2行目: 新規性、3行目: 実用性。４行目: 高校生でもわかるような簡単な説明を追加してください。",
            "出力は必ずJSON配列で、各要素は {\"title\": ..., \"score\": 1-5, \"summary\": [line1, line2, line3, line4]} を返してください。",
            "評価基準:\n" + evaluation_criteria,
            "以下の論文を入力します。順番を変えずに出力してください。"
        ]

        for idx, paper in enumerate(batch):
            abstract = re.sub(r"\s+", " ", (paper.get('abstract') or '')).strip()[:1200]
            lines.append(f"---\nINDEX: {idx}\nTITLE: {paper.get('title', '')}\nABSTRACT: {abstract}")

        lines.append("出力はJSONのみ、説明やコードブロックは付けないでください。")
        return "\n\n".join(lines)

    for start in range(0, len(papers), batch_size):
        batch = papers[start:start + batch_size]
        prompt = build_batch_prompt(batch)

        for attempt in range(1, 10):
            try:
                response = model.generate_content(prompt)
                text = extract_response_text(response).strip()
                result = extract_json_array(text)

                for idx, item in enumerate(result[:len(batch)]):
                    paper = batch[idx]
                    score = item.get("score", 0)
                    summary = item.get("summary", ["要約情報の取得に失敗しました", "-", "-"])
                    evaluated_papers.append({
                        "title": item.get("title") or paper.get("title"),
                        "url": paper.get("url", ""),
                        "score": int(score) if isinstance(score, (int, float)) else 0,
                        "summary": summary[:3] if isinstance(summary, list) else ["要約情報の取得に失敗しました", "-", "-"]
                    })

                if len(result) < len(batch):
                    for idx in range(len(result), len(batch)):
                        paper = batch[idx]
                        evaluated_papers.append({
                            "title": paper.get("title", "Unknown"),
                            "url": paper.get("url", ""),
                            "score": 0,
                            "summary": ["⚠️ 要約の生成に失敗しました", "-", "-"]
                        })
                break

            except Exception as e:
                err_str = str(e)
                print(f"LLMバッチ処理エラー (batch {start}〜{start+len(batch)-1}) attempt {attempt}: {err_str}")
                if '429' in err_str or 'Too Many Requests' in err_str or 'Rate' in err_str:
                    sleep_time = 2 ** attempt
                    print(f"レート制限のため {sleep_time} 秒待ってリトライします...")
                    time.sleep(sleep_time)
                    continue

                for paper in batch:
                    evaluated_papers.append({
                        "title": paper.get("title", "Unknown"),
                        "url": paper.get("url", ""),
                        "score": 0,
                        "summary": [f"⚠️ LLM処理に失敗しました: {err_str}", "-", "-"]
                    })
                break

        time.sleep(5)

    evaluated_papers.sort(key=lambda x: x.get("score", 0), reverse=True)
    return evaluated_papers


# ==========================================
# 4. LINEへメッセージを送信する関数
# ==========================================
def send_line_message(sections):
    messages = []
    for section_title, papers in sections.items():
        text_content = f"【{section_title}】\n\n"
        for paper in papers:
            text_content += f"■ {paper.get('title', 'Unknown')} (スコア: {paper.get('score', 0)})\n"
            for line in paper.get('summary', []):
                text_content += f"・{line}\n"
            text_content += f"{paper.get('url', '')}\n\n"

        messages.append({
            "type": "text",
            "text": text_content.strip()
        })

    if not LINE_ACCESS_TOKEN:
        print("LINE_ACCESS_TOKEN が設定されていません。LINE送信をスキップします。")
        return

    if not LINE_USER_ID:
        print("LINE_USER_ID が設定されていません。LINE送信をスキップします。")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    data = {
        "to": LINE_USER_ID,
        "messages": messages[:5]
    }

    try:
        response = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data)
        response.raise_for_status()
        print("LINE送信ステータス:", response.status_code)
    except Exception as e:
        print("LINE送信に失敗しました:", e, "status:", getattr(response, 'status_code', 'unknown'), "body:", getattr(response, 'text', ''))


# ==========================================
# 5. メイン処理
# ==========================================
def main():
    final_output = {}

    routing_papers = fetch_recent_papers('all:"routing optimization" AND all:"communication"', 10)
    routing_eval = evaluate_and_summarize(routing_papers, "通信ネットワークにおけるルーティング最適化の新規性")

    semantic_papers = fetch_recent_papers('all:"semantic communication"', 10)
    semantic_eval = evaluate_and_summarize(semantic_papers, "セマンティック通信技術の新規性と実用性")

    ai_comms_selected = routing_eval[:1] + semantic_eval[:2]
    final_output[" AI×通信 (ルーティング/セマンティック)"] = ai_comms_selected

    research_papers = fetch_recent_papers('all:"AI in education" OR all:"LLM reasoning" OR all:"research assistant"', 5)
    research_eval = evaluate_and_summarize(research_papers, "AIの教育分野への効果的な活用例と一般学生が明日からでも実践できそうな実用性")
    final_output[" AIの学習・研究活用"] = research_eval[:1]

    psych_papers = fetch_recent_papers('all:"psychology" AND (all:"artificial intelligence" OR all:"LLM")', 5)
    psych_eval = evaluate_and_summarize(psych_papers, "AIと心理学・認知科学を組み合わせた研究の面白さと新規性")
    final_output[" AI×心理学"] = psych_eval[:1]

    send_line_message(final_output)


if __name__ == "__main__":
    main()
