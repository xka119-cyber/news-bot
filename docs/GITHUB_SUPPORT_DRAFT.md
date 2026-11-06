# GitHub 지원팀 문의 글 초안 (예약 실행 지연)

왜 이 파일이 있나: 2026-11-05~06 이틀 동안 이 저장소의 예약 실행(schedule)이 매번 4~11시간 늦게 돌았습니다.
저장소·계정 설정은 전부 정상이어서, "왜 이 저장소만 이렇게 심한지"는 GitHub 내부만 알 수 있습니다.
답을 꼭 듣고 싶으면 아래 글을 보내면 됩니다(무료 계정도 가능). 보내지 않아도 뉴스봇은 tick 방식으로 돌아갑니다.

보내는 곳: https://support.github.com/request → "GitHub Actions" 선택 → 아래 영어 글 붙여 넣기.

---

**Subject:** Scheduled workflows in xka119-cyber/news-bot start 4–11 hours late (every run)

Hello,

Scheduled (`on: schedule`) workflow runs in my public repository **xka119-cyber/news-bot** have been delayed by 4 to 11 hours on every single occurrence since 2026-11-05, while `workflow_dispatch` runs start immediately.

Examples (UTC): a cron set for 21:23 created its run at 2026-11-06 01:39; a cron for 22:32 created its run at 02:43; crons for 09:03, 11:23 and 12:32 on 2026-11-05 created runs at 17:02, 18:49 and 20:12 the same day. Several cron times produced no run at all.

I have verified: the workflow files are on the default branch (`main`), the workflows are active, Actions is enabled with all actions allowed, the repository is not a fork and was pushed to every day, the cron minutes avoid the top of the hour, and I re-registered the schedules once (changed the cron minutes and disabled/enabled the workflows). Nothing changed.

Could you check whether something on the GitHub side is deprioritizing or dropping scheduled events for this repository, and whether there is anything I can change to get schedules within the usual few-minute delay?

Repository: https://github.com/xka119-cyber/news-bot
Affected workflows: `.github/workflows/generate.yml`, `publish.yml` (schedules now moved to `tick.yml`), `maintenance.yml`
Example runs: https://github.com/xka119-cyber/news-bot/actions/runs/42145250996 (cron 21:23 UTC, created 01:39 UTC next day)

Thank you.
