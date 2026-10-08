#!/usr/bin/env python3
"""
朝の日本経済・AIニュース：台本(.md) → 音声(mp3) → GitHub Release → Podcast RSS

GitHub Actions から実行される。ローカル確認用に --dry-run（APIを呼ばずテスト音を生成）あり。
依存：Python 3 標準ライブラリ、ffmpeg、gh CLI（Actions ランナーに標準搭載）

音声化の流れ（Gemini の場合）
  1. 台本をパートごとに分ける（オープニング＋経済ニュース／資産形成コラム／AI活用コラム＋まとめ）
  2. パートごとに音声化し、その場で検査する
       ① 長さ（文字数から見て短すぎ・長すぎでないか）
       ② 波形（途中からの無音・雑音がないか）
       ③ 書き起こし（同じ部分の読み直し・読み飛ばしがないか）※無料枠の Gemini を使用
  3. 検査に通らないパートだけ作り直す。通ったものをつないで mp3 にする
  制限時間（time_budget_min）を超えそうなときは中断し、次の自動実行（7:40 / 8:40）に任せる。
"""
import argparse
import array
import base64
import datetime as dt
import difflib
import email.utils
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import unicodedata
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
LOG_FILES = [COLUMN_LOG, "episodes/bright_log.md", "episodes/money_log.md"]
REBUILD_DAYS = 7  # 台本が書き換えられたら作り直す対象期間の既定値（config.json の rebuild_days で変更可）
START_TIME = time.time()


class BudgetExceeded(Exception):
    """制限時間を超えそうなので中断する"""


class EpisodeFailed(Exception):
    """この回の音声を作れなかった（ほかの回の処理は続ける）"""


class TTSUnavailable(RuntimeError):
    """指定したモデルがどれも使えない（無料枠の上限、モデルの終了など）"""


def text_hash(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def log(*a):
    print(*a, flush=True)


def warn(msg):
    """GitHub Actions の実行結果ページに黄色い注意として表示する"""
    log(f"::warning::{msg}")


def remaining_sec(cfg):
    return float(cfg.get("time_budget_min", 48)) * 60 - (time.time() - START_TIME)


def sleep_in_budget(cfg, seconds):
    if remaining_sec(cfg) - seconds < float(cfg.get("finish_reserve_sec", 300)):
        raise BudgetExceeded("待ち時間を入れると制限時間を超えます")
    time.sleep(max(0.0, seconds))


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


# ---------------------------------------------------------------- パート分け
# 台本の「決まったセリフ」（CLAUDE.md の 8.）を目印に、話題の変わり目で分ける。
# 声は音声化1回ごとにわずかに変わるので、切れ目を話題の変わり目に置いて目立たなくする。
PART_MARKERS = [
    ("news", ("経済ニュースからです", "経済ニュースの振り返りからです")),
    ("bright", ("明日が楽しみになるニュースです",)),  # 旧構成の台本を作り直すとき用
    ("money", ("資産形成コラムです",)),
    ("ai", ("AI活用コラムのお時間です",)),
    ("summary", ("今日のまとめです", "今週のまとめです")),
    ("ending", ("朝の日本経済・AIニュースでした",)),
]


def spoken(line):
    """話者名を除いた、実際に読まれる部分"""
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


def spoken_chars(lines):
    return sum(len(spoken(l)) for l in lines)


def find_markers(lines):
    """各パートの始まりの行番号を返す {種類: 行番号}。順番がおかしいものは無視する"""
    found, last = {}, -1
    for n, l in enumerate(lines):
        if not l.startswith("Aki:"):
            continue
        text = spoken(l)
        if len(text) > 45:
            continue
        for order, (kind, phrases) in enumerate(PART_MARKERS):
            if kind not in found and order > last and any(p in text for p in phrases):
                found[kind] = n
                last = order
                break
    return found


def split_even(lines, limit, prefer=()):
    """文字数が limit を超えるかたまりを、ほぼ同じ長さに分ける。
    切れ目は prefer（パートの始まり）に近ければそこ、なければ Aki の発言の前に置く"""
    total = sum(len(l) for l in lines)
    n = math.ceil(total / limit)
    if n <= 1 or len(lines) < 2 * n:
        return [lines]
    cum = [0]
    for l in lines:
        cum.append(cum[-1] + len(l))
    cuts, last = [], 0
    for k in range(1, n):
        target = total * k / n
        cands = [i for i in range(last + 1, len(lines)) if lines[i].startswith("Aki:")] \
            or list(range(last + 1, len(lines)))
        if not cands:
            break
        best = min(cands, key=lambda i: abs(cum[i] - target))
        near = [i for i in prefer if last < i < len(lines) and abs(cum[i] - target) <= total * 0.12]
        if near:
            best = min(near, key=lambda i: abs(cum[i] - target))
        cuts.append(best)
        last = best
    return [p for p in (lines[a:b] for a, b in zip([0] + cuts, cuts + [len(lines)])) if p]


def split_parts(lines, cfg):
    """台本を音声化の単位に分ける。戻り値: [(名前, 行のリスト), ...]"""
    limit = int(cfg.get("part_max_chars", 1700))
    m = find_markers(lines)
    if "ai" not in m:
        # 決まったセリフが見つからない台本（古い形式など）は、長さだけで分ける
        pieces = split_even(lines, limit)
        return [(f"台本 {i}/{len(pieces)}", p) for i, p in enumerate(pieces, 1)]
    a = m["ai"]
    if "money" in m and 0 < m["money"] < a:
        groups = [("オープニング＋経済ニュース", 0, m["money"]), ("資産形成コラム", m["money"], a),
                  ("AI活用コラム＋まとめ", a, len(lines))]
    else:
        groups = [("オープニング＋経済ニュース", 0, a), ("AI活用コラム＋まとめ", a, len(lines))]
    out = []
    for name, s, e in groups:
        if e <= s:
            continue
        prefer = [i - s for i in m.values() if s < i < e]
        pieces = split_even(lines[s:e], limit, prefer)
        for i, p in enumerate(pieces, 1):
            out.append((name if len(pieces) == 1 else f"{name} {i}/{len(pieces)}", p))
    return out


# ---------------------------------------------------------------- TTS
def tts_gemini(chunk, cfg, api_key, models=None, seed_shift=0, attempts=4):
    """Gemini TTS で1回分を音声化する。戻り値: (PCM, 使ったモデル名)"""
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
    # 声のブレを抑える：乱数（seed）を固定し、揺らぎ（temperature）を控えめにする。
    # 検査に通らず作り直すときだけ seed を1つずらす（同じ seed だと同じ失敗を繰り返すため）
    if cfg.get("tts_seed") is not None:
        body["generationConfig"]["seed"] = int(cfg["tts_seed"]) + int(seed_shift)
    if cfg.get("tts_temperature") is not None:
        body["generationConfig"]["temperature"] = float(cfg["tts_temperature"])
    reserve = float(cfg.get("finish_reserve_sec", 300))
    last_err = None
    for model in (models or cfg["tts_models"]):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(attempts):
            # 1回の待ち時間に上限をつける（以前は最長15分待ち、全体が60分で強制終了された）
            timeout = min(float(cfg.get("tts_call_timeout_sec", 420)), remaining_sec(cfg) - reserve)
            if timeout < 60:
                raise BudgetExceeded("音声化を始めると制限時間を超えます")
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                b64 = next(p["inlineData"]["data"] for p in parts if "inlineData" in p)
                log(f"  TTS OK model={model}")
                return base64.b64decode(b64), model
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="ignore")[:3000]
                last_err = f"{model} HTTP {e.code}: {msg[:300]}"
                log(f"  TTS error: {last_err}")
                if e.code == 429:
                    if "PerDay" in msg or "per day" in msg.lower():
                        log(f"  {model} は今日の無料枠を使い切りました")
                        break  # 待っても回復しない → 次のモデルへ
                    mm = re.search(r'"retryDelay":\s*"(\d+)', msg)
                    sleep_in_budget(cfg, min(90, int(mm.group(1)) + 3 if mm else 30 * (attempt + 1)))
                    continue
                if e.code >= 500:
                    sleep_in_budget(cfg, min(60, 20 * (attempt + 1)))
                    continue
                break  # 400 / 403 / 404 など：このモデルは使えない → 次のモデルへ
            except BudgetExceeded:
                raise
            except Exception as e:  # ネットワーク、時間切れ、音声が返らなかった など
                last_err = f"{model}: {e}"
                log(f"  TTS error: {last_err}")
                sleep_in_budget(cfg, 15)
    raise TTSUnavailable(f"TTS に失敗しました（無料枠の上限の可能性あり）: {last_err}")


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


_DUMMY = {}


def tts_dummy(chunk):
    """dry-run 用：声に似せたテスト音（強弱と息つぎの間がある）。長さは文字数に比例する"""
    if not _DUMMY:
        n = int(SAMPLE_RATE * 0.1)
        for amp in (0, 2000, 4000, 7000, 10000):
            _DUMMY[amp] = b"".join(struct.pack("<h", int(amp * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)))
                                   for i in range(n))
    out, k = [], 0
    for line in chunk:
        for _ in range(max(3, round(len(spoken(line)) / 6.5 * 10) - 4)):
            k += 1
            out.append(_DUMMY[(2000, 7000, 4000, 10000, 4000, 7000)[k % 6] if k % 9 else 0])
        out += [_DUMMY[0]] * 4
    return b"".join(out)


# ---------------------------------------------------------------- パートごとの音声化（検査つき）
SHARED = {"last_call": 0.0}  # 回をまたいで共有する状態（直前の呼び出し時刻、書き起こしに使えるモデル）


def tts_call(seg, st, seed_shift=0):
    cfg = st["cfg"]
    if st["dry"]:
        return tts_dummy(seg)
    if st["calls"] >= int(cfg.get("max_tts_calls_per_episode", 9)):
        raise EpisodeFailed("音声化のやり直しが上限回数に達しました（無料枠を守るため中止）")
    # 無料枠は1分あたりの回数が少ないので、呼び出しの間隔をあける
    wait = float(cfg.get("tts_min_interval_sec", 21)) - (time.time() - SHARED["last_call"])
    if wait > 0:
        sleep_in_budget(cfg, wait)
    st["calls"] += 1
    SHARED["last_call"] = time.time()
    # 声の質感をそろえるため、1回分はすべて同じモデルで音声化する
    audio, used = tts_gemini(seg, cfg, st["api_key"], [st["model"]] if st["model"] else st["models"], seed_shift)
    st["model"] = used
    return audio


def synth_segment(name, seg, st, depth=0):
    """1パートを音声化して検査する。通らなければ作り直し、それでもだめなら小さく分けてやり直す"""
    cfg = st["cfg"]
    attempts = int(cfg.get("segment_attempts", 3)) if depth == 0 else 2
    doubtful = []  # 波形は正常だが、書き起こし検品で疑いが出た音声
    for attempt in range(1, attempts + 1):
        log(f"  音声化：{name}（{sum(len(l) for l in seg)}文字" + (f"・{attempt}回目" if attempt > 1 else "") + "）")
        audio = tts_call(seg, st, seed_shift=(attempt - 1) + depth * 10)
        audio, problems = check_audio(audio, seg, cfg)
        if problems:
            log("  検査で不合格：" + " ／ ".join(problems))
            continue
        found = check_transcript(audio, seg, cfg, st["api_key"], SHARED) if st["transcript"] else None
        if not found:
            log(f"  検査OK（{pcm_seconds(audio):.0f}秒）")
            return audio
        log("  書き起こし検品で疑い：" + " ／ ".join(found))
        doubtful.append((len(found), audio))
        if len(doubtful) > int(cfg.get("transcript_retries", 1)):
            break
    if doubtful:
        if cfg.get("transcript_strict", False):
            raise EpisodeFailed(f"{name} が書き起こし検品に通りませんでした")
        warn(f"{st['date']} {name}：書き起こし検品の疑いが残りましたが、波形は正常なので採用しました。念のため聴いて確認してください")
        return min(doubtful, key=lambda x: x[0])[1]
    if depth == 0:
        total = sum(len(l) for l in seg)
        small = split_even(seg, min(int(cfg.get("chunk_chars", 1400)), max(400, total // 2 + 60)))
        if len(small) > 1:
            log(f"  {name} を {len(small)} つに分けてやり直します")
            gap = b"\x00\x00" * int(SAMPLE_RATE * 0.35)
            return gap.join(synth_segment(f"{name}・分割{i}", s, st, 1) for i, s in enumerate(small, 1))
    raise EpisodeFailed(f"{name} の音声が検査に通りませんでした（無音・雑音・長さの異常）")


def synthesize_gemini(lines, cfg, api_key, dry, date):
    """台本1本を Gemini で音声化する。戻り値: 24kHz / 16bit / mono の PCM"""
    if cfg.get("split_mode", "parts") == "parts":
        parts = split_parts(lines, cfg)
    else:
        chunks = chunk_lines(lines, int(cfg.get("chunk_chars", 1400)))
        parts = [(f"台本 {i}/{len(chunks)}", c) for i, c in enumerate(chunks, 1)]
    log("  分け方：" + "、".join(f"{n}（{sum(len(l) for l in p)}文字）" for n, p in parts))
    st = dict(cfg=cfg, api_key=api_key, dry=dry, date=date, calls=0, model=None, models=list(cfg["tts_models"]),
              transcript=bool(cfg.get("transcript_check", True)) and not dry and bool(api_key))
    gap = b"\x00\x00" * int(SAMPLE_RATE * float(cfg.get("part_gap_sec", 0.5)))
    while True:
        try:
            out = [synth_segment(n, p, st) for n, p in parts]
            if not dry:
                log(f"  使ったモデル：{st['model']}／音声化 {st['calls']} 回")
            return gap.join(out)
        except TTSUnavailable as e:
            used, models = st["model"], st["models"]
            if used and used in models and models.index(used) + 1 < len(models):
                log(f"  {used} が途中で使えなくなったため、次のモデルで最初から作り直します")
                st["models"] = models[models.index(used) + 1:]
                st["model"] = None
                continue
            raise EpisodeFailed(str(e))


# ---------------------------------------------------------------- 音声の検査
# 基準は、実際に配信した回（正常な回と、無音・雑音が入った回）の波形を比べて決めた。
#   正常な回：無音は最長1秒ほど／15秒ごとに必ず息つぎの静かな瞬間がある／音量の強弱が大きい
#   壊れた回：数十秒〜数分の無音、または「静かな瞬間がない」「強弱がない」音が続く
def pcm_seconds(pcm):
    return len(pcm) / (SAMPLE_RATE * 2)


def frame_levels(pcm, frame_sec=0.1, step=4):
    """0.1秒ごとの音量（RMS）のリスト。速くするためサンプルを間引いて計算する"""
    a = array.array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    n = int(SAMPLE_RATE * frame_sec)
    out = []
    for i in range(0, len(a) - n + 1, n):
        f = a[i:i + n:step]
        out.append(math.sqrt(sum(x * x for x in f) / len(f)))
    return out


def _pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def _ratio(db):
    return 10 ** (db / 20)


def trim_silence(pcm, levels=None):
    """前後の無音を切り落とす（途中で声が消えたまま終わった音声は、これで短くなり長さの検査で見つかる）"""
    r = levels or frame_levels(pcm)
    if len(r) < 10:
        return pcm
    ref = _pct(r, 0.90)
    sil = max(ref * _ratio(-35), 20)
    voiced = [i for i, x in enumerate(r) if x >= sil]
    if not voiced:
        return pcm
    n = int(SAMPLE_RATE * 0.1) * 2  # 0.1秒ぶんのバイト数
    start = max(0, voiced[0] - 2) * n
    end = min(len(r), voiced[-1] + 3) * n
    return pcm[start:end]


def check_waveform(pcm, cfg):
    """無音・雑音を波形から見つける（API を使わないので無料）。問題の説明のリストを返す。空なら合格"""
    r = frame_levels(pcm)
    if len(r) < 30:
        return ["音声が3秒未満です"]
    ref = _pct(r, 0.90)  # 声が出ているときの音量の目安
    if ref < 150:
        return ["全体がほぼ無音です"]
    problems = []

    # ① 長い無音（正常な回は最長でも1秒ほど）
    sil = ref * _ratio(-35)
    run = best = end = 0
    for i, x in enumerate(r):
        run = run + 1 if x < sil else 0
        if run > best:
            best, end = run, i
    if best / 10 >= float(cfg.get("max_silence_sec", 3.0)):
        problems.append(f"{(end - best + 1) / 10:.0f}秒付近から {best / 10:.0f}秒間の無音")

    # ② 雑音・異常な音（15秒の窓を5秒ずつずらして調べる）
    W = min(150, len(r))
    starts = list(range(0, len(r) - W + 1, 50))
    if starts[-1] != len(r) - W:
        starts.append(len(r) - W)
    bad = []
    for i in starts:
        w = r[i:i + W]
        mean = sum(w) / len(w)
        cv = math.sqrt(sum((x - mean) ** 2 for x in w) / len(w)) / max(mean, 1.0)
        if _pct(w, 0.75) < sil:
            continue  # 無音の窓は ① で扱う
        if _pct(w, 0.03) > ref * _ratio(-25):
            bad.append((i, "静かな瞬間がない（雑音が乗っている）"))
        elif cv < 0.40:
            bad.append((i, "音量の強弱がない（声ではない音）"))
        elif _pct(w, 0.75) < ref * _ratio(-20):
            bad.append((i, "音量が大きく落ちている"))
    if len(bad) >= 2:
        problems.append(f"{bad[0][0] / 10:.0f}秒付近から異常な音：{bad[0][1]}（{len(bad)}か所）")
    return problems


def check_audio(pcm, lines, cfg):
    """1回分の音声を検査する。戻り値: (前後の無音を除いた音声, 問題のリスト)"""
    pcm = trim_silence(pcm)
    problems = []
    expected = spoken_chars(lines) / float(cfg.get("chars_per_sec", 6.5))
    lo, hi = cfg.get("part_length_ratio", [0.65, 1.4])
    got = pcm_seconds(pcm)
    if expected > 0 and not (lo <= got / expected <= hi):
        kind = "短すぎます（途中で切れた可能性）" if got < expected else "長すぎます（読み直しの可能性）"
        problems.append(f"長さが{kind}：{got:.0f}秒 / 想定{expected:.0f}秒")
    if cfg.get("waveform_check", True):
        problems += check_waveform(pcm, cfg)
    return pcm, problems


# ---------------------------------------------------------------- 書き起こし検品（無料枠の Gemini）
# 音声を文字に起こして台本と比べ、「同じ部分の読み直し（ループ）」「読み飛ばし」を見つける。
# 波形では見つけられない失敗のための検査。モデルが使えないときは、この検査だけ飛ばす。
TRANSCRIBE_PROMPT = ("この音声は、日本語の2人の掛け合いによるニュース番組です。聞こえたとおりに、最初から最後まで"
                     "文字起こししてください。話者名・時刻・説明・要約は付けず、話された言葉だけを出力してください。"
                     "同じ内容が繰り返し話されている場合は、省略せず、繰り返されたとおりに書いてください。")


def normalize_text(s):
    s = unicodedata.normalize("NFKC", s).lower()
    return "".join(ch for ch in s if ch.isalnum())


def transcribe(pcm, cfg, api_key, state):
    """戻り値: 書き起こした文字列。検品できなかったときは None"""
    models = state.setdefault("transcript_models",
                              list(cfg.get("transcript_models", ["gemini-flash-latest", "gemini-2.5-flash"])))
    if not models:
        return None
    raw = os.path.join(BUILD, "check.pcm")
    mp3 = os.path.join(BUILD, "check.mp3")
    with open(raw, "wb") as f:
        f.write(pcm)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-i", raw, "-b:a", "32k", mp3], check=True)
    with open(mp3, "rb") as f:
        audio = base64.b64encode(f.read()).decode()
    os.remove(raw)
    os.remove(mp3)
    body = {
        "contents": [{"parts": [{"text": TRANSCRIBE_PROMPT},
                                {"inlineData": {"mimeType": "audio/mp3", "data": audio}}]}],
        "generationConfig": {"temperature": 0},
    }
    for model in list(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(2):
            timeout = min(180.0, remaining_sec(cfg) - float(cfg.get("finish_reserve_sec", 300)))
            if timeout < 30:
                return None
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                cand = data["candidates"][0]
                if cand.get("finishReason") not in (None, "STOP"):
                    log(f"  書き起こしが途中で終わりました（{cand.get('finishReason')}）")
                    return None
                text = "".join(p.get("text", "") for p in cand["content"]["parts"] if not p.get("thought"))
                return text or None
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="ignore")[:1500]
                log(f"  書き起こし error: {model} HTTP {e.code}: {msg[:200]}")
                if e.code == 429 and "PerDay" not in msg and attempt == 0 and remaining_sec(cfg) > 600:
                    time.sleep(25)
                    continue
                models.remove(model)  # このモデルは今回はもう使わない
                break
            except Exception as e:
                log(f"  書き起こし error: {model}: {e}")
                break
    return None


def compare_transcript(script_lines, transcript, cfg):
    """台本と書き起こしを比べる。問題の説明のリストを返す。空なら合格"""
    S = normalize_text("".join(spoken(l) for l in script_lines))
    T = normalize_text(transcript)
    if not S or not T:
        return []
    problems = []
    ratio = len(T) / len(S)
    if ratio < 0.75:
        problems.append(f"書き起こしが台本よりかなり短い（{ratio:.0%}）")
    elif ratio > 1.30:
        problems.append(f"書き起こしが台本よりかなり長い（{ratio:.0%}）")
    loop_min = int(cfg.get("loop_min_chars", 40))
    skip_min = int(cfg.get("skip_min_chars", 60))

    def grams(text, n):
        d = {}
        for i in range(len(text) - n + 1):
            g = text[i:i + n]
            d[g] = d.get(g, 0) + 1
        return d

    s4, t4, t3 = grams(S, 4), grams(T, 4), grams(T, 3)
    sm = difflib.SequenceMatcher(None, S, T, autojunk=False)
    # 台本と書き起こしが食い違う区間を取り出す。書き起こしには細かなゆれ（漢字・かな、聞き間違い）が
    # あるので、10文字未満の一致をはさんで続く食い違いは、ひとつの区間としてまとめる
    regions, cur = [], None
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" and (i2 - i1) >= 10:
            if cur:
                regions.append(cur)
                cur = None
            continue
        if tag == "equal" and cur is None:
            continue
        cur = [cur[0], i2, cur[2], j2] if cur else [i1, i2, j1, j2]
    if cur:
        regions.append(cur)
    for i1, i2, j1, j2 in regions:
        ds, dt_ = i2 - i1, j2 - j1
        if dt_ - ds >= loop_min:
            # 台本にない文章が音声に入っている。それが「台本より多い回数」出てくるなら読み直し
            region = T[j1:j2]
            gs = [region[k:k + 4] for k in range(len(region) - 3)]
            extra = sum(1 for g in gs if t4.get(g, 0) > max(1, s4.get(g, 0)))
            if gs and extra / len(gs) >= 0.25:
                problems.append(f"同じ部分の読み直し（約{dt_ - ds}文字ぶん）：「{region[:24]}…」")
        if ds - dt_ >= skip_min:
            # 台本にある文章が音声にない。書き起こしのどこにも出てこないなら読み飛ばし
            region = S[i1:i2]
            gs = [region[k:k + 3] for k in range(len(region) - 2)]
            hit = sum(1 for g in gs if g in t3)
            if gs and hit / len(gs) < 0.5:
                problems.append(f"読み飛ばし（約{ds - dt_}文字ぶん）：「{region[:24]}…」")
    return problems


def check_transcript(pcm, lines, cfg, api_key, state):
    """戻り値: 問題のリスト（空なら合格）。検品できなかったときは None"""
    text = transcribe(pcm, cfg, api_key, state)
    if text is None:
        if not state.get("transcript_skipped"):
            log("  書き起こし検品は使えないため、今回は飛ばします（波形検査は実施済み）")
            state["transcript_skipped"] = True
        return None
    return compare_transcript(lines, text, cfg)


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
def build_one(date, ref, path, text, cfg, args, api_key, repo, items, by_date):
    """1回分を音声化して公開する。戻り値: 更新後の items"""
    engine = cfg.get("tts_engine", "gemini")
    meta, lines, sources = parse_episode(text)
    lines = validate_lines(lines, cfg["speakers"])
    log(f"  {len(lines)}行 / {sum(len(l) for l in lines)}文字")

    pcm = None
    # 台本の先頭（front matter）に tts_engine: gemini / aivis があれば、その回だけ読み上げ方式を変える
    ep_engine = (meta.get("tts_engine") or engine).lower()
    log(f"  読み上げ方式: {ep_engine}")
    if ep_engine == "aivis" and not args.dry_run:
        log("  AivisSpeech で音声化します")
        try:
            pcm = tts_aivis_episode(lines, cfg)
            for p in check_waveform(pcm, cfg):
                warn(f"{date}（AivisSpeech）：{p}")
        except Exception as e:
            if not api_key:
                raise
            log(f"  AivisSpeech での音声化に失敗 → 予備として Gemini で音声化します（{e}）")
            pcm = None
    if pcm is None:
        pcm = synthesize_gemini(lines, cfg, api_key, args.dry_run, date)

    # 全体の長さの確認（パートごとの検査は済んでいるので、ここは注意の表示だけ）
    expected = spoken_chars(lines) / float(cfg.get("chars_per_sec", 6.5))
    lo, hi = cfg.get("length_ratio", [0.7, 1.25])
    got = pcm_seconds(pcm)
    if not (lo <= got / expected <= hi):
        warn(f"{date}：全体の長さが想定と離れています（{got:.0f}秒 / 想定{expected:.0f}秒）。"
             "毎回出る場合は config.json の chars_per_sec を見直してください")

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

    # コラムなどの記録（episodes/*_log.md）も main に反映する
    for log_path in LOG_FILES:
        log_text = run(["git", "show", f"{ref}:{log_path}"], check=False)
        if log_text:
            with open(os.path.join(ROOT, log_path), "w", encoding="utf-8") as f:
                f.write(log_text)

    rev = by_date[date].get("rev", 1) + 1 if date in by_date else 1
    items = [it for it in items if it["date"] != date] + [{
        "date": date, "title": title, "summary": meta.get("summary", ""),
        "sources": sources[:12], "url": url, "bytes": os.path.getsize(mp3), "seconds": round(seconds),
        "sha": text_hash(text), "rev": rev,
    }]
    log(f"  完成: {fmt_duration(seconds)} / {os.path.getsize(mp3) // 1024} KB")
    return items


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
    rebuild_days = int(cfg.get("rebuild_days", REBUILD_DAYS))
    todo, texts = [], {}
    for d in sorted(candidates):
        ref, path = candidates[d]
        texts[d] = read_from_ref(ref, path)
        if d not in by_date:
            todo.append(d)
        elif by_date[d].get("sha") != text_hash(texts[d]) and \
                (today - dt.date.fromisoformat(d)).days <= rebuild_days:
            todo.append(d)  # 台本が作り直された回
    # 新しい回を先に作る（時間や無料枠が足りなくなっても、今日の回を優先するため）。1回の実行で作る数にも上限
    queue = sorted(todo, reverse=True)[:int(cfg.get("max_episodes_per_run", 3))]
    log(f"検出した台本: {sorted(candidates)} / 作る必要がある回: {todo} / 今回作る回: {queue}")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    engine = cfg.get("tts_engine", "gemini")
    if queue and not args.dry_run and not api_key:
        if engine == "gemini":
            sys.exit("GEMINI_API_KEY が設定されていません（GitHub の Secrets を確認）")
        log("注意: GEMINI_API_KEY がないため、AivisSpeech が失敗したときの予備（Gemini）は使えません")

    processed_refs, built, failed, stopped = set(), [], [], False
    for date in queue:
        ref, path = candidates[date]
        if not args.dry_run and remaining_sec(cfg) < float(cfg.get("episode_min_sec", 600)):
            stopped = True
            warn(f"{date}：残り時間が少ないため、今回は作りません（次の自動実行で作ります）")
            break
        log(f"== {date} を処理（{ref}）")
        try:
            items = build_one(date, ref, path, texts[date], cfg, args, api_key, repo, items, by_date)
            built.append(date)
            processed_refs.add(ref)
        except BudgetExceeded as e:
            stopped = True
            warn(f"{date}：制限時間が近いため中断しました。次の自動実行で作り直します（{e}）")
            break
        except Exception as e:  # この回はあきらめ、ほかの回と配信の更新は続ける
            failed.append(date)
            log(f"::error::{date} の音声を作れませんでした：{e}")

    items = prune(items, cfg["keep_episodes"], repo, args.dry_run)
    ready = False if args.dry_run else pages_ready(site)
    log("音声の配信元: " + ("GitHub Pages" if ready else "GitHub Releases（Pages 未切り替え）"))
    set_play_urls(items, cfg, site, ready)
    save_index(items)
    write_feed(items, cfg, site)
    write_index_html(items, cfg, site)
    sync_pages(items, cfg, repo, set(built), args.dry_run)

    if args.delete_branches and not args.dry_run:
        for ref in processed_refs:
            if ref.startswith("origin/claude/"):
                run(["git", "push", "origin", "--delete", ref[len("origin/"):]], check=False)
    log(f"完了（作った回: {built or 'なし'} / 作れなかった回: {failed or 'なし'}"
        + (" / 時間切れで中断あり" if stopped else "") + "）")
    if (failed or stopped) and not built:
        sys.exit(1)  # 何も作れなかったときは失敗として表示する（7:40 / 8:40 の自動実行で再挑戦）


if __name__ == "__main__":
    main()
