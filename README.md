# newsbot

인스타그램 카드뉴스를 자동으로 만들고, 텔레그램 승인을 거쳐 게시하는 엔진입니다.

```
[수집] → [작성] → [디자인] → [승인] → [게시]
```

운영 방법은 [docs/RUNBOOK.md](docs/RUNBOOK.md)를 보세요.

## 설치

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
git config core.hooksPath .githooks
```

## 점검

```bash
.venv/Scripts/python -m newsbot check
.venv/Scripts/python -m pytest
```
