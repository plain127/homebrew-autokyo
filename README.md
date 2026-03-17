# AutoKyo

> macOS용 교보문고 전자도서관 eBook 작업 자동화 도구  
> 자동 클릭, 페이지 변화 감지, PDF 변환, MCP 등록 지원

`AutoKyo`는 반복 클릭과 페이지 넘김을 줄이고, `captures/` 이미지를 PDF로 묶기 쉽게 만든 로컬 도구입니다.

  이 프로젝트는 편의성과 접근성 향상을 위한 목적으로 제공됩니다. 교보도서관 이용 시 적용되는 저작권, 서
  비스 약관, 대여 조건은 사용자가 직접 확인해야 합니다. 본 도구를 통해 생성한 도서 이미지 또는 스크린샷
  을 개인적 이용 범위를 벗어나 공유, 배포, 재판매하는 행위는 저작권법 및 관련 약관 위반에 해당할 수 있습
  니다. 개발자는 관련 법령이나 약관을 위반하는 이용을 권장하거나 보증하지 않으며, 그에 따른 책임은 전적
  으로 사용자에게 있습니다.

## 요구사항

- macOS
- Python 3.11 이상
- Python 인터프리터에 `손쉬운 사용` 권한
- Python 인터프리터에 `화면 및 시스템 오디오 녹화` 권한
- `autokyo setup` 또는 MCP 설정 툴로 만든 `config.toml`

`config.toml`은 아래 순서로 자동 탐색합니다.

- `./config.toml`
- `~/Library/Application Support/AutoKyo/config.toml`
- `~/.config/autokyo/config.toml`

## 설치

```bash
brew install plain127/autokyo/autokyo
```

## 빠른사용법
### 우선 교보도서관 앱을 키고 다운받은 도서를 화면에 띄우세요.

### CLI로 초기 설정

좌표를 직접 찾는 방식이 아니라, 마우스를 원하는 버튼 위에 올린 뒤 그 순간의 좌표를 읽어 저장합니다.

```bash
autokyo setup
```

터미널이 순서대로 아래를 묻습니다.

- 캡처 버튼 위에 마우스를 올리고 `Enter`
- 확인 버튼 위에 마우스를 올리고 `Enter`
- 페이지 변화 영역의 왼쪽 위에 마우스를 올리고 `Enter`
- 페이지 변화 영역의 오른쪽 아래에 마우스를 올리고 `Enter`
- 마지막에 `config.toml` 저장

### 페이지 변화 감지 확인

```bash
autokyo probe
```

### 실행

```bash
autokyo run
```

### PDF 만들기

```bash
autokyo pdf --delete-source
```

`autokyo pdf`는 기본적으로 아래 순서로 진행합니다.

- PDF 제목을 묻고
- 결과 파일을 바탕화면 `~/Desktop/<제목>.pdf`에 저장하고
- 현재 세션의 Photos 이미지를 `captures/`로 내보낸 뒤 PDF를 만들고
- `--delete-source`를 붙였으면 `captures/` 이미지와 해당 Photos 이미지까지 정리합니다.

Photos 수량이 세션 수량과 정확히 일치하지 않으면 기본적으로 가능한 범위만 PDF로 만들고, 정확히 일치하지 않을 때 실패시키고 싶다면 `--strict-count`를 사용하세요.

```bash
autokyo pdf --delete-source --strict-count
```

이미 `captures/` 폴더에 이미지가 준비되어 있을 때만 아래 명령으로 바로 PDF를 만들 수도 있습니다.

```bash
autokyo pdf --input ./captures --delete-source
```

### MCP 등록

터미널에서 바로 등록할 수 있습니다.

```bash
autokyo mcp-install codex
autokyo mcp-install claude
autokyo mcp-install openclaw
autokyo mcp-install antigravity
```

프롬프트 예시:

- `AutoKyo로 설정해줘`
- `교보전자도서관 책 PDF 만들어줘`
- `AutoKyo로 현재 마우스 좌표 알려줘`
- `AutoKyo 상태 보여줘`
- `AutoKyo로 변화 영역 확인해줘`
- `AutoKyo 실행해줘`
- `AutoKyo captures를 PDF로 만들어줘`

현재 MCP에서 노출하는 툴:

- `setup_autokyo`
- `setup_capture_button`
- `setup_confirm_button`
- `setup_change_region`
- `save_config`
- `capture_to_pdf`
- `get_mouse_position`
- `run_capture_session`
- `get_session_status`
- `probe_region`
- `build_pdf`

## 자주 쓰는 명령

- `autokyo setup`: 캡처 버튼, 확인 버튼, 변화 영역을 순서대로 읽어 설정 저장
- `autokyo run`: 자동화 실행
- `autokyo pdf --delete-source`: PDF 제목을 묻고, 현재 세션을 바탕화면 PDF로 만들고, 원본 정리
- `autokyo export-photos-to-captures --make-pdf --delete-source`: 현재 세션의 Photos 이미지를 `captures/`로 내보내고 PDF 생성
- `autokyo probe`: 화면 변화 감지 영역 확인
- `autokyo mousepos --watch`: 마우스 좌표 확인
- `autokyo status`: 현재 세션 상태 출력
- `autokyo pdf --input ./captures --delete-source`: 이미 준비된 `captures/`를 PDF로 만들고 원본 삭제
- `autokyo mcp-http`: 로컬 streamable HTTP MCP 서버 수동 실행
- `autokyo mcp`: 로컬 stdio MCP 서버 수동 실행

## 주의사항

- 창 위치가 바뀌면 좌표 클릭이 틀어질 수 있습니다.
- `page.change_region`이 잘못 잡히면 마지막 페이지 판정이 흔들릴 수 있습니다.
- 저장 완료 자체는 확인하지 않고, 설정된 대기 시간 뒤에 다음 단계로 넘어갑니다.
- `max_pages = 0`이면 화면 변화가 멈출 때까지 계속 진행합니다.
