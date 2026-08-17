# Documentation index

このdirectoryは、`coding-review-agent-loop`の利用方法、設計計画、監査記録を管理する。

## Categories

| Category | Purpose | Documents |
| --- | --- | --- |
| Current usage and reference | 現在実装されているCLI、skill mode、運用方法 | [`local_agent_loop.md`](local_agent_loop.md), [`skill_mode.md`](skill_mode.md) |
| Plans | 未実装または合意中の完成像、実装計画、decision log | [`plans/`](plans/) |
| Audit | 特定時点のコード・設計監査記録 | [`audit/`](audit/) |

## Plan documents

- [`plans/claude-codex-target-experience.md`](plans/claude-codex-target-experience.md)
  - Claude Code実装、Codexレビュー、ユーザーのmerge判断までの完成イメージ
  - Parent roadmap: [#1](https://github.com/Mega-Gorilla/coding-review-agent-loop/issues/1)
  - Alignment issue: [#2](https://github.com/Mega-Gorilla/coding-review-agent-loop/issues/2)

## Document status

Plan documentは冒頭に次のstatusを持つ。

- `Draft`: たたき台。内容は未合意
- `In Review`: ユーザーと確認中
- `Agreed`: 完成イメージまたは計画として合意済み
- `Implemented`: 対応する実装と検証が完了
- `Superseded`: 別文書へ置き換え済み

文書内の個別項目には次の分類を使用する。

- `Decided`: 合意済み
- `Proposed`: 現在の提案。変更可能
- `Open`: 判断または検証が必要

## Organization policy

- 既存文書はREADME、source、test、upstreamから直接参照されているため、初期整理では移動しない
- 文書を移動する場合は、参照更新と回帰testを含む独立したPRで行う
- category directoryは実際に管理対象の文書ができた時点で追加する
- 同じ要件をIssue、plan、referenceへ重複記載せず、source of truthへのlinkを使用する
- roadmap Issueは長期目的、alignment Issueは合意作業、plan documentは最新の合意内容、実装Issueは具体的な作業を管理する
