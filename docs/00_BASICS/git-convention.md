# Git 컨벤션

## 1. 브랜치 전략 (Gitflow)

```
main (배포) ← develop (통합) ← feature/* (기능 개발)
```

- **main**: 배포 가능한 안정 버전만.
- **develop**: 기능들이 모이는 통합 브랜치.
- **feature/***: 기능 단위 개발 브랜치. `develop`에서 따고 `develop`으로 머지.

### 브랜치명 규칙
- **kebab-case**, 영어 소문자, **한글 금지**.
- 형식: `feature/기능설명` (예: `feature/user-login`, `feature/search-filter`).
- 버그: `fix/버그설명` (예: `fix/login-crash`).
- **PR은 항상 `develop`을 대상으로** 연다. (`main` 직접 대상 금지)

## 2. 커밋 규칙 (Conventional Commits)

형식: `<type>: <설명>`

| type | 용도 |
|---|---|
| `feat` | 새 기능 추가 |
| `fix` | 버그 수정 |
| `docs` | 문서 수정 |
| `refactor` | 기능 변화 없는 코드 개선 |
| `test` | 테스트 추가/수정 |
| `chore` | 빌드·설정·기타 잡일 |

### 예시
```
feat: 로그인 API 연동
fix: 검색 결과 중복 표시 수정
docs: git-convention 문서 추가
refactor: 유저 서비스 로직 분리
test: 로그인 유효성 검사 테스트 추가
chore: eslint 설정 업데이트
```

- 제목은 명령형·간결하게. 마침표 없이.
- 상세 설명이 필요하면 본문에 빈 줄 후 작성.

## 3. PR 템플릿

`.github/pull_request_template.md`로 추가되어 있으며, 아래 내용을 따른다.

```markdown
## 작업 내용
<!-- 무엇을 왜 했는지 간단히 -->

## 변경 사항
<!-- 주요 변경점 목록 -->
-

## 관련 이슈
<!-- 예: closes #12 -->

## 체크리스트
- [ ] PR 대상 브랜치가 `develop`인가
- [ ] 브랜치명이 kebab-case(한글 없음)인가
- [ ] 커밋 메시지가 Conventional Commits를 따르는가
- [ ] 관련 문서(features/system)를 업데이트했는가
- [ ] collaborator나 co-author에 claude 등 AI를 추가하지 않았는가
```
