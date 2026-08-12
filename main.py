import os
import json
import datetime
import time  # 追記: APIの待機時間用
import re    # 追記: 余計な文字を削除するため
import arxiv
import requests
import google.generativeai as genai
from dotenv import load_dotenv
load_dotenv()

# ==========================================
# 1. 初期設定と認証
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
LINE_ACCESS_TOKEN = os.environ.get("LINE_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID") # プッシュ通知先

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('models/gemini-3.5-flash')

# ==========================================
# 2. arXivから前日の論文を取得する関数
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

    # Fallback: call export.arxiv.org API directly using requests (robust to some package/API mismatches)
    try:
        q = requests.utils.requote_uri(f"https://export.arxiv.org/api/query?search_query={query}&start=0&max_results={max_results}")
        headers = {"User-Agent": "NewsAI/1.0 (+https://example.org)"}
        resp = requests.get(q, headers=headers, timeout=15)
        resp.raise_for_status()

        # Parse Atom XML
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

    # If all methods fail, return empty list
    return []

# ==========================================
# 3. LLMでスコアリングと3行要約を行う関数 (修正版)
# ==========================================
def evaluate_and_summarize(papers, evaluation_criteria):
    evaluated_papers = []
    
    for paper in papers:
        prompt = f"""
        あなたは最先端論文を評価するリサーチエージェントです。
        以下の論文を読み、基準に従って評価と要約を行ってください。
        １行目は論文の要点を簡潔にまとめ、２行目は研究の新規性や独自性、３行目は実用性や応用可能性について述べてください。

        【評価基準】
        {evaluation_criteria}
        - 基準への適合度と新規性を1〜5点でスコア化してください。

        【論文データ】
        タイトル: {paper['title']}
        Abstract: {paper['abstract']}

        【出力要件】
        以下のJSON形式のみで出力してください。
        {{
            "score": 8,
            "summary": [
                "1行目の要約",
                "2行目の要約",
                "3行目の要約"
            ]
        }}
        """
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            
            # --- JSONパースの堅牢化（余計なマークダウン記号を削る） ---
            text = re.sub(r"^```(?:json)?", "", text)
            text = re.sub(r"```$", "", text)
            text = text.strip()
            
            result_json = json.loads(text)
            
            evaluated_papers.append({
                "title": paper["title"],
                "url": paper["url"],
                "score": result_json.get("score", 0),
                "summary": result_json.get("summary", ["要約情報の取得に失敗しました", "-", "-"])
            })
        except Exception as e:
            # エラーが起きてもスキップせず、タイトルとURLだけはリストに残す（フェイルセーフ）
            print(f"LLM処理エラー ({paper['title']}): {e}")
            evaluated_papers.append({
                "title": paper["title"],
                "url": paper["url"],
                "score": 0,
                "summary": ["⚠️ 要約の生成に失敗しました", f"エラー詳細: {e}"]
            })
            
        # APIのレート制限（Too Many Requests）を避けるために3秒待機
        time.sleep(10)

    # スコアの降順にソート
    evaluated_papers.sort(key=lambda x: x["score"], reverse=True)
    return evaluated_papers

# ==========================================
# 4. LINEへメッセージを送信する関数 (Flex Message対応準備)
# ==========================================
def send_line_message(sections):
    messages = []
    for section_title, papers in sections.items():
        text_content = f"【{section_title}】\n\n"
        for p in papers:
            text_content += f"■ {p['title']} (スコア: {p['score']})\n"
            for line in p['summary']:
                text_content += f"・{line}\n"
            text_content += f"{p['url']}\n\n"
        
        messages.append({
            "type": "text",
            "text": text_content.strip()
        })

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_ACCESS_TOKEN}"
    }
    data = {
        "to": LINE_USER_ID,
        "messages": messages[:5] # LINE APIの制限で一度に5件まで
    }

    response = requests.post("https://api.line.me/v2/bot/message/push", headers=headers, json=data)
    print("LINE送信ステータス:", response.status_code)

# ==========================================
# 5. メイン処理
# ==========================================
def main():
    final_output = {}

    # --- カテゴリ1: AI×通信 ---
    # ルーティング最適化 (V2Xなどのネットワーク応用を含む)
    routing_papers = fetch_recent_papers('all:"routing optimization" AND (all:"communication" OR all:"V2X")', 3)
    routing_eval = evaluate_and_summarize(routing_papers, "通信ネットワークにおけるルーティング最適化の新規性")
    
    # セマンティック通信等
    semantic_papers = fetch_recent_papers('all:"semantic communication"', 3)
    semantic_eval = evaluate_and_summarize(semantic_papers, "セマンティック通信技術の新規性と実用性")

    # 選定: ルーティング1件、セマンティック通信2件 
    ai_comms_selected = routing_eval[:1] + semantic_eval[:2]
    final_output[" AI×通信 (ルーティング/セマンティック)"] = ai_comms_selected

    # --- カテゴリ2: AIの学習・研究活用 ---
    research_papers = fetch_recent_papers('all:"AI in education" OR all:"LLM reasoning" OR all:"research assistant"', 2)
    research_eval = evaluate_and_summarize(research_papers, "AIの教育分野への効果的な活用例としての実用性")
    final_output[" AIの学習・研究活用"] = research_eval[:1]

    # --- カテゴリ3: AI×心理学 ---
    psych_papers = fetch_recent_papers('all:"psychology" AND (all:"artificial intelligence" OR all:"LLM")', 2)
    psych_eval = evaluate_and_summarize(psych_papers, "AIと心理学・認知科学を組み合わせた研究の面白さと新規性")
    final_output[" AI×心理学"] = psych_eval[:1]

    # LINEへ送信
    send_line_message(final_output)

if __name__ == "__main__":
    main()