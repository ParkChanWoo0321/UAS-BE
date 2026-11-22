# UAS-BE

해양 감시 UAS 시뮬레이션을 조작하는 로컬 웹 콘솔입니다. FastAPI가 웹 화면과 API를 함께 제공하고, MySQL이 임무 설정과 실행 이력을 저장합니다. 시뮬레이터 연결에는 기존 Docker 런타임과 ROS 2를 사용합니다.

## 준비물

- Ubuntu 22.04 이상에서 Python 3.12와 `venv`
- 실행 중인 로컬 MySQL 서버와 콘솔용 데이터베이스
- 비행 기능을 사용할 경우 Docker 및 기존 `maritime` 실행 도구(`~/.local/bin/maritime`). `maritime` 런타임에는 `sea_mission` Gazebo 월드, PX4 SITL, ROS 2의 `px4_msgs`와 VTOL 임무 노드가 준비되어 있어야 합니다.

임무 설정 저장·조회·수정·삭제에는 FastAPI와 MySQL만 필요합니다. 비행을 실행할 PC에서는 Docker 컨테이너가 호스트 홈 디렉터리의 `backend/ros_agent.py`를 읽을 수 있도록 저장소를 홈 디렉터리 아래에 두세요.

## 설치와 실행

### 1. 저장소와 가상환경 만들기

```bash
git clone https://github.com/ParkChanWoo0321/UAS-BE.git "$HOME/UAS-BE"
cd "$HOME/UAS-BE"
python3.12 -m venv .venv
.venv/bin/python -m pip install fastapi==0.165.0 uvicorn==0.54.0 PyMySQL==1.2.4 pydantic==2.14.4
```

이미 `backend/`와 이 라이브러리들이 설치된 가상환경을 사용하는 경우, 해당 프로젝트 디렉터리에서 3단계를 실행하세요.

### 2. MySQL 설정

기존 `~/.config/maritime/mysql.cnf`에 MySQL 접속 정보와 데이터베이스가 설정되어 있다면 그대로 사용할 수 있습니다. 새로 설정하는 경우, MySQL 서비스가 실행된 상태에서 관리자 SQL 세션으로 데이터베이스와 전용 계정을 만드세요. 아래 예시의 비밀번호는 사용할 값으로 바꿉니다.

```sql
CREATE DATABASE IF NOT EXISTS maritime_surveillance CHARACTER SET utf8mb4;
CREATE USER 'maritime_console'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_LOCAL_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE
    ON maritime_surveillance.* TO 'maritime_console'@'127.0.0.1';
```

`~/.config/maritime/mysql.cnf`에 아래 형식으로 실제 로컬 접속 정보를 입력하세요.

```ini
[client]
host=127.0.0.1
port=3306
user=maritime_console
password=REPLACE_WITH_LOCAL_PASSWORD
database=maritime_surveillance
```

```bash
chmod 600 "$HOME/.config/maritime/mysql.cnf"
```

다른 경로를 쓰려면 실행 전에 `MARITIME_MYSQL_CONFIG` 환경 변수에 파일 경로를 지정하세요. 접속 설정은 PC 안에 보관하세요. 콘솔은 처음 조회할 때 `maritime_console_missions`와 `maritime_console_runs` 테이블을 생성합니다.

### 3. 콘솔 실행

가상환경을 준비한 프로젝트 디렉터리에서 아래 명령을 실행하세요.

```bash
.venv/bin/python -B -m backend
```

터미널을 열어 둔 채 [콘솔](http://127.0.0.1:8765/)을 엽니다. 화면 상단에서 **MySQL 연결됨**을 확인하세요. 서버를 종료하려면 터미널에서 `Ctrl+C`를 누릅니다.

## 화면 사용법

### 임무 설정 저장·불러오기·수정·삭제

| 기능 | 사용 방법 |
| --- | --- |
| 새 설정 저장 | 북쪽 좌표, 동쪽 좌표, 목표 고도를 입력합니다. **임무 이름**을 적고 **새 설정 저장**을 누릅니다. |
| 불러오기 | **설정 선택**에서 저장된 임무를 고르고 **불러오기**를 누릅니다. 저장된 좌표와 고도가 입력칸에 채워집니다. |
| 변경사항 저장 | 설정을 불러온 다음 이름, 좌표 또는 고도를 바꿉니다. **변경사항 저장**을 누릅니다. |
| 삭제 | 목록에서 설정을 고르고 **삭제**를 누릅니다. 확인창에 표시된 이름을 확인한 뒤 삭제합니다. |

북쪽과 동쪽 좌표는 시뮬레이션 출발점 기준의 미터 단위입니다. 목표 고도는 미터 단위로 `0.1`보다 큰 값을 입력하세요. 임무 이름은 앞뒤 공백을 제외하고 1~80자입니다.

### 시뮬레이션 실행과 귀환

1. Docker와 `maritime` 실행 도구가 준비된 일반 Ubuntu 터미널에서 콘솔을 시작합니다. 기존 시뮬레이션이 실행 중이라면 해당 실행을 먼저 종료하세요.
2. 좌표와 고도를 직접 입력하거나 저장된 설정을 불러옵니다. 첫 실행에서는 화면 기본값인 북쪽 `1200 m`, 동쪽 `500 m`, 고도 `35 m`를 사용할 수 있습니다.
3. **시뮬레이션 실행**을 누릅니다. 백엔드가 `sea_mission` 월드를 헤드리스로 시작하고, PX4의 시동 전 점검과 ROS 2 연결이 확인되면 목표값을 전송합니다.
4. **비행 상태**에서 위치, 고도, 목표까지의 거리, 비행 모드, 궤적을 확인합니다.
5. PX4가 시동된 상태에서 위치를 확인할 수 있으면 **출발점 귀환** 버튼이 활성화됩니다. 버튼을 누르면 PX4에 RTL 요청을 보내고, PX4가 해당 모드에 진입하면 화면에 **귀환 중**이 표시됩니다.
6. **시뮬레이션 종료**를 누르면 콘솔에서 시작한 실행을 종료합니다. **실행 이력**에서 최근 10건의 시작 시각, 입력 좌표와 상태를 볼 수 있습니다.

기본 `maritime` 실행 도구는 헤드리스 모드에서 선박 감지 프로그램과 QGroundControl을 시작하는 단계를 생략합니다.

## API

서버가 실행 중일 때 [FastAPI API 문서](http://127.0.0.1:8765/docs)에서 요청 형식을 확인할 수 있습니다.

| 메서드 | 경로 | 동작 |
| --- | --- | --- |
| `GET` | `/api/status` | MySQL·실행 도구 상태, 현재 실행, 최근 실행 이력 |
| `POST` | `/api/runs` | 북쪽·동쪽 좌표와 목표 고도로 시뮬레이션 실행 요청 |
| `POST` | `/api/runs/{run_id}/return` | 현재 실행의 출발점 귀환 요청 |
| `POST` | `/api/runs/{run_id}/stop` | 현재 실행 종료 요청 |
| `GET` | `/api/missions` | 저장된 임무 설정 목록 조회 |
| `GET` | `/api/missions/{mission_id}` | 임무 설정 한 건 조회 |
| `POST` | `/api/missions` | 임무 설정 생성 |
| `PATCH` | `/api/missions/{mission_id}` | 지정한 임무 설정 필드 수정 |
| `DELETE` | `/api/missions/{mission_id}` | 임무 설정 삭제 |

쓰기 요청에는 `X-Maritime-Control: 1` 헤더를 붙입니다. 시뮬레이션 실행 본문에는 `north`, `east`, `altitude`를 담고, 임무 설정 생성에는 `name`을 추가합니다.

```bash
curl -X POST http://127.0.0.1:8765/api/missions \
  -H 'X-Maritime-Control: 1' \
  -H 'Content-Type: application/json' \
  -d '{"name":"동쪽 해안 순찰","north":1200,"east":500,"altitude":35}'
```

## 코드 구성

| 경로 | 역할 |
| --- | --- |
| `backend/__main__.py` | Uvicorn을 `127.0.0.1:8765`에서 실행 |
| `backend/app.py` | FastAPI 앱, 시뮬레이션 API, 정적 웹 파일 제공 |
| `backend/schemas.py` | 좌표·고도·임무 설정 입력 모델 |
| `backend/database.py` | 공통 MySQL 연결과 트랜잭션 |
| `backend/mission_routes.py` | 임무 설정 CRUD API |
| `backend/mission_storage.py` | 임무 설정 테이블과 SQL |
| `backend/storage.py` | 시뮬레이션 실행 이력 저장 |
| `backend/simulator.py` | 시뮬레이터 프로세스와 실행 상태 관리 |
| `backend/ros_agent.py` | 시뮬레이터 컨테이너 안에서 ROS 2 상태 수신과 PX4 귀환 요청 처리 |
| `backend/web/` | 콘솔 화면과 JavaScript |

## 문제 해결

| 증상 | 확인할 항목 |
| --- | --- |
| 웹페이지가 열리지 않음 | 실행 터미널에서 Uvicorn 메시지와 포트 `8765` 사용 여부를 확인합니다. |
| MySQL 연결 필요 | `systemctl is-active mysql`과 MySQL 설정 파일의 호스트, 사용자, 데이터베이스 및 권한을 확인합니다. |
| 임무 설정 목록 로딩 실패 | MySQL 연결을 확인하고 화면의 **목록 다시 불러오기**를 누릅니다. |
| `maritime` 실행 실패 또는 Docker 권한 오류 | 일반 Ubuntu 터미널에서 `docker info`와 `~/.local/bin/maritime status`가 실행되는지 확인합니다. |
| PX4 응답 대기, 신호 끊김 또는 페일세이프 | 화면 메시지와 `~/.local/bin/maritime status`를 확인합니다. 실행별 로그는 `~/maritime-setup/runs/`에 있습니다. ROS 2 중계 오류는 해당 실행의 `console.log`를 확인하세요. |
| 귀환 버튼이 비활성화됨 | PX4 시동, 위치 추정, 상태 수신을 확인합니다. 귀환 요청 처리 중에도 버튼이 비활성화됩니다. |

## 다른 PC에 적용하기

링크를 받은 사람은 사용할 Ubuntu PC에서 위의 **설치와 실행** 1~3단계를 진행하면 됩니다. 공개 저장소의 소스는 `git clone`으로 받을 수 있습니다. 콘솔 주소 `127.0.0.1:8765`는 명령을 실행한 PC의 브라우저에서 엽니다.

Ubuntu 24.04에서 Git, Python 가상환경 패키지, MySQL이 설치되어 있지 않다면 먼저 설치하세요.

```bash
sudo apt update
sudo apt install git python3.12-venv mysql-server
sudo systemctl start mysql
```

- **콘솔과 임무 설정만 사용**하려면 Python 가상환경과 해당 PC의 MySQL을 준비합니다. **MySQL 연결됨**이 표시되면 임무 설정을 저장하고 불러올 수 있습니다.
- **비행 시뮬레이션까지 사용**하려면 해당 PC에 별도로 설치한 해양 감시 시뮬레이터가 필요합니다. Docker를 시작하고 일반 터미널에서 `docker info`와 `~/.local/bin/maritime status`가 실행되는지 확인하세요. `$HOME/UAS-BE`에서 콘솔을 실행하면 기존 런타임을 호출합니다. PX4·Gazebo 월드와 ROS 2 런타임은 기존 시뮬레이터 설치 절차에 따라 준비하세요.
- **이미 시뮬레이터 프로젝트가 있는 경우**에도 새로 받은 `$HOME/UAS-BE`를 별도 디렉터리로 두고 실행할 수 있습니다. 해당 PC에서 쓰던 MySQL 설정을 사용하려면 시작 전에 `MARITIME_MYSQL_CONFIG`로 설정 파일을 지정하세요.

PC마다 연결한 MySQL에 임무 설정과 실행 이력이 저장됩니다. 새 데이터베이스에 연결하면 목록은 비어 있으며, 필요한 좌표와 고도는 새 콘솔에서 다시 저장할 수 있습니다.
