"""
Weekly YouTube playlist digest.

Pipeline:
  1. Pull videos published in the last N days from a playlist (YouTube Data API).
  2. Fetch each video's transcript via a managed transcript API (works fine from
     cloud runners, unlike the scraping-based youtube-transcript-api library,
     which gets IP-blocked on AWS/GCP/Azure/GitHub Actions).
  3. Run each transcript through a 3-stage Claude pass:
       extract -> verify -> synthesize
  4. Email one weekly digest with all the summaries.

Run weekly via the included GitHub Actions workflow (playlist-digest.yml).
No persistent state store is used — videos are selected by publish date with
a small lookback buffer, so a missed run just gets picked up next time.
"""

import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
PLAYLIST_ID = os.environ["PLAYLIST_ID"]
TRANSCRIPT_API_KEY = os.environ["TRANSCRIPT_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ["SMTP_USER"]
SMTP_PASS = os.environ["SMTP_PASS"]
DIGEST_TO = os.environ["DIGEST_TO"]

LOOKBACK_DAYS = 9  # weekly cadence + a few days' buffer in case a run is missed
TRANSCRIPT_CHAR_LIMIT = 15000  # keep prompts a reasonable size for long videos


def get_recent_videos():
    """Return videos published in the playlist within LOOKBACK_DAYS."""
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet,contentDetails",
        "playlistId": PLAYLIST_ID,
        "maxResults": 50,
        "key": YOUTUBE_API_KEY,
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    videos, next_page = [], None

    while True:
        if next_page:
            params["pageToken"] = next_page
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("items", []):
            published = datetime.fromisoformat(
                item["contentDetails"]["videoPublishedAt"].replace("Z", "+00:00")
            )
            if published >= cutoff:
                videos.append(
                    {
                        "video_id": item["contentDetails"]["videoId"],
                        "title": item["snippet"]["title"],
                        "published": published,
                    }
                )

        next_page = data.get("nextPageToken")
        if not next_page:
            break

    return videos


def get_transcript(video_id):
    """Fetch a transcript via a managed transcript API.

    Example below uses Supadata's endpoint/response shape as a placeholder —
    swap in whichever provider you choose (this space changes fast, so check
    current options/pricing rather than trusting this verbatim).
    """
    resp = requests.get(
        "https://api.supadata.ai/v1/youtube/transcript",
        params={"videoId": video_id},
        headers={"x-api-key": TRANSCRIPT_API_KEY},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Transcript API error: {data.get('message')}")
    return " ".join(seg["text"] for seg in data.get("content", []))


def call_claude(prompt, max_tokens=1500):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    blocks = resp.json()["content"]
    return "".join(b["text"] for b in blocks if b["type"] == "text")


def summarize_video(title, transcript):
    transcript = transcript[:TRANSCRIPT_CHAR_LIMIT]

    extraction = call_claude(f"""Video title: {title}

Transcript:
{transcript}

Extract, without summarizing yet:
1. The main claim or goal of the video.
2. Each concrete step or instruction given, in order.
3. The reasoning or evidence stated for each step (only what's actually
   said — don't fill in gaps or add outside knowledge).""")

    verification = call_claude(f"""Transcript:
{transcript}

Extracted steps and reasoning:
{extraction}

Check each piece of reasoning against the transcript:
- Is it actually supported by what's said?
- Is it internally consistent?
- Are there gaps, unsupported claims, or contradictions?
List any issues found. If everything checks out, say so explicitly.""")

    return call_claude(f"""Video title: {title}

Verified extraction:
{extraction}

Verification notes:
{verification}

Using only the reasoning that held up under verification, write a concise
summary (150-250 words) for a reader who hasn't watched the video:
- What it's about
- The key steps
- Why they matter (the reasoning that passed the check)
- Any caveats the verification flagged""")


def send_digest(entries):
    if not entries:
        print("No new videos this week — skipping email.")
        return

    sections = [
        f"{e['title']}\nhttps://www.youtube.com/watch?v={e['video_id']}\n\n{e['summary']}"
        for e in entries
    ]
    body = "\n\n-----\n\n".join(sections)

    msg = MIMEMultipart()
    msg["Subject"] = f"Weekly playlist digest — {len(entries)} video(s)"
    msg["From"] = SMTP_USER
    msg["To"] = DIGEST_TO
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def main():
    videos = get_recent_videos()
    entries = []

    for v in videos:
        try:
            transcript = get_transcript(v["video_id"])
            if not transcript.strip():
                print(f"No transcript available for {v['video_id']}, skipping.")
                continue
            summary = summarize_video(v["title"], transcript)
            entries.append({**v, "summary": summary})
        except Exception as exc:
            print(f"Skipping {v['video_id']} ({v['title']}): {exc}")

    send_digest(entries)


if __name__ == "__main__":
    main()
