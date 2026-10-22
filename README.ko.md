# ntdrive

**VMware Workstation 위의 Windows 게스트를 LLM 에이전트에서 조종합니다. 전원과 스냅샷, kd.exe 커널 디버깅,
실시간 SSH 터미널을 하나의 데몬 뒤에.**

[![CI](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml/badge.svg)](https://github.com/jiy2745/ntdrive/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)](#설치)
[![Interfaces](https://img.shields.io/badge/interfaces-MCP%20%7C%20CLI%20%7C%20SDK-blue.svg)](#구조)

[English](README.md) | **한국어**

VMware Workstation 위의 Windows 게스트를 LLM 에이전트에서 조종합니다. 전원과 스냅샷, KDNET 또는 시리얼
파이프를 통한 `kd.exe` 커널 디버깅, 실시간 SSH 터미널을 하나의 로컬 데몬과 하나의 툴 집합 뒤에 두고,
MCP 서버와 CLI, Python SDK라는 세 개의 창구로 노출합니다. 커널 드라이버와 Windows 보안 작업의 반복
루프를 위해 만들었습니다. 빌드하고, 게스트에 배포하고, 로드하고, 크래시나 브레이크포인트를 맞고,
디버거에서 분석하고, 스냅샷을 되돌리고, 다시 반복. 각 단계가 툴 호출 하나이고, 조각들의 상태를 맞추는
일은 데몬이 대신하므로 에이전트가 신경 쓸 필요가 없습니다. Windows 전용입니다. 이름 그대로입니다.
`NT`는 Windows 커널이고, ntdrive는 그것을 조종합니다.

필요한 것: Windows 11 호스트, VMware Workstation Pro 17.6 이상, Debugging Tools for Windows
(Windows SDK 또는 WDK의 `kd.exe`와 `kdnet.exe`), Python 3.12, uv. 게스트: VMware Tools가 설치된
Windows 10 또는 11 x64, Secure Boot 해제, KDNET을 쓸 경우 `e1000e` NIC. 호스트에 관리자 셸은
필요 없습니다.

가장 빠른 시작 경로 (게스트 먼저, 그 다음 호스트):

1. 게스트에 `scripts\setup-guest.cmd`를 복사해 실행합니다.
2. 호스트에서:

   ```powershell
   git clone https://github.com/jiy2745/ntdrive
   cd ntdrive
   scripts\setup-host.cmd
   ```

   마지막에 `ntdrive verify`가 돌고 ALL SET이 출력됩니다.
3. MCP 클라이언트가 설치된 `ntdrive-mcp` 명령을 바라보게 합니다 (클론에는 `.mcp.json`이 들어 있습니다).

다음에 읽을 것: 툴을 사용하는 에이전트라면 `SKILL.md`, 이 코드를 고친다면 `AGENTS.md`, 요구사항이
궁금하면 `PRD.md`. 전체 목록은 아래 문서 지도에 있습니다.

> 영어 `README.md`가 정본입니다. 이 번역이 뒤처진 것 같으면 영어 쪽을 확인하세요.

## 왜 만들었나

드라이버를 디버깅하려는 에이전트에게는 창이 동시에 세 개 필요합니다. VMware 제어, WinDbg, 그리고
게스트로 들어가는 SSH나 RDP 세션입니다. ntdrive는 이 셋을 모두 툴로 노출하고, 더 중요하게는 **그 상태를
서로 맞춰 둡니다.** 스냅샷을 되돌리면 디버거가 알아서 다시 붙고 터미널도 다시 연결됩니다. 디버거로
브레이크를 걸면 게스트가 얼어 있으므로 터미널 툴들은 매달리지 않고 즉시 거절합니다. 이 툴 간 일관성이
바로 기존의 디버거 전용 또는 VM 전용 도구들이 사용자에게 떠넘기는 부분입니다.

## 구조

```
 Claude Code (agent)      Human / CI (shell)      pytest / automation
        | stdio                  | argv                  | import
        v                        v                       v
   ntdrive-mcp              ntdrive CLI             Python SDK (NtDrive)
        |                        |                       |
        +------------------------+-----------------------+
                                 | HTTP + WebSocket, 127.0.0.1, token
                                 v
   +------------- ntdrived (daemon, holds every session) -------------+
   |  ToolRegistry -> StateStore -> Orchestrator -> AuditLog          |
   |  VmwareAdapter (vmrun)   KdSession (kd.exe)   TermManager (SSH)   |
   +---------+------------------------+-----------------------+--------+
             | vmrun.exe              | UDP KDNET             | TCP 22 (SSH PTY)
             v                        v                       v
   +------------------------ VM (Windows guest) -----------------------+
   |  KDNET (boot-time)   OpenSSH -> PowerShell PTY   VMware Tools      |
   +-------------------------------------------------------------------+
```

세션을 소유하는 것은 데몬입니다. MCP 서버와 CLI, SDK는 그 앞에 붙은 얇고 상태 없는 클라이언트라서,
에이전트와 셸 앞의 사람과 테스트 스크립트가 모두 같은 live 세션을 봅니다. 모든 툴은 하나의 레지스트리
(`ntdrive.core.registry`)에 한 번만 선언되고 세 창구는 거기서 생성되므로, 서로 어긋나지 않습니다.

## 설치

호스트: Windows 11, VMware Workstation Pro 17.6 이상, Debugging Tools for Windows (Windows SDK
또는 WDK의 `kd.exe`와 `kdnet.exe`), Python 3.12, [uv](https://docs.astral.sh/uv/). 관리자 셸은
필요 없습니다. 게스트: VMware Tools가 설치된 Windows 10 또는 11 x64, VM 설정에서 Secure Boot 해제
(`bcdedit /debug on`에 필요합니다), KDNET을 쓸 경우 `e1000e` NIC. 게스트를 먼저 하고 호스트를 나중에
합니다. 호스트 스크립트가 끝에서 전 구간 점검을 돌리고, 디버거에 필요하면 게스트를 직접 재부팅합니다.
빠진 것이 있으면 `ntdrive sys health`가 고치는 방법을 알려 줍니다.

**1. 게스트.** `scripts\setup-guest.cmd`를 게스트로 복사하고 (VMware Tools가 깔려 있으면 끌어다 놓기가
됩니다) 아무 셸에서나 실행하거나 더블클릭합니다. 관리자 권한은 스크립트가 직접 요청하고 (UAC 클릭 한 번),
로컬 관리자 계정 `ntdrive`를 만들고 비밀번호를 물어보며 (호스트의 `ntdrive setup`에 같은 것을 입력하세요),
OpenSSH Server를 PowerShell을 기본 셸로 설치하고, 22번 포트를 열고, KDNET을 켭니다. 호스트 IP는 NAT
게이트웨이에서 가져오고 키는 게스트에서 생성되므로 손으로 옮길 것이 없습니다. 다시 실행해도 안전합니다.
재부팅하라고 하면 게스트를 재부팅하세요.

**2. 호스트.** 클론에서 명령 하나:

```powershell
git clone https://github.com/jiy2745/ntdrive
cd ntdrive
scripts\setup-host.cmd
```

`uv sync`를 돌리고, `ntdrive`와 `ntdrive-mcp`, `ntdrived` 명령을 PATH에 올리고 (editable
`uv tool install`이라 클론을 따라갑니다), VMware 라이브러리에서 고른 VM마다 게스트 계정과 비밀번호를
물어본 다음 (입력 중에는 가려지고, 사용자 환경 변수로 저장되며, 파일에는 들어가지 않습니다) 데몬을
재시작하고, UAC 프롬프트 한 번으로 KDNET용 호스트 방화벽을 고치고, `ntdrive verify`로 끝냅니다. 설정은
`%LOCALAPPDATA%\ntdrive\vms.yaml`에 저장되고, 모든 필드는 `vms.example.yaml`에 설명돼 있습니다. 클론
없이 하려면 `uv tool install git+https://github.com/jiy2745/ntdrive` 다음에 `ntdrive setup`입니다.

**3. 검증.** 호스트 스크립트가 이미 돌렸습니다. 무엇이든 바꾼 뒤에, 또는 호스트보다 게스트를 나중에
설정했을 때 다시 돌리세요:

```powershell
ntdrive verify                  # config, power, SSH login, firewall, then attach, break in, resume
```

`ALL SET`, 아니면 가장 먼저 고칠 것과 그 방법으로 끝납니다. 게스트를 방금 설정했다면 디버거가 붙도록
게스트를 직접 재부팅합니다.

선택 옵션:

| 위치 | 스위치 | 하는 일 |
|---|---|---|
| 게스트 | `setup-guest.cmd -NoAccount` | `ntdrive` 계정을 만들지 않고 기존 Windows 계정을 SSH에 사용 |
| 게스트 | `setup-guest.cmd -NoStandard` | `ntdrive-user`를 건너뜀 (기본 생성되며, `term_open account=standard`용 일반 계정) |
| 게스트 | `setup-guest.cmd -OpenSshZip <파일 또는 URL>` | 인터넷 없는 게스트를 위해 Win32-OpenSSH zip에서 OpenSSH 설치 (Insider 빌드에는 capability 패키지가 없고, 스크립트가 알아서 다운로드로 넘어갑니다) |
| 게스트 | `setup-guest.cmd -Serial` | KDNET 대신 시리얼 named-pipe 트랜스포트 |
| 호스트 | `setup-host.cmd -Verify` | 전 구간 점검만 실행 |
| 호스트 | `setup-host.cmd -FirewallOnly` | 관리자 셸에서 KDNET 방화벽 규칙만 수동 복구 |
| 호스트 | `ntdrive setup --transport serial` | UAC 프롬프트를 승인할 사람이 없는 호스트용 시리얼 트랜스포트. 이후 VM을 끈 상태에서 `ntdrive kd setup-host <vm>` |

VM이 여러 대일 때: VM마다 `setup-host.cmd` (또는 `ntdrive setup`)를 다시 돌리고 게스트마다
`setup-guest.cmd`를 실행하세요. 게스트는 각자 머신 id에서 KDNET 포트를 고르고, 다른 VM과 포트가
겹치면 호스트가 옮겨 줍니다. 손으로 설정한 게스트는 `ntdrive kd setup-guest <vm>`으로 KDNET 설정을
받습니다. 설정 스크립트를 돌리기 전에 `scripts\probe-guest.ps1`로 게스트 상태(빌드, Secure Boot,
NIC 모델, OpenSSH 상태)를 확인할 수 있습니다.

**4. MCP 클라이언트.** 서버는 설치된 `ntdrive-mcp` 명령입니다. stdio이고 인자도 환경 변수도 없으며
(설정은 `%LOCALAPPDATA%\ntdrive`에 있습니다) 데몬도 스스로 띄웁니다. 대기 계열 툴이 long-poll을 하므로
클라이언트의 MCP 툴 호출 타임아웃을 600초보다 크게 잡으세요.

Claude Code: 클론에는 `.mcp.json`이 들어 있고, 그 밖에서는 `claude mcp add ntdrive -- ntdrive-mcp`.
권한 규칙 `mcp__ntdrive__*`로 툴을 허용하세요.

```json
{ "mcpServers": { "ntdrive": { "command": "ntdrive-mcp" } } }
```

Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`)은 PATH를 찾지 않으므로 전체 경로를
적어야 합니다. `uv tool dir --bin`이 디렉터리를 알려 주며, 기본값은 `%USERPROFILE%\.local\bin`입니다:

```json
{ "mcpServers": { "ntdrive": { "command": "C:\\Users\\you\\.local\\bin\\ntdrive-mcp.exe" } } }
```

VS Code는 `.vscode/mcp.json`, Cursor는 `.cursor/mcp.json`에 Claude Desktop과 같은 형태로 적습니다:

```json
{ "servers": { "ntdrive": { "type": "stdio", "command": "ntdrive-mcp" } } }
```

## 빠른 시작 (CLI)

CLI는 같은 툴을 하위 명령으로 제공합니다. 첫 호출에서 데몬이 자동으로 뜹니다.

데몬은 창 없이 실행되고 (`pythonw.exe`로 detached 실행), 데몬이 띄우는 vmrun과 `kd.exe` 프로세스도
숨겨지므로 바탕화면에 아무것도 뜨지 않습니다. `ntdrive daemon status`가 떠 있는지 알려 줍니다.
`ntdrive daemon logs -f`는 하는 일을 따라갑니다. 툴 호출당 한 줄 (이름, 호출자, 결과이며 인자는 절대
남기지 않습니다) 과 데몬 자신의 메시지가 `%LOCALAPPDATA%\ntdrive\logs\daemon.out.log`에서 나옵니다.

```powershell
ntdrive sys health                 # host binaries and config, then each VM live: power, SSH, debugger transport
ntdrive vm start win11
ntdrive term open win11            # prints a session id and a CoView URL, a browser page that mirrors the session
ntdrive kd attach win11            # serial: attaches at once. net: the target connects while the guest boots
ntdrive kd break win11             # freezes the guest at a kd> prompt
ntdrive kd exec win11 "!process 0 0"
ntdrive kd go win11                # resume the guest
```

아무 명령에나 `--json`을 붙이면 가공되지 않은 툴 결과가 나옵니다. 종료 코드: 0 정상, 1 오류,
2 잘못된 인자, 3 확인 필요, 4 디버거가 게스트를 얼림, 5 타임아웃.

에이전트가 열어 둔 세션에 사람이 그대로 앉을 수 있습니다:

```powershell
ntdrive term attach <session-id>   # Ctrl+] to detach, your keystrokes are logged as human
```

Python SDK는 같은 툴을 메서드로 제공합니다. `vt.<group>.<verb>(...)` 형태입니다:

```python
from ntdrive.sdk import NtDrive

vt = NtDrive()                     # talks to the daemon, starts it if needed
vt.vm.start("win11")
print(vt.sys.state(vm="win11"))    # sys_state takes keyword arguments only
```

## 에이전트가 할 수 있는 일

- **VM 전원과 스냅샷**: 시작, 정지, 일시중단, 세 가지 재부팅 모드, 그리고 트리 목록과 되돌리기,
  삭제가 되는 live 스냅샷.
- **KDNET 또는 시리얼 파이프를 통한 커널 디버깅**: 게스트 설정, `kd.exe` attach, 브레이크,
  디버거 명령 실행, 버그체크나 브레이크포인트 대기, detach.
- **실시간 터미널**: SSH PTY 세션 열기, 출력 스트리밍, 화면 렌더링, 정규식 대기,
  `{ctrl+c}`를 포함한 키 전송.
- **콘솔과 파일**: 콘솔 스크린샷 캡처 (BSOD나 로그인 화면용) 와 체크섬 검증이 붙은 양방향 파일 복사.
- **통합된 상태**: `sys_state` 한 번으로 VM 전원과 디버거 상태, 터미널 세션이 모두 나오고,
  스냅샷 되돌리기 같은 복합 동작은 하나의 조율된 단계로 실행됩니다.

전체 툴 목록은 툴 레지스트리에서 생성되며, 영어 `README.md`의
[tool table](README.md#what-an-agent-can-do) 절에 있습니다. 그 표는 코드의 툴 설명에서 그대로
생성되고 (`scripts/tools_table.py --write README.md`가 갱신하며 테스트가 최신 상태를 지킵니다) 설명은
영어로 유지되므로, 여기서 번역본을 따로 두면 금세 어긋납니다. 표의 effect 열은 MCP 애너테이션을
뜻합니다. read 툴은 아무것도 바꾸지 않고, additive는 무언가를 추가하거나 시작하며, destructive는
상태를 버릴 수 있고, `confirm=true`가 필요한 툴은 인자에 그렇게 적혀 있습니다. 인자는 `PRD.md` 7절과
`ntdrive <group> <verb> --help`에 있습니다.

## 지켜야 할 상태 모델

- 디버거가 브레이크된 동안 (`kd_state == broken`) 게스트 전체가 얼어 있습니다. 터미널과 파일,
  스크린샷 툴은 매달리는 대신 즉시 `guest_frozen_by_debugger`를 반환하고, 진행 중이던 `term_exec`나
  `term_read` 대기도 같은 식으로 끝납니다. 먼저 `kd go`를 실행하세요.
- `snap revert`는 디버거를 detach하고, `vm reboot`은 재연결을 기다리도록 `kd.exe`를 남겨 둡니다.
  둘 다 터미널 세션을 끊었다가, `--no-reattach-kd`나 `--no-reopen-term`을 주지 않는 한 다시 붙이고
  다시 엽니다. 다시 열린 세션은 새 id를 받으며, 결과의 `term[].new`에 나옵니다.
- 재연결 후에도 이전 터미널 세션 id는 남아 그 후속 세션을 가리킵니다.

전체 상태 모델과 절차는 `SKILL.md`에 있습니다.

## 암호화된 VM

`encryption_password_env` (환경 변수 이름이며 이쪽을 권장합니다) 또는 `encryption_password`
(인라인이며, `vms.yaml`이 git에서 제외되므로 허용됩니다) 를 설정하면 vmx를 여는 모든 작업이 vmrun에
`-vp`를 넘깁니다. 전원, 스냅샷 목록, 되돌리기, 삭제, 스크린샷, 게스트 IP, 파일 복사가 모두 암호화된
VM에서 동작하며, Windows 11 vTPM이 요구하는 부분 암호화도 포함합니다. 거친 부분이 하나 있습니다.
vmrun은 실행 중인 암호화 VM의 live 스냅샷이나 메모리 스냅샷 삭제를 거부합니다. 그 경우 `snap_take`와
`snap_delete`가 `error.reason=encrypted_live_snapshot`으로 답하고, `--allow-suspend`가 처리합니다
(일시중단, 스냅샷 또는 삭제, 재개. 터미널 세션은 끊기고 디버거는 다시 붙습니다). 전원이 꺼진 VM의
스냅샷에는 필요 없습니다. 재개가 실패하면 스냅샷은 기록된 채로 호출이 실패하고 사실관계가 오류에
담기며, vmx가 Workstation이 복원할 수 없는 saved state를 가리키고 있을 때는
`vm_start --discard-saved-state`가 새로 부팅합니다. vmrun이 받아들이는 것들은 `AGENTS.md`의
Things that bit us에 적혀 있습니다.

## 안전

- 데몬은 `127.0.0.1`에만 바인딩하고 모든 요청에서 데몬별 토큰을 확인합니다.
- 비밀 값 (게스트 비밀번호, KDNET 키, VM 암호화 비밀번호, 데몬 토큰) 은 `vms.yaml`과 환경 변수,
  데몬 상태 파일에만 존재합니다. 툴 인자와 결과, 로그에는 절대 나타나지 않고 감사 로그는 이들을
  마스킹합니다.
- 게스트의 SSH 호스트 키는 처음 사용할 때 VM별로 `%LOCALAPPDATA%\ntdrive\hostkeys` 아래에 고정됩니다.
  나중에 키가 달라지면 비밀번호를 보내기 전에 거절합니다. 게스트를 다시 설치했다면 그 파일을 지우세요.
- CoView URL에는 view 토큰이 들어 있습니다. 터미널 세션 목록과 그 스트림만 열 수 있는 두 번째 비밀
  값입니다. 그래도 게스트 안의 셸이므로 비밀번호처럼 다루고 채팅이나 티켓에 붙여넣지 마세요. 로그
  디렉터리의 터미널 기록은 에이전트든 사람이든 세션에 입력한 모든 것을 남기므로, 대화식으로 입력한
  비밀번호도 거기에 남습니다.
- `kd_exec`는 어떤 디버거 명령이든 실행하고, kd의 `.shell`은 호스트에서 명령을 실행합니다. 그 권한을
  주지 말아야 할 에이전트에는 정책 파일에서 `kd_exec: deny`를 설정할 수 있습니다.
- 파괴적인 동작 (`snap_delete`, `vm_stop mode=hard` 또는 `kill`, `vm_reboot mode=hard`) 은
  `confirm=true`를 요구하고 `policy.yaml`에서 조정할 수 있는 정책 게이트를 거칩니다.

## 문서 지도

| 파일 | 이런 사람이 읽습니다 |
|---|---|
| `README.md` | ntdrive를 설치하거나 사용하는 사람 (영어 정본) |
| `README.ko.md` | 이 문서. `README.md`의 한국어 번역 |
| `SKILL.md` | 툴을 사용하는 에이전트. 상태 모델, 표준 절차, 피해야 할 동작 |
| `AGENTS.md` | 이 저장소를 고치는 모든 코딩 에이전트. 관례, 명령, 코드 위치 |
| `CLAUDE.md` | Claude Code. `AGENTS.md`를 가리키고 Claude 전용 사항을 더함 |
| `PRD.md` | 전체 요구사항과 설계 근거가 궁금한 사람 |
| `vms.example.yaml` | `vms.yaml`을 직접 고치는 사람. 모든 필드가 설명돼 있음 |
| `policy.example.yaml` | 툴별 allow, confirm, deny 수준을 조정하는 사람 |
| `scripts/` | 호스트와 게스트 설치. 위의 설치 절 참고 |

`SKILL.md`는 에이전트를 위해 쓰였습니다. 스킬 파일을 읽는 MCP 클라이언트가 알아서 가져가고, 어떤
에이전트에게든 가리켜 줄 수 있습니다. 상태 모델과 설치 및 디버그 루프, BSOD 복구 절차, 그리고 금지된
동작 (디버거가 브레이크된 동안 터미널을 건드리는 것 같은) 을 다룹니다.

## 개발

```powershell
uv sync
uv run --no-sync pre-commit run --all-files   # ruff format, ruff check, mypy, prettier, ASCII check
uv run --no-sync pytest -q                    # unit tests, all fakes, no real VM needed
```

커밋 전에 둘 다 통과해야 하고, CI는 windows-latest에서 정확히 이 두 가지를 돌립니다. `--no-sync`인
이유는 sync가 `ntdrive-mcp.exe`를 다시 쓰는데, Claude Code 같은 MCP 클라이언트가 그 파일을 잡고 있으면
실패하기 때문입니다. 관례와 코드 지도, 툴이 바뀔 때 함께 고쳐야 할 것들은 `AGENTS.md`에 있습니다.
Python 포매터와 린터는 Ruff 하나뿐이고, mypy는 패키지 전체에 strict이며, Prettier는 CoView 웹
자산에만 돌아갑니다. pre-commit 훅이 ASCII가 아닌 문자와 Markdown 산문의 세미콜론을 거절해서 모든
문서를 영어와 평문으로 유지합니다. 이 파일은 그 검사에서 의도적으로 제외돼 있습니다
(`pyproject.toml`의 `[tool.check_ascii] exclude`). 테스트에는 VM이 필요 없습니다. 실제 Windows
방화벽을 건드리는 단 하나의 테스트는 `NTDRIVE_LIVE_TESTS=1`일 때만 돌고, 실전 점검은
`ntdrive verify`입니다 (설치 3단계).

MCP 쪽은 inspector로 직접 두드려 볼 수 있습니다. `npx @modelcontextprotocol/inspector ntdrive-mcp`
(클론에서는 `npx @modelcontextprotocol/inspector uv run ntdrive-mcp`) 가 툴과 애너테이션을 나열하고
호출할 수 있는 페이지를 엽니다.

구현된 백엔드는 VMware뿐입니다. Hyper-V와 VirtualBox는 같은 `HypervisorAdapter` 인터페이스 뒤에
있으며 이후 버전으로 계획돼 있습니다.

## 코드 배치

```
src/ntdrive/
  config.py       vms.yaml and policy.yaml models
  errors.py       NtDriveError with code, hint and backend reason tags
  hostproc.py     runs vmrun and the PowerShell helpers without a console window
  paths.py        host path helpers shared by the CLI, SDK and file tools
  core/           registry, state store, policy gate, audit log, orchestrator, service
  core/tools/     tool handlers: vm, snap, kd, term, console, file, sys
  hypervisor/     HypervisorAdapter, the VMware (vmrun) adapter, vmx file access
  kd/             KdSession around kd.exe, host firewall repair for KDNET
  term/           terminal transports, sessions, reconnect and successor ids, key tokens
  screen/         VNC framebuffer capture for con_screenshot method=vnc
  daemon/         ntdrived HTTP and WebSocket app, lifecycle, client, CoView page
  mcp/ sdk/       generated front doors
  cli/            generated tool commands plus setup, verify, term attach and the daemon group
scripts/          host and guest setup with their .cmd launchers, a guest probe, the ASCII hook,
                  the README table generator
tests/            unit tests with fake vmrun, fake kd.exe and fake terminal channels
```

## 라이선스

MIT. `LICENSE`를 보세요.
