"""인스타 게시 (Graph API, 설계서 4.5).

캐러셀: 이미지마다 자식 컨테이너 → CAROUSEL 부모 컨테이너(캡션) → 준비 완료 확인 → media_publish → permalink.
릴스: REELS 컨테이너(video_url·cover_url·캡션) → 영상 처리 끝날 때까지 기다림 → media_publish → permalink (M7).
토큰은 Authorization 헤더로만 보내고 오류 메시지·로그에는 넣지 않는다.
"""

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from newsbot.util.masking import mask_text


class InstagramError(RuntimeError):
    """메시지에 토큰이 들어가지 않는 오류."""


@dataclass
class Published:
    media_id: str
    permalink: str
    recovered: bool = False  # 인스타가 오류·실패라고 답했지만 실제로는 올라가 있어서 결과만 찾아온 경우


class InstagramClient:
    def __init__(self, token: str, user_id: str, host: str, version: str, timeout: int = 60,
                 retries: int = 3, sleep=time.sleep):
        if not token or not user_id:
            raise InstagramError("인스타 토큰 또는 사용자 ID가 비어 있습니다.")
        self._token = token
        self.user_id = user_id
        self.base = f"{host.rstrip('/')}/{version}"
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def _once(self, method: str, path: str, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{self.base}/{path}"
        data = None
        if method == "GET":
            url += f"?{query}" if query else ""
        else:
            data = query.encode()
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def request(self, method: str, path: str, **params) -> dict:
        """네트워크 오류·서버 오류(5xx)는 1→2→4초 간격으로 다시 시도한다."""
        for attempt in range(self.retries + 1):
            try:
                return self._once(method, path, params)
            except urllib.error.HTTPError as error:
                try:
                    detail = json.load(error).get("error", {})
                except ValueError:
                    detail = {}
                message = mask_text(str(detail.get("error_user_msg") or detail.get("message") or ""), [self._token])
                if error.code < 500 or attempt == self.retries:
                    code = detail.get("code")
                    if detail.get("error_subcode"):
                        code = f"{code}-{detail['error_subcode']}"  # 예: 4-2207051 = '활동 제한'(스팸 의심). 문서 찾을 때 필요
                    raise InstagramError(
                        f"인스타 {path.split('/')[-1]} 실패 (HTTP {error.code}, 코드 {code}) {message}".strip()
                    ) from None
            except (OSError, http.client.HTTPException, ValueError) as error:
                # 연결 끊김(URLError·ConnectionResetError·RemoteDisconnected)과 깨진 응답(IncompleteRead·JSON 아님)도 다시 시도 (QA 11-07)
                if attempt == self.retries:
                    raise InstagramError(f"인스타 연결 실패 ({type(error).__name__})") from None
            self.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    # ---------- 개별 호출 ----------

    def quota_left(self) -> int:
        data = self.request("GET", f"{self.user_id}/content_publishing_limit", fields="config,quota_usage")["data"][0]
        return data["config"]["quota_total"] - data["quota_usage"]

    def create_item(self, image_url: str, alt_text: str | None = None) -> str:
        params = {"image_url": image_url, "is_carousel_item": "true"}
        if alt_text:
            params["alt_text"] = alt_text[:1000]  # 검색·접근성용 대체 텍스트(최대 1000자)
        return self.request("POST", f"{self.user_id}/media", **params)["id"]

    def create_carousel(self, children: list[str], caption: str) -> str:
        return self.request("POST", f"{self.user_id}/media", media_type="CAROUSEL",
                            children=",".join(children), caption=caption)["id"]

    def create_reel(self, video_url: str, caption: str, cover_url: str | None = None, share_to_feed: bool = False) -> str:
        params = {"media_type": "REELS", "video_url": video_url, "caption": caption,
                  "share_to_feed": "true" if share_to_feed else "false"}
        if cover_url:
            params["cover_url"] = cover_url
        return self.request("POST", f"{self.user_id}/media", **params)["id"]

    def status(self, container_id: str) -> str:
        return self.request("GET", container_id, fields="status_code")["status_code"]

    def wait_ready(self, container_id: str, interval: int = 5, timeout: int = 300) -> str:
        """FINISHED(게시 가능)나 PUBLISHED가 될 때까지 기다린다. ERROR·EXPIRED·시간 초과는 오류."""
        waited = 0
        while True:
            status = self.status(container_id)
            if status in ("FINISHED", "PUBLISHED"):
                return status
            if status in ("ERROR", "EXPIRED") or waited >= timeout:
                raise InstagramError(f"인스타가 이미지·영상을 준비하지 못했어요 (상태 {status})")
            self.sleep(interval)
            waited += interval

    def publish(self, container_id: str) -> str:
        return self.request("POST", f"{self.user_id}/media_publish", creation_id=container_id)["id"]

    def permalink(self, media_id: str) -> str:
        return self.request("GET", media_id, fields="permalink").get("permalink", "")

    def latest_media_id(self) -> str:
        return self.request("GET", f"{self.user_id}/media", fields="id", limit=1)["data"][0]["id"]

    def recent_media(self, limit: int = 5) -> list[dict]:
        """최근 글(최신순)의 캡션·종류(FEED/REELS). 인스타가 '오류'라고 답한 글이 실제로 올라갔는지 볼 때 요청 1번으로 가져온다."""
        return self.request("GET", f"{self.user_id}/media", fields="id,caption,media_product_type,timestamp",
                            limit=limit).get("data", [])


def _norm(text: str) -> str:
    """캡션 비교용: 줄바꿈·연속 공백 차이는 무시한다(인스타가 저장하며 공백을 다듬을 수 있어서)."""
    return " ".join(text.split())


RETRY_LOOKBACK = 25  # 다시 올리기 전에 되볼 최근 글 수(요청은 1번). 하루 8개(카드+릴스)면 사흘치. 5개면 반나절 뒤 재시도에서 밀려나 중복될 수 있음(QA 11-07)


def find_posted(client: InstagramClient, caption: str, product: str, limit: int = 5,
                fallback_newest: bool = False) -> str | None:
    """최근 글 limit개 안에 같은 캡션·같은 종류(FEED=카드뉴스, REELS=릴스)의 글이 있으면 그 media id, 없으면 None.
    fallback_newest=True(컨테이너가 PUBLISHED라 올라간 건 확실한데 캡션을 못 맞춘 경우)면 같은 종류의 가장 새 글,
    그것도 없으면 계정의 가장 새 글을 돌려준다(다른 종류의 글을 잘못 잡지 않게 — QA 11-07).
    인스타는 글을 올려 놓고도 '오류'라고 답할 때가 있다(2026-11-07 21:30: 응답 403 코드 4-2207051 '활동 제한',
    컨테이너 상태는 ERROR, 그런데 글은 올라가 있었음). 그래서 오류가 났을 때와 다시 올리기 전에만 되묻는다.
    정상 게시 때는 부르지 않는다(추가 요청 0번)."""
    wanted = _norm(caption)
    newest = None
    for media in client.recent_media(limit):
        if media.get("media_product_type") != product:
            continue
        if _norm(media.get("caption") or "") == wanted:
            return media["id"]
        newest = newest or media["id"]
    if fallback_newest:
        return newest or client.latest_media_id()
    return None


def _container_state(client: InstagramClient, container: str) -> str:
    try:
        return client.status(container)
    except InstagramError:
        return "EXPIRED"  # 지난 컨테이너를 못 찾음(지워짐·24시간 지남)


def _posted_before(client: InstagramClient, container: str, caption: str, product: str) -> tuple[str, str | None]:
    """지난 실행에서 만든 컨테이너의 상태와, 그 글이 이미 올라가 있으면 media id (없으면 None).
    상태가 PUBLISHED가 아니어도(ERROR 포함) 최근 글에 같은 캡션이 있으면 올라간 것으로 본다 → 두 번 올리지 않는다."""
    state = _container_state(client, container)
    return state, find_posted(client, caption, product, limit=RETRY_LOOKBACK, fallback_newest=(state == "PUBLISHED"))


def _publish_container(client: InstagramClient, container: str, caption: str, product: str,
                       interval: int, timeout: int) -> tuple[str, bool]:
    """준비를 기다렸다가 게시한다. (media id, 인스타가 오류라고 답했지만 실제로는 올라가 있었는지)를 돌려준다.
    게시 응답을 못 받았거나 '오류'라고 답해도 실제로 올라갔으면 성공으로 본다."""
    if client.wait_ready(container, interval=interval, timeout=timeout) == "PUBLISHED":
        return find_posted(client, caption, product, fallback_newest=True), True
    try:
        return client.publish(container), False
    except InstagramError:
        client.sleep(3)  # 인스타가 글을 올려 놓고 오류라고 답하는 경우(2026-11-07 21:30): 잠깐 뒤 최근 글에서 찾는다
        posted = _posted_quietly(client, container, caption, product)
        if not posted:
            raise
        return posted, True


def _posted_quietly(client: InstagramClient, container: str, caption: str, product: str) -> str | None:
    """오류 뒤의 되묻기. 이 확인 요청마저 실패하면 '못 찾음'으로 보고 원래 오류를 그대로 알린다."""
    try:
        published = _container_state(client, container) == "PUBLISHED"
        return find_posted(client, caption, product, fallback_newest=published)
    except InstagramError:
        return None


def publish_carousel(client: InstagramClient, image_urls: list[str], caption: str, progress: dict,
                     save=lambda: None, alt_texts: list[str] | None = None) -> Published:
    """캐러셀을 게시한다. progress(초안 기록의 일부)에 부모 컨테이너 ID를 남기고 save()로 바로 저장해,
    중간에 끊긴 뒤 다시 실행해도 두 번 올라가지 않게 한다."""
    if not 2 <= len(image_urls) <= 20:
        raise InstagramError(f"캐러셀은 이미지 2~20장이어야 해요 (지금 {len(image_urls)}장)")
    container = progress.get("container_id")
    media_id = None
    if container:
        # 지난번에 게시는 됐는데 결과를 못 받은 경우(오류라고 답했지만 올라간 경우 포함): 다시 올리지 않고 결과만 찾는다.
        _, media_id = _posted_before(client, container, caption, "FEED")
    recovered = media_id is not None
    if media_id is None:
        if client.quota_left() < 1:
            raise InstagramError("오늘 게시 한도를 다 썼어요(24시간 기준). 나중에 다시 시도해야 해요.")
        alts = list(alt_texts or []) + [None] * len(image_urls)
        children = [client.create_item(url, alt) for url, alt in zip(image_urls, alts)]
        for child in children:
            client.wait_ready(child)
        container = client.create_carousel(children, caption)
        progress["container_id"] = container
        save()
        media_id, recovered = _publish_container(client, container, caption, "FEED", interval=5, timeout=300)
    return Published(media_id, client.permalink(media_id), recovered)


def publish_reel(client: InstagramClient, video_url: str, caption: str, progress: dict, save=lambda: None,
                 cover_url: str | None = None, share_to_feed: bool = False, wait_timeout: int = 600) -> Published:
    """릴스를 게시한다. 캐러셀과 같이 컨테이너 ID를 progress에 먼저 저장해 두 번 올라가지 않게 한다.
    영상 처리는 보통 30초~몇 분 걸려서 10초 간격으로 최대 wait_timeout초 기다린다."""
    container = progress.get("container_id")
    state, media_id = _posted_before(client, container, caption, "REELS") if container else (None, None)
    recovered = media_id is not None
    if media_id is None:
        if state in (None, "ERROR", "EXPIRED"):  # 컨테이너가 없거나 못 쓰게 됨 → 새로 만든다. 처리 중인 것은 그대로 기다린다
            if client.quota_left() < 1:
                raise InstagramError("오늘 게시 한도를 다 썼어요(24시간 기준). 나중에 다시 시도해야 해요.")
            container = client.create_reel(video_url, caption, cover_url, share_to_feed)
            progress["container_id"] = container
            save()
        media_id, recovered = _publish_container(client, container, caption, "REELS", interval=10, timeout=wait_timeout)
    return Published(media_id, client.permalink(media_id), recovered)
