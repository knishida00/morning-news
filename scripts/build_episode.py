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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
EPISODES_DIR = os.path.join(ROOT, "episodes")
BUILD = os.path.join(ROOT, "build")
INDEX_JSON = os.path.join(DOCS, "episodes.json")
JST = dt.timezone(dt.timedelta(hours=9))
SAMPLE_RATE = 24000  # Gemini TTS は 24kHz / 16bit / mono PCM を返す
DATE_RE = re.compile(r"^episodes/(\d{4}-\d{2}-\d{2})\.md$")
COLUMN_LOG = "episodes/column_log.md"
REBUILD_DAYS = 7  # 台本が書き換えられたら作り直す対象期間（無料枠保護のため直近のみ）


def text_hash(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


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
def tts_gemini(chunk, cfg, api_key, models=None, attempts=8):
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
    # 声のブレを抑える：乱数（seed）を固定し、揺らぎ（temperature）を控えめにする
    if cfg.get("tts_seed") is not None:
        body["generationConfig"]["seed"] = int(cfg["tts_seed"])
    if cfg.get("tts_temperature") is not None:
        body["generationConfig"]["temperature"] = float(cfg["tts_temperature"])
    last_err = None
    for model in (models or cfg["tts_models"]):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(attempts):
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                b64 = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
                log(f"  TTS OK model={model}")
                return base64.b64decode(b64), model
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


# ---------------------------------------------------------------- AivisSpeech（無料・ローカル実行）
# AivisSpeech Engine を GitHub Actions 上で起動して読み上げる。
# 同じ音声合成モデル・スタイルを使うので、1本の中でも毎日でも声がそろう。
# 有料の Aivis Cloud API は使わない。
AIVIS_PORT = 10101
_aivis_proc = None


def aivis_http(method, path, data=None, form=None, timeout=600):
    url = f"http://127.0.0.1:{AIVIS_PORT}{path}"
    headers = {}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # ローカル接続はプロキシを通さない
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def start_aivis(acfg):
    global _aivis_proc
    base = os.path.join(BUILD, "aivis")
    runner = None
    for root, _dirs, files in os.walk(base) if os.path.isdir(base) else []:
        if "run" in files and "engine_internal" in _dirs:
            runner = os.path.join(root, "run")
            break
    if runner is None:
        os.makedirs(base, exist_ok=True)
        archive = os.path.join(base, "engine.7z")
        log("  AivisSpeech Engine をダウンロード")
        run(["curl", "-sSL", "--retry", "3", "-o", archive, acfg["engine_url"]])
        if not shutil.which("7z"):
            run(["sudo", "apt-get", "install", "-y", "-q", "p7zip-full"], check=False)
        run(["7z", "x", "-y", f"-o{base}", archive])
        os.remove(archive)
        for root, _dirs, files in os.walk(base):
            if "run" in files and "engine_internal" in _dirs:
                runner = os.path.join(root, "run")
                break
    if runner is None:
        raise RuntimeError("AivisSpeech Engine の実行ファイルが見つかりません")
    os.chmod(runner, 0o755)
    logf = open(os.path.join(BUILD, "aivis_engine.log"), "w")
    _aivis_proc = subprocess.Popen([runner, "--host", "127.0.0.1", "--port", str(AIVIS_PORT)],
                                   stdout=logf, stderr=subprocess.STDOUT)
    import atexit
    atexit.register(stop_aivis)
    log("  AivisSpeech Engine を起動中（初回は読み上げ用データの取得に数分かかります）")
    for _ in range(300):
        if _aivis_proc.poll() is not None:
            raise RuntimeError("AivisSpeech Engine が起動できませんでした（build/aivis_engine.log を確認）")
        try:
            aivis_http("GET", "/version", timeout=5)
            return
        except Exception:
            time.sleep(3)
    raise RuntimeError("AivisSpeech Engine の起動がタイムアウトしました")


def stop_aivis():
    global _aivis_proc
    if _aivis_proc and _aivis_proc.poll() is None:
        _aivis_proc.terminate()
        try:
            _aivis_proc.wait(timeout=20)
        except Exception:
            _aivis_proc.kill()
    _aivis_proc = None


def aivis_style_ids(acfg):
    """設定した話者（モデル）を入れて、Aki / Ken のスタイル ID を調べる"""
    installed = json.loads(aivis_http("GET", "/aivm_models"))
    for name, sp in acfg["speakers"].items():
        if sp["model"] not in installed:
            log(f"  {name} の音声モデルをインストール（{sp['model']}）")
            aivis_http("POST", "/aivm_models/install",
                       form={"url": f"https://hub.aivis-project.com/aivm-models/{sp['model']}"}, timeout=1800)
    speakers = json.loads(aivis_http("GET", "/speakers"))
    ids = {}
    for name, sp in acfg["speakers"].items():
        cand = [x for x in speakers if x.get("speaker_uuid") == sp.get("speaker_uuid")] or \
               [x for x in speakers if x.get("name") == sp.get("speaker_name")]
        if not cand:
            raise RuntimeError(f"{name} の話者が見つかりません: {sp}")
        styles = cand[0]["styles"]
        st = next((x for x in styles if x["name"] == sp.get("style")), styles[0])
        ids[name] = st["id"]
        log(f"  {name} = {cand[0]['name']}（{st['name']} / ID {st['id']}）")
    return ids


def tts_aivis_episode(lines, cfg):
    """台本を1行ずつ読み上げ、24kHz / 16bit / mono の PCM を返す"""
    import io
    import wave
    acfg = cfg["aivis"]
    start_aivis(acfg)
    try:
        ids = aivis_style_ids(acfg)
        gap = b"\x00\x00" * int(SAMPLE_RATE * float(acfg.get("line_gap_sec", 0.3)))
        pcm = b""
        for i, line in enumerate(lines, 1):
            name, text = line.split(":", 1)
            name, text = name.strip(), text.strip()
            sp = acfg["speakers"][name]
            sid = ids[name]
            q = json.loads(aivis_http("POST", "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": sid})))
            q.update(outputSamplingRate=SAMPLE_RATE, outputStereo=False, prePhonemeLength=0.05, postPhonemeLength=0.1)
            for key, conf in (("speedScale", "speed"), ("intonationScale", "style_strength"),
                              ("tempoDynamicsScale", "tempo_dynamics"), ("pitchScale", "pitch")):
                if conf in sp:
                    q[key] = sp[conf]
            wav = aivis_http("POST", f"/synthesis?speaker={sid}", data=q)
            with wave.open(io.BytesIO(wav)) as w:
                if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                    raise RuntimeError("AivisSpeech の出力形式が想定外です")
                pcm += w.readframes(w.getnframes()) + gap
            if i % 10 == 0 or i == len(lines):
                log(f"  読み上げ {i}/{len(lines)} 行")
        return pcm
    finally:
        stop_aivis()


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


# ---------------------------------------------------------------- 音声の配信（GitHub Pages）
# GitHub Releases は mp3 を「application/octet-stream」で返すため、
# iPhone の Podcast アプリはダウンロードせずに再生（ストリーミング）できない。
# そこで直近の回の mp3 を gh-pages ブランチに置き、GitHub Pages から
# 正しい種類（audio/mpeg）で配信する。gh-pages は毎回履歴なしで作り直し、
# リポジトリが肥大化しないようにする。Releases は保管庫として残す。
PAGES_BRANCH = "gh-pages"
PAGES_MARKER = "pages-source.txt"


def pages_ready(site):
    """GitHub Pages が gh-pages ブランチから配信されているか（目印ファイルの有無で判定）"""
    import urllib.request
    try:
        with urllib.request.urlopen(site + PAGES_MARKER + f"?t={int(time.time())}", timeout=15) as r:
            return r.status == 200
    except Exception:
        return False


def pages_audio_dates(items, cfg):
    keep = int(cfg.get("pages_audio_episodes", 120))
    return {it["date"] for it in sorted(items, key=lambda x: x["date"], reverse=True)[:keep]}


def set_play_urls(items, cfg, site, ready):
    on_pages = pages_audio_dates(items, cfg) if ready else set()
    for it in items:
        it["play_url"] = f"{site}audio/{it['date']}.mp3" if it["date"] in on_pages else it["url"]


def sync_pages(items, cfg, repo, built_dates, dry):
    site_dir = os.path.join(BUILD, "site")
    shutil.rmtree(site_dir, ignore_errors=True)
    os.makedirs(os.path.join(site_dir, "audio"))

    # 前回までに置いた音声を引き継ぐ
    if run(["git", "rev-parse", "--verify", "--quiet", f"origin/{PAGES_BRANCH}"], check=False):
        tar = subprocess.run(["git", "archive", f"origin/{PAGES_BRANCH}", "audio"], cwd=ROOT, capture_output=True)
        if tar.returncode == 0 and tar.stdout:
            subprocess.run(["tar", "-x", "-C", site_dir], input=tar.stdout, check=True)

    # サイト本体（docs の中身）を最新にする
    for name in os.listdir(DOCS):
        src = os.path.join(DOCS, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(site_dir, name))
    open(os.path.join(site_dir, ".nojekyll"), "w").close()
    with open(os.path.join(site_dir, PAGES_MARKER), "w") as f:
        f.write("gh-pages\n")

    # 直近の回の音声をそろえ、それより古いものは消す
    want = pages_audio_dates(items, cfg)
    audio_dir = os.path.join(site_dir, "audio")
    for name in os.listdir(audio_dir):
        if name[:-4] not in want:
            os.remove(os.path.join(audio_dir, name))
    for d in sorted(want):
        dst = os.path.join(audio_dir, f"{d}.mp3")
        local = os.path.join(BUILD, f"{d}.mp3")
        if d in built_dates and os.path.isfile(local):
            shutil.copy2(local, dst)
        elif not os.path.isfile(dst) and not dry:
            run(["gh", "release", "download", f"ep-{d}", "--repo", repo, "--pattern", f"{d}.mp3",
                 "--dir", audio_dir, "--clobber"], check=False)
    log(f"Pages に置く音声: {len(os.listdir(audio_dir))} 回分")

    if dry:
        return
    # 履歴を持たない1コミットとして gh-pages を置き換える
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(BUILD, "pages.index"),
               GIT_AUTHOR_NAME="podcast-bot", GIT_AUTHOR_EMAIL="podcast-bot@users.noreply.github.com",
               GIT_COMMITTER_NAME="podcast-bot", GIT_COMMITTER_EMAIL="podcast-bot@users.noreply.github.com")
    if os.path.exists(env["GIT_INDEX_FILE"]):
        os.remove(env["GIT_INDEX_FILE"])
    g = lambda *a: subprocess.run(["git", f"--work-tree={site_dir}", *a], cwd=ROOT, env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()
    g("add", "-A", ".")
    tree = g("write-tree")
    old = run(["git", "rev-parse", "--verify", "--quiet", f"origin/{PAGES_BRANCH}^{{tree}}"], check=False).strip()
    if old == tree:
        log("Pages は変更なし")
        return
    commit = g("commit-tree", tree, "-m", f"配信用サイトを更新 {dt.datetime.now(JST):%Y-%m-%d %H:%M}")
    run(["git", "push", "--force", "origin", f"{commit}:refs/heads/{PAGES_BRANCH}"])
    log("Pages（gh-pages）を更新")


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
            f'<guid isPermaLink="false">morning-news-{it["date"]}{"-r" + str(it["rev"]) if it.get("rev", 1) > 1 else ""}</guid>',
            f'<enclosure url="{it.get("play_url", it["url"])}" length="{it["bytes"]}" type="audio/mpeg"/>',
            f"<itunes:duration>{fmt_duration(it['seconds'])}</itunes:duration>",
            "</item>",
        ]
    out += ["</channel>", "</rss>"]
    with open(os.path.join(DOCS, "feed.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def write_index_html(items, cfg, site):
    rows = "".join(
        f'<li><time>{it["date"]}</time><a href="{it.get("play_url", it["url"])}">{html.escape(it["title"])}</a>'
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
    by_date = {it["date"]: it for it in items}
    candidates = find_candidates()

    # 以前の版で作った回には台本の指紋がないので、main にある台本から補う
    for d, it in by_date.items():
        if "sha" not in it and d in candidates:
            head = run(["git", "show", f"HEAD:episodes/{d}.md"], check=False)
            if head:
                it["sha"] = text_hash(head)

    today = dt.datetime.now(JST).date()
    todo, texts = [], {}
    for d in sorted(candidates):
        ref, path = candidates[d]
        texts[d] = read_from_ref(ref, path)
        if d not in by_date:
            todo.append(d)
        elif by_date[d].get("sha") != text_hash(texts[d]) and \
                (today - dt.date.fromisoformat(d)).days <= REBUILD_DAYS:
            todo.append(d)  # 台本が作り直された回
    log(f"検出した台本: {sorted(candidates)} / 今回作る回: {todo}")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    engine = cfg.get("tts_engine", "gemini")
    if todo and not args.dry_run and not api_key:
        if engine == "gemini":
            sys.exit("GEMINI_API_KEY が設定されていません（GitHub の Secrets を確認）")
        log("注意: GEMINI_API_KEY がないため、AivisSpeech が失敗したときの予備（Gemini）は使えません")

    processed_refs = set()
    for date in todo[-3:]:  # 取りこぼしがあっても最大3回分まで（無料枠保護）
        ref, path = candidates[date]
        log(f"== {date} を処理（{ref}）")
        text = texts[date]
        meta, lines, sources = parse_episode(text)
        lines = validate_lines(lines, cfg["speakers"])
        chunks = chunk_lines(lines, cfg["chunk_chars"])
        log(f"  {len(lines)}行 / {sum(len(l) for l in lines)}文字 / {len(chunks)}回に分けて音声化")

        silence = b"\x00\x00" * int(SAMPLE_RATE * 0.35)

        def synthesize(parts, attempts=8):
            # 声の質感をそろえるため、1回分はすべて同じモデルで音声化する。
            # 途中でそのモデルが使えなくなったら、次のモデルで最初から作り直す。
            models = list(cfg["tts_models"])
            while True:
                pcm, used = b"", None
                try:
                    for i, ch in enumerate(parts, 1):
                        log(f"  音声化 {i}/{len(parts)}")
                        if args.dry_run:
                            audio = tts_dummy(ch)
                        else:
                            audio, used = tts_gemini(ch, cfg, api_key, [used] if used else models, attempts)
                        pcm += audio + silence
                        if not args.dry_run and i < len(parts):
                            time.sleep(8)  # 分あたりの上限対策
                    return pcm
                except RuntimeError:
                    if used and used in models and models.index(used) + 1 < len(models):
                        log(f"  {used} が途中で使えなくなったため、次のモデルで最初から作り直します")
                        models = models[models.index(used) + 1:]
                        continue
                    raise

        pcm = None
        total_chars = sum(len(l) for l in lines)
        if engine == "aivis" and not args.dry_run:
            log("  AivisSpeech で音声化します")
            try:
                pcm = tts_aivis_episode(lines, cfg)
            except Exception as e:
                if not api_key:
                    raise
                log(f"  AivisSpeech での音声化に失敗 → 予備として Gemini で音声化します（{e}）")
        if pcm is None and cfg.get("single_pass", True) and len(chunks) > 1 and not args.dry_run:
            # まず台本全体を1回で音声化する（分割の境目で声が変わるのを防ぐ）
            log("  台本全体を1回で音声化します")
            try:
                whole = synthesize([lines], attempts=3)
                got = len(whole) / (SAMPLE_RATE * 2)
                expected = total_chars / 6.5  # 1秒あたり約6.5文字
                if got >= expected * 0.75:
                    pcm = whole
                else:
                    log(f"  音声が途中で切れた可能性（{got:.0f}秒 / 想定{expected:.0f}秒）→ 分割方式に切り替えます")
            except RuntimeError as e:
                log(f"  1回での音声化に失敗 → 分割方式に切り替えます（{e}）")
            if pcm is None:
                time.sleep(15)
        if pcm is None:
            pcm = synthesize(chunks)

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

        # コラムの記録（episodes/column_log.md）も main に反映する
        log_text = run(["git", "show", f"{ref}:{COLUMN_LOG}"], check=False)
        if log_text:
            with open(os.path.join(ROOT, COLUMN_LOG), "w", encoding="utf-8") as f:
                f.write(log_text)

        rev = by_date[date].get("rev", 1) + 1 if date in by_date else 1
        items = [it for it in items if it["date"] != date] + [{
            "date": date, "title": title, "summary": meta.get("summary", ""),
            "sources": sources[:12], "url": url, "bytes": os.path.getsize(mp3), "seconds": round(seconds),
            "sha": text_hash(text), "rev": rev,
        }]
        processed_refs.add(ref)
        log(f"  完成: {fmt_duration(seconds)} / {os.path.getsize(mp3) // 1024} KB")

    items = prune(items, cfg["keep_episodes"], repo, args.dry_run)
    ready = False if args.dry_run else pages_ready(site)
    log("音声の配信元: " + ("GitHub Pages" if ready else "GitHub Releases（Pages 未切り替え）"))
    set_play_urls(items, cfg, site, ready)
    save_index(items)
    write_feed(items, cfg, site)
    write_index_html(items, cfg, site)
    sync_pages(items, cfg, repo, set(todo[-3:]), args.dry_run)

    if args.delete_branches and not args.dry_run:
        for ref in processed_refs:
            if ref.startswith("origin/claude/"):
                run(["git", "push", "origin", "--delete", ref[len("origin/"):]], check=False)
    log("完了")


if __name__ == "__main__":
    main()
