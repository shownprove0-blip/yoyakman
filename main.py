"""
요약맨 (YoyakMan) - 카카오 i 오픈빌더 스킬 서버
뉴스 기사 URL 또는 유튜브 URL을 받아서 요약해서 반환합니다.
AI 엔진: Google Gemini (무료)

실행: uvicorn main:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from bs4 import BeautifulSoup
import google.generativeai as genai
import re
import os

app = FastAPI()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "여기에_Gemini_API_키_입력")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ──────────────────────────────────────────
# URL 타입 판별
# ──────────────────────────────────────────
def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url

def extract_youtube_id(url: str) -> str | None:
    patterns = [
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtu\.be/([^?]+)",
        r"youtube\.com/shorts/([^?]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

# ──────────────────────────────────────────
# 뉴스 기사 본문 추출
# ──────────────────────────────────────────
async def fetch_article_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # 불필요한 태그 제거
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "iframe"]):
        tag.decompose()

    # 주요 본문 선택자 순서대로 시도
    selectors = [
        "article",
        '[class*="article-body"]',
        '[class*="article_body"]',
        '[class*="news-content"]',
        '[class*="news_content"]',
        '[id*="articleBody"]',
        '[id*="article-body"]',
        "main",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text[:4000]  # Claude 입력 최대 4000자

    # fallback: body 전체
    return soup.body.get_text(separator="\n", strip=True)[:4000] if soup.body else ""

# ──────────────────────────────────────────
# 유튜브 자막 추출 (youtube_transcript_api)
# ──────────────────────────────────────────
async def fetch_youtube_transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        transcript_list = YouTubeTranscriptApi.get_transcript(
            video_id, languages=["ko", "en"]
        )
        full_text = " ".join(item["text"] for item in transcript_list)
        return full_text[:4000]
    except Exception as e:
        return f"[자막 추출 실패: {str(e)}]"

# ──────────────────────────────────────────
# Claude API로 요약
# ──────────────────────────────────────────
def summarize_with_gemini(content: str, content_type: str = "뉴스 기사") -> str:
    prompt = f"""다음 {content_type} 내용을 카카오톡 채팅방에 공유하기 좋게 요약해줘.

형식을 정확히 지켜줘:
📌 핵심 요약
[한 줄로 핵심 내용]

📋 주요 내용
• [포인트 1]
• [포인트 2]
• [포인트 3]

💡 한줄 평
[전체적인 의미나 시사점 한 줄]

--- 원문 ---
{content}"""

    response = model.generate_content(prompt)
    return response.text

# ──────────────────────────────────────────
# 카카오 오픈빌더 스킬 응답 형식
# ──────────────────────────────────────────
def kakao_response(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text
                    }
                }
            ]
        }
    }

# ──────────────────────────────────────────
# 메인 스킬 엔드포인트
# ──────────────────────────────────────────
@app.post("/summarize")
async def summarize(request: Request):
    try:
        body = await request.json()

        # 오픈빌더에서 사용자 발화(utterance) 가져오기
        utterance: str = body.get("userRequest", {}).get("utterance", "")

        # URL 추출
        url_pattern = r"https?://[^\s]+"
        urls = re.findall(url_pattern, utterance)

        if not urls:
            return JSONResponse(kakao_response(
                "❌ URL을 찾을 수 없어요.\n\n"
                "사용법: 요약하고 싶은 링크를 그냥 보내주세요!\n"
                "예) https://news.naver.com/...\n"
                "예) https://youtu.be/..."
            ))

        url = urls[0]

        # 유튜브 처리
        if is_youtube_url(url):
            video_id = extract_youtube_id(url)
            if not video_id:
                return JSONResponse(kakao_response("❌ 유튜브 링크를 인식할 수 없어요."))

            transcript = await fetch_youtube_transcript(video_id)

            if "자막 추출 실패" in transcript:
                return JSONResponse(kakao_response(
                    f"⚠️ 이 영상은 자막이 없어서 요약이 어려워요.\n({transcript})"
                ))

            summary = summarize_with_gemini(transcript, "유튜브 영상 자막")
            result = f"🎬 유튜브 영상 요약\n\n{summary}"

        # 일반 뉴스/기사 처리
        else:
            article_text = await fetch_article_text(url)

            if len(article_text) < 100:
                return JSONResponse(kakao_response(
                    "⚠️ 기사 내용을 가져오지 못했어요.\n"
                    "일부 사이트는 크롤링이 제한될 수 있어요."
                ))

            summary = summarize_with_gemini(article_text, "뉴스 기사")
            result = f"📰 기사 요약\n\n{summary}"

        return JSONResponse(kakao_response(result))

    except httpx.HTTPStatusError as e:
        return JSONResponse(kakao_response(f"❌ 페이지를 불러올 수 없어요. ({e.response.status_code})"))
    except Exception as e:
        return JSONResponse(kakao_response(f"❌ 오류가 발생했어요: {str(e)}"))


# ──────────────────────────────────────────
# 헬스체크 (Render.com 슬립 방지용)
# ──────────────────────────────────────────
@app.get("/")
def health_check():
    return {"status": "요약맨 서버 정상 작동 중 🤖"}
