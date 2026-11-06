// 뉴스봇 시계 — Cloudflare Worker (무료 cron). 10분마다 GitHub Actions의 `tick` 작업을 깨운다 (2026-11-06).
//
// 왜: GitHub 자체 예약(schedule)이 11-05·06 이틀 동안 매번 4~11시간 늦게 오거나 오지 않았다. 반면 수동 실행(workflow_dispatch)은
//     매번 바로 됐으므로, 밖에서 정확한 시각에 "지금 돌아!"(workflow_dispatch)만 보내 주면 된다. 설명: docs/RUNBOOK.md '자동 실행'
// 하는 일: POST https://api.github.com/repos/<저장소>/actions/workflows/tick.yml/dispatches 한 번.
//          "지금 미리보기를 만들까, 게시할까"의 판단은 전부 GitHub 쪽 tick(newsbot/tick.py)이 한다. 이 파일은 시계일 뿐이다.
// 비밀값(wrangler secret put — scripts/set_clock_secrets.py가 .env에서 화면에 안 보이게 옮긴다):
//   GH_DISPATCH_TOKEN   GitHub fine-grained 토큰 (news-bot 저장소만, Actions: Read and write). 없으면 깨우지 않고 기록만 남긴다.
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   깨우기에 실패했을 때 텔레그램으로 알리는 데 쓴다 (한 시간에 한 번만).
// 일반 설정(wrangler.jsonc의 vars): GITHUB_REPO, WORKFLOW_FILE, GIT_REF

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(wake(env, new Date(event.scheduledTime)));
  },
};

export const SOURCE = "cloudflare"; // tick.yml의 입력값 → Actions 탭에서 실행 이름이 "tick · cloudflare"로 보인다

/** GitHub를 한 번 깨운다. 실패하면 매시 정각 깨어남(분 0~9)에서만 텔레그램으로 알린다. */
export async function wake(env, now, fetcher = fetch) {
  if (!env.GH_DISPATCH_TOKEN) {
    console.log("GH_DISPATCH_TOKEN 비밀값이 없어 깨우지 않음 (RUNBOOK '시계 켜기' 순서대로 넣으면 다음 10분부터 작동)");
    return { ok: false, status: 0, alerted: false, reason: "토큰 없음" };
  }
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  let status = 0;
  let detail = "";
  try {
    const res = await fetcher(url, {
      method: "POST",
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        "content-type": "application/json",
        "user-agent": "newsbot-clock", // GitHub API는 User-Agent가 없으면 거절한다
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ ref: env.GIT_REF, inputs: { source: SOURCE } }),
    });
    status = res.status;
    const body = await res.text();
    if (res.ok) {
      // GitHub는 2026년 기준 200(실행 번호·링크 포함)으로 답한다. 예전 문서의 204(내용 없음)도 성공으로 본다.
      const link = runLink(body);
      console.log(`깨움 ${now.toISOString()} → ${env.GITHUB_REPO} ${env.WORKFLOW_FILE}${link ? ` ${link}` : ""}`);
      return { ok: true, status, alerted: false, link };
    }
    detail = body.slice(0, 300);
  } catch (err) {
    detail = String((err && err.message) || err);
  }
  const text = scrub(explain(status, detail, env), env);
  console.error(text);
  const alerted = shouldAlert(now) ? await alert(env, text, fetcher) : false;
  return { ok: false, status, alerted, reason: text };
}

/** 200 응답 본문에서 실행 링크를 꺼낸다. 본문이 없거나(204) JSON이 아니면 빈 문자열. */
export function runLink(body) {
  if (!body) return "";
  try {
    const data = JSON.parse(body);
    return (data && (data.html_url || data.run_url)) || "";
  } catch {
    return "";
  }
}

/** 10분마다 깨어나므로 알림은 매시 정각 깨어남(분 0~9)에서만 → 한 시간에 최대 1번. */
export function shouldAlert(now) {
  return now.getUTCMinutes() < 10;
}

/** 외부에서 온 글(응답 본문·오류 메시지)에 비밀값이 섞여 있어도 ****로 바꾼다. */
export function scrub(text, env) {
  for (const value of [env.GH_DISPATCH_TOKEN, env.TELEGRAM_BOT_TOKEN]) {
    if (value) text = text.split(value).join("****");
  }
  return text;
}

/** 실패 이유를 사람 말로. 토큰 값은 넣지 않는다(외부에서 온 detail은 scrub이 한 번 더 가린다). */
export function explain(status, detail, env) {
  const where = `${env.GITHUB_REPO} ${env.WORKFLOW_FILE}`;
  const hints = {
    401: "GitHub 토큰이 틀렸거나 만료됐어요. 새 토큰을 .env의 GH_DISPATCH_TOKEN에 넣고 scripts/set_clock_secrets.py를 다시 돌립니다.",
    403: "GitHub가 거절했어요(권한 부족 또는 요청 한도). 토큰의 Actions 권한이 'Read and write'인지 확인합니다.",
    404: "저장소나 tick.yml을 못 찾았어요. 토큰이 이 저장소(news-bot)에 접근할 수 있는지 확인합니다.",
    422: "GitHub가 요청 내용을 거절했어요. main 브랜치의 tick.yml에 source 입력이 있는지 확인합니다.",
  };
  const hint =
    status === 0
      ? "GitHub에 연결하지 못했어요(네트워크). 다음 10분에 다시 시도합니다."
      : hints[status] || "다음 10분에 다시 시도합니다. 계속되면 Claude에게 보여 주세요.";
  const what = status === 0 ? `연결 실패: ${detail}` : `GitHub 응답 ${status}${detail ? `: ${detail}` : ""}`;
  return `⏰ 뉴스봇 시계가 GitHub를 깨우지 못했어요 (${where})\n${what}\n${hint}`;
}

async function alert(env, text, fetcher) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
    console.error("텔레그램 알림 설정(TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID)이 없어 알리지 못함");
    return false;
  }
  try {
    const res = await fetcher(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
    });
    if (!res.ok) console.error(`텔레그램 알림 실패: 응답 ${res.status}`);
    return res.ok;
  } catch {
    console.error("텔레그램 알림 실패: 연결 오류"); // 오류 글에는 토큰이 든 주소가 섞일 수 있어 적지 않는다
    return false;
  }
}
