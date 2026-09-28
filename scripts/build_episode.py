#!/usr/bin/env python3
"""
朝の日本経済・AIニュース：台本(.md) → 音声(mp3) → GitHub Release → Podcast RSS

GitHub Actions から実行される。ローカル確認用に --dry-run（APIを呼ばずテスト音を生成）あり。
依存：Python 3 標準ライブラリ、ffmpeg、gh CLI（Actions ランナーに標準搭載）
"""
import argparse
import base64
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
EPISODES_DIR = os.path.join(ROOT, "episodes")
BUILD = os.path.join(ROOT, "build")
INDEX_JSON = os.path.join(DOCS, "episodes.json")
JST = dt.timezone(dt.timedelta(hours=9))
SAMPLE_RATE = 24000  # Gemini TTS は 24kHz / 16bit / mono PCM を返す
DATE_RE = re.compile(r"^episodes/(\d{4}-\d{2}-\d{2})\.md$")


def log(*a):
    print(*a, flush=True)


def run(cmd, check=True, capture=True):
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout if capture else ""


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_index():
    if os.path.exists(INDEX_JSON):
        with open(INDEX_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_index(items):
    items.sort(key=lambda x: x["date"], reverse=True)
    with open(INDEX_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 台本の検出
def find_candidates():
    """main と全リモートブランチから episodes/YYYY-MM-DD.md を探す。
    戻り値: {date: (ref, path)}  同じ日付が複数ある場合は最新コミットのものを採用"""
    refs = ["HEAD"]
    out = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"], check=False)
    refs += [r for r in out.split() if r and not r.endswith("/HEAD") and r != "origin"]
    found = {}
    for ref in refs:
        files = run(["git", "ls-tree", "-r", "--name-only", ref, "episodes/"], check=False).split("\n")
        ts = run(["git", "log", "-1", "--format=%ct", ref], check=False).strip() or "0"
        for p in files:
            m = DATE_RE.match(p.strip())
            if not m:
                continue
            d = m.group(1)
            if d not in found or int(ts) > found[d][2]:
                found[d] = (ref, p.strip(), int(ts))
    return {d: (v[0], v[1]) for d, v in found.items()}


def read_from_ref(ref, path):
    return run(["git", "show", f"{ref}:{path}"])


# ---------------------------------------------------------------- 台本の解析
def parse_episode(text):
    meta = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"')
        body = m.group(2)

    def section(name):
        mm = re.search(rf"^##\s*{name}\s*\n(.*?)(?=^##\s|\Z)", body, re.S | re.M)
        return mm.group(1).strip() if mm else ""

    script = section("SCRIPT")
    sources = [l.lstrip("-* ").strip() for l in section("SOURCES").splitlines() if l.strip()]
    lines = [l.strip() for l in script.splitlines() if l.strip()]
    return meta, lines, sources


def validate_lines(lines, speakers):
    good = []
    for l in lines:
        name = l.split(":", 1)[0].strip() if ":" in l else ""
        if name in speakers and len(l.split(":", 1)[1].strip()) > 0:
            good.append(f"{name}: {l.split(':', 1)[1].strip()}")
        else:
            log(f"  [skip] 話者名のない行: {l[:40]}")
    if len(good) < 10:
        raise ValueError(f"台本の有効行が少なすぎます（{len(good)}行）")
    return good


def chunk_lines(lines, limit):
    chunks, cur, size = [], [], 0
    for l in lines:
        if cur and size + len(l) > limit:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(l)
        size += len(l)
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------- TTS
def tts_gemini(chunk, cfg, api_key):
    prompt = cfg["tts_style"] + "\n\n" + "\n".join(chunk)
    speakers = [
        {"speaker": s, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v}}}
        for s, v in cfg["speakers"].items()
    ]
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": speakers}},
        },
    }
    last_err = None
    for model in cfg["tts_models"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(5):
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                b64 = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
                log(f"  TTS OK model={model}")
                return base64.b64decode(b64)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="ignore")[:300]
                last_err = f"{model} HTTP {e.code}: {msg}"
                log(f"  TTS error: {last_err}")
                if e.code in (404, 400) and ("not found" in msg.lower() or "not supported" in msg.lower()):
                    break  # このモデルは使えない → 次のモデルへ
                if e.code == 429 or e.code >= 500:
                    time.sleep(min(90, 20 * (attempt + 1)))
                    continue
                break
            except Exception as e:  # ネットワーク等
                last_err = f"{model}: {e}"
                log(f"  TTS error: {last_err}")
                time.sleep(15)
    raise RuntimeError(f"TTS に失敗しました（無料枠の上限の可能性あり）: {last_err}")


def tts_dummy(chunk):
    """dry-run 用：文字数に比例した長さの小さなビープ音"""
    seconds = max(1.0, sum(len(l) for l in chunk) / 6.0 / 10)
    n = int(SAMPLE_RATE * seconds)
    return b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))) for i in range(n))


def audio_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def build_mp3(pcm, out_path, bitrate, cfg):
    """会話音声を整音し、あれば OP / ED ジングルを前後に付けて mp3 にする"""
    raw = os.path.join(BUILD, "tmp.pcm")
    voice = os.path.join(BUILD, "voice.wav")
    with open(raw, "wb") as f:
        f.write(pcm)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
         "-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1", voice],
        check=True,
    )
    os.remove(raw)

    op = os.path.join(ROOT, cfg.get("opening_audio", "")) if cfg.get("opening_audio") else ""
    ed = os.path.join(ROOT, cfg.get("ending_audio", "")) if cfg.get("ending_audio") else ""
    op = op if op and os.path.isfile(op) else ""
    ed = ed if ed and os.path.isfile(ed) else ""
    if not op and not ed:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", voice, "-b:a", bitrate, out_path], check=True)
        os.remove(voice)
        return

    # 配置：OP → （OP の余韻に少し重ねて）会話 → 少し間をあけて ED
    overlap = float(cfg.get("opening_overlap_sec", 1.0))
    gap = float(cfg.get("ending_gap_sec", 0.6))
    inputs, parts, t = [], [], 0.0
    if op:
        inputs += ["-i", op]
        parts.append((len(parts), 0.0))
        t = max(0.0, audio_seconds(op) - overlap)
    inputs += ["-i", voice]
    parts.append((len(parts), t))
    t += audio_seconds(voice) + gap
    if ed:
        inputs += ["-i", ed]
        parts.append((len(parts), t))
    chains = [f"[{i}:a]aformat=sample_rates=44100:channel_layouts=mono,adelay={int(d * 1000)}:all=1[a{i}]"
              for i, d in parts]
    mix = "".join(f"[a{i}]" for i, _ in parts) + f"amix=inputs={len(parts)}:duration=longest:normalize=0[out]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(chains + [mix]),
                    "-map", "[out]", "-ar", "44100", "-ac", "1", "-b:a", bitrate, out_path], check=True)
    os.remove(voice)


# ---------------------------------------------------------------- 公開
def repo_info():
    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    owner, name = repo.split("/", 1)
    site = f"https://{owner.lower()}.github.io/{name}/"
    return repo, site


def publish_release(repo, date, mp3_path, title):
    tag = f"ep-{date}"
    run(["gh", "release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag"], check=False)
    run(["gh", "release", "create", tag, mp3_path, "--repo", repo, "--title", title,
         "--notes", f"{date} の放送"])
    return f"https://github.com/{repo}/releases/download/{tag}/{os.path.basename(mp3_path)}"


def prune(items, keep, repo, dry):
    items.sort(key=lambda x: x["date"], reverse=True)
    for old in items[keep:]:
        log(f"古い回を削除: {old['date']}")
        if not dry:
            run(["gh", "release", "delete", f"ep-{old['date']}", "--repo", repo, "--yes", "--cleanup-tag"], check=False)
    return items[:keep]


def fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_feed(items, cfg, site):
    now = email.utils.format_datetime(dt.datetime.now(JST))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{html.escape(cfg['podcast_title'])}</title>",
        f"<link>{site}</link>",
        f'<atom:link href="{site}feed.xml" rel="self" type="application/rss+xml"/>',
        f"<language>{cfg['language']}</language>",
        f"<description>{html.escape(cfg['podcast_description'])}</description>",
        f"<itunes:author>{html.escape(cfg['podcast_author'])}</itunes:author>",
        f"<itunes:summary>{html.escape(cfg['podcast_description'])}</itunes:summary>",
        f'<itunes:image href="{site}cover.png"/>',
        '<itunes:category text="News"><itunes:category text="Business News"/></itunes:category>',
        "<itunes:explicit>false</itunes:explicit>",
        "<itunes:block>Yes</itunes:block>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for it in sorted(items, key=lambda x: x["date"], reverse=True):
        pub = email.utils.format_datetime(dt.datetime.fromisoformat(it["date"] + "T07:00:00+09:00"))
        src = "".join(f"<li>{html.escape(s)}</li>" for s in it.get("sources", []))
        desc = f"<p>{html.escape(it.get('summary', ''))}</p>" + (f"<p>主な情報源</p><ul>{src}</ul>" if src else "")
        out += [
            "<item>",
            f"<title>{html.escape(it['title'])}</title>",
            f"<description><![CDATA[{desc}]]></description>",
            f"<pubDate>{pub}</pubDate>",
            f'<guid isPermaLink="false">morning-news-{it["date"]}</guid>',
            f'<enclosure url="{it["url"]}" length="{it["bytes"]}" type="audio/mpeg"/>',
            f"<itunes:duration>{fmt_duration(it['seconds'])}</itunes:duration>",
            "</item>",
        ]
    out += ["</channel>", "</rss>"]
    with open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def write_index_html(items, cfg, site):
    rows = "".join(
        f'<li><time>{it["date"]}</time><a href="{it["url"]}">{html.escape(it["title"])}</a>'
        f'<span>{fmt_duration(it["seconds"])[3:]}</span></li>'
        for it in sorted(items, key=lambda x: x["date"], reverse=True)
    ) or "<li>まだ放送はありません。最初の回は翌朝に届きます。</li>"
    page = f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(cfg['podcast_title'])}</title>
<link rel="alternate" type="application/rss+xml" href="feed.xml">
<style>
:root{{--ink:#14213d;--paper:#fbfaf7;--sun:#f2a541;--mute:#5c6475;--line:#e3e0d8}}
@media (prefers-color-scheme:dark){{:root{{--ink:#eef0f5;--paper:#101522;--mute:#9aa3b5;--line:#2a3142}}}}
body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.75 "Hiragino Sans","Noto Sans JP",system-ui,sans-serif}}
main{{max-width:40rem;margin:0 auto;padding:max(2rem,env(safe-area-inset-top)) 1.25rem 3rem}}
h1{{font-size:1.9rem;line-height:1.3;margin:0 0 .5rem;border-left:.4rem solid var(--sun);padding-left:.8rem}}
p{{color:var(--mute);margin:.25rem 0 1.5rem}}
.feed{{display:block;padding:.9rem 1rem;border:1px solid var(--line);border-radius:.6rem;word-break:break-all;font-size:.9rem}}
ul{{list-style:none;padding:0;margin:2rem 0 0}}
li{{display:grid;grid-template-columns:6.5rem 1fr auto;gap:.75rem;padding:.8rem 0;border-top:1px solid var(--line)}}
time,span{{color:var(--mute);font-variant-numeric:tabular-nums}}
a{{color:inherit}}
</style></head><body><main>
<h1>{html.escape(cfg['podcast_title'])}</h1>
<p>{html.escape(cfg['podcast_description'])}</p>
<p>Podcastアプリに登録するURL</p>
<code class="feed">{site}feed.xml</code>
<ul>{rows}</ul>
</main></body></html>
"""
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


# ---------------------------------------------------------------- メイン
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="APIもGitHubも呼ばずにテスト")
    ap.add_argument("--delete-branches", action="store_true", help="処理済みの claude/* ブランチを削除")
    args = ap.parse_args()

    cfg = load_config()
    os.makedirs(BUILD, exist_ok=True)
    repo, site = repo_info()
    items = load_index()
    done = {it["date"] for it in items}

    candidates = find_candidates()
    todo = sorted(d for d in candidates if d not in done)
    log(f"検出した台本: {sorted(candidates)} / 未公開: {todo}")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if todo and not args.dry_run and not api_key:
        sys.exit("GEMINI_API_KEY が設定されていません（GitHub の Secrets を確認）")

    processed_refs = set()
    for date in todo[-3:]:  # 取りこぼしがあっても最大3回分まで（無料枠保護）
        ref, path = candidates[date]
        log(f"== {date} を処理（{ref}）")
        text = read_from_ref(ref, path)
        meta, lines, sources = parse_episode(text)
        lines = validate_lines(lines, cfg["speakers"])
        chunks = chunk_lines(lines, cfg["chunk_chars"])
        log(f"  {len(lines)}行 / {sum(len(l) for l in lines)}文字 / {len(chunks)}回に分けて音声化")

        silence = b"\x00\x00" * int(SAMPLE_RATE * 0.35)
        pcm = b""
        for i, ch in enumerate(chunks, 1):
            log(f"  音声化 {i}/{len(chunks)}")
            pcm += (tts_dummy(ch) if args.dry_run else tts_gemini(ch, cfg, api_key)) + silence
            if not args.dry_run and i < len(chunks):
                time.sleep(8)  # 分あたりの上限対策

        mp3 = os.path.join(BUILD, f"{date}.mp3")
        build_mp3(pcm, mp3, cfg["mp3_bitrate"], cfg)
        seconds = audio_seconds(mp3)
        title = meta.get("title") or f"{date} 朝の日本経済・AIニュース"
        url = f"https://github.com/{repo}/releases/download/ep-{date}/{date}.mp3" if args.dry_run \
            else publish_release(repo, date, mp3, title)

        # 台本を main に保存（記録用）
        os.makedirs(EPISODES_DIR, exist_ok=True)
        with open(os.path.join(ROOT, path), "w", encoding="utf-8") as f:
            f.write(text)

        items = [it for it in items if it["date"] != date] + [{
            "date": date, "title": title, "summary": meta.get("summary", ""),
            "sources": sources[:12], "url": url, "bytes": os.path.getsize(mp3), "seconds": round(seconds),
        }]
        processed_refs.add(ref)
        log(f"  完成: {fmt_duration(seconds)} / {os.path.getsize(mp3) // 1024} KB")

    items = prune(items, cfg["keep_episodes"], repo, args.dry_run)
    save_index(items)
    write_feed(items, cfg, site)
    write_index_html(items, cfg, site)

    if args.delete_branches and not args.dry_run:
        for ref in processed_refs:
            if ref.startswith("origin/claude/"):
                run(["git", "push", "origin", "--delete", ref[len("origin/"):]], check=False)
    log("完了")


if __name__ == "__main__":
    main()
