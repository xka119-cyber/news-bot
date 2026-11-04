"""인스타 게시 (Graph API, 설계서 4.5).

캐러셀: 이미지마다 자식 컨테이너 → CAROUSEL 부모 컨테이너(캡션) → 준비 완료 확인 → media_publish → permalink.
토큰은 Authorization 헤더로만 보내고 오류 메시지·로그에는 넣지 않는다.
"""

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
                    raise InstagramError(
                        f"인스타 {path.split('/')[-1]} 실패 (HTTP {error.code}, 코드 {detail.get('code')}) {message}".strip()
                    ) from None
            except (urllib.error.URLError, TimeoutError) as error:
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
                raise InstagramError(f"인스타가 이미지를 준비하지 못했어요 (상태 {status})")
            self.sleep(interval)
            waited += interval

    def publish(self, container_id: str) -> str:
        return self.request("POST", f"{self.user_id}/media_publish", creation_id=container_id)["id"]

    def permalink(self, media_id: str) -> str:
        return self.request("GET", media_id, fields="permalink").get("permalink", "")

    def latest_media_id(self) -> str:
        return self.request("GET", f"{self.user_id}/media", fields="id", limit=1)["data"][0]["id"]


def publish_carousel(client: InstagramClient, image_urls: list[str], caption: str, progress: dict,
                     save=lambda: None, alt_texts: list[str] | None = None) -> Published:
    """캐러셀을 게시한다. progress(초안 기록의 일부)에 부모 컨테이너 ID를 남기고 save()로 바로 저장해,
    중간에 끊긴 뒤 다시 실행해도 두 번 올라가지 않게 한다."""
    if not 2 <= len(image_urls) <= 20:
        raise InstagramError(f"캐러셀은 이미지 2~20장이어야 해요 (지금 {len(image_urls)}장)")
    container = progress.get("container_id")
    if container and client.status(container) == "PUBLISHED":
        # 지난번에 게시는 됐는데 결과를 못 받은 경우: 다시 올리지 않고 결과만 찾는다.
        media_id = client.latest_media_id()
    else:
        if client.quota_left() < 1:
            raise InstagramError("오늘 게시 한도를 다 썼어요(24시간 기준). 나중에 다시 시도해야 해요.")
        alts = list(alt_texts or []) + [None] * len(image_urls)
        children = [client.create_item(url, alt) for url, alt in zip(image_urls, alts)]
        for child in children:
            client.wait_ready(child)
        container = client.create_carousel(children, caption)
        progress["container_id"] = container
        save()
        if client.wait_ready(container) == "PUBLISHED":
            media_id = client.latest_media_id()
        else:
            try:
                media_id = client.publish(container)
            except InstagramError:
                if client.status(container) != "PUBLISHED":
                    raise
                media_id = client.latest_media_id()
    return Published(media_id, client.permalink(media_id))
