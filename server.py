import os
import re
import json
import requests
import feedparser
import trafilatura
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
import anthropic
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SUB_URL = "https://mailchi.mp/cce5b43af537/subscribe-to-ime-brief"

NEWS_FEEDS = [
    {"name": "Jerusalem Post",  "url": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx"},
    {"name": "Ynet News",       "url": "https://www.ynetnews.com/Integration/StoryRss3082.xml"},
    {"name": "Israel Hayom",    "url": "https://www.israelhayom.com/feed/"},
    {"name": "Al-Monitor",      "url": "https://www.al-monitor.com/rss"},
    {"name": "Times of Israel", "url": "https://news.google.com/rss/search?q=site:timesofisrael.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Haaretz",         "url": "https://news.google.com/rss/search?q=site:haaretz.com+Israel&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Arutz Sheva",     "url": "https://news.google.com/rss/search?q=site:israelnationalnews.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "i24 News",        "url": "https://news.google.com/rss/search?q=site:i24news.tv+Israel&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Reuters",         "url": "https://news.google.com/rss/search?q=Israel+Middle+East+site:reuters.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "AP",              "url": "https://news.google.com/rss/search?q=Israel+Middle+East+site:apnews.com&hl=en-US&gl=US&ceid=US:en"},
    {"name": "Axios",           "url": "https://news.google.com/rss/search?q=Israel+Middle+East+site:axios.com&hl=en-US&gl=US&ceid=US:en"},
]

COMMENTARY_FEEDS = [
    {"name": "Jerusalem Post Opinion",  "url": "https://www.jpost.com/rss/rssfeedsopinion.aspx"},
    {"name": "Ynet Opinions",           "url": "https://www.ynetnews.com/Integration/StoryRss3084.xml"},
    {"name": "Israel Hayom Opinions",   "url": "https://www.israelhayom.com/opinions/feed/"},
    {"name": "Times of Israel Blogs",   "url": "https://news.google.com/rss/search?q=site:blogs.timesofisrael.com&hl=en-US&gl=US&ceid=US:en"},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def clean_text(text):
    """Remove HTML tags, clean whitespace, strip characters that break JSON"""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    text = soup.get_text()
    text = re.sub(r'\s+', ' ', text)
    text = text.replace('\u2028', ' ')
    text = text.replace('\u2029', ' ')
    text = text.replace('\r', ' ')
    text = ''.join(c for c in text if ord(c) >= 32 or c == '\n')
    text = text.encode('utf-8', errors='ignore').decode('utf-8')
    return text.strip()


def fetch_rss_feed(feed, hours=28):
    """Fetch articles from an RSS feed published in the last N hours"""
    articles = []
    try:
        parsed = feedparser.parse(feed["url"])
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        for entry in parsed.entries[:30]:
            # Parse date
            pub = None
            for f in ["published_parsed", "updated_parsed"]:
                val = getattr(entry, f, None)
                if val:
                    try:
                        pub = datetime(*val[:6], tzinfo=timezone.utc)
                    except Exception:
                        pass
                    break

            if pub and pub < cutoff:
                continue

            title = clean_text(entry.get("title", ""))
            url = entry.get("link", "")
            summary = clean_text(entry.get("summary", ""))[:600]

            if title and url:
                articles.append({
                    "title": title,
                    "url": url,
                    "source": feed["name"],
                    "summary": summary,
                    "sentences": "",
                    "author": "",
                    "selected": False,
                })
    except Exception as e:
        print(f"RSS error [{feed['name']}]: {e}")
    return articles


def scrape_sentences(url, num=3):
    """Extract verbatim opening sentences using trafilatura"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return ""
        text = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            no_fallback=False,
        )
        if not text:
            return ""
        # Split into sentences naively and take first N
        parts = re.split(r'(?<=[.!?])\s+', text.strip())
        sentences = " ".join(parts[:num]).strip()
        return sentences
    except Exception as e:
        print(f"Scrape error [{url}]: {e}")
        return ""


def scrape_author(url):
    """Try to extract author name from article page"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return ""
        meta = trafilatura.extract_metadata(resp.text)
        if meta and meta.author:
            return meta.author.strip()
        # Fallback: look for byline in HTML
        soup = BeautifulSoup(resp.content, "html.parser")
        for sel in ['[class*="author"]', '[rel="author"]', '[class*="byline"]', 'meta[name="author"]']:
            el = soup.select_one(sel)
            if el:
                author = el.get("content", "") or el.get_text()
                author = clean_text(author)
                if author and 3 < len(author) < 60:
                    return author
    except Exception:
        pass
    return ""


def claude_select(articles, kind, max_items):
    """Ask Claude to pick the most relevant Israel/Middle East articles"""
    if not articles:
        return []
    lines = "\n".join(f"{i+1}. [{a['source']}] {a['title']}" for i, a in enumerate(articles))
    prompt = (
        f"From the list of {'news headlines' if kind == 'news' else 'opinion/commentary pieces'} below, "
        f"select the {max_items} most significant and relevant to Israel and the Middle East today. "
        f"Ignore duplicates. Return ONLY a JSON array of 1-based index numbers like [1, 5, 8]. No other text.\n\n{lines}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text
        m = re.search(r'\[[\d,\s]+\]', text)
        if m:
            indices = [int(x) for x in json.loads(m.group(0))]
            return [articles[i - 1] for i in indices if 0 < i <= len(articles)]
    except Exception as e:
        print(f"Claude select error: {e}")
    return articles[:max_items]


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/debug-feeds")
def debug_feeds():
    """Test all RSS feeds and report which ones are working"""
    results = []
    all_feeds = NEWS_FEEDS + COMMENTARY_FEEDS
    for feed in all_feeds:
        try:
            parsed = feedparser.parse(feed["url"])
            count = len(parsed.entries)
            first = parsed.entries[0].get("title", "no title") if count > 0 else "no entries"
            results.append({
                "name": feed["name"],
                "status": "ok",
                "articles": count,
                "first_title": first[:80]
            })
        except Exception as e:
            results.append({
                "name": feed["name"],
                "status": "error",
                "error": str(e)
            })
    return jsonify(results)


@app.route("/fetch", methods=["POST"])
def fetch():
    """Fetch, select, and scrape today's articles"""
    # 1. Pull all RSS feeds
    all_news = []
    for feed in NEWS_FEEDS:
        all_news.extend(fetch_rss_feed(feed, hours=28))

    all_com = []
    for feed in COMMENTARY_FEEDS:
        all_com.extend(fetch_rss_feed(feed, hours=52))

    # 2. Claude picks best articles
    news_selected = claude_select(all_news, "news", 12)
    com_selected = claude_select(all_com, "commentary", 4)

    # 3. Scrape verbatim opening sentences for news
    for art in news_selected:
        sentences = scrape_sentences(art["url"])
        art["sentences"] = sentences if sentences else art["summary"]
        art["sentences_source"] = "scraped" if sentences else "rss_summary"

    # 4. Scrape author for commentary
    for art in com_selected:
        art["author"] = scrape_author(art["url"])

    return jsonify({
        "news": news_selected,
        "commentary": com_selected,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
    })


@app.route("/generate", methods=["POST"])
def generate():
    """Build the three Mailchimp HTML blocks from selected articles"""
    data = request.json
    date = data.get("date", "")
    headlines = data.get("headlines", [])
    commentary = data.get("commentary", [])

    # ── Block 1: Content TOC (plain text, no links) ──────────────────────
    hl_items = ""
    for h in headlines:
        t = h["title"].replace('"', '&quot;')
        hl_items += (
            f'<li style="text-align:left;">'
            f'<p style="text-align:left;">'
            f'<strong><span style="font-size:18px">'
            f'<span style="font-family:Arial,\'Helvetica Neue\',Helvetica,sans-serif">{t}</span>'
            f'</span></strong></p></li>\n'
        )

    com_items = ""
    for c in commentary:
        t = c["title"].replace('"', '&quot;')
        author = c.get("author", "[Author]")
        com_items += (
            f'<li><p class="" style="text-align:left;">'
            f'<strong><span style="font-size:18px">'
            f'<span style="font-family:Arial,\'Helvetica Neue\',Helvetica,sans-serif">'
            f'&ldquo;{t}&rdquo; - {author}'
            f'</span></span></strong></p></li>\n'
        )

    block1 = (
        f'<p class="" style="text-align:center;"><strong><span style="font-size:18px">'
        f'<span style="font-family:Arial,\'Helvetica Neue\',Helvetica,sans-serif">Headlines</span>'
        f'</span></strong></p>\n<ul>\n{hl_items}</ul>\n'
        f'<p class="" style="text-align:center;"><strong><span style="font-size:18px">'
        f'<span style="font-family:Arial,\'Helvetica Neue\',Helvetica,sans-serif">Commentary</span>'
        f'</span></strong></p>\n<ul>\n{com_items}</ul>'
    )

    # ── Block 2: Headlines with sentences ────────────────────────────────
    block2 = ""
    for h in headlines:
        t = h["title"].replace('"', '&quot;')
        sentences = h.get("sentences", "").strip()
        source = h.get("source", "")
        block2 += (
            f'<p style="line-height:0;mso-line-height-alt:0%;"></p>\n'
            f'<h1 class="mcePastedContent" style="text-align:left;">'
            f'<a href="{h["url"]}" target="_blank" tabindex="-1">'
            f'<strong><span style="font-size:29px">{t}</span></strong></a></h1>\n'
            f'<p class="" style="text-align:left;">{sentences} ({source})</p>\n'
            f'<p style="text-align:left;"></p>\n\n'
        )

    # ── Block 3: Commentary ───────────────────────────────────────────────
    block3 = ""
    for c in commentary:
        t = c["title"].replace('"', '&quot;')
        author = c.get("author", "[Author]")
        source = c.get("source", "")
        block3 += (
            f'<p style="text-align:left;">'
            f'<a href="{c["url"]}" target="_blank" tabindex="-1">'
            f'<strong><span style="font-size:21px">{t}</span></strong></a></p>\n'
            f'<p class="" style="text-align:left;">{author} ({source})</p>\n'
            f'<p class="" style="text-align:left;"></p>\n'
            f'<p style="line-height:0;mso-line-height-alt:0%;"></p>\n'
            f'<p style="line-height:0;mso-line-height-alt:0%;"></p>\n'
            f'<p style="line-height:0;mso-line-height-alt:0%;"></p>\n\n'
        )

    return jsonify({
        "block1": block1.strip(),
        "block2": block2.strip(),
        "block3": block3.strip(),
    })


@app.route("/mailchimp", methods=["POST"])
def mailchimp():
    """Create a Mailchimp campaign draft with the newsletter content"""
    data = request.json
    api_key = data.get("apiKey", "")
    list_id = data.get("listId", "")
    subject = data.get("subject", "")
    block1 = data.get("block1", "")
    block2 = data.get("block2", "")
    block3 = data.get("block3", "")
    date = data.get("date", "")

    if not api_key or not list_id:
        return jsonify({"error": "Mailchimp API key and Audience ID are required"}), 400

    dc = api_key.split("-")[-1] if "-" in api_key else "us1"
    base = f"https://{dc}.api.mailchimp.com/3.0"
    auth = ("anystring", api_key)

    full_html = f"""<div style="max-width:600px;margin:0 auto;font-family:Arial,sans-serif;">
<p style="text-align:center;font-style:italic;font-weight:700;font-size:18px;padding:16px 0;color:#111;">{date}</p>
{block1}
{block2}
{block3}
<div style="text-align:center;padding:20px 0;font-size:13px;color:#444;line-height:1.7;">
<p>Know someone who would appreciate this newsletter? Forward this email to them or share the subscribe link below.</p>
<a href="{SUB_URL}" style="display:inline-block;background:#1a3fa3;color:white;padding:10px 32px;border-radius:6px;text-decoration:none;font-weight:700;margin-top:12px;font-size:14px;">Subscribe</a>
</div></div>"""

    try:
        r = requests.post(
            f"{base}/campaigns",
            auth=auth,
            json={
                "type": "regular",
                "recipients": {"list_id": list_id},
                "settings": {
                    "subject_line": subject or f"Israel & Middle East Brief — {date}",
                    "from_name": "Israel & Middle East Brief",
                    "reply_to": os.environ.get("REPLY_TO_EMAIL", "newsletter@example.com"),
                },
            },
        )
        if r.status_code not in (200, 201):
            return jsonify({"error": f"Campaign creation failed: {r.text}"}), 400

        campaign_id = r.json()["id"]

        r2 = requests.put(
            f"{base}/campaigns/{campaign_id}/content",
            auth=auth,
            json={"html": full_html},
        )
        if r2.status_code not in (200, 204):
            return jsonify({"error": f"Content upload failed: {r2.text}"}), 400

        return jsonify({"success": True, "campaign_id": campaign_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
