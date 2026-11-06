// 시계 Worker 검사: 실행은 `npm test` (clock/ 안에서). 진짜 GitHub·텔레그램에는 요청하지 않는다(가짜 fetch).
import { test } from "node:test";
import assert from "node:assert/strict";

import worker, { wake, explain, shouldAlert, scrub, runLink, SOURCE } from "../worker.js";

const ENV = {
  GH_DISPATCH_TOKEN: "test-token-123",
  TELEGRAM_BOT_TOKEN: "bot-token",
  TELEGRAM_CHAT_ID: "42",
  GITHUB_REPO: "xka119-cyber/news-bot",
  WORKFLOW_FILE: "tick.yml",
  GIT_REF: "main",
};

const RUN_URL = "https://github.com/xka119-cyber/news-bot/actions/runs/42200000000";
const DISPATCH_200 = { status: 200, body: JSON.stringify({ workflow_run_id: 42200000000, run_url: "https://api.github.com/x", html_url: RUN_URL }) };

/** 주소 일부 → 응답(코드, {status, body}, 또는 Error)을 정해 두는 가짜 fetch. 받은 요청을 calls에 모은다. */
function fakeFetch(plan) {
  const calls = [];
  const fetcher = async (url, init) => {
    calls.push({ url, init });
    const entry = Object.entries(plan).find(([part]) => url.includes(part));
    const outcome = entry ? entry[1] : 200;
    if (outcome instanceof Error) throw outcome;
    const status = typeof outcome === "number" ? outcome : outcome.status;
    const body = typeof outcome === "number" ? (status === 204 ? "" : `{"message":"status ${status}"}`) : outcome.body;
    return { status, ok: status >= 200 && status < 300, text: async () => body };
  };
  return { fetcher, calls };
}

const at = (minute) => new Date(Date.UTC(2026, 10, 6, 12, minute, 3)); // 분만 다른 시각

test("깨우기 성공(200 + 실행 링크): GitHub에 workflow_dispatch 한 번, 텔레그램은 안 보냄", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": DISPATCH_200 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.ok, true);
  assert.equal(result.link, RUN_URL);
  assert.equal(calls.length, 1);
  const [{ url, init }] = calls;
  assert.equal(url, "https://api.github.com/repos/xka119-cyber/news-bot/actions/workflows/tick.yml/dispatches");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.authorization, "Bearer test-token-123");
  assert.ok(init.headers["user-agent"], "GitHub API는 User-Agent가 필요하다");
  assert.deepEqual(JSON.parse(init.body), { ref: "main", inputs: { source: "cloudflare" } });
  assert.equal(SOURCE, "cloudflare");
});

test("깨우기 성공(204, 내용 없음)도 성공으로 본다", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": 204 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.ok, true);
  assert.equal(result.link, "");
  assert.equal(calls.length, 1);
});

test("runLink: 본문이 없거나 JSON이 아니면 빈 문자열", () => {
  assert.equal(runLink(""), "");
  assert.equal(runLink("not json"), "");
  assert.equal(runLink('{"workflow_run_id":1}'), "");
  assert.equal(runLink(DISPATCH_200.body), RUN_URL);
});

test("토큰이 없으면 아무 데도 요청하지 않는다 (토큰을 넣기 전 대기 상태)", async () => {
  const { fetcher, calls } = fakeFetch({});
  const result = await wake({ ...ENV, GH_DISPATCH_TOKEN: "" }, at(0), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.reason, "토큰 없음");
  assert.equal(calls.length, 0);
});

test("실패 + 정각 깨어남 → 텔레그램 알림 1번, 알림 글에 토큰 값은 없음", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": 401, "api.telegram.org": 200 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.status, 401);
  assert.equal(result.alerted, true);
  assert.equal(calls.length, 2);
  const telegram = calls[1];
  assert.equal(telegram.url, "https://api.telegram.org/botbot-token/sendMessage");
  const body = JSON.parse(telegram.init.body);
  assert.equal(body.chat_id, "42");
  assert.match(body.text, /401/);
  assert.match(body.text, /토큰/);
  assert.ok(!body.text.includes("test-token-123"), "토큰 값이 알림에 들어가면 안 된다");
});

test("실패 + 정각이 아닌 깨어남 → 알림 없음 (한 시간에 한 번만)", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": 401 });
  const result = await wake(ENV, at(20), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.alerted, false);
  assert.equal(calls.length, 1);
});

test("네트워크 오류도 같은 길로 알린다", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": new Error("boom"), "api.telegram.org": 200 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.status, 0);
  assert.equal(result.alerted, true);
  assert.equal(calls.length, 2);
  assert.match(JSON.parse(calls[1].init.body).text, /연결 실패: boom/);
});

test("오류 메시지에 토큰이 섞여 와도 ****로 가려서 알린다", async () => {
  const leaky = new Error('Headers.append: "Bearer test-token-123" is an invalid header value');
  const { fetcher, calls } = fakeFetch({ "api.github.com": leaky, "api.telegram.org": 200 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.alerted, true);
  const text = JSON.parse(calls[1].init.body).text;
  assert.ok(!text.includes("test-token-123"));
  assert.match(text, /\*\*\*\*/);
  assert.ok(!result.reason.includes("test-token-123"));
});

test("scrub: 두 토큰 값을 모두 가린다", () => {
  assert.equal(scrub("a test-token-123 b bot-token c bot-token", ENV), "a **** b **** c ****");
  assert.equal(scrub("그대로", { ...ENV, GH_DISPATCH_TOKEN: "", TELEGRAM_BOT_TOKEN: "" }), "그대로");
});

test("텔레그램이 거절하면 alerted는 false (실패는 기록만)", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": 500, "api.telegram.org": 502 });
  const result = await wake(ENV, at(0), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.alerted, false);
  assert.equal(calls.length, 2);
});

test("텔레그램 설정이 없으면 알림은 건너뛰고 실패만 돌려준다", async () => {
  const { fetcher, calls } = fakeFetch({ "api.github.com": 500 });
  const result = await wake({ ...ENV, TELEGRAM_BOT_TOKEN: "", TELEGRAM_CHAT_ID: "" }, at(0), fetcher);
  assert.equal(result.ok, false);
  assert.equal(result.alerted, false);
  assert.equal(calls.length, 1);
});

test("shouldAlert: 분 0~9만 참", () => {
  assert.equal(shouldAlert(at(0)), true);
  assert.equal(shouldAlert(at(9)), true);
  assert.equal(shouldAlert(at(10)), false);
  assert.equal(shouldAlert(at(50)), false);
});

test("explain: 응답 코드별 안내", () => {
  assert.match(explain(404, "", ENV), /저장소/);
  assert.match(explain(422, "", ENV), /source 입력/);
  assert.match(explain(403, "", ENV), /권한/);
  assert.match(explain(0, "timeout", ENV), /연결 실패: timeout/);
  assert.match(explain(500, "", ENV), /다음 10분/);
});

test("scheduled 핸들러는 예약 시각을 넘겨 waitUntil로 깨운다", async () => {
  let pending;
  await worker.scheduled({ scheduledTime: at(0).getTime() }, { ...ENV, GH_DISPATCH_TOKEN: "" }, { waitUntil: (p) => { pending = p; } });
  const result = await pending;
  assert.equal(result.reason, "토큰 없음");
});
