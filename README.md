# AutoKyo

> macOS용 교보문고 전자도서관 eBook 작업 자동화 도구  
> 자동 클릭, 페이지 변화 감지, PDF 변환, MCP 등록 지원

`AutoKyo`는 반복 클릭과 페이지 넘김을 줄이고, `captures/` 이미지를 PDF로 묶기 쉽게 만든 로컬 도구입니다.

이 프로젝트는 편의성과 접근성을 위한 도구입니다. 사용 전 저작권, 서비스 약관, 대여 조건을 직접 확인해야 하며, 권리 없는 공유·배포·재판매는 저작권 및 약관 위반이 될 수 있습니다.

## 요구사항

- macOS
- Python 3.11 이상
- Python 인터프리터에 `손쉬운 사용` 권한
- Python 인터프리터에 `화면 및 시스템 오디오 녹화` 권한
- 실제 좌표와 딜레이가 들어간 `config.toml`

`config.toml`은 아래 순서로 자동 탐색합니다.

- `./config.toml`
- `~/Library/Application Support/AutoKyo/config.toml`
- `~/.config/autokyo/config.toml`

## 설치

통합 저장소 이름을 `homebrew-autokyo`로 바꾼 뒤:

```bash
brew install plain127/autokyo/autokyo
```

## 빠른사용법
### MCP 등록

터미널에서 바로 등록할 수 있습니다.

```bash
autokyo mcp-install codex
autokyo mcp-install claude
autokyo mcp-install openclaw
autokyo mcp-install antigravity
```

Antigravity가 설정 파일을 못 찾으면 직접 지정하면 됩니다.

```bash
autokyo mcp-install antigravity --client-config /path/to/mcp_config.json
```

저장소에서 직접 실행 중이면 아래처럼 쓰면 됩니다.

```bash
python3 -m autokyo mcp-install codex
python3 -m autokyo mcp-install claude
python3 -m autokyo mcp-install openclaw
python3 -m autokyo mcp-install antigravity --client-config /path/to/mcp_config.json
```

현재 MCP에서 노출하는 툴:

- `run_capture_session`
- `get_session_status`
- `probe_region`
- `get_mouse_position`
- `build_pdf`

MCP 등록 후 LLM에게 이렇게 말하면 됩니다.

- `AutoKyo MCP로 현재 마우스 좌표 확인해`
- `AutoKyo MCP로 probe_region 실행해서 change_region이 맞는지 봐줘`
- `AutoKyo MCP로 현재 세션 상태 확인해`
- `AutoKyo MCP로 현재 설정대로 자동화 실행해`
- `AutoKyo MCP로 captures 폴더를 PDF로 만들어`

## 세부 사용법
1. 캡처 버튼 좌표 확인

```bash
python3 -m autokyo mousepos --watch
```

2. `config.toml` 수정

- `triggers.capture`
- `capture.post_steps`
- `triggers.next_page`
- `page.change_region`
- `capture.post_action_delay_ms`
- `page.stall_timeout_seconds`
- `loop.max_pages`

3. 페이지 변화 감지 확인

```bash
python3 -m autokyo probe
```

4. 실행

```bash
python3 -m autokyo run
```

5. PDF 만들기

```bash
python3 -m autokyo pdf --delete-source
```

설치형으로 쓰는 경우 `python3 -m autokyo` 대신 `autokyo`만 쓰면 됩니다.

## 자주 쓰는 명령

- `autokyo run`: 자동화 실행
- `autokyo probe`: 화면 변화 감지 영역 확인
- `autokyo mousepos --watch`: 마우스 좌표 확인
- `autokyo status`: 현재 세션 상태 출력
- `autokyo pdf --delete-source`: `captures/`를 PDF로 만들고 원본 삭제
- `autokyo mcp`: 로컬 stdio MCP 서버 실행

## 주의사항

- 창 위치가 바뀌면 좌표 클릭이 틀어질 수 있습니다.
- `page.change_region`이 잘못 잡히면 마지막 페이지 판정이 흔들릴 수 있습니다.
- 저장 완료 자체는 확인하지 않고, 설정된 대기 시간 뒤에 다음 단계로 넘어갑니다.
- `max_pages = 0`이면 화면 변화가 멈출 때까지 계속 진행합니다.
