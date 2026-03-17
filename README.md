# AutoKyo

> 교보문고 전자도서관 eBook 작업을 자동화하는 macOS용 로컬 도구  
> 자동 클릭, 페이지 넘김, 화면 변화 감지, PDF 변환, MCP 연동 지원

`AutoKyo`는 `교보문고 전자도서관`, `전자책`, `eBook`, `macOS`, `자동 캡처`, `PDF 변환` 같은 반복 작업을 더 쉽게 처리하기 위한 로컬 자동화 도구입니다. 쉽게 말해, 사람이 계속 눌러야 하는 클릭과 페이지 넘김을 대신하고, 페이지가 바뀌었는지 확인하고, `captures/` 폴더의 이미지를 하나의 PDF로 묶는 스크립트입니다. `Codex`, `Claude Code` 같은 MCP 클라이언트에서도 호출할 수 있습니다.

이 프로젝트는 반복 작업을 줄이고 접근성과 편의성을 높이기 위한 목적으로 만들었습니다. 사용자는 관련 저작권, 서비스 약관, 대여 조건을 직접 확인해야 하며, 권리 없는 공유·배포·재판매는 저작권 및 약관 위반이 될 수 있습니다.

## 한눈에 보기

- 교보문고 전자도서관 같은 앱에서 반복 클릭 작업을 줄입니다.
- 페이지가 실제로 바뀌었는지 확인하면서 다음 단계로 진행합니다.
- `captures/` 폴더의 이미지를 바로 PDF로 만들 수 있습니다.
- 로컬 MCP 서버로 실행해서 AI 클라이언트와 연결할 수 있습니다.

## 기능

- `macOS 전용`으로 단순하게 설계된 로컬 자동화
- `페이지 변화 감지`로 마지막 페이지 판정 지원
- `post_steps`로 확인 버튼 같은 후속 클릭 처리 가능
- `captures -> PDF` 변환 내장
- `MCP 지원`으로 Codex, Claude Code 같은 클라이언트에서 호출 가능

## 필수 요구사항

- macOS
- Python 3.11 이상
- 대상 앱과 같은 맥에서 실행
- Python 인터프리터에 `손쉬운 사용` 권한
- Python 인터프리터에 `화면 및 시스템 오디오 녹화` 권한
- `config.toml`에 실제 좌표와 딜레이 값 반영

## 빠른 시작

좌표를 모를 때:

```bash
cd /path/to/AutoKyo
./.venv/bin/python main.py mousepos --watch
```

페이지 변화 감지 영역 테스트:

```bash
cd /path/to/AutoKyo
./.venv/bin/python main.py --config config.toml probe
```

실제 자동화 실행:

```bash
cd /path/to/AutoKyo
./.venv/bin/python main.py --config config.toml run
```

캡처 이미지를 PDF로 변환:

```bash
cd /path/to/AutoKyo
./.venv/bin/python main.py make-pdf --delete-source
```

## 주요 명령

- `run`: 캡처 클릭 -> 대기 -> 후속 클릭 -> 다음 페이지 -> 화면 변화 확인 반복
- `probe`: `page.change_region`이 맞는지 한 번 캡처해서 확인
- `mousepos`: 현재 마우스 좌표 측정
- `status`: 현재 세션 상태 확인
- `make-pdf`: `captures/` 안 이미지를 PDF로 묶기
- `mcp`: 로컬 stdio MCP 서버로 실행

## config.toml에서 먼저 볼 값

- `page.change_region`: 페이지가 바뀌면 달라지는 작은 영역
- `triggers.capture`: 캡처 버튼 좌표
- `capture.post_steps`: 확인 버튼 같은 후속 클릭
- `triggers.next_page`: 다음 페이지 키
- `capture.post_action_delay_ms`: 캡처 후 대기 시간
- `page.stall_timeout_seconds`: 더 이상 페이지가 안 바뀔 때 종료로 볼 시간
- `loop.max_pages`: `0`이면 끝까지, 그 외에는 현재 위치부터 지정 장수만 진행

## MCP 사용

AutoKyo의 MCP 모드는 `로컬 stdio 서버`입니다. 즉 MCP 클라이언트가 AutoKyo를 자식 프로세스로 실행하고, 표준입출력으로 툴 호출을 주고받습니다.

공통 실행 명령:

```bash
./.venv/bin/python main.py --config config.toml mcp
```

현재 MCP에서 노출하는 툴:

- `run_capture_session`
- `get_session_status`
- `probe_region`
- `get_mouse_position`
- `build_pdf`

실제 동작 순서:

- MCP 클라이언트가 AutoKyo 프로세스를 실행
- AutoKyo가 stdio로 대기
- 클라이언트가 툴 목록을 읽고 필요한 툴 호출
- `run_capture_session` 호출 시 로컬 맥에서 실제 클릭, 키 입력, 화면 변화 확인 수행
- `build_pdf` 호출 시 `captures/` 안 이미지를 PDF로 생성

CLI의 모든 명령이 MCP로 노출되는 것은 아니며, 현재는 위 5개 툴만 사용합니다.

## Codex

Codex CLI는 `~/.codex/config.toml`의 MCP 설정을 읽습니다.

```toml
[mcp_servers.autokyo]
command = "/path/to/AutoKyo/.venv/bin/python"
args = ["/path/to/AutoKyo/main.py", "--config", "/path/to/AutoKyo/config.toml", "mcp"]
```

## Claude Code

Claude Code는 프로젝트 루트의 `.mcp.json`이나 `claude mcp add`를 사용할 수 있습니다.

```json
{
  "mcpServers": {
    "autokyo": {
      "command": "/path/to/AutoKyo/.venv/bin/python",
      "args": [
        "/path/to/AutoKyo/main.py",
        "--config",
        "/path/to/AutoKyo/config.toml",
        "mcp"
      ],
      "env": {}
    }
  }
}
```

## Antigravity

Antigravity는 `mcp_config.json`의 `mcpServers`에 로컬 커맨드 기반 MCP 서버를 넣는 방식으로 연결하면 됩니다.

```json
{
  "mcpServers": {
    "autokyo": {
      "command": "/path/to/AutoKyo/.venv/bin/python",
      "args": [
        "/path/to/AutoKyo/main.py",
        "--config",
        "/path/to/AutoKyo/config.toml",
        "mcp"
      ],
      "env": {}
    }
  }
}
```

## OpenClaw

OpenClaw은 배포판이나 MCP 브리지 구성에 따라 설정 형식이 다를 수 있습니다. `command`와 `args` 기반 로컬 MCP 서버 등록을 지원하면 아래 값만 맞춰 넣으면 됩니다.

```text
command: /path/to/AutoKyo/.venv/bin/python
args: ["/path/to/AutoKyo/main.py", "--config", "/path/to/AutoKyo/config.toml", "mcp"]
```

## 주의사항

- 창 위치가 바뀌면 좌표 클릭이 어긋날 수 있습니다.
- `page.change_region`이 잘못 잡히면 마지막 페이지 판정이 흔들릴 수 있습니다.
- `max_pages = 0`이면 화면 변화가 멈출 때까지 계속 진행합니다.
- 결과 저장 완료 자체는 확인하지 않고, 설정된 대기 시간 뒤에 다음 단계로 넘어갑니다.
