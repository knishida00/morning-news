# 朝の日本経済・AIニュース（自動ポッドキャスト）

毎朝、Claude がニュースを調べて2人の掛け合い台本を書き、GitHub Actions が Gemini TTS で音声化して Podcast として配信します。
費用は Claude Max（サブスク）＋無料枠のみ。PC は不要です。

## しくみ

```
6:30  Claude Routine（Anthropic のクラウド）
        └ ニュース収集 → 台本 episodes/日付.md を push
        ↓ push をきっかけに自動起動
      GitHub Actions（GitHub のクラウド・公開リポジトリは無料）
        └ Gemini TTS（無料枠）で音声化 → mp3 を Release に置く → feed.xml 更新
        ↓
      iPhone の Podcast アプリが新しい回を自動ダウンロード
```

## ファイル

| ファイル | 役割 |
|---|---|
| `CLAUDE.md` | Claude 用の制作マニュアル（ニュースの選び方・台本ルール）。番組の中身を変えたいときはここを編集 |
| `ROUTINE_PROMPT.md` | Claude Routine に貼るプロンプト |
| `config.json` | 番組名、声の種類、TTS モデル名など |
| `scripts/build_episode.py` | 台本 → 音声 → 配信 |
| `.github/workflows/build.yml` | 上のスクリプトを自動実行する設定 |
| `docs/` | Podcast の RSS（feed.xml）と紹介ページ。GitHub Pages で公開 |

## 課金を発生させないためのルール

- Google AI Studio / Google Cloud で **課金（Billing）を設定しない**。無料枠を超えたらエラーで止まるだけで、請求は来ない
- Anthropic の API キーは使わない（Claude は Max 契約内の Routine で動く）
- GitHub は無料プランのまま。リポジトリは公開（Public）のままにする（非公開にすると Pages と Actions の無料条件が変わる）

## うまくいかないとき

- **音声が来ない**：GitHub のリポジトリ →「Actions」タブで赤い × の実行を開くとエラー内容が見られる
  - `GEMINI_API_KEY が設定されていません` → Secrets の登録を確認
  - `TTS に失敗しました … 429` → 無料枠の上限。翌日は自動で復帰する
  - モデル名のエラー → `config.json` の `tts_models` を AI Studio で使えるモデル名に変更
- **台本が来ない**：claude.ai/code/routines で実行履歴を確認
- **声を変えたい**：`config.json` の `speakers` の値（Kore, Charon など）を AI Studio の音声一覧にある名前に変える
