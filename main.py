import os
import feedparser
import requests
from google import genai

# --- 1. 設定情報（環境変数から取得） ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

# --- 2. 情報収集 (RSSフィードから記事を取得) ---
def fetch_latest_articles():
    # 例：arXiv (AI分野) と はてなブックマーク (心理学・学術系) のRSS
    rss_urls = [
        "https://rss.arxiv.org/rss/cs.AI",  # arXiv AI
        "https://b.hatena.ne.jp/entrylist/knowledge.rss"  # はてな学び・学術
    ]
    
    articles = []
    for url in rss_urls:
        feed = feedparser.parse(url)
        for entry in feed.entries[:5]:  # 各フィードから上位5件を取得
            articles.append({
                "title": entry.title,
                "link": entry.link,
                "summary": entry.get("summary", "")
            })
    return articles

# --- 3. AIエージェントによる選定＆要約 ---
def summarize_with_gemini(articles):
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # 収集した記事をテキストにまとめる
    articles_text = ""
    for i, a in enumerate(articles, 1):
        articles_text += f"\n[{i}] タイトル: {a['title']}\nURL: {a['link']}\n概要: {a['summary'][:200]}\n"

    prompt = f"""
あなたは優秀な情報リサーチエージェントです。
以下は本日収集したニュース・記事・論文のリストです。

【収集データ】
{articles_text}

【指示】
1. 上記の中から、「AI技術の最新動向」および「生産性を高める心理学・科学」に関連する特に有益な記事を【合計2〜3本】厳選してください。
2. 毎朝LINEでサクッと読めるように、以下のフォーマットで出力してください。

【出力フォーマット】
🌅 本日の厳選リサーチニュース

■ [記事タイトル]
🔗 [URL]
💡 3行要約:
・要点1
・要点2
・要点3
🎯 実践・活用ポイント: (短く)

-------------------
(次の記事)
"""

    response = client.models.generate_content(
        model='models/gemini-3.5-flash',
        contents=prompt
    )
    return response.text

# --- 4. LINEへメッセージ送信 ---
def send_line_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code

# --- メイン処理 ---
if __name__ == "__main__":
    print("情報収集を開始します...")
    articles = fetch_latest_articles()
    
    print("Geminiで分析・要約中...")
    summary = summarize_with_gemini(articles)
    
    print("LINEへ送信中...")
    status = send_line_message(summary)
    if status == 200:
        print("送信完了しました！")
    else:
        print(f"送信失敗: {status}")