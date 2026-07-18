# ASKU

대학 웹사이트 자동 QA 시스템. 기획은 [`docs/00_BASICS/PLAN.md`](docs/00_BASICS/PLAN.md) 참고.

## 폴더 구조

```
backend/                FastAPI 백엔드
  app/
    api.py              REST 엔드포인트
    crawler.py          공지·학사 페이지 크롤링
    extractor.py        LLM 기반 정보·엔티티 추출
    rag.py              검색 + 답변 생성 (벡터 → Graph RAG로 확장)
    llm.py              LLM 추상화 (로컬/API 라우팅)
    storage.py          Vector/Graph DB · 원문 저장 접근
    models.py           데이터 모델 (School, Document, 노드/엣지)
  tests/
frontend/               React/Next.js 프론트엔드
docs/                   설계·협업 문서 (00_BASICS / 01_SYSTEM / 02_FEATURES)
```

각 시스템 단위 설계는 [`docs/01_SYSTEM`](docs/01_SYSTEM) 참고. 코드 작성 전 관련 문서를 먼저 확인한다.
